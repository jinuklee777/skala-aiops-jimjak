"""알림: 멈춤 감시, 일일 보고, 웹훅·하트비트."""
from __future__ import annotations

import json
import logging
import shlex
import shutil
import statistics
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from collector import __version__, db, quality
from collector.config import KST, iso, parse_time
from collector.schedule import PRIMARY_JOB

log = logging.getLogger("collector")

PREFIX = "[짐작 수집기]"
REPEAT_HOURS = 6  # 멈춤이 이어지면 이 간격으로 다시 알린다
MIN_FREE_BYTES = 2 * 10**9  # 디스크 여유가 이보다 적으면 일일 보고에 경고를 붙인다
COMMAND_TIMEOUT = 200  # 초. 알림 명령이 모델을 부르면 오래 걸릴 수 있다
WEEKDAYS = "월화수목금토일"


def short(moment) -> str:
    """'10/12 14:20' 꼴."""
    return parse_time(moment).strftime("%m/%d %H:%M") if moment else "-"


def webhook_payload(url, message) -> dict:
    # 디스코드 웹훅은 content, 슬랙 같은 나머지는 text를 읽는다.
    key = "content" if "discord" in urllib.parse.urlparse(url).netloc else "text"
    return {key: message[:1900]}


def _request(url, data=None) -> None:
    headers = {"User-Agent": f"jimjak-collector/{__version__}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=10) as response:
        response.read()


def send(cfg, message) -> bool:
    """웹훅과 알림 명령으로 보낸다. 둘 다 없으면 로그에만 남는다."""
    log.info("알림: %s", message)
    sent = post_webhook(cfg.webhook_url, message)
    return run_command(cfg.notify_command, message) or sent


def post_webhook(url, message) -> bool:
    if not url:
        return False
    try:
        _request(url, json.dumps(webhook_payload(url, message)).encode("utf-8"))
        return True
    except Exception as error:
        log.error("웹훅으로 알림을 보내지 못함: %s", error)
        return False


def run_command(command, message) -> bool:
    """알림 명령 끝에 메시지를 인자 하나로 붙여 실행한다. 종료 코드가 0이면 보낸 것으로 본다."""
    if not command:
        return False
    try:
        result = subprocess.run(shlex.split(command) + [message], capture_output=True, text=True,
                                timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        log.error("알림 명령을 실행하지 못함: %s", error)
        return False
    if result.returncode != 0:
        log.error("알림 명령 실패(종료 코드 %d): %s", result.returncode,
                  (result.stderr or result.stdout).strip()[-300:])
        return False
    return True


def ping(url) -> bool:
    """하트비트. healthchecks.io 같은 바깥 감시는 이 신호가 끊기면 알린다. 컴퓨터가 꺼져도 알 수 있다."""
    if not url:
        return False
    try:
        _request(url)
        return True
    except Exception as error:
        log.warning("하트비트를 보내지 못함: %s", error)
        return False


def check(collector) -> None:
    for step in (check_stale, send_daily_report):
        try:
            step(collector)
        except Exception:
            log.exception("%s 실패", step.__name__)


def check_stale(collector) -> None:
    """마지막으로 데이터를 받은 지 stale_minutes가 지나면 알리고, 다시 받으면 재개를 알린다."""
    cfg, conn, now = collector.cfg, collector.conn, collector.clock()
    first = db.first_run_at(conn, PRIMARY_JOB)
    if first is None:
        return
    last = db.last_data_at(conn, PRIMARY_JOB)
    minutes = int((now - parse_time(last or first)).total_seconds() // 60)
    state, sent_at = db.get_notice(conn, "stale")
    if minutes >= cfg.stale_minutes:
        if state == "stale" and now - parse_time(sent_at) < timedelta(hours=REPEAT_HOURS):
            return
        since = f"마지막 성공 {short(last)} ({minutes}분 전)" if last else f"{short(first)}부터 한 번도 받지 못함"
        send(cfg, f"{PREFIX} 수집 멈춤: {since}. 최근 오류: {db.last_error(conn, PRIMARY_JOB) or '기록 없음'}")
        db.set_notice(conn, "stale", "stale", iso(now))
    elif state == "stale":
        send(cfg, f"{PREFIX} 수집 재개: {short(last)}에 받음")
        db.set_notice(conn, "stale", "ok", iso(now))


def send_daily_report(collector) -> None:
    """report_hour가 지나면 하루 한 번 어제 수집 결과를 보낸다."""
    now = collector.clock()
    if now.hour < collector.cfg.report_hour:
        return
    today = now.date().isoformat()
    state, _ = db.get_notice(collector.conn, "report")
    if state == today:
        return
    yesterday = now.date() - timedelta(days=1)
    first = db.first_run_at(collector.conn, PRIMARY_JOB)
    if first and parse_time(first).date() <= yesterday:
        send(collector.cfg, format_summary(daily_summary(collector, yesterday)))
    db.set_notice(collector.conn, "report", today, iso(now))


def label_counts(conn, day, as_of=None, loaded=None) -> dict:
    """연결된 운항편 기준 집계. 늦은 편은 실제 착륙일·시각 기준 국제선이다."""
    report = quality.report(conn, day, as_of=as_of, loaded=loaded)
    late = report['international_by_landing_hour'].get('23', {'landed': 0, 'with_last': 0})
    return {**{k: report[k] for k in ('flights', 'cancelled', 'no_carousel', 'arrived', 'with_last')},
            'late_arrived': late['landed'], 'late_with_last': late['with_last'], 'quality': report}


def daily_summary(collector, day) -> dict:
    cfg, conn = collector.cfg, collector.conn
    job = collector.jobs[PRIMARY_JOB]
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + timedelta(days=1)
    first = db.first_run_at(conn, PRIMARY_JOB)
    first_slot = job.slot(parse_time(first)) if first else end  # 처음 돌기 시작한 날은 그 뒤만 센다
    expected = [iso(slot) for slot in job.slots_on(day) if slot >= first_slot]
    statuses = {}
    for row in db.runs_between(conn, PRIMARY_JOB, iso(start), iso(end)):
        statuses.setdefault(row["slot"], set()).add(row["status"])
    now = collector.clock()
    loaded = quality.load_tracks(conn, until=now)
    summary = {
        "day": day,
        "expected": len(expected),
        "ok": sum("ok" in found for found in statuses.values()),
        "partial": sum("ok" not in found and "partial" in found for found in statuses.values()),
        "failed": sum(not (found & {"ok", "partial"}) for found in statuses.values()),
        "missed": sum(slot not in statuses for slot in expected),
        "calls": db.calls_between(conn, iso(start), iso(end)),
        "daily_limit": cfg.daily_limit,
        "raw_bytes": sum(path.stat().st_size for path in cfg.raw_dir.glob(f"*/{day.isoformat()}/*.json.gz")),
        "disk_free": shutil.disk_usage(cfg.data_dir).free,
        **label_counts(conn, day, as_of=now, loaded=loaded),
    }
    previous = [sum(quality.moment(t.item, 'scheduleDatetime') is not None and
                    quality.moment(t.item, 'scheduleDatetime').date() == day - timedelta(days=k)
                    for t in loaded[0].values()) for k in range(1, 8)]
    previous = [count for count in previous if count]
    summary["usual_flights"] = statistics.median(previous) if len(previous) >= 3 else None
    summary["warnings"] = warnings_for(summary)
    return summary


def warnings_for(s) -> list:
    warnings = []
    received = s["ok"] + s["partial"]
    if s["expected"] and received < 0.9 * s["expected"]:
        warnings.append(f"받은 회차({received})가 예정({s['expected']})의 90%에 못 미침")
    if s["expected"] and s["partial"] > 0.1 * s["expected"]:
        warnings.append(f"일부 쪽만 받은 회차가 {s['partial']}번")
    if s["usual_flights"] and s["flights"] < 0.7 * s["usual_flights"]:
        warnings.append(f"편 수({s['flights']})가 평소({s['usual_flights']:.0f})보다 크게 적음")
    if s["calls"] > 0.9 * s["daily_limit"]:
        warnings.append(f"호출 수({s['calls']})가 하루 한도({s['daily_limit']})에 가까움")
    if s["disk_free"] < MIN_FREE_BYTES:
        warnings.append(f"디스크 여유가 {s['disk_free'] / 1e9:.1f}GB뿐")
    report = s.get('quality', {})
    missing = [m for m in report.get('missing', []) if m['type_of_flight'] == 'I' and m['landing_at']]
    if missing:
        names = ', '.join(m['aliases'][-1].split('|')[0] for m in missing[:8])
        warnings.append(f"국제선 착륙 후 마지막 짐 미확보 {len(missing)}편 (응답 소실 또는 120분 경과): {names}")
    if s['late_with_last'] < s['late_arrived']:
        warnings.append(f"23시대 국제선 마지막 짐 미확보 {s['late_arrived'] - s['late_with_last']}편")
    if report.get('statuses', {}).get('invalid'):
        warnings.append(f"시각 순서·형식 또는 편 연결 점검 필요 {report['statuses']['invalid']}편")
    return warnings


def percent(part, whole) -> str:
    return f" ({part / whole:.0%})" if whole else ""


def format_summary(s) -> str:
    day = s["day"]
    label = f"{day:%m/%d}({WEEKDAYS[day.weekday()]})"
    lines = [
        f"{PREFIX} {label} 수집 보고",
        f"- 회차: 성공 {s['ok']} · 일부 {s['partial']} · 실패 {s['failed']} · 빠짐 {s['missed']} / 예정 {s['expected']}",
        f"- 호출: {s['calls']}회 (하루 한도 {s['daily_limit']})",
        f"- 도착 예정 {s['flights']}편: 결항 {s['cancelled']}, 수취대 미배정 {s['no_carousel']}, "
        f"도착 {s['arrived']}편 중 마지막 짐 시각 확보 {s['with_last']}편{percent(s['with_last'], s['arrived'])}",
        f"- 실제 23시대 착륙 국제선 {s['late_arrived']}편 중 마지막 짐 시각 확보 {s['late_with_last']}편",
        f"- 정답 수정·철회 {len(s['quality']['label_revisions'])}편 · 예정시각 변경 연결 {len(s['quality']['identity_links'])}건",
        f"- 원본: {s['raw_bytes'] / 1e6:.1f}MB · 디스크 여유 {s['disk_free'] / 1e9:.1f}GB",
    ]
    return "\n".join(lines + [f"⚠ {warning}" for warning in s["warnings"]])
