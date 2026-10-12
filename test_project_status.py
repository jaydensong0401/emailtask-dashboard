"""프로젝트 수동 상태의 영구 저장 · 새 메일 유지 · 되돌리기 검증."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nwmail.feedback import FeedbackError, FeedbackStore, MARKS, mark_active


class ProjectStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "feedback.db"

    def test_not_mine_survives_reopen_new_mail_and_undo_keeps_history(self):
        project = "오로라 포럼"
        received = "2026-09-15T00:00:00+00:00"
        with FeedbackStore(self.path) as fb:
            state = fb.mark("project", project, "not_mine", received, project)
            self.assertEqual(state["label"], "not_mine")
            self.assertEqual(MARKS["project"][state["label"]], "내 일 아님")

        with FeedbackStore(self.path) as fb:
            state = fb.marks("project")[project]
            self.assertEqual(state["title"], project)
            self.assertEqual(state["ref_at"], received)
            self.assertEqual(mark_active(state, "2026-09-20T00:00:00+00:00"), state)
            self.assertIsNone(fb.mark("project", project, "open"))
            self.assertNotIn(project, fb.marks("project"))
            actions = [r["action"] for r in fb.conn.execute(
                "SELECT action FROM mark_log WHERE target = ? AND key = ? ORDER BY id",
                ("project", project))]
            self.assertEqual(actions, ["not_mine", "open"])

        with FeedbackStore(self.path) as fb:
            self.assertNotIn(project, fb.marks("project"))

    def test_status_changes_replace_current_state_but_keep_every_event(self):
        with FeedbackStore(self.path) as fb:
            for label in ("done", "not_mine", "done", "deleted", "open"):
                fb.mark("project", "루미", label)
                self.assertEqual(len(fb.marks("project")), label != "open")
                if label != "open":
                    self.assertEqual(fb.marks("project")["루미"]["label"], label)
            self.assertEqual([r["action"] for r in fb.conn.execute(
                "SELECT action FROM mark_log ORDER BY id")],
                ["done", "not_mine", "done", "deleted", "open"])

    def test_not_mine_is_a_project_only_status(self):
        with FeedbackStore(self.path) as fb:
            with self.assertRaises(FeedbackError):
                fb.mark("thread", "<mail@test>", "not_mine")
            self.assertEqual(fb.marks("thread"), {})

    def test_existing_new_mail_behavior_for_done_answered_and_deleted(self):
        before = "2026-09-15T00:00:00+00:00"
        after = "2026-09-16T00:00:00+00:00"
        for label in ("done", "answered"):
            state = {"label": label, "ref_at": before}
            self.assertEqual(mark_active(state, before), state)
            self.assertIsNone(mark_active(state, after))
        state = {"label": "deleted", "ref_at": before}
        self.assertEqual(mark_active(state, after), state)


if __name__ == "__main__":
    unittest.main()
