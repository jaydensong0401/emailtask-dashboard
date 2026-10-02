"""동일 업무 연결·기록 이관·모델/서버 통합 회귀 검증. 실제 메일/LLM은 사용하지 않는다."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from nwmail.dashboard import build_model, render_html
from nwmail.feedback import FeedbackError, FeedbackStore
from nwmail.server import make_server, TOKEN_HEADER
from nwmail.store import Store
from nwmail.tasklinks import task_id, task_links
from test_dashboard import ME, NOW, TODAY, mail, put_extraction

KEY = "<linked@test>"
TEXT = "기획안 수정사항 검토 후 승인 여부 회신"
CANON = task_id(KEY, "me", TEXT)
ALIAS = task_id(KEY, "task", TEXT)


def payload():
    return {"project": "루미", "summary": "기획안 승인 대기", "status": "진행중",
            "tasks": [{"text": TEXT, "role": "기타", "deadline": TODAY.isoformat()}],
            "my_actions": [{"text": TEXT, "deadline": TODAY.isoformat(), "urgency": "high"}]}


class TaskLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fb = FeedbackStore(Path(self.tmp.name) / "feedback.db")
        self.addCleanup(self.fb.close)
        self.data = payload()
        self.links = task_links({KEY: self.data}, [ME, "홍길동"])

    def test_exact_and_explicit_reference(self):
        self.assertEqual(self.links[ALIAS]["task_id"], CANON)
        self.data["my_actions"][0].update(text="수정 기획안을 검토하고 승인 여부를 회신", task_text=TEXT)
        self.assertEqual(len(task_links({KEY: self.data}, [ME])), 1)

    def test_do_not_merge_ambiguous_or_other_person_or_deadline(self):
        cases = []
        d = payload(); d["tasks"][0]["assignee"] = "다른담당자"; cases.append(d)
        d = payload(); d["tasks"][0]["deadline"] = "2026-09-12"; cases.append(d)
        d = payload(); d["tasks"].append(copy.deepcopy(d["tasks"][0])); cases.append(d)
        d = payload(); d["my_actions"].append(copy.deepcopy(d["my_actions"][0])); cases.append(d)
        d = payload(); d["tasks"][0]["text"] += " 결과 확인"; cases.append(d)
        d = payload(); d["my_actions"][0]["task_text"] = "없는 업무"; cases.append(d)
        for d in cases:
            with self.subTest(d=d):
                self.assertEqual(task_links({KEY: d}, [ME]), {})
        self.assertEqual(task_links({"a": {"tasks": self.data["tasks"]},
                                     "b": {"my_actions": self.data["my_actions"]}}, [ME]), {})

    def test_owner_from_sent_mail_name(self):
        self.data["tasks"][0]["assignee"] = "홍길동 선임"
        self.assertEqual(len(task_links({KEY: self.data}, ["홍길동"])), 1)

    def test_migration_latest_action_and_idempotence(self):
        self.fb.apply(CANON, "done", now=NOW)
        self.fb.apply(ALIAS, "excluded", "참고만 하면 됨", {"kind": "task"}, now=NOW + timedelta(seconds=1))
        self.fb.link_tasks(self.links)
        self.assertEqual(set(self.fb.states()), {CANON})
        self.assertEqual(self.fb.states()[CANON]["label"], "excluded")
        self.assertEqual(self.fb.states()[CANON]["kind"], "me")
        self.fb.link_tasks(self.links)
        self.assertEqual(len(self.fb.recent(now=NOW)), 1)
        self.assertEqual(self.fb.conn.execute("SELECT count(*) FROM feedback_log").fetchone()[0], 2)

    def test_migration_respects_latest_undo(self):
        self.fb.apply(ALIAS, "done", now=NOW)
        self.fb.apply(CANON, "open", now=NOW + timedelta(seconds=1))
        self.fb.link_tasks(self.links)
        self.assertEqual(self.fb.states(), {})

    def test_every_status_and_undo_use_one_record(self):
        self.fb.link_tasks(self.links)
        for label in ("done", "excluded", "deleted"):
            self.fb.apply(ALIAS, label, snapshot={"kind": "task", "text": "프로젝트 문구"}, now=NOW)
            self.assertEqual(self.fb.states()[CANON]["text"], TEXT)
            self.assertEqual(self.fb.states()[CANON]["label"], label)
            self.assertNotIn(ALIAS, self.fb.states())
            self.fb.apply(CANON, "open", now=NOW)
            self.assertFalse(self.fb.states())
        self.fb.apply(CANON, "done", now=NOW)
        self.fb.apply(ALIAS, "open", now=NOW)
        self.assertFalse(self.fb.states())

    def test_migration_respects_project_undo_without_alias_row(self):
        self.fb.apply(CANON, "done", now=NOW)
        self.fb.apply(ALIAS, "open", now=NOW + timedelta(seconds=1))
        self.fb.set_due(CANON, "2026-09-20", now=NOW)
        self.fb.set_due(ALIAS, "", now=NOW + timedelta(seconds=1))
        self.fb.link_tasks(self.links)
        self.assertFalse(self.fb.states())
        self.assertFalse(self.fb.dues())

    def test_invalid_link_rolls_back_entire_migration(self):
        self.fb.apply(ALIAS, "done", now=NOW)
        with self.assertRaises(FeedbackError):
            self.fb.link_tasks({**self.links, "invalid": {"task_id": CANON}})
        self.assertEqual(set(self.fb.states()), {ALIAS})
        self.assertEqual(self.fb.conn.execute("SELECT count(*) FROM task_links").fetchone()[0], 0)

    def test_due_migration_and_reset(self):
        self.fb.set_due(ALIAS, "2026-09-20", now=NOW)
        self.fb.link_tasks(self.links)
        self.assertEqual(self.fb.dues()[CANON]["due"], "2026-09-20")
        self.fb.set_due(ALIAS, "", now=NOW)
        self.assertFalse(self.fb.dues())

    def test_model_roundtrip_and_counts(self):
        with Store(Path(self.tmp.name) / "mail.db") as st:
            st.save_mails([mail(1, "[루미] 기획안 검토", "sender@example.com", KEY, to=[ME])], me=ME)
            put_extraction(st, KEY, self.data)
            def model():
                return build_model(st, me=ME, today=TODAY, now=NOW, feedback=self.fb)
            m = model()
            tasks = [t for p in m["projects"] for i in p["items"] for ts in i["roles"].values() for t in ts]
            self.assertEqual(tasks[0]["id"], CANON)
            self.assertEqual(m["my_actions"][0]["id"], CANON)
            self.assertEqual(len(m["schedule"]), 1)
            self.fb.apply(ALIAS, "done", now=NOW)
            m = model()
            self.assertEqual(m["stats"]["my_open"], 0)
            self.assertEqual(m["stats"]["done_today"], 1)
            self.assertEqual(m["done_actions"][0]["kind"], "me")
            self.assertEqual(m["projects"][0]["open_count"], 0)
            self.assertEqual(len(m["schedule"]), 1)
            self.fb.apply(CANON, "open", now=NOW)
            self.assertEqual(model()["stats"]["my_open"], 1)

    def test_http_alias_and_canonical(self):
        self.fb.link_tasks(self.links)
        page = Path(self.tmp.name) / "dashboard.html"
        page.write_text("test", encoding="utf-8")
        server = make_server(0, page, self.fb)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for tid, label in ((ALIAS, "done"), (CANON, "open")):
                conn = http.client.HTTPConnection(*server.server_address, timeout=5)
                conn.request("POST", "/api/feedback", json.dumps({"task_id": tid, "label": label}),
                             {TOKEN_HEADER: self.fb.token(), "Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())["states"][tid]
                self.assertEqual(result["label"] if result else "open", label)
                conn.close()
            self.assertFalse(self.fb.states())
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
