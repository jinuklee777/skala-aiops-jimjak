"""수집 실행. cron·launchd가 5분마다 tick을 부르고, tick은 할 때가 된 작업만 한다."""
from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from collector import alerts, api, archive, db, flights
from collector.config import iso, now_kst
from collector.schedule import PRIMARY_JOB, build_jobs

log = logging.getLogger("collector")

MAX_PAGES = 20
MAX_RUNS_PER_SLOT = 2   # 실패한 슬롯은 같은 구간 안에서 한 번 더 시도한다
RETRY_DELAYS = (5, 20)  # 초. 같은 쪽을 다시 부르기 전에 기다린다
PAGE_DELAY = 0.5        # 초. 초당 호출 한도에 걸리지 않게 쪽 사이에 쉰다
STATUS_LABELS = {"ok": "성공", "partial": "일부만 받음", "failed": "실패", "running": "실행 중"}


class Busy(Exception):
    """다른 수집이 아직 실행 중이다."""


@dataclass
class RunResult:
    run_uid: str
    job: str
    slot: str
    status: str = "failed"  # ok: 빠짐없이 받음, partial: 일부만 받음, failed: 하나도 못 받음
    pages: int = 0
    calls: int = 0
    items: int = 0
    total_count: int | None = None
    error: str | None = None
    notes: list = field(default_factory=list)
    ingest: flights.IngestResult | None = None


def is_last_page(pages, num_of_rows) -> bool:
    last = pages[-1]
    if not last.items:
        return True
    if last.total_count is not None:  # 서버가 쪽 크기를 줄여 보내도 전체 건수까지는 넘긴다
        return sum(len(p.items) for p in pages) >= last.total_count
    return len(last.items) < num_of_rows


def is_complete(pages, num_of_rows) -> bool:
    """끝까지 빠짐없이 받았는가. 이때만 '응답에서 사라진 편'을 기록한다."""
    if not pages or not is_last_page(pages, num_of_rows):
        return False
    total = pages[-1].total_count
    return total is None or sum(len(p.items) for p in pages) == total


def describe(result: RunResult) -> str:
    count = f"{result.items:,}행" + (f"(전체 {result.total_count:,})" if result.total_count is not None else "")
    parts = [f"{result.job} {STATUS_LABELS[result.status]}", f"{result.pages}쪽", count, f"호출 {result.calls}회"]
    if result.ingest:
        ingest = result.ingest
        parts.append(f"새 편 {ingest.new:,} · 바뀐 편 {ingest.changed:,} · 사라진 편 {ingest.gone:,}")
        if ingest.duplicates:
            parts.append(f"중복 키 {ingest.duplicates}")
        if ingest.keyless:
            parts.append(f"편명·예정일시 없는 행 {ingest.keyless}")
        if ingest.note:
            parts.append(ingest.note)
    parts += result.notes
    if result.error:
        parts.append(f"오류: {result.error}")
    return " · ".join(parts)


class Collector:
    def __init__(self, cfg, clock=now_kst, sleep=time.sleep):
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.jobs = {job.name: job for job in build_jobs(cfg)}
        self.conn = db.connect(cfg.db_path)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def lock(self):
        """한 번에 하나만 수집한다. cron이 겹쳐 불러도 같은 슬롯을 두 번 받지 않는다."""
        with open(self.cfg.data_dir / "collector.lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy("다른 수집이 아직 실행 중이다.") from None
            db.abandon_running(self.conn, iso(self.clock()))
            yield

    def tick(self) -> list:
        """할 때가 된 작업을 하고 멈춤·일일 보고를 확인한다."""
        with self.lock():
            now = self.clock()
            results = [self.run_job(job) for job in self.jobs.values() if self.is_due(job, now)]
            alerts.check(self)
        return results

    def is_due(self, job, now) -> bool:
        statuses = db.slot_statuses(self.conn, job.name, iso(job.slot(now)))
        return "ok" not in statuses and len(statuses) < MAX_RUNS_PER_SLOT

    def run_now(self, name) -> RunResult:
        with self.lock():
            return self.run_job(self.jobs[name])

    def run_job(self, job) -> RunResult:
        started = self.clock()
        previous = db.last_run(self.conn, job.name)
        # 직전 실행도 실패했으면 장애가 이어지는 중이다. 재시도로 하루 호출 한도를 다 쓰지 않게 한 번씩만 부른다.
        attempts = 1 if previous is not None and previous["status"] == "failed" else self.cfg.max_attempts
        run_uid = base = f"{job.name}-{started:%Y%m%dT%H%M%S}"
        suffix = 2
        while db.run_exists(self.conn, run_uid):
            run_uid, suffix = f"{base}-{suffix}", suffix + 1
        result = RunResult(run_uid, job.name, iso(job.slot(started)))
        db.start_run(self.conn, run_uid, job.name, result.slot, iso(started))
        try:
            pages, observed_at = self._fetch_pages(job, result, started, attempts)
            if pages:
                self._ingest(job, result, pages, observed_at)
        except Exception as error:  # 예기치 못한 오류도 실행 기록에 남기고 다음 작업으로 넘어간다
            log.exception("%s 수집 중 오류", job.name)
            result.error = f"{type(error).__name__}: {error}"
        finally:
            db.finish_run(self.conn, result, iso(self.clock()))
        log.info(describe(result))
        if job.name == PRIMARY_JOB and result.status in ("ok", "partial"):
            alerts.ping(self.cfg.heartbeat_url)
        return result

    def _fetch_pages(self, job, result, started, attempts):
        pages, observed_at = [], None
        for page_no in range(1, MAX_PAGES + 1):
            if page_no > 1:
                self.sleep(PAGE_DELAY)
            page, requested_at = self._fetch_page(job, result, started, page_no, attempts)
            if page is None:
                break
            pages.append(page)
            observed_at = observed_at or requested_at
            if is_last_page(pages, self.cfg.num_of_rows):
                break
        return pages, observed_at

    def _fetch_page(self, job, result, started, page_no, attempts):
        cfg = self.cfg
        params = job.params + (("numOfRows", str(cfg.num_of_rows)), ("pageNo", str(page_no)))
        url = api.build_url(cfg.base_url, job.operation, params, cfg.service_key)
        errors = []
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self.sleep(RETRY_DELAYS[min(attempt - 2, len(RETRY_DELAYS) - 1)])
            requested_at = iso(self.clock())
            response = api.http_get(url, cfg.timeout_seconds)
            result.calls += 1
            raw_path = None
            if response.body is not None:  # 오류 응답도 원본으로 남긴다
                record = archive.envelope(job=job.name, run_uid=result.run_uid, slot=result.slot,
                                          operation=job.operation, params=params, page_no=page_no,
                                          attempt=attempt, requested_at=requested_at, response=response)
                raw_path = archive.write(cfg.raw_dir, record, f"{started:%Y-%m-%d}")
            outcome = api.interpret(response)
            page, error = outcome.page, api.redact(outcome.error, cfg.service_key)
            db.record_call(self.conn, {
                "run_uid": result.run_uid, "page": page_no, "attempt": attempt, "requested_at": requested_at,
                "elapsed_ms": response.elapsed_ms, "http_status": response.status, "result_code": outcome.code,
                "items": len(page.items) if page else None, "total_count": page.total_count if page else None,
                "raw_path": str(raw_path.relative_to(cfg.data_dir)) if raw_path else None, "error": error})
            if page is not None:
                if errors:
                    result.notes.append(f"{page_no}쪽은 {attempt}번째 시도에 받음 (앞선 오류: {errors[-1]})")
                return page, requested_at
            errors.append(error)
            log.warning("%s %d쪽 %d번째 시도 실패: %s", job.name, page_no, attempt, error)
            if not outcome.retryable:
                break
        result.error = errors[-1]
        return None, None

    def _ingest(self, job, result, pages, observed_at):
        num_of_rows = self.cfg.num_of_rows
        items = [item for page in pages for item in page.items]
        complete = is_complete(pages, num_of_rows)
        result.status = "ok" if complete else "partial"
        result.pages, result.items, result.total_count = len(pages), len(items), pages[-1].total_count
        if is_last_page(pages, num_of_rows) and not complete:
            result.notes.append(f"끝까지 받았지만 행 수가 전체 건수({result.total_count})와 달라 사라진 편은 판정하지 않음")
        if ("codeshare", "MA") in job.params:
            slaves = sum(1 for item in items if flights.text(item, "codeshare") == "Slave")
            if slaves:
                result.notes.append(f"codeshare=MA로 요청했는데 Slave 행 {slaves}개가 섞여 옴")
        try:
            result.ingest = flights.ingest(self.conn, job.name, result.run_uid, observed_at, items, complete)
        except Exception as error:  # 원본은 이미 저장했다. 편 상태 표는 rebuild로 다시 만들 수 있다
            log.exception("편 상태 표 갱신 실패")
            result.error = f"편 상태 표 갱신 실패: {error}"


def rebuild(conn, raw_dir) -> dict:
    """원본 파일만으로 편별 상태 표(snapshots·latest)를 처음부터 다시 만든다. 실행 기록(runs·calls)은 그대로 둔다."""
    grouped = archive.runs(raw_dir)
    with conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM latest")
    used = 0
    for run_uid in sorted(grouped):  # 실행 이름이 '작업-시각'이라 작업마다 시각 순서가 된다
        pages, first = [], None
        for page_no in range(1, len(grouped[run_uid]) + 1):
            accepted = _accepted(grouped[run_uid].get(page_no, []))
            if accepted is None:
                break
            record, page = accepted
            first = first or record
            pages.append(page)
        if not pages:
            continue
        items = [item for page in pages for item in page.items]
        complete = is_complete(pages, int(first["params"]["numOfRows"]))
        flights.ingest(conn, first["job"], run_uid, first["requested_at"], items, complete)
        used += 1
    return {"runs": len(grouped), "used": used, "snapshots": db.snapshot_count(conn),
            "flights": conn.execute("SELECT COUNT(*) FROM latest").fetchone()[0]}


def _accepted(attempts):
    """한 쪽의 시도들 가운데 실시간 수집과 같은 기준으로 받아들인 응답."""
    for _, path in sorted(attempts):
        record = archive.read(path)
        outcome = api.interpret(api.Response(record.get("http_status"), {}, archive.body_bytes(record)))
        if outcome.page is not None:
            return record, outcome.page
    return None
