"""원본 응답 보관. 응답 하나 = 파일 하나(gzip JSON).

파일에는 응답 본문을 받은 그대로 넣고, 언제 무엇을 요청했는지도 함께 적는다.
그래서 DB가 없어도 이 파일들만으로 편별 상태 표를 다시 만들 수 있다(collect.rebuild).
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import tempfile
from pathlib import Path

from collector import __version__

FORMAT = 1
NAME = re.compile(r"^(?P<run>.+)-p(?P<page>\d+)-a(?P<attempt>\d+)\.json\.gz$")


def envelope(*, job, run_uid, slot, operation, params, page_no, attempt, requested_at, response) -> dict:
    record = {
        "format": FORMAT,
        "collector": __version__,
        "job": job,
        "run": run_uid,
        "slot": slot,
        "operation": operation,
        "params": dict(params),  # serviceKey는 넣지 않는다
        "page": page_no,
        "attempt": attempt,
        "requested_at": requested_at,
        "elapsed_ms": response.elapsed_ms,
        "http_status": response.status,
        "headers": response.headers,
    }
    try:
        record["body"] = response.body.decode("utf-8")
    except UnicodeDecodeError:
        record["body_base64"] = base64.b64encode(response.body).decode("ascii")
    return record


def body_bytes(record) -> bytes:
    """보관한 본문을 받은 그대로의 바이트로 돌려준다."""
    if "body" in record:
        return record["body"].encode("utf-8")
    return base64.b64decode(record["body_base64"])


def write(raw_dir: Path, record, day: str) -> Path:
    """raw/<작업>/<실행 날짜>/<실행>-p<쪽>-a<시도>.json.gz 로 쓴다. 다 쓴 뒤에 이름을 붙여 반쯤 쓴 파일이 남지 않는다."""
    folder = raw_dir / record["job"] / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record['run']}-p{record['page']:02d}-a{record['attempt']}.json.gz"
    data = gzip.compress(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def runs(raw_dir: Path) -> dict:
    """원본 파일을 실행별로 묶는다: {실행: {쪽: [(시도, 경로), ...]}}"""
    grouped = {}
    for path in raw_dir.glob("*/*/*.json.gz"):
        match = NAME.match(path.name)
        if match:
            pages = grouped.setdefault(match["run"], {})
            pages.setdefault(int(match["page"]), []).append((int(match["attempt"]), path))
    return grouped
