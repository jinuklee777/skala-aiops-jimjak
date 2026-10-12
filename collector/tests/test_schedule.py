import unittest
from datetime import date, datetime, timezone

from collector.config import KST, Config
from collector.schedule import build_jobs


def at(*moment):
    return datetime(*moment, tzinfo=KST)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.arrivals, self.slave = build_jobs(Config(interval_minutes=10))

    def test_every_ten_minutes(self):
        self.assertEqual(self.arrivals.slot(at(2026, 10, 12, 14, 7, 59)), at(2026, 10, 12, 14, 0))

    def test_every_five_minutes_around_midnight(self):
        cases = {(23, 29): (23, 20), (23, 37): (23, 35), (0, 29): (0, 25), (0, 31): (0, 30)}
        for (hour, minute), (slot_hour, slot_minute) in cases.items():
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(self.arrivals.slot(at(2026, 10, 12, hour, minute)),
                                 at(2026, 10, 12, slot_hour, slot_minute))

    def test_slots_per_day(self):
        day = date(2026, 10, 12)
        self.assertEqual(len(self.arrivals.slots_on(day)), 150)  # 10분마다 138회 + 자정 앞뒤 5분마다 12회
        self.assertEqual(len(build_jobs(Config())[0].slots_on(day)), 288)  # 기본값: 5분마다
        self.assertEqual(self.slave.slots_on(day), [at(2026, 10, 12, 6, 0), at(2026, 10, 12, 18, 0)])

    def test_twice_a_day(self):
        self.assertEqual(self.slave.slot(at(2026, 10, 12, 5, 0)), at(2026, 10, 11, 18, 0))
        self.assertEqual(self.slave.slot(at(2026, 10, 12, 17, 59)), at(2026, 10, 12, 6, 0))

    def test_slots_are_in_korean_time(self):
        self.assertEqual(self.arrivals.slot(datetime(2026, 10, 12, 5, 7, tzinfo=timezone.utc)),
                         at(2026, 10, 12, 14, 0))
