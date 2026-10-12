"""무엇을 언제 받는가. 한 번의 수집이 맡는 구간을 슬롯이라 하고, 구간의 시작 시각으로 부른다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from collector.config import KST

ARRIVALS = "getFltArrivalsBaggageClaimDesk"
PRIMARY_JOB = "arrivals"  # 멈춤 감시·하트비트·일일 보고의 기준
JOB_NAMES = ("arrivals", "arrivals_slave")

# 자정이 지나면 전날 편이 응답(D~D+1)에서 빠진다. 23시대 도착편의 마지막 짐 시각을 놓치지 않게 앞뒤로 더 자주 받는다.
DENSE_WINDOW = ("23:30", "00:30")
DENSE_INTERVAL = 5
# 코드쉐어 편(Slave)은 편마다 공동운항 편 수를 셀 때만 쓴다. 하루 두 번이면 충분하다.
SLAVE_TIMES = ("06:00", "18:00")


def minute_of_day(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class Job:
    name: str
    params: tuple             # serviceKey·numOfRows·pageNo를 뺀 요청 파라미터
    operation: str = ARRIVALS
    interval: int = 0         # 분. 0이면 daily_at에만 받는다
    dense_interval: int = 0
    dense_window: tuple = ()  # (시작, 끝) 자정부터의 분. 끝이 시작보다 작으면 자정을 넘긴다
    daily_at: tuple = ()      # 자정부터의 분

    def _in_dense_window(self, minute: int) -> bool:
        if not self.dense_window:
            return False
        start, end = self.dense_window
        return start <= minute < end if start < end else minute >= start or minute < end

    def slot(self, moment: datetime) -> datetime:
        """moment가 속한 슬롯의 시작 시각."""
        moment = moment.astimezone(KST)
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        minute = moment.hour * 60 + moment.minute
        if self.interval:
            step = self.dense_interval if self._in_dense_window(minute) else self.interval
            return midnight + timedelta(minutes=minute - minute % step)
        earlier = [m for m in self.daily_at if m <= minute]
        if earlier:
            return midnight + timedelta(minutes=max(earlier))
        return midnight + timedelta(minutes=max(self.daily_at) - 24 * 60)

    def slots_on(self, day: date) -> list:
        """그날 시작하는 슬롯들."""
        midnight = datetime(day.year, day.month, day.day, tzinfo=KST)
        moments = (midnight + timedelta(minutes=m) for m in range(24 * 60))
        return [moment for moment in moments if self.slot(moment) == moment]


def build_jobs(cfg) -> list:
    params = (("type", "json"), ("searchDay", "0"))  # searchDay=0: 오늘과 내일
    return [
        Job("arrivals", params + (("codeshare", "MA"),), interval=cfg.interval_minutes,
            dense_interval=min(DENSE_INTERVAL, cfg.interval_minutes),
            dense_window=tuple(minute_of_day(t) for t in DENSE_WINDOW)),
        Job("arrivals_slave", params + (("codeshare", "SL"),),
            daily_at=tuple(minute_of_day(t) for t in SLAVE_TIMES)),
    ]
