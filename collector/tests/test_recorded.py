"""2026-10-12 21:12에 실제 API에서 받은 응답(녹화본)으로 해석부터 저장까지 돌린다."""
import gzip
import tempfile
import unittest

from collector import api, flights
from collector.collect import Collector
from collector.tests.helpers import FakeClock, FakeServer, fixture, make_config

RECORDED = "recorded_arrivals_20261012T211256.json.gz"


class RecordedResponseTest(unittest.TestCase):
    def setUp(self):
        self.body = gzip.decompress(fixture(RECORDED))

    def test_shape_of_the_real_response(self):
        page = api.parse_page(self.body)
        self.assertEqual((len(page.items), page.total_count, page.result_code), (1105, 1105, "00"))
        self.assertEqual({len(item) for item in page.items}, {27})
        self.assertEqual({item["codeshare"] for item in page.items}, {"Master"})  # codeshare=MA가 듣는다
        self.assertEqual(len({flights.flight_key(item) for item in page.items}), 1105)  # 편 키가 겹치지 않는다

    def test_collects_the_real_response_in_one_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeServer(lambda method, path, query, body: (200, self.body))
            collector = Collector(make_config(tmp, server.url, num_of_rows=2000),
                                  clock=FakeClock(2026, 10, 12, 21, 12, 56), sleep=lambda seconds: None)
            try:
                result = collector.run_now("arrivals")
                states = flights.state_as_of(collector.conn, "arrivals", "2026-10-12T21:15:00+09:00")
            finally:
                collector.close()
                server.close()
        self.assertEqual((result.status, result.pages, result.calls, result.ingest.new), ("ok", 1, 1, 1105))
        self.assertEqual(result.notes, [])
        self.assertEqual(len(states), 1105)
