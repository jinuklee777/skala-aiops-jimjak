"""자정 소실, 예정시각 정정, 정답 수정과 시간 누수에 대한 회귀 테스트."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector import alerts, cli, dataset, db, flights, quality
from collector.config import parse_time
from collector.schedule import PRIMARY_JOB
from collector.tests.helpers import make_config


def item(**updates):
    return dict(flightId='TW248', scheduleDatetime='202610121405', estimatedDatetime='202610121405',
                airportCode='NRT', aircraftRegNo='HL8098', typeOfFlight='I', codeshare='Master',
                bagCarouselId='6') | updates


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'collector.db'
        self.conn = db.connect(self.path)
        self.counter = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_at(self, time, items, status='ok', finished=None, recorded=True):
        self.counter += 1
        uid = f'run-{self.counter:03}'
        at = parse_time(time).isoformat()
        end = parse_time(finished or time).isoformat()
        if recorded:
            db.start_run(self.conn, uid, PRIMARY_JOB, at, at)
        flights.ingest(self.conn, PRIMARY_JOB, uid, at, items, status == 'ok')
        if recorded:
            with self.conn:
                self.conn.execute('UPDATE runs SET status = ?, finished_at = ?, items = ? WHERE run_uid = ?',
                                  (status, end, len(items), uid))
        return uid

    def test_cross_date_schedule_change_preserves_one_operation_and_raw_keys(self):
        old = item(scheduleDatetime='202610130025', estimatedDatetime='202610122355')
        new = dict(old, scheduleDatetime='202610122355')
        self.run_at('2026-10-12 23:50', [old])
        self.run_at('2026-10-12 23:55', [new])
        self.run_at('2026-10-13 00:00', [dict(new, landingDatetime='202610122357', remark='착륙')])
        tracks, _, links, ambiguous = quality.load_tracks(self.conn)
        self.assertEqual((len(tracks), len(links), len(ambiguous)), (1, 1, 0))
        self.assertEqual(len(next(iter(tracks.values())).aliases), 2)
        self.assertEqual(db.flight_counts(self.conn, PRIMARY_JOB), (1, 2))
        result = quality.report(self.conn, parse_time('2026-10-12').date())
        self.assertEqual(result['flights'], 1)
        self.assertEqual(result['international_by_landing_hour']['23'], {'landed': 1, 'with_last': 0})

    def test_different_or_unknown_tail_and_already_landed_are_not_merged(self):
        for old_changes, new_changes in [({}, {'aircraftRegNo': 'OTHER'}),
                                         ({'aircraftRegNo': ''}, {'aircraftRegNo': ''}),
                                         ({'landingDatetime': '202610121350'}, {})]:
            with self.subTest(old=old_changes, new=new_changes):
                a, b = item(**old_changes), item(scheduleDatetime='202610121410', **new_changes)
                events = [dict(key=flights.flight_key(a), item=a, observed_at='a', run_uid='a', available_at='a'),
                          dict(key=flights.flight_key(a), item=None, observed_at='b', run_uid='b', available_at='b'),
                          dict(key=flights.flight_key(b), item=b, observed_at='b', run_uid='b', available_at='b')]
                tracks, _, links, _ = quality.reconcile(events)
                self.assertEqual((len(tracks), len(links)), (2, 0))

    def test_daily_repeated_flight_is_not_merged(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-13 13:00', [item(scheduleDatetime='202610131405')])
        tracks, _, links, _ = quality.load_tracks(self.conn)
        self.assertEqual((len(tracks), len(links)), (2, 0))

    def test_ambiguous_replacements_are_flagged_not_merged(self):
        self.run_at('2026-10-12 13:00', [item(), item(scheduleDatetime='202610121410')])
        self.run_at('2026-10-12 13:05', [item(scheduleDatetime='202610121415')])
        tracks, _, links, ambiguous = quality.load_tracks(self.conn)
        self.assertEqual((len(tracks), len(links), len(ambiguous)), (3, 0, 1))
        self.assertTrue(all(t.identity_conflict for t in tracks.values()))
        self.assertEqual(dataset.build(self.conn)['samples'], 0)

    def test_old_alias_reappearing_blocks_evaluation(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:05', [item(scheduleDatetime='202610121410')])
        self.run_at('2026-10-12 13:10', [item(), item(scheduleDatetime='202610121410')])
        tracks, _, _, _ = quality.load_tracks(self.conn)
        self.assertTrue(next(iter(tracks.values())).identity_conflict)
        self.assertEqual(dataset.build(self.conn)['samples'], 0)

    def test_midnight_disappearance_and_overdue_remain_missing(self):
        a = item(flightId='LOST', scheduleDatetime='202610122250', landingDatetime='202610122305', remark='착륙')
        b = item(flightId='STILL', scheduleDatetime='202610122355', landingDatetime='202610122357')
        c = item(flightId='TOMORROW', scheduleDatetime='202610131000')
        self.run_at('2026-10-12 23:59', [a, b, c])
        self.run_at('2026-10-13 00:00', [b, c])
        self.run_at('2026-10-13 09:00', [b, c])
        result = alerts.label_counts(self.conn, parse_time('2026-10-12').date())
        self.assertEqual((result['arrived'], result['late_arrived'], result['late_with_last']), (2, 2, 0))
        self.assertEqual({m['status'] for m in result['quality']['missing']},
                         {'missing_disappeared', 'missing_overdue'})
        warnings = alerts.warnings_for(dict(result, expected=1, ok=1, partial=0, usual_flights=None,
                                            calls=1, daily_limit=500, disk_free=10**10))
        self.assertTrue(any('마지막 짐 미확보 2편' in w for w in warnings))

    def test_label_revisions_and_retraction_respect_as_of(self):
        a = item(landingDatetime='202610121350', bagFirstTime='202610121410', bagLastTime='202610121425')
        self.run_at('2026-10-12 14:30', [a])
        self.run_at('2026-10-12 14:45', [dict(a, bagLastTime='202610121440')])
        self.run_at('2026-10-12 16:00', [dict(a, bagLastTime='')])
        day = parse_time('2026-10-12').date()
        before = quality.report(self.conn, day, parse_time('2026-10-12 14:35'))
        self.assertEqual((before['with_last'], before['label_revisions']), (1, []))
        after = quality.report(self.conn, day)
        label = after['label_revisions'][0]
        self.assertEqual((after['with_last'], label['version'], label['status']), (0, 3, 'missing_overdue'))
        self.assertEqual([v['value'] for v in label['versions']], ['202610121425', '202610121440', None])

    def test_invalid_label_order_and_format_cannot_be_evaluated(self):
        a = item(landingDatetime='202610121350', bagLastTime='202610121340')
        b = item(flightId='BAD', landingDatetime='garbage', bagLastTime='202610121425')
        self.run_at('2026-10-12 15:00', [a, b])
        result = quality.report(self.conn, parse_time('2026-10-12').date())
        self.assertEqual((result['with_last'], result['statuses']['invalid']), (0, 2))

    def test_landing_correction_also_versions_the_target(self):
        self.run_at('2026-10-12 14:30', [item(landingDatetime='202610121350', bagLastTime='202610121425')])
        self.run_at('2026-10-12 14:35', [item(landingDatetime='202610121355', bagLastTime='202610121425')])
        label = quality.report(self.conn, parse_time('2026-10-12').date())['label_revisions'][0]
        self.assertEqual((label['version'], label['wait_minutes']), (2, 30))

    def test_dataset_freezes_features_and_versions_labels(self):
        self.run_at('2026-10-12 13:00', [item()])
        source = self.run_at('2026-10-12 13:05', [item()])  # 값이 같아 스냅샷은 안 늘어남
        self.run_at('2026-10-12 14:30', [item(landingDatetime='202610121350', bagLastTime='202610121425',
                                                   bagCarouselId='9')])
        self.run_at('2026-10-12 14:45', [item(landingDatetime='202610121350', bagLastTime='202610121440',
                                                   bagCarouselId='9')])
        result = dataset.build(self.conn)
        self.assertEqual((result['samples'], result['labeled_samples']), (1, 1))
        row = result['rows'][0]
        self.assertEqual((row['features']['bagCarouselId'], row['source_run']), ('6', source))
        self.assertNotIn('landingDatetime', row['features'])
        self.assertNotIn('remark', row['features'])
        self.assertEqual(row['label']['wait_minutes'], 50)
        self.assertEqual(row['label']['version'], 2)
        before = dataset.build(self.conn, until=parse_time('2026-10-12 14:35'))['rows'][0]
        self.assertEqual(before['label']['wait_minutes'], 35)
        self.assertEqual(before['label']['version'], 1)

    def test_dataset_uses_completion_time_and_no_initial_backfill(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:04', [item(bagCarouselId='8')], finished='2026-10-12 13:06')
        self.assertEqual(dataset.build(self.conn, until=parse_time('2026-10-12 13:05'))['samples'], 0)
        row = dataset.build(self.conn)['rows'][0]
        self.assertEqual((row['forecast_at'], row['eta_lead_minutes']), ('2026-10-12T13:06:00+09:00', 59))
        self.assertEqual(row['features']['bagCarouselId'], '8')
        self.run_at('2026-10-12 13:10', [item(), item(flightId='INITIAL', estimatedDatetime='202610121350')])
        self.assertEqual(dataset.build(self.conn)['samples'], 1)

    def test_eta_jump_records_actual_shorter_lead(self):
        self.run_at('2026-10-12 13:00', [item(estimatedDatetime='202610121500')])
        self.run_at('2026-10-12 13:05', [item(estimatedDatetime='202610121320')])
        row = dataset.build(self.conn)['rows'][0]
        self.assertEqual(row['eta_lead_minutes'], 15)
        self.assertFalse(row['eligible_for_evaluation'])

    def test_large_gap_does_not_generate_forecast(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:20', [item()])
        self.assertEqual(dataset.build(self.conn)['samples'], 0)

    def test_domestic_slave_and_already_landed_do_not_generate_forecasts(self):
        rows = [item(typeOfFlight='D'), item(flightId='SLAVE', codeshare='Slave'),
                item(flightId='LANDED', landingDatetime='202610121250')]
        self.run_at('2026-10-12 13:00', rows)
        self.run_at('2026-10-12 13:05', rows)
        self.assertEqual(dataset.build(self.conn)['samples'], 0)

    def test_partial_update_is_visible_only_after_complete_collection(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:05', [item(bagCarouselId='8')], status='partial')
        self.assertEqual(dataset.build(self.conn)['samples'], 0)
        self.run_at('2026-10-12 13:06', [item(bagCarouselId='8')])
        row = dataset.build(self.conn)['rows'][0]
        self.assertEqual((row['features']['bagCarouselId'], row['forecast_at']),
                         ('8', '2026-10-12T13:06:00+09:00'))

    def test_missing_completion_and_suppressed_disappearance_are_excluded(self):
        self.run_at('2026-10-12 13:00', [item()], recorded=False)
        self.run_at('2026-10-12 13:01', [item()])
        self.run_at('2026-10-12 13:05', [item()])
        result = dataset.build(self.conn)
        self.assertEqual((result['samples'], result['flights_without_completion']), (0, 1))
        self.assertGreater(result['inconsistent_runs'], 0)

    def test_collapse_protection_does_not_issue_from_stale_state(self):
        self.run_at('2026-10-12 13:00', [item(), item(flightId='B'), item(flightId='C')])
        self.run_at('2026-10-12 13:05', [item()])  # 급감 보호로 사라짐을 기록하지 않음
        result = dataset.build(self.conn)
        self.assertEqual((result['samples'], result['inconsistent_runs']), (0, 1))

    def test_schedule_change_keeps_forecast_features_and_joins_later_label(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:05', [item()])
        new = item(scheduleDatetime='202610121410', bagCarouselId='9')
        self.run_at('2026-10-12 13:10', [new])
        self.run_at('2026-10-12 14:30', [dict(new, landingDatetime='202610121350', bagLastTime='202610121425')])
        result = dataset.build(self.conn)
        self.assertEqual((result['samples'], result['labeled_samples']), (1, 1))
        row = result['rows'][0]
        self.assertEqual(row['features']['scheduleDatetime'], '202610121405')
        self.assertEqual(len(row['aliases']), 2)

    def test_readonly_and_cli_jsonl(self):
        self.run_at('2026-10-12 13:00', [item()])
        self.run_at('2026-10-12 13:05', [item()])
        conn = db.readonly(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM runs')
        finally:
            conn.close()
        cfg = make_config(self.tmp.name, 'http://unused')
        args = cli.build_parser().parse_args(['dataset'])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_dataset(cfg, args), 0)
        self.assertEqual(json.loads(err.getvalue())['samples'], 1)
        self.assertEqual(json.loads(out.getvalue())['features']['bagCarouselId'], '6')

    def test_empty_database_and_invalid_parameters(self):
        self.assertEqual(dataset.build(self.conn)['rows'], [])
        self.assertEqual(quality.report(self.conn, parse_time('2026-10-12').date())['flights'], 0)
        with self.assertRaises(ValueError):
            dataset.build(self.conn, horizon_minutes=0)
