"""수집기 설정. 환경 변수가 collector/.env 파일보다 우선한다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = COLLECTOR_DIR.parent
DEFAULT_BASE_URL = "https://apis.data.go.kr/B551177/statusOfBaggageClaimDesk"

# API 시각이 한국 시각이라 기록도 한국 시각(+09:00)으로 통일한다. 서머타임이 없어 고정 시차로 충분하다.
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def iso(moment: datetime) -> str:
    """DB와 원본 파일에 쓰는 시각 표기. 모두 같은 형식이라 문자열 비교가 곧 시각 비교다."""
    return moment.astimezone(KST).isoformat(timespec="seconds")


def parse_time(text: str) -> datetime:
    """'2026-10-12 14:00', ISO 표기, API식 '202610121400'을 받는다. 시간대가 없으면 한국 시각으로 본다."""
    text = text.strip()
    formats = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    if text.isdigit() and len(text) in formats:
        moment = datetime.strptime(text, formats[len(text)])
    else:
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.astimezone(KST)


@dataclass(frozen=True)
class Config:
    service_key: str = ""
    data_dir: Path = COLLECTOR_DIR / "data"
    base_url: str = DEFAULT_BASE_URL
    interval_minutes: int = 5  # 한 번에 한 쪽(2,000행)이면 5분 간격으로 하루 약 290회. 개발계정 500회 안이다
    num_of_rows: int = 2000
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    heartbeat_url: str = ""
    webhook_url: str = ""
    notify_command: str = ""  # 알림 메시지를 마지막 인자로 붙여 실행할 명령
    stale_minutes: int = 60
    report_hour: int = 9
    daily_limit: int = 500

    @property
    def db_path(self) -> Path:
        return self.data_dir / "collector.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


# 환경 변수 → (설정 이름, 변환)
ENV_VARS = {
    "DATA_GO_KR_SERVICE_KEY": ("service_key", str),
    "COLLECTOR_DATA_DIR": ("data_dir", project_path),
    "COLLECTOR_BASE_URL": ("base_url", lambda value: value.rstrip("/")),
    "COLLECTOR_INTERVAL_MINUTES": ("interval_minutes", int),
    "COLLECTOR_NUM_OF_ROWS": ("num_of_rows", int),
    "COLLECTOR_TIMEOUT_SECONDS": ("timeout_seconds", float),
    "COLLECTOR_MAX_ATTEMPTS": ("max_attempts", int),
    "COLLECTOR_HEARTBEAT_URL": ("heartbeat_url", str),
    "COLLECTOR_WEBHOOK_URL": ("webhook_url", str),
    "COLLECTOR_NOTIFY_COMMAND": ("notify_command", str),
    "COLLECTOR_STALE_MINUTES": ("stale_minutes", int),
    "COLLECTOR_REPORT_HOUR": ("report_hour", int),
    "COLLECTOR_DAILY_LIMIT": ("daily_limit", int),
}


def read_env_file(path: Path) -> dict:
    """KEY=VALUE 줄만 읽는다. 빈 줄, 주석, export 접두어, 값을 감싼 따옴표는 건너뛴다."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def load_config(environ=None) -> Config:
    environ = os.environ if environ is None else environ
    env_file = project_path(environ.get("COLLECTOR_ENV_FILE", str(COLLECTOR_DIR / ".env")))
    merged = {**read_env_file(env_file), **environ}
    values = {}
    for name, (field, convert) in ENV_VARS.items():
        raw = str(merged.get(name, "")).strip()
        if raw:
            try:
                values[field] = convert(raw)
            except ValueError:
                raise ValueError(f"{name} 값을 읽을 수 없다: {raw!r}") from None
    cfg = Config(**values)
    if not 1 <= cfg.interval_minutes <= 60:
        raise ValueError("COLLECTOR_INTERVAL_MINUTES는 1~60이어야 한다.")
    if cfg.num_of_rows < 1 or cfg.max_attempts < 1:
        raise ValueError("COLLECTOR_NUM_OF_ROWS와 COLLECTOR_MAX_ATTEMPTS는 1 이상이어야 한다.")
    if not 0 <= cfg.report_hour <= 23:
        raise ValueError("COLLECTOR_REPORT_HOUR는 0~23이어야 한다.")
    return cfg
