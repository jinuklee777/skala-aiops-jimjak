"""원본 키를 보존하는 분석용 운항편 연결과 정답 품질 점검. DB를 수정하지 않는다."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from collector import flights
from collector.config import iso, parse_time
from collector.schedule import PRIMARY_JOB

IDENTITY_WINDOW_MINUTES = 120
OVERDUE_MINUTES = 120  # 품질 점검용 대기시간. SLA나 지연 정답이 아니다.


def moment(item, name):
    value = flights.text(item, name)
    try:
        return parse_time(value) if value else None
    except ValueError:
        return None


@dataclass
class Track:
    flight_id: str  # 최초 원본 키. 나중에 예정시각이 바뀌어도 유지한다.
    aliases: list = field(default_factory=list)
    events: list = field(default_factory=list)
    item: dict = field(default_factory=dict)
    present: bool = False
    last_seen_at: str | None = None
    identity_conflict: bool = False


def events_from_db(conn, job=PRIMARY_JOB, until=None):
    """완료 시각이 없는 복구 이력은 품질 점검에만 사용하고 예측 입력에서는 제외한다."""
    events = []
    for row in conn.execute(
        "SELECT s.*, r.finished_at FROM snapshots s "
        "LEFT JOIN runs r ON s.run_uid = r.run_uid AND s.job = r.job "
        "WHERE s.job = ? ORDER BY s.observed_at, s.run_uid, s.flight_key", (job,)):
        available = row['finished_at']
        if until and parse_time(available or row['observed_at']) > until:
            continue
        events.append(dict(key=row['flight_key'], observed_at=row['observed_at'],
                           available_at=available, run_uid=row['run_uid'],
                           item=json.loads(row['item']) if row['present'] else None))
    return events


def same_operation(old, new):
    """같은 응답의 소실·등장 후보를 보수적으로 대조한다."""
    if any(flights.text(old, f) for f in ('landingDatetime', 'bagFirstTime', 'bagLastTime')):
        return False
    for f in ('flightId', 'airportCode', 'aircraftRegNo', 'typeOfFlight'):
        a, b = flights.text(old, f), flights.text(new, f)
        if not a or a != b:
            return False
    a, b = moment(old, 'scheduleDatetime'), moment(new, 'scheduleDatetime')
    return bool(a and b and 0 < abs((a - b).total_seconds()) <= IDENTITY_WINDOW_MINUTES * 60)


def reconcile(events):
    """같은 응답에서 일대일로 소실·등장한 키만 연결한다. 애매하면 보류한다."""
    tracks, aliases, links, ambiguous = {}, {}, [], []
    active = {}
    active_roots = Counter()
    groups = defaultdict(list)
    for e in events:
        groups[(e['observed_at'], e['run_uid'])].append(e)
    for _, batch in sorted(groups.items()):
        gone = [e for e in batch if e['item'] is None and e['key'] in active]
        new = [e for e in batch if e['item'] is not None and e['key'] not in aliases]
        candidates = {e['key']: [g['key'] for g in gone if same_operation(active[g['key']], e['item'])]
                      for e in new}
        reverse = defaultdict(list)
        for key, options in candidates.items():
            for old in options:
                reverse[old].append(key)
        for e in new:
            key, options = e['key'], candidates[e['key']]
            if len(options) == 1 and len(reverse[options[0]]) == 1:
                old = options[0]
                root = aliases[old]
                aliases[key] = root
                tracks[root].aliases.append(key)
                links.append(dict(old_key=old, new_key=key, flight_id=root,
                                  observed_at=e['observed_at'], available_at=e['available_at']))
            else:
                aliases[key] = key
                tracks[key] = Track(key, [key])
                if options:
                    ambiguous.append(dict(new_key=key, candidates=options, observed_at=e['observed_at']))
                    tracks[key].identity_conflict = True
                    for old in options:
                        tracks[aliases[old]].identity_conflict = True
        for e in batch:
            key = e['key']
            if key not in aliases:  # 처음부터 소실 기록만 있는 불완전 이력
                continue
            t = tracks[aliases[key]]
            t.events.append(e)
            if e['item'] is None:
                if key in active:
                    active_roots[t.flight_id] -= 1
                active.pop(key, None)
            else:
                if key not in active:
                    active_roots[t.flight_id] += 1
                active[key] = e['item']
                t.item = e['item']
                t.last_seen_at = e['observed_at']
        for root in {aliases[e['key']] for e in batch if e['key'] in aliases}:
            t = tracks[root]
            t.present = active_roots[root] > 0
            if active_roots[root] > 1:
                t.identity_conflict = True
    return tracks, aliases, links, ambiguous


def load_tracks(conn, job=PRIMARY_JOB, until=None):
    events = events_from_db(conn, job, until)
    tracks, aliases, links, ambiguous = reconcile(events)
    # 스냅샷은 변경 시점만 담으므로 해당 기준 시각 안에 있는 latest로 수신 시각을 보완한다.
    for r in conn.execute('SELECT flight_key, last_seen_at FROM latest WHERE job = ?', (job,)):
        if r['flight_key'] in aliases and (until is None or parse_time(r['last_seen_at']) <= until):
            t = tracks[aliases[r['flight_key']]]
            t.last_seen_at = max(t.last_seen_at or '', r['last_seen_at'])
    return tracks, aliases, links, ambiguous


def label_history(track):
    """빈 값으로 철회한 것도 버전으로 남기고 과거 값을 되살리지 않는다."""
    versions, previous = [], ('', '', '')
    for e in track.events:
        if e['item'] is None:
            continue
        value = flights.text(e['item'], 'bagLastTime')
        current = (value, flights.text(e['item'], 'landingDatetime'), flights.text(e['item'], 'bagFirstTime'))
        if current != previous and (value or previous[0]):
            versions.append(dict(value=value or None, observed_at=e['observed_at'],
                                 landing_at=current[1] or None, first_bag_at=current[2] or None,
                                 available_at=e['available_at'], source_key=e['key']))
        previous = current
    return versions


def label_details(track, as_of, overdue_minutes=OVERDUE_MINUTES):
    item = track.item
    landing, first, last = (moment(item, f) for f in ('landingDatetime', 'bagFirstTime', 'bagLastTime'))
    versions = label_history(track)
    errors = []
    for f in ('landingDatetime', 'bagFirstTime', 'bagLastTime'):
        if flights.text(item, f) and moment(item, f) is None:
            errors.append('invalid_' + f)
    if last and not landing:
        errors.append('last_without_landing')
    if landing and last and last < landing:
        errors.append('last_before_landing')
    if first and landing and first < landing:
        errors.append('first_before_landing')
    if first and last and last < first:
        errors.append('last_before_first')
    if any(t and t > as_of for t in (landing, first, last)):
        errors.append('event_after_as_of')
    if track.identity_conflict:
        errors.append('identity_conflict')
    if errors:
        status = 'invalid'
    elif flights.text(item, 'remark') == '결항':
        status = 'cancelled'
    elif landing and last:
        status = 'observed'  # API에 확정 표시가 없다. 영구 확정이라고 부르지 않는다.
    elif not track.present:
        status = 'missing_disappeared'
    elif landing and as_of - landing >= timedelta(minutes=overdue_minutes):
        status = 'missing_overdue'
    elif landing:
        status = 'pending_label'
    else:
        status = 'pending_arrival'
    return dict(status=status, landing_at=iso(landing) if landing else None,
                last_bag_at=iso(last) if last else None,
                wait_minutes=(last - landing).total_seconds() / 60 if last and landing else None,
                version=len(versions), versions=versions,
                first_observed_at=versions[0]['observed_at'] if versions else None,
                changed_at=versions[-1]['observed_at'] if versions else None, errors=errors)


def latest_observation(conn, job=PRIMARY_JOB):
    row = conn.execute('SELECT MAX(t) FROM ('
                       'SELECT observed_at AS t FROM snapshots WHERE job = ? UNION ALL '
                       'SELECT finished_at AS t FROM runs WHERE job = ?)', (job, job)).fetchone()
    return parse_time(row[0]) if row[0] else None


def report(conn, day, as_of=None, loaded=None):
    """예정일 기준 편 집계와 실제 착륙 시각 기준 시간대별 확보율."""
    cutoff = as_of or latest_observation(conn)
    tracks, _, links, ambiguous = loaded or load_tracks(conn, until=as_of)
    selected = [t for t in tracks.values() if (moment(t.item, 'scheduleDatetime') and
                moment(t.item, 'scheduleDatetime').date() == day)]
    statuses = defaultdict(int)
    missing, revisions, hourly = [], [], {}
    intl_landed = intl_labeled = arrived = with_last = cancelled = no_carousel = 0
    for t in selected:
        d = label_details(t, cutoff)
        statuses[d['status']] += 1
        if flights.text(t.item, 'remark') == '결항':
            cancelled += 1
        else:
            no_carousel += not flights.text(t.item, 'bagCarouselId')
            arrived += bool(d['landing_at'])
            with_last += d['status'] == 'observed'
        if len(d['versions']) > 1:
            revisions.append(dict(flight_id=t.flight_id, aliases=t.aliases, **d))
        if d['status'] in ('missing_disappeared', 'missing_overdue'):
            missing.append(dict(flight_id=t.flight_id, aliases=t.aliases, status=d['status'],
                                type_of_flight=flights.text(t.item, 'typeOfFlight'),
                                landing_at=d['landing_at'], last_seen_at=t.last_seen_at))
        landing = moment(t.item, 'landingDatetime')
        if landing and flights.text(t.item, 'typeOfFlight') == 'I':
            intl_landed += 1
            intl_labeled += d['status'] == 'observed'
    # 실제 착륙일로 선택하여 예정일을 넘겨 도착한 편도 포함한다.
    for t in tracks.values():
        landing = moment(t.item, 'landingDatetime')
        if not landing or landing.date() != day or flights.text(t.item, 'typeOfFlight') != 'I':
            continue
        bucket = hourly.setdefault(f'{landing.hour:02}', dict(landed=0, with_last=0))
        bucket['landed'] += 1
        bucket['with_last'] += label_details(t, cutoff)['status'] == 'observed'
    selected_keys = {key for t in selected for key in t.aliases}
    return dict(day=day.isoformat(), as_of=iso(cutoff) if cutoff else None,
                flights=len(selected), cancelled=cancelled, no_carousel=no_carousel,
                arrived=arrived, with_last=with_last, international_landed=intl_landed,
                international_with_last=intl_labeled, statuses=dict(statuses),
                international_by_landing_hour=dict(sorted(hourly.items())), missing=missing,
                label_revisions=revisions,
                identity_links=[l for l in links if l['new_key'] in selected_keys],
                ambiguous_links=[l for l in ambiguous if l['new_key'] in selected_keys])
