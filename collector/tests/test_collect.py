import gzip
import json
import tempfile
import unittest

from collector import archive, collect, db, flights
from collector.collect import Busy, Collector
from collector.config import iso
from collector.tests.helpers import FakeClock, FakeServer, fixture, make_config

KE902 = "KE902|202610122340"
CANCELLED = "7C1302|202610121530"


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = {("MA", 1): fixture("arrivals_p1.json"), ("MA", 2): fixture("arrivals_p2.json"),
                      ("SL", 1): fixture("arrivals_slave_p1.json")}
        self.queued = []  # 쪽과 상관없이 먼저 내보낼 (상태, 본문)
        self.server = FakeServer(self.serve)
        self.cfg = make_config(self.tmp.name, self.server.url)
        self.clock = FakeClock(2026, 10, 12, 14, 0, 5)
        self.collector = Collector(self.cfg, clock=self.clock, sleep=lambda seconds: None)
        self.conn = self.collector.conn

    def tearDown(self):
        self.collector.close()
        self.server.close()
        self.tmp.cleanup()

    def serve(self, method, path, query, body):
        if self.queued:
            return self.queued.pop(0)
        page = self.pages.get((query.get("codeshare"), int(query.get("pageNo", 1))))
        return (200, page) if page is not None else (500, b"Internal Server Error")

    def edit(self, key, change):
        data = json.loads(self.pages[key])
        change(data["response"]["body"])
        self.pages[key] = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def drop_cancelled_flight(self):
        def drop(body):
            body["items"].pop(0)
            body["totalCount"] = 4
        self.edit(("MA", 2), drop)
        self.edit(("MA", 1), lambda body: body.update(totalCount=4))

    def run_at(self, *moment, job="arrivals"):
        self.clock.set(*moment)
        return self.collector.run_now(job)

    def tick_at(self, *moment):
        self.clock.set(*moment)
        return [result.job for result in self.collector.tick()]

    def keys_as_of(self, when):
        return {state.flight_key for state in flights.state_as_of(self.conn, "arrivals", when)}

    def test_keeps_every_response_as_received(self):
        result = self.collector.run_now("arrivals")
        self.assertEqual((result.status, result.pages, result.items, result.calls), ("ok", 2, 5, 2))
        paths = sorted(self.cfg.raw_dir.glob("arrivals/2026-10-12/*.json.gz"))
        self.assertEqual([path.name for path in paths], ["arrivals-20261012T140005-p01-a1.json.gz",
                                                         "arrivals-20261012T140005-p02-a1.json.gz"])
        record = archive.read(paths[0])
        self.assertEqual(archive.body_bytes(record), self.pages[("MA", 1)])
        self.assertEqual(record["params"], {"type": "json", "searchDay": "0", "codeshare": "MA",
                                            "numOfRows": "3", "pageNo": "1"})
        self.assertEqual((record["http_status"], record["requested_at"]), (200, "2026-10-12T14:00:05+09:00"))

    def test_keeps_paging_when_the_server_sends_smaller_pages(self):
        collector = Collector(make_config(self.tmp.name, self.server.url, num_of_rows=5),
                              clock=self.clock, sleep=lambda seconds: None)
        try:
            result = collector.run_now("arrivals")  # 5행씩 달라고 했는데 3행만 와도 전체 5행까지 받는다
        finally:
            collector.close()
        self.assertEqual((result.status, result.pages, result.items, result.notes), ("ok", 2, 5, []))

    def test_service_key_is_sent_encoded_once_and_never_stored(self):
        self.collector.run_now("arrivals")
        _, path, query, _ = self.server.requests[0]
        self.assertEqual(path, "/B551177/statusOfBaggageClaimDesk/getFltArrivalsBaggageClaimDesk")
        self.assertTrue(query.startswith("serviceKey=test%2BKey%2Fabc%3D%3D&"))
        stored = b"".join(gzip.decompress(p.read_bytes()) for p in self.cfg.raw_dir.rglob("*.json.gz"))
        stored += b"".join(p.read_bytes() for p in self.cfg.data_dir.glob("collector.db*"))
        self.assertNotIn(b"test+Key", stored)
        self.assertNotIn(b"test%2BKey", stored)

    def test_records_a_snapshot_only_when_a_flight_changes(self):
        self.collector.run_now("arrivals")
        second = self.run_at(2026, 10, 12, 14, 10, 5)
        self.assertEqual((second.ingest.new, second.ingest.changed, second.ingest.gone), (0, 0, 0))
        self.assertEqual(db.snapshot_count(self.conn), 5)

        self.edit(("MA", 1), lambda body: body["items"][2].update(fstandPosition="233"))
        third = self.run_at(2026, 10, 12, 14, 20, 5)
        self.assertEqual(third.ingest.changed, 1)
        versions = flights.history(self.conn, "arrivals", "KE902")[KE902]
        self.assertEqual([(at, item["fstandPosition"]) for at, item in versions],
                         [("2026-10-12T14:00:05+09:00", "245"), ("2026-10-12T14:20:05+09:00", "233")])
        latest = self.conn.execute("SELECT first_seen_at, last_seen_at, changed_at FROM latest "
                                   "WHERE flight_key = ?", (KE902,)).fetchone()
        self.assertEqual(tuple(latest), ("2026-10-12T14:00:05+09:00", "2026-10-12T14:20:05+09:00",
                                         "2026-10-12T14:20:05+09:00"))

    def test_state_as_of_follows_flights_that_leave_the_response(self):
        self.collector.run_now("arrivals")
        self.drop_cancelled_flight()
        result = self.run_at(2026, 10, 12, 14, 10, 5)
        self.assertEqual(result.ingest.gone, 1)
        self.assertEqual(self.keys_as_of("2026-10-12T13:59:00+09:00"), set())
        self.assertIn(CANCELLED, self.keys_as_of("2026-10-12T14:05:00+09:00"))
        after = self.keys_as_of("2026-10-12T14:10:05+09:00")
        self.assertEqual((len(after), CANCELLED in after), (4, False))
        row = self.conn.execute("SELECT present, item FROM latest WHERE flight_key = ?", (CANCELLED,)).fetchone()
        self.assertEqual((row["present"], json.loads(row["item"])["remark"]), (0, "결항"))  # 마지막 값은 남는다

    def test_partial_run_does_not_mark_flights_gone(self):
        self.collector.run_now("arrivals")
        self.pages[("MA", 2)] = None  # 두 번째 쪽은 계속 500
        result = self.run_at(2026, 10, 12, 14, 10, 5)
        self.assertEqual((result.status, result.pages, result.calls, result.ingest.gone), ("partial", 1, 3, 0))
        self.assertIn("HTTP 500", result.error)
        self.assertEqual(len(self.keys_as_of(iso(self.clock()))), 5)

    def test_retries_a_failed_call(self):
        self.queued = [(503, b"Service Unavailable")]
        result = self.collector.run_now("arrivals")
        self.assertEqual((result.status, result.calls, result.error), ("ok", 3, None))
        self.assertIn("1쪽은 2번째 시도에 받음 (앞선 오류: HTTP 503: Service Unavailable)", result.notes)
        calls = [tuple(row) for row in self.conn.execute(
            "SELECT page, attempt, http_status, error FROM calls ORDER BY page, attempt")]
        self.assertEqual(calls, [(1, 1, 503, "HTTP 503: Service Unavailable"), (1, 2, 200, None), (2, 1, 200, None)])
        self.assertEqual(len(list(self.cfg.raw_dir.rglob("*.json.gz"))), 3)  # 실패한 응답도 남는다

    def test_calls_each_page_once_while_an_outage_lasts(self):
        self.pages[("MA", 1)] = None
        first = self.collector.run_now("arrivals")
        second = self.run_at(2026, 10, 12, 14, 10, 5)  # 직전이 실패라 재시도하지 않는다
        self.pages[("MA", 1)] = fixture("arrivals_p1.json")
        third = self.run_at(2026, 10, 12, 14, 20, 5)
        self.queued = [(503, b"Service Unavailable")]
        fourth = self.run_at(2026, 10, 12, 14, 30, 5)  # 다시 받기 시작하면 재시도도 돌아온다
        self.assertEqual([(r.status, r.calls) for r in (first, second, third, fourth)],
                         [("failed", 2), ("failed", 1), ("ok", 2), ("ok", 3)])

    def test_does_not_retry_a_quota_error(self):
        self.queued = [(200, fixture("error_quota.xml"))]
        result = self.collector.run_now("arrivals")
        self.assertEqual((result.status, result.calls), ("failed", 1))
        self.assertIn("API 오류 22", result.error)

    def test_empty_response_does_not_wipe_out_flights(self):
        self.collector.run_now("arrivals")
        self.edit(("MA", 1), lambda body: body.update(items=[], totalCount=0))
        result = self.run_at(2026, 10, 12, 14, 10, 5)
        self.assertEqual((result.status, result.ingest.gone), ("ok", 0))
        self.assertIn("급감", result.ingest.note)
        self.assertEqual(len(self.keys_as_of(iso(self.clock()))), 5)

    def test_notes_slave_rows_in_a_master_request(self):
        self.edit(("MA", 1), lambda body: body["items"][0].update(codeshare="Slave"))
        result = self.collector.run_now("arrivals")
        self.assertIn("codeshare=MA로 요청했는데 Slave 행 1개가 섞여 옴", result.notes)

    def test_tick_runs_each_slot_once(self):
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 0, 5), ["arrivals", "arrivals_slave"])
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 5, 5), [])
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 10, 5), ["arrivals"])
        self.assertEqual(self.tick_at(2026, 10, 12, 18, 0, 5), ["arrivals", "arrivals_slave"])
        self.assertEqual(self.tick_at(2026, 10, 12, 23, 35, 5), ["arrivals"])
        self.assertEqual(self.tick_at(2026, 10, 12, 23, 40, 5), ["arrivals"])
        self.assertEqual(self.tick_at(2026, 10, 13, 6, 0, 30), ["arrivals", "arrivals_slave"])

    def test_missed_slot_runs_late(self):
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 0, 5), ["arrivals", "arrivals_slave"])
        # 18:00에 컴퓨터가 잠들어 있었으면 깨어난 뒤에라도 받는다
        self.assertEqual(self.tick_at(2026, 10, 12, 23, 35, 5), ["arrivals", "arrivals_slave"])

    def test_failed_slot_is_tried_twice_at_most(self):
        self.queued = [(401, b"Unauthorized"), (200, self.pages[("SL", 1)]), (401, b"Unauthorized")]
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 0, 5), ["arrivals", "arrivals_slave"])
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 5, 5), ["arrivals"])
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 7, 5), [])
        self.assertEqual(self.tick_at(2026, 10, 12, 14, 10, 5), ["arrivals"])
        statuses = [row[0] for row in self.conn.execute(
            "SELECT status FROM runs WHERE job = 'arrivals' ORDER BY started_at")]
        self.assertEqual(statuses, ["failed", "failed", "ok"])

    def test_rebuild_from_raw_files_gives_the_same_tables(self):
        self.collector.run_now("arrivals")
        self.collector.run_now("arrivals_slave")
        self.edit(("MA", 1), lambda body: body["items"][0].update(bagCarouselId="7"))
        self.run_at(2026, 10, 12, 14, 10, 5)
        self.drop_cancelled_flight()
        self.run_at(2026, 10, 12, 14, 20, 5)
        self.pages[("MA", 2)] = None
        self.run_at(2026, 10, 12, 14, 30, 5)  # 일부만 받은 실행도 섞는다

        def tables():
            return ([tuple(row) for row in self.conn.execute(
                        "SELECT * FROM snapshots ORDER BY job, flight_key, observed_at")],
                    [tuple(row) for row in self.conn.execute("SELECT * FROM latest ORDER BY job, flight_key")])

        before = tables()
        summary = collect.rebuild(self.conn, self.cfg.raw_dir)
        self.assertEqual(tables(), before)
        self.assertEqual((summary["runs"], summary["used"]), (5, 5))

    def test_only_one_collection_at_a_time(self):
        other = Collector(self.cfg, clock=self.clock, sleep=lambda seconds: None)
        try:
            with other.lock(), self.assertRaises(Busy):
                self.collector.tick()
        finally:
            other.close()

    def test_runs_cut_off_midway_are_marked_failed(self):
        db.start_run(self.conn, "arrivals-cut", "arrivals", "2026-10-12T13:50:00+09:00", "2026-10-12T13:50:01+09:00")
        self.collector.tick()
        row = self.conn.execute("SELECT status, error FROM runs WHERE run_uid = 'arrivals-cut'").fetchone()
        self.assertEqual(tuple(row), ("failed", "실행이 중간에 끝남"))
