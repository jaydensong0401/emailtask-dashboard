"""토큰 비교의 기간/미집계/기록 보존 검증. 외부 API와 메일 본문은 사용하지 않는다."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO

from nwmail import usage
from nwmail.config import ROOT
from nwmail.store import KST, Store
from nwmail.usage_report import build_report, render_markdown, render_report_html, save_report
from usage_report import main as report_main


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.now = datetime(2026, 10, 12, 18, 0, tzinfo=KST)

    def tearDown(self):
        self.store.close()

    def call(self, day, tokens, *, purpose="extract", mode="full", threads=2,
             cache=0, error="", private=False):
        at = datetime.fromisoformat(day + "T10:00:00+09:00").astimezone(timezone.utc).isoformat()
        return usage.record_call(self.store, purpose, at=at, mode=mode, threads=threads,
                                 usage={"input_tokens": tokens, "cache_read_tokens": cache}, error=error,
                                 detail=[["SECRET-ID", "SECRET-SUBJECT", "SECRET-PROJECT", 10]] if private else None)

    def seed(self, current=50, previous=100):
        self.call("2026-09-28", previous, private=True)
        self.call("2026-10-05", current, mode="delta", private=True)

    def test_completed_korean_dates_and_future_excluded(self):
        self.seed()
        # 한국 날짜 10/5 00:00 은 UTC 10/4 15:00: 직전/최근 경계를 확인한다.
        usage.record_call(self.store, "extract", at="2026-10-04T14:59:59+00:00", usage={"input_tokens": 3})
        usage.record_call(self.store, "extract", at="2026-10-04T15:00:00+00:00", usage={"input_tokens": 4})
        self.call("2026-10-12", 100000)
        self.call("2026-10-13", 100000)
        report = build_report(self.store, self.now)
        self.assertEqual((report["previous"]["start"], report["previous"]["end"]), ("2026-09-28", "2026-10-04"))
        self.assertEqual((report["current"]["start"], report["current"]["end"]), ("2026-10-05", "2026-10-11"))
        self.assertEqual((report["previous"]["tokens"], report["current"]["tokens"]), (103, 54))
        self.assertEqual(report["source"]["tokens"], 157)
        self.assertEqual(len(report["current"]["daily"]), 7)
        self.assertEqual(report["current"]["daily"][0]["tokens"], 54)

    def test_reduced_increased_and_unchanged(self):
        self.seed()
        report = build_report(self.store, self.now)
        self.assertTrue(report["comparison"]["comparable"])
        self.assertEqual(report["comparison"]["status"], "reduced")
        self.assertEqual(report["comparison"]["saved_tokens"], 50)
        self.assertEqual(report["comparison"]["reduction_pct"], 0.5)
        self.call("2026-10-05", 50)
        self.assertEqual(build_report(self.store, self.now)["comparison"]["status"], "unchanged")
        self.call("2026-10-05", 25)
        increased = build_report(self.store, self.now)
        self.assertEqual(increased["comparison"]["status"], "increased")
        self.assertIn("25개 (25.0%) 증가", render_markdown(increased))
        self.assertIn("+25 토큰", render_report_html(increased))
        self.assertNotIn("-25.0% 감소", render_report_html(increased))

    def test_no_baseline_and_partial_baseline_do_not_claim_savings(self):
        self.call("2026-10-05", 50)
        report = build_report(self.store, self.now)
        self.assertEqual(report["comparison"]["status"], "insufficient_history")
        self.assertIsNone(report["comparison"]["reduction_pct"])
        self.call("2026-09-30", 100)
        report = build_report(self.store, self.now)
        self.assertFalse(report["comparison"]["comparable"])
        self.assertEqual(report["comparison"]["status"], "insufficient_history")
        self.assertNotIn("감소했습니다", report["headline"])

    def test_empty_current_and_unmetered_calls_do_not_claim_savings(self):
        self.call("2026-09-28", 100)
        report = build_report(self.store, self.now)
        self.assertEqual(report["comparison"]["status"], "no_current_activity")
        self.call("2026-10-05", 0)
        report = build_report(self.store, self.now)
        self.assertEqual(report["current"]["unmetered_calls"], 1)
        self.assertEqual(report["comparison"]["status"], "unmetered")
        self.assertFalse(report["comparison"]["comparable"])

    def test_measured_zero_with_run_and_operational_counts(self):
        self.call("2026-09-28", 100)
        self.store.conn.execute("INSERT INTO llm_runs (at, threads, cached, skipped) VALUES (?,?,?,?)",
                                ("2026-10-05T01:00:00+00:00", 5, 5, 3))
        report = build_report(self.store, self.now)
        self.assertEqual(report["comparison"]["status"], "reduced")
        self.assertEqual(report["comparison"]["reduction_pct"], 1)
        self.assertEqual((report["current"]["cached_threads"], report["current"]["skipped_threads"]), (5, 3))
        self.assertIsNone(report["estimates"]["avoided_tokens"])

    def test_retry_tokens_count_and_workload_normalization_excludes_verify(self):
        self.seed()
        self.call("2026-10-05", 10, cache=10, error="synthetic failure")
        self.call("2026-10-06", 1000, purpose="verify", threads=0)
        report = build_report(self.store, self.now)
        self.assertEqual(report["current"]["tokens"], 1070)
        self.assertEqual(report["current"]["failed_calls"], 1)
        self.assertEqual(report["current"]["unmetered_calls"], 0)
        self.assertEqual(report["current"]["tokens_per_sent_thread"], 17.5)
        self.assertEqual({r["mode"]: r["tokens"] for r in report["current"]["modes"]}, {"full": 20, "delta": 50})

    def test_report_does_not_read_private_columns_or_change_logs(self):
        self.seed()
        calls = [tuple(r) for r in self.store.conn.execute("SELECT * FROM llm_calls ORDER BY id")]
        denied = []

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and (table == "mails" or column in ("detail", "error")):
                denied.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.conn.set_authorizer(authorize)
        report = build_report(self.store, self.now)
        self.assertTrue(report["available"])
        self.assertEqual(denied, [])
        self.store.conn.set_authorizer(None)
        self.assertEqual(calls, [tuple(r) for r in self.store.conn.execute("SELECT * FROM llm_calls ORDER BY id")])
        for output in (json.dumps(report), render_markdown(report), render_report_html(report)):
            self.assertNotIn("SECRET", output)

    def test_missing_tables_and_empty_data_graceful(self):
        report = build_report(self.store, self.now)
        self.assertEqual(report["comparison"]["status"], "no_data")
        self.assertIn("기록이 없습니다", render_markdown(report))
        self.store.conn.execute("DROP TABLE llm_calls")
        report = build_report(self.store, self.now)
        self.assertFalse(report["available"])
        self.assertEqual(report["comparison"]["status"], "unavailable")
        self.assertIn("비교를 보류", render_report_html(report))
        self.assertEqual(usage.summary(self.store, self.now)["reduction_report"]["comparison"]["status"], "unavailable")

    def test_snapshots_preserve_dates_and_changes_without_duplicates(self):
        self.seed()
        base = ROOT / "out" / "usage-report-tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            report = build_report(self.store, self.now)
            path = save_report(report, tmp)
            original = path.read_bytes()
            same = build_report(self.store, self.now + timedelta(hours=1))
            self.assertEqual(save_report(same, tmp), path)
            self.assertEqual(path.read_bytes(), original)
            self.call("2026-10-06", 25)
            changed = build_report(self.store, self.now)
            changed_path = save_report(changed, tmp)
            self.assertNotEqual(changed_path, path)
            next_path = save_report(build_report(self.store, self.now + timedelta(days=1)), tmp)
            self.assertNotEqual(next_path, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 3)
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(set(pool.map(lambda _: save_report(report, tmp), range(4))), {path})
            self.assertEqual(len(list(Path(tmp).glob("*.md"))), 3)
            self.assertEqual(list(Path(tmp).glob(".report-*.tmp")), [])

    def test_cli_reads_database_without_mutation(self):
        base = ROOT / "out" / "usage-report-tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            db = Path(tmp) / "usage.db"
            self.seed()
            other = sqlite3.connect(db)
            self.store.conn.backup(other)
            other.close()
            original = db.read_bytes()
            with redirect_stdout(StringIO()):
                status = report_main(["--db", str(db), "--out", str(Path(tmp) / "reports"), "--as-of", "2026-10-12"])
            self.assertEqual(status, 0)
            self.assertEqual(db.read_bytes(), original)
            self.assertEqual(len(list((Path(tmp) / "reports").glob("*.md"))), 1)

    def test_invalid_days_rejected(self):
        for days in (0, -1, 367, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                build_report(self.store, self.now, days)


if __name__ == "__main__":
    unittest.main()
