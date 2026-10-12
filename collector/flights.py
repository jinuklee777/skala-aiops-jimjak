"""응답 항목 → 편별 상태.

편마다 값이 바뀐 시점에만 스냅샷을 남긴다. 그래서 '시각 T에 무엇을 알고 있었나'를 다시 볼 수 있다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# 끝까지 받은 응답인데 편 수가 직전의 절반 아래로 줄면 응답 이상으로 보고 '사라짐'을 기록하지 않는다.
MIN_PRESENT_RATIO = 0.5


def field(item, name):
    """필드 이름 대소문자가 API마다 달라서 가리지 않고 찾는다."""
    if not isinstance(item, dict):
        return None
    if name in item:
        return item[name]
    lowered = name.lower()
    return next((value for key, value in item.items() if key.lower() == lowered), None)


def text(item, name) -> str:
    value = field(item, name)
    return "" if value is None else str(value).strip()


def flight_key(item) -> str | None:
    """편 하나 = (편명, 도착 예정일시)."""
    flight_id, scheduled = text(item, "flightId"), text(item, "scheduleDatetime")
    return f"{flight_id}|{scheduled}" if flight_id and scheduled else None


def row_hash(item) -> str:
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass
class IngestResult:
    flights: int = 0     # 서로 다른 편
    new: int = 0
    changed: int = 0
    gone: int = 0
    duplicates: int = 0  # 한 응답에 같은 편이 두 번 이상 나온 행. 뒤의 것을 쓴다
    keyless: int = 0     # 편명이나 예정일시가 없는 행. 원본에만 남는다
    note: str = ""


@dataclass
class FlightState:
    flight_key: str
    observed_at: str
    item: dict


def ingest(conn, job, run_uid, observed_at, items, complete) -> IngestResult:
    """한 번의 응답을 편별 상태에 반영한다. complete가 아니면 '사라짐'은 판정하지 않는다."""
    result = IngestResult()
    rows = {}
    for item in items:
        key = flight_key(item)
        if key is None:
            result.keyless += 1
            continue
        if key in rows:
            result.duplicates += 1
        rows[key] = item
    result.flights = len(rows)

    current = {row["flight_key"]: (row["row_hash"], row["present"]) for row in conn.execute(
        "SELECT flight_key, row_hash, present FROM latest WHERE job = ?", (job,))}
    snapshots, upserts, seen = [], [], []
    for key, item in rows.items():
        digest = row_hash(item)
        before = current.get(key)
        if before == (digest, 1):
            seen.append((observed_at, job, key))
            continue
        payload = json.dumps(item, ensure_ascii=False)
        snapshots.append((job, key, observed_at, run_uid, 1, digest, payload))
        upserts.append((job, key, observed_at, observed_at, observed_at, digest, payload))
        if before is None:
            result.new += 1
        else:
            result.changed += 1

    gone = []
    if complete:
        present = [key for key, (_, is_present) in current.items() if is_present]
        missing = [key for key in present if key not in rows]
        if missing and len(rows) < len(present) * MIN_PRESENT_RATIO:
            result.note = f"편 수가 {len(present)}→{len(rows)}로 급감해 사라짐을 기록하지 않음"
        else:
            gone = missing
    result.gone = len(gone)

    with conn:
        conn.executemany(
            "INSERT INTO snapshots (job, flight_key, observed_at, run_uid, present, row_hash, item) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            snapshots + [(job, key, observed_at, run_uid, 0, None, None) for key in gone])
        conn.executemany(
            "INSERT INTO latest (job, flight_key, first_seen_at, last_seen_at, changed_at, present, row_hash, item) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT (job, flight_key) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, changed_at = excluded.changed_at, present = 1, "
            "row_hash = excluded.row_hash, item = excluded.item",
            upserts)
        conn.executemany("UPDATE latest SET last_seen_at = ? WHERE job = ? AND flight_key = ?", seen)
        conn.executemany("UPDATE latest SET present = 0, changed_at = ? WHERE job = ? AND flight_key = ?",
                         [(observed_at, job, key) for key in gone])
    return result


def state_as_of(conn, job, when) -> list:
    """when(ISO 표기) 시점에 마지막으로 본 편별 값. 그때 응답에서 사라져 있던 편은 뺀다."""
    rows = conn.execute(
        # SQLite는 MAX()와 함께 고른 다른 열을 MAX가 나온 행에서 가져온다.
        "SELECT flight_key, MAX(observed_at) AS observed_at, present, item FROM snapshots "
        "WHERE job = ? AND observed_at <= ? GROUP BY flight_key", (job, when))
    return [FlightState(row["flight_key"], row["observed_at"], json.loads(row["item"]))
            for row in rows if row["present"]]


def history(conn, job, flight_id, scheduled_prefix="") -> dict:
    """편명(과 예정일시 앞부분)이 맞는 편들의 변화: {편 키: [(관측 시각, 항목 또는 None=사라짐), ...]}"""
    grouped = {}
    for row in conn.execute(
            "SELECT flight_key, observed_at, item FROM snapshots WHERE job = ? AND flight_key LIKE ? "
            "ORDER BY flight_key, observed_at", (job, f"{flight_id}|{scheduled_prefix}%")):
        item = json.loads(row["item"]) if row["item"] else None
        grouped.setdefault(row["flight_key"], []).append((row["observed_at"], item))
    return grouped


def diff(before: dict, after: dict) -> list:
    keys = list(after) + [key for key in before if key not in after]
    return [(key, before.get(key), after.get(key)) for key in keys if before.get(key) != after.get(key)]
