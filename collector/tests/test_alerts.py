import json
import tempfile
import unittest

from collector import alerts
from collector.collect import Collector
from collector.tests.helpers import FakeClock, FakeServer, fixture, make_config


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api_down = False
        self.server = FakeServer(self.serve)
        self.cfg = make_config(self.tmp.name, self.server.url, webhook_url=f"{self.server.url}/hook",
                               heartbeat_url=f"{self.server.url}/ping")
        self.clock = FakeClock(2026, 10, 12, 14, 0, 5)
        self.collector = Collector(self.cfg, clock=self.clock, sleep=lambda seconds: None)

    def tearDown(self):
        self.collector.close()
        self.server.close()
        self.tmp.cleanup()

    def serve(self, method, path, query, body):
        if path in ("/hook", "/ping"):
            return 200, b"ok"
        if self.api_down:
            return 500, b"Internal Server Error"
        name = {"MA": "arrivals_p{}.json", "SL": "arrivals_slave_p{}.json"}[query["codeshare"]]
        return 200, fixture(name.format(query["pageNo"]))

    def messages(self):
        return [json.loads(body)["text"] for _, path, _, body in self.server.requests if path == "/hook"]

    def tick_at(self, *moment):
        self.clock.set(*moment)
        self.collector.tick()

    def test_alerts_when_collection_stops_and_when_it_resumes(self):
        self.tick_at(2026, 10, 12, 14, 0, 5)
        self.api_down = True
        self.tick_at(2026, 10, 12, 15, 0, 0)  # 59분 지남: 아직 알리지 않는다
        self.assertEqual(self.messages(), [])
        self.tick_at(2026, 10, 12, 15, 1, 0)
        self.tick_at(2026, 10, 12, 15, 10, 30)  # 같은 멈춤은 바로 다시 알리지 않는다
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("수집 멈춤: 마지막 성공 10/12 14:00 (60분 전). 최근 오류: HTTP 500", self.messages()[0])
        self.tick_at(2026, 10, 12, 21, 2, 0)  # 6시간이 지나면 한 번 더
        self.assertEqual(len(self.messages()), 2)
        self.api_down = False
        self.tick_at(2026, 10, 12, 21, 10, 30)
        self.assertEqual(self.messages()[2], "[짐작 수집기] 수집 재개: 10/12 21:10에 받음")

    def test_heartbeat_follows_successful_collection_only(self):
        self.collector.run_now("arrivals")
        self.api_down = True
        self.clock.set(2026, 10, 12, 14, 10, 5)
        self.collector.run_now("arrivals")
        self.api_down = False
        self.collector.run_now("arrivals_slave")
        self.assertEqual(len([request for request in self.server.requests if request[1] == "/ping"]), 1)

    def test_daily_report_once_a_day(self):
        self.tick_at(2026, 10, 12, 14, 0, 5)
        self.tick_at(2026, 10, 12, 14, 10, 5)
        self.tick_at(2026, 10, 13, 9, 0, 30)
        self.tick_at(2026, 10, 13, 9, 10, 30)
        reports = [message for message in self.messages() if "수집 보고" in message]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].splitlines(), [
            "[짐작 수집기] 10/12(월) 수집 보고",
            "- 회차: 성공 2 · 일부 0 · 실패 0 · 빠짐 61 / 예정 63",  # 14:00 첫 실행부터 자정까지 63회
            "- 호출: 5회 (하루 한도 500)",
            "- 도착 예정 4편: 결항 1, 수취대 미배정 0, 도착 2편 중 마지막 짐 시각 확보 2편 (100%)",
            "- 23시대 도착 0편 중 마지막 짐 시각 확보 0편",
            reports[0].splitlines()[5],
            "⚠ 받은 회차(2)가 예정(63)의 90%에 못 미침",
        ])
        self.assertTrue(reports[0].splitlines()[5].startswith("- 원본: "))


class WebhookPayloadTest(unittest.TestCase):
    def test_discord_reads_content_and_others_read_text(self):
        self.assertEqual(alerts.webhook_payload("https://discord.com/api/webhooks/1/x", "hi"), {"content": "hi"})
        self.assertEqual(alerts.webhook_payload("https://hooks.slack.com/services/T/B/x", "hi"), {"text": "hi"})
