"""성공한 수집을 시간순으로 재생해 당시 ETA 기준 예측 입력과 수정 가능한 정답을 분리한다."""
from __future__ import annotations

from datetime import timedelta

from collector import flights, quality
from collector.config import iso, parse_time
from collector.schedule import PRIMARY_JOB

# 정답, 도착 후 현황은 입력에 넣지 않는다. 기상 등 외부 정보는 이 명령의 범위 밖이다.
FEATURES = ('airlineCode', 'scheduleDatetime', 'estimatedDatetime', 'terminalId', 'typeOfFlight',
            'aircraftType', 'aircraftSubtype', 'aircraftRegNo', 'airportCode', 'fstandPosition',
            'bagCarouselId', 'exit', 'gateNo', 'runway')


def build(conn, until=None, horizon_minutes=60, max_gap_minutes=10):
    if horizon_minutes <= 0 or max_gap_minutes <= 0:
        raise ValueError('예측 선행시간과 최대 관측 간격은 양수여야 한다.')
    runs = conn.execute("SELECT * FROM runs WHERE job = ? AND status = 'ok' AND error IS NULL "
                        "AND finished_at IS NOT NULL ORDER BY finished_at, started_at, run_uid",
                        (PRIMARY_JOB,)).fetchall()
    runs = [r for r in runs if not until or parse_time(r['finished_at']) <= until]
    cutoff = until or quality.latest_observation(conn)
    # 나중에 확인한 키 연결은 정답 연결에만 쓴다. 입력은 완료된 과거 수집으로 재생한다.
    tracks, aliases, links, ambiguous = quality.load_tracks(conn, until=cutoff)
    events = quality.events_from_db(conn, until=cutoff)
    unknown_roots = {aliases[e['key']] for e in events
                     if not e['available_at'] and e['key'] in aliases}
    state, previous, emitted, rows = {}, {}, set(), []
    # 일부 쪽에서 갱신된 값도 다음 정상 수집에 그대로 나올 수 있어 재생에는 포함한다.
    # 예측은 전체 응답을 받은 회차에만 발행한다.
    pending = sorted((e for e in events if e['available_at']),
                     key=lambda e: (e['available_at'], e['observed_at'], e['key']))
    cursor = 0
    inconsistent_runs = 0
    for run in runs:
        at = parse_time(run['finished_at'])
        while cursor < len(pending) and parse_time(pending[cursor]['available_at']) <= at:
            e = pending[cursor]
            if e['item'] is None:
                state.pop(e['key'], None)
            else:
                state[e['key']] = e['item']
            cursor += 1
        # 급감 보호로 소실이 보류됐거나 복구 이력이 불완전하면 현재 응답을 보장할 수 없다.
        if len(state) != run['items']:
            previous.clear()
            inconsistent_runs += 1
            continue
        present_roots = {aliases[k] for k in state if k in aliases}
        # 사라졌다 다시 나타난 편은 연속 관측으로 보지 않는다.
        previous = {k: v for k, v in previous.items() if k in present_roots}
        for key, item in sorted(state.items()):
            root = aliases.get(key)
            if root is None or root in unknown_roots or tracks[root].identity_conflict or root in emitted:
                continue
            if flights.text(item, 'typeOfFlight') != 'I' or flights.text(item, 'codeshare') != 'Master':
                continue
            if flights.text(item, 'remark') == '결항':
                previous.pop(root, None)
                continue
            eta = quality.moment(item, 'estimatedDatetime')
            if not eta or any(flights.text(item, f) for f in ('landingDatetime', 'bagFirstTime', 'bagLastTime')):
                previous.pop(root, None)
                continue
            remaining = (eta - at).total_seconds() / 60
            before = previous.get(root)
            if (before and before[1] > horizon_minutes and 0 < remaining <= horizon_minutes and
                    at - before[0] <= timedelta(minutes=max_gap_minutes)):
                label = quality.label_details(tracks[root], cutoff)
                landing = parse_time(label['landing_at']) if label['landing_at'] else None
                row = dict(flight_id=root, source_key=key, aliases=tracks[root].aliases,
                           forecast_at=iso(at), source_run=run['run_uid'],
                           horizon_minutes=horizon_minutes, max_gap_minutes=max_gap_minutes,
                           eta_lead_minutes=round(remaining, 3),
                           features={f: flights.text(item, f) or None for f in FEATURES},
                           was_delayed=flights.text(item, 'remark') == '지연',
                           label_as_of=iso(cutoff), label=label,
                           actual_landing_lead_minutes=round((landing - at).total_seconds() / 60, 3)
                           if landing else None)
                row['eligible_for_evaluation'] = bool(label['status'] == 'observed' and landing > at)
                rows.append(row)
                emitted.add(root)
            previous[root] = (at, remaining)
    return dict(policy_version=1, as_of=iso(cutoff) if cutoff else None,
                scope='international_master', horizon_minutes=horizon_minutes,
                max_gap_minutes=max_gap_minutes, samples=len(rows),
                labeled_samples=sum(r['eligible_for_evaluation'] for r in rows),
                snapshots_without_completion=sum(not e['available_at'] for e in events),
                flights_without_completion=len(unknown_roots), inconsistent_runs=inconsistent_runs,
                identity_links=links, ambiguous_links=ambiguous, rows=rows)
