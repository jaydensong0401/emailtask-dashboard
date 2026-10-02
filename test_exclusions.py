"""분석 제외 · 수신자 재지정 알림 검증. 실제 메일·AI 호출 없이 임시 저장소만 사용한다."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nwmail.client import Mail
from nwmail.dashboard import build_model, recipient_alerts, render_html
from nwmail.extract import Extractor
from nwmail.feedback import FeedbackStore
from nwmail.store import Store


ME = "mine@example.com"
AT = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.store.close)
        self.fb = FeedbackStore(Path(self.tmp.name) / "feedback.db")
        self.addCleanup(self.fb.close)
        self.cfg = SimpleNamespace(imap_user=ME, summary_backend="claude_code")
        self.add_mail(1, when=AT - timedelta(hours=1))
        self.key = self.store.threads(None)[0]["thread_key"]
        self.ext = Extractor(self.cfg, self.store, self.fb)
        self.seen = []

        def call(prompt):
            self.seen.append(prompt)
            return {"threads": [dict(thread_key=t["thread_key"], summary="정리 결과",
                                     project="", status="진행중", tasks=[], my_actions=[], decisions=[])
                                for t in self.store.threads(None)
                                if f'key="{t["thread_key"]}"' in prompt]}
        self.ext._call = call

    def add_mail(self, uid, *, to=(ME,), cc=(), when=None, folder="INBOX", sender="sender@example.com",
                 refs=None, subject="테스트 대화"):
        when = when or AT + timedelta(minutes=uid)
        mail = Mail(str(uid), subject, "보낸사람", sender, when.isoformat(),
                    body=f"{uid}번째 요청", to=list(to), cc=list(cc), folder=folder,
                    message_id=f"<m{uid}@test>", references=["<m1@test>"] if refs is None else refs)
        self.store.save_mails([mail], me=ME)

    def exclude(self, label="excluded", reason="남의 일", tid="aaaaaaaaaaaa", key=None):
        return self.fb.apply(tid, label, reason,
                             {"thread_key": key or self.key, "text": "확인 요청", "kind": "me"}, now=AT)

    def run_extract(self, **kwargs):
        return self.ext.run(self.store.threads(None), progress=lambda *_: None, **kwargs)

    def alerts(self):
        return recipient_alerts(self.store, self.fb.states(), ME)

    def test_new_reply_and_force_never_sent(self):
        self.exclude()
        self.add_mail(2)
        for force in (False, True):
            result = self.run_extract(force=force)
            self.assertEqual((result["excluded"], result["extracted"], result["cached"]), (1, 0, 0))
        self.assertEqual(self.seen, [])
        run = self.store.conn.execute("SELECT * FROM llm_runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((run["threads"], run["skipped"], run["calls"]), (0, 1, 0))

    def test_not_a_task_also_excludes_and_undo_resumes(self):
        self.exclude("deleted", "할 일 아님")
        self.assertEqual(self.run_extract()["excluded"], 1)
        self.fb.apply("aaaaaaaaaaaa", "open")
        self.assertEqual(self.run_extract()["extracted"], 1)
        self.assertEqual(len(self.seen), 1)

    def test_other_reasons_do_not_exclude_whole_thread(self):
        for label, reason in (("done", ""), ("deleted", "중복"), ("excluded", "참고만 하면 됨")):
            with self.subTest(reason=reason):
                self.exclude(label, reason)
                self.assertEqual(self.run_extract(force=True)["extracted"], 1)

    def test_all_exclusions_must_be_undone(self):
        self.exclude()
        self.exclude("deleted", "할 일 아님", tid="bbbbbbbbbbbb")
        self.fb.apply("aaaaaaaaaaaa", "open")
        self.assertEqual(self.run_extract()["excluded"], 1)
        self.fb.apply("bbbbbbbbbbbb", "open")
        self.assertEqual(self.run_extract()["extracted"], 1)

    def test_cached_result_is_untouched_and_new_mail_processed_after_undo(self):
        self.run_extract()
        before = self.ext.rows([self.key])[self.key]
        self.exclude()
        self.add_mail(2)
        self.run_extract(force=True)
        self.assertEqual(self.ext.rows([self.key])[self.key], before)
        self.fb.apply("aaaaaaaaaaaa", "open")
        self.assertEqual(self.run_extract()["extracted"], 1)
        self.assertIn("2번째 요청", self.seen[-1])

    def test_exclusions_survive_restart_and_do_not_match_subject(self):
        self.exclude()
        with FeedbackStore(self.fb.path) as reopened:
            self.ext.feedback = reopened
            self.add_mail(3, refs=[])
            result = self.run_extract(force=True)
        self.assertEqual((result["excluded"], result["extracted"]), (1, 1))
        self.assertNotIn(f'key="{self.key}"', self.seen[0])

    def test_merged_thread_retains_exclusion(self):
        self.exclude(key="<old@test>")
        with self.store.tx() as conn:
            conn.execute("INSERT INTO thread_alias VALUES (?,?)", ("<old@test>", self.key))
        self.add_mail(2)
        self.assertEqual(self.run_extract()["excluded"], 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_feedback_read_failure_makes_no_ai_call(self):
        with patch.object(self.fb, "states", side_effect=sqlite3.OperationalError("locked")):
            result = self.run_extract(force=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.seen, [])

    def test_to_alert_excludes_old_mail_cc_and_sent(self):
        self.exclude()
        self.assertEqual(self.alerts(), [])
        self.add_mail(2, to=("other@example.com",), cc=(ME,))
        self.add_mail(3, folder="Sent", sender=ME)
        self.add_mail(4, folder="Archive")
        self.add_mail(5, to=("홍길동 <another@example.com>",))
        self.assertEqual(self.alerts(), [])
        self.add_mail(6, to=(f"홍길동 <{ME.upper()}>",))
        self.assertEqual([a["uid"] for a in self.alerts()], ["INBOX:6"])
        self.assertEqual(self.run_extract()["excluded"], 1)

    def test_latest_notification_only_and_undo_clears(self):
        self.exclude()
        self.add_mail(2)
        self.add_mail(3)
        self.assertEqual([a["uid"] for a in self.alerts()], ["INBOX:3"])
        self.assertEqual(self.alerts(), self.alerts())
        self.fb.apply("aaaaaaaaaaaa", "open")
        self.assertEqual(self.alerts(), [])

    def test_normal_thread_does_not_raise_reassignment_alert(self):
        self.add_mail(2)
        self.assertEqual(self.alerts(), [])

    def test_old_exclusion_remains_available_for_undo(self):
        self.exclude()
        model = build_model(self.store, ME, now=AT + timedelta(days=45), feedback=self.fb)
        self.assertIn("aaaaaaaaaaaa", [a["id"] for a in model["dropped_actions"]])
        self.fb.apply("aaaaaaaaaaaa", "open")
        model = build_model(self.store, ME, now=AT + timedelta(days=45), feedback=self.fb)
        self.assertEqual(model["dropped_actions"], [])

    def test_dashboard_notification_escaped_and_no_prompt_for_excluded(self):
        self.exclude()
        self.add_mail(2, subject='<script>alert("x")</script>')
        model = build_model(self.store, ME, now=AT + timedelta(hours=1), feedback=self.fb)
        self.assertNotIn(self.key, model["sizes"])
        page = render_html(model)
        self.assertIn("받는사람으로 재지정되었습니다.", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('<script>alert("x")</script>', page)
        self.assertIn('"analysisExclusions": {"excluded": "남의 일", "deleted": "할 일 아님"}', page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
