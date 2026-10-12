"""SQLite 저장소. 실행 기록(runs·calls), 편별 상태(snapshots·latest), 알림 상태(notices)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE runs (
    run_uid      TEXT PRIMARY KEY,
    job          TEXT NOT NULL,
    slot         TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,      -- running, ok(끝까지 받음), partial(일부 쪽만), failed
    pages        INTEGER NOT NULL DEFAULT 0,
    calls        INTEGER NOT NULL DEFAULT 0,
    items        INTEGER NOT NULL DEFAULT 0,
    total_count  INTEGER,
    new_flights  INTEGER,
    changed      INTEGER,
    gone         INTEGER,
    error        TEXT
);
CREATE INDEX runs_job_slot ON runs (job, slot);

-- 호출 한 번에 한 행. 실패한 시도도 남긴다.
CREATE TABLE calls (
    run_uid      TEXT NOT NULL,
    page         INTEGER NOT NULL,
    attempt      INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    elapsed_ms   INTEGER,
    http_status  INTEGER,
    result_code  TEXT,
    items        INTEGER,
    total_count  INTEGER,
    raw_path     TEXT,
    error        TEXT,
    PRIMARY KEY (run_uid, page, attempt)
);
CREATE INDEX calls_requested_at ON calls (requested_at);

-- 편마다 값이 바뀐 시점에만 한 행. present = 0은 그때 응답에서 사라졌다는 표시다.
CREATE TABLE snapshots (
    job          TEXT NOT NULL,
    flight_key   TEXT NOT NULL,      -- 편명|도착 예정일시
    observed_at  TEXT NOT NULL,
    run_uid      TEXT NOT NULL,
    present      INTEGER NOT NULL,
    row_hash     TEXT,
    item         TEXT,               -- 응답 항목 JSON 그대로
    PRIMARY KEY (job, flight_key, observed_at)
);

-- 편별 최신 상태. 응답에서 사라진 편도 마지막 값을 남긴다.
CREATE TABLE latest (
    job           TEXT NOT NULL,
    flight_key    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    present       INTEGER NOT NULL,
    row_hash      TEXT NOT NULL,
    item          TEXT NOT NULL,
    PRIMARY KEY (job, flight_key)
);

CREATE TABLE notices (
    name     TEXT PRIMARY KEY,
    state    TEXT,
    sent_at  TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
    elif version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"{path}의 DB 형식({version})을 이 수집기({SCHEMA_VERSION})가 모른다.")
    return conn


def start_run(conn, run_uid, job, slot, started_at) -> None:
    with conn:
        conn.execute("INSERT INTO runs (run_uid, job, slot, started_at, status) VALUES (?, ?, ?, ?, 'running')",
                     (run_uid, job, slot, started_at))


def finish_run(conn, result, finished_at) -> None:
    ingest = result.ingest
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, pages = ?, calls = ?, items = ?, total_count = ?, "
            "new_flights = ?, changed = ?, gone = ?, error = ? WHERE run_uid = ?",
            (finished_at, result.status, result.pages, result.calls, result.items, result.total_count,
             ingest and ingest.new, ingest and ingest.changed, ingest and ingest.gone, result.error,
             result.run_uid))


def record_call(conn, call: dict) -> None:
    with conn:
        conn.execute(
            "INSERT INTO calls (run_uid, page, attempt, requested_at, elapsed_ms, http_status, result_code, "
            "items, total_count, raw_path, error) VALUES (:run_uid, :page, :attempt, :requested_at, "
            ":elapsed_ms, :http_status, :result_code, :items, :total_count, :raw_path, :error)", call)


def abandon_running(conn, now) -> None:
    """앞선 실행이 강제로 끝나 'running'으로 남았으면 실패로 정리한다. 잠금을 잡은 뒤에만 부른다."""
    with conn:
        conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, "
                     "error = COALESCE(error, '실행이 중간에 끝남') WHERE status = 'running'", (now,))


def run_exists(conn, run_uid) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE run_uid = ?", (run_uid,)).fetchone() is not None


def slot_statuses(conn, job, slot) -> list:
    return [row[0] for row in conn.execute("SELECT status FROM runs WHERE job = ? AND slot = ?", (job, slot))]


def first_run_at(conn, job):
    return conn.execute("SELECT MIN(started_at) FROM runs WHERE job = ?", (job,)).fetchone()[0]


def last_data_at(conn, job, until=None):
    """마지막으로 데이터를 받은(ok·partial) 실행의 시작 시각."""
    sql = "SELECT MAX(started_at) FROM runs WHERE job = ? AND status IN ('ok', 'partial')"
    args = (job,)
    if until:
        sql += " AND started_at <= ?"
        args += (until,)
    return conn.execute(sql, args).fetchone()[0]


def last_run(conn, job):
    return conn.execute("SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 1", (job,)).fetchone()


def last_error(conn, job):
    row = conn.execute("SELECT error FROM runs WHERE job = ? AND error IS NOT NULL "
                       "ORDER BY started_at DESC LIMIT 1", (job,)).fetchone()
    return row[0] if row else None


def runs_between(conn, job, start, end) -> list:
    """슬롯이 [start, end) 안에 있는 실행."""
    return conn.execute("SELECT slot, status FROM runs WHERE job = ? AND slot >= ? AND slot < ?",
                        (job, start, end)).fetchall()


def calls_between(conn, start, end) -> int:
    return conn.execute("SELECT COUNT(*) FROM calls WHERE requested_at >= ? AND requested_at < ?",
                        (start, end)).fetchone()[0]


def flight_counts(conn, job) -> tuple:
    """(지금 응답에 있는 편, 누적 편)"""
    row = conn.execute("SELECT COALESCE(SUM(present), 0), COUNT(*) FROM latest WHERE job = ?", (job,)).fetchone()
    return row[0], row[1]


def snapshot_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


def get_notice(conn, name) -> tuple:
    row = conn.execute("SELECT state, sent_at FROM notices WHERE name = ?", (name,)).fetchone()
    return (row["state"], row["sent_at"]) if row else (None, None)


def set_notice(conn, name, state, sent_at) -> None:
    with conn:
        conn.execute("INSERT INTO notices (name, state, sent_at) VALUES (?, ?, ?) "
                     "ON CONFLICT (name) DO UPDATE SET state = excluded.state, sent_at = excluded.sent_at",
                     (name, state, sent_at))
