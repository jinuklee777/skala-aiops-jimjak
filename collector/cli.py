"""명령줄. 저장소 루트에서 `python -m collector <명령>`으로 부른다."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
from datetime import timedelta

from collector import alerts, collect, db, flights
from collector.config import iso, load_config, parse_time
from collector.schedule import JOB_NAMES, PRIMARY_JOB

log = logging.getLogger("collector")

TICK_TIMEOUT = 280  # 초. 5분마다 불리므로 다음 차례 전에 끝낸다
RUN_TIMEOUT = 600
CSV_FIELDS = ("scheduleDatetime", "flightId", "airlineCode", "airportCode", "terminalId", "bagCarouselId",
              "fstandPosition", "gateNo", "aircraftSubtype", "aircraftRegNo", "estimatedDatetime", "remark",
              "landingDatetime", "bagFirstTime", "bagLastTime", "bagRemark")
HISTORY_FIELDS = ("bagCarouselId", "fstandPosition", "aircraftSubtype", "estimatedDatetime", "remark")


class Timeout(BaseException):
    """제한 시간 초과. 수집 코드의 except Exception에 잡히지 않고 끝까지 올라온다."""


def set_timeout(seconds) -> None:
    def expire(signum, frame):
        raise Timeout(f"{seconds}초 안에 끝나지 않음")

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)


def has_key(cfg) -> bool:
    if cfg.service_key:
        return True
    print("DATA_GO_KR_SERVICE_KEY가 없다. collector/.env에 넣거나 환경 변수로 준다.", file=sys.stderr)
    return False


def cmd_tick(cfg, args):
    if not has_key(cfg):
        return 2
    set_timeout(TICK_TIMEOUT)
    collector = collect.Collector(cfg)
    try:
        collector.tick()
    finally:
        collector.close()
    return 0


def cmd_run(cfg, args):
    if not has_key(cfg):
        return 2
    set_timeout(RUN_TIMEOUT)
    collector = collect.Collector(cfg)
    try:
        result = collector.run_now(args.job)
    finally:
        collector.close()
    print(f"원본: {cfg.raw_dir / args.job}")
    return 1 if result.status == "failed" else 0


def cmd_status(cfg, args):
    print(f"데이터 폴더: {cfg.data_dir}")
    print(f"서비스키: {'있음' if cfg.service_key else '없음 (collector/.env에 DATA_GO_KR_SERVICE_KEY)'}")
    print(f"알림: 웹훅 {'있음' if cfg.webhook_url else '없음'} · 하트비트 {'있음' if cfg.heartbeat_url else '없음'}")
    if not cfg.db_path.exists():
        print("아직 수집 기록이 없다.")
        return 0
    collector = collect.Collector(cfg)
    try:
        conn, now = collector.conn, collector.clock()
        for name in collector.jobs:
            row = db.last_run(conn, name)
            if row is None:
                print(f"{name}: 기록 없음")
                continue
            last = db.last_data_at(conn, name)
            ago = f"{int((now - parse_time(last)).total_seconds() // 60)}분 전" if last else "없음"
            line = (f"{name}: 최근 {alerts.short(row['started_at'])} {collect.STATUS_LABELS[row['status']]} · "
                    f"{row['pages']}쪽 {row['items']:,}행 · 마지막으로 받은 때 {ago}")
            print(line + (f" · 오류: {row['error']}" if row["error"] else ""))
        midnight = now.replace(hour=0, minute=0, second=0)
        calls = db.calls_between(conn, iso(midnight), iso(midnight + timedelta(days=1)))
        print(f"오늘 호출: {calls}회 (하루 한도 {cfg.daily_limit})")
        present, total = db.flight_counts(conn, PRIMARY_JOB)
        print(f"편 상태 표: 지금 응답에 있는 편 {present:,} · 누적 {total:,}편 · 스냅샷 {db.snapshot_count(conn):,}행")
        day = parse_time(args.date).date() if args.date else now.date() - timedelta(days=1)
        print()
        print(alerts.format_summary(alerts.daily_summary(collector, day)))
    finally:
        collector.close()
    return 0


def cmd_asof(cfg, args):
    when = iso(parse_time(args.time))
    collector = collect.Collector(cfg)
    try:
        states = flights.state_as_of(collector.conn, args.job, when)
        last = db.last_data_at(collector.conn, args.job, until=when)
    finally:
        collector.close()
    if args.date:
        prefix = args.date.replace("-", "")
        states = [s for s in states if flights.text(s.item, "scheduleDatetime").startswith(prefix)]
    states.sort(key=lambda s: (flights.text(s.item, "scheduleDatetime"), s.flight_key))
    print(f"{when} 기준 {len(states)}편 · 그 전 마지막 수집 {last or '없음'}", file=sys.stderr)
    if args.json:
        for s in states:
            print(json.dumps({"flight_key": s.flight_key, "observed_at": s.observed_at, "item": s.item},
                             ensure_ascii=False))
        return 0
    writer = csv.writer(sys.stdout)
    writer.writerow(("observed_at",) + CSV_FIELDS)
    for s in states:
        writer.writerow((s.observed_at,) + tuple(flights.text(s.item, name) for name in CSV_FIELDS))
    return 0


def cmd_history(cfg, args):
    collector = collect.Collector(cfg)
    try:
        found = flights.history(collector.conn, args.job, args.flight.strip().upper(),
                                (args.date or "").replace("-", ""))
    finally:
        collector.close()
    if not found:
        print("기록이 없다.")
        return 1
    for key, versions in found.items():
        print(key)
        before = None
        for observed_at, item in versions:
            if item is None:
                print(f"  {observed_at}  응답에서 사라짐")
                continue
            if before is None:
                shown = ", ".join(f"{name}={flights.text(item, name) or '-'}" for name in HISTORY_FIELDS)
                print(f"  {observed_at}  처음 봄: {shown}")
            else:
                changes = ", ".join(f"{name} {old or '-'}→{new or '-'}" for name, old, new in flights.diff(before, item))
                print(f"  {observed_at}  {changes or '다시 나타남'}")
            before = item
    return 0


def cmd_rebuild(cfg, args):
    collector = collect.Collector(cfg)
    try:
        with collector.lock():
            summary = collect.rebuild(collector.conn, cfg.raw_dir)
    finally:
        collector.close()
    print(f"원본 실행 {summary['runs']:,}회 중 {summary['used']:,}회로 다시 만듦 · "
          f"스냅샷 {summary['snapshots']:,}행 · 편 {summary['flights']:,}개")
    return 0


def cmd_notify_test(cfg, args):
    sent = alerts.send(cfg, f"{alerts.PREFIX} 알림 시험. 이 메시지가 보이면 알림 설정이 된 것이다.")
    pinged = alerts.ping(cfg.heartbeat_url)
    print("웹훅:", "보냄" if sent else "실패 (로그 확인)" if cfg.webhook_url else "설정 안 됨 (COLLECTOR_WEBHOOK_URL)")
    print("하트비트:", "보냄" if pinged else "실패 (로그 확인)" if cfg.heartbeat_url else "설정 안 됨 (COLLECTOR_HEARTBEAT_URL)")
    return 0 if sent == bool(cfg.webhook_url) and pinged == bool(cfg.heartbeat_url) else 1


COMMANDS = {"tick": cmd_tick, "run": cmd_run, "status": cmd_status, "asof": cmd_asof,
            "history": cmd_history, "rebuild": cmd_rebuild, "notify-test": cmd_notify_test}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m collector", description="인천공항 수하물 API 수집기")
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")
    commands.add_parser("tick", help="할 때가 된 수집을 하고 멈춤을 확인한다 (cron·launchd가 5분마다 부른다)")
    run = commands.add_parser("run", help="지금 한 번 수집한다")
    run.add_argument("job", nargs="?", default=PRIMARY_JOB, choices=JOB_NAMES)
    status = commands.add_parser("status", help="최근 수집 상태와 하루 요약")
    status.add_argument("--date", help="요약할 날짜 (기본: 어제). 예: 2026-10-12")
    asof = commands.add_parser("asof", help="시각 T에 알고 있던 편별 상태를 CSV로")
    asof.add_argument("time", help="예: '2026-10-12 14:00'")
    asof.add_argument("--date", help="이 날 도착 예정인 편만. 예: 2026-10-12")
    asof.add_argument("--job", default=PRIMARY_JOB, choices=JOB_NAMES)
    asof.add_argument("--json", action="store_true", help="응답 항목 전체를 한 줄에 하나씩 JSON으로")
    history = commands.add_parser("history", help="한 편의 값이 언제 어떻게 바뀌었나")
    history.add_argument("flight", help="편명. 예: KE902")
    history.add_argument("--date", help="도착 예정일. 예: 2026-10-12")
    history.add_argument("--job", default=PRIMARY_JOB, choices=JOB_NAMES)
    commands.add_parser("rebuild", help="원본 파일로 편별 상태 표를 다시 만든다")
    commands.add_parser("notify-test", help="웹훅·하트비트 설정을 시험한다")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except ValueError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](cfg, args)
    except collect.Busy as error:
        log.warning("%s 이번 차례는 건너뛴다.", error)
        return 0 if args.command == "tick" else 1
    except Timeout as error:
        log.error("제한 시간 초과: %s", error)
        return 1
