"""Synthetic demo parity checks. Does not open a browser, mailbox, or AI service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from demo import build_demo as demo
from nwmail import filter_api
from nwmail.dashboard import BLOCK_JS, CSS, JS, RULES_PAGE, THEME_JS, USAGE_PAGE, build_model
from nwmail.feedback import LABELS, MARKS, REASONS, FeedbackStore
from nwmail.store import Store


class DemoParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="mailwork-demo-test-", dir=demo.ROOT)
        root = Path(cls.temp.name)
        assert root.resolve().is_relative_to(demo.ROOT.resolve())
        cls.mail, cls.feedback = root / "mail.db", root / "feedback.db"
        cls.patches = [patch.object(demo, "DEMO_MAIL", cls.mail),
                       patch.object(demo, "DEMO_FB", cls.feedback)]
        for active in cls.patches:
            active.start()
        demo.seed()
        cls.dashboard = demo.render(None)
        cls.usage = demo.render_usage()
        cls.rules = demo.render_rules()

    @classmethod
    def tearDownClass(cls):
        for active in reversed(cls.patches):
            active.stop()
        cls.temp.cleanup()

    def test_current_operating_dashboard_renderer(self):
        for expected in (CSS, 'data-pmark="not_mine"', 'data-pview="not_mine"',
                         f'href="{RULES_PAGE}"', f'href="{USAGE_PAGE}"', '"projectActions":'):
            self.assertIn(expected, self.dashboard)
        # The production application code is retained, except the theme storage namespace.
        self.assertIn(JS.replace("'nwmail.theme'", "'mailwork.demo.theme'"), self.dashboard)

    def test_auxiliary_pages_and_real_usage_report(self):
        self.assertIn('data-sub="reduction"', self.usage)
        self.assertIn('data-view-key="nwmail.rulesView"', self.rules)
        self.assertIn('메일 분류 규칙', self.rules)
        self.assertIn(demo.NOTICE, self.rules)
        self.assertIn(demo.NOTICE, self.usage)
        for page in (self.dashboard, self.usage, self.rules):
            self.assertIn('<html lang="ko" data-theme="light">', page)
            self.assertIn("getItem('mailwork.demo.theme')||'light'", page)
            self.assertNotIn("getItem('nwmail.theme')", page)
            self.assertIn("modes=['system','light','dark']", page)

    def test_fixture_shares_one_task_across_today_project_and_schedule(self):
        text = "기획전 페이지 최종 검수 후 오픈 승인 요청"
        with Store(self.mail) as store, FeedbackStore(self.feedback) as feedback:
            model = build_model(store, me=demo.ME, now=demo.NOW, window_days=7, feedback=feedback)
            action = next(a for a in model["my_actions"] if a["text"] == text)
            project = next(p for p in model["projects"] if p["name"] == "오로라 스토어")
            task = next(t for item in project["items"] for rows in item["roles"].values()
                        for t in rows if t["text"] == text)
            self.assertEqual(task["id"], action["id"])
            feedback.apply(action["id"], "done", snapshot=action)
            updated = build_model(store, me=demo.ME, now=demo.NOW, window_days=7, feedback=feedback)
            project = next(p for p in updated["projects"] if p["name"] == "오로라 스토어")
            task = next(t for item in project["items"] for rows in item["roles"].values()
                        for t in rows if t["text"] == text)
            self.assertEqual(task["state"]["label"], "done")
            self.assertTrue(any(a["id"] == action["id"] for a in updated["done_actions"]))
            self.assertTrue(any(a["id"] == action["id"] and a["state"]["label"] == "done"
                                for a in updated["schedule"]))
            feedback.apply(action["id"], "open")

    def test_static_demo_separates_real_server_only_filters(self):
        self.assertIn(demo.STATIC_FILTER_NOTICE, self.dashboard)
        self.assertIn('id="demo-contract"', self.dashboard)
        server = demo.render({"port": 8790})
        self.assertNotIn('id="demo-seed"', server)
        self.assertIn(BLOCK_JS, server)
        self.assertIn('"port": 8790', server)

    def test_demo_server_rules_change_rebuilds_all_three_pages(self):
        outputs = {}

        def rebuild():
            outputs.update(index=demo.render({"port": 8790}),
                           rules=demo.render_rules(), usage=demo.render_usage())
            return True

        result = filter_api.handle({"op": "protect", "domain": "travelmag.example", "note": "가상 보호"},
                                   store_factory=lambda: Store(self.mail), rebuild=rebuild)
        self.assertTrue(result["ok"])
        self.assertTrue(result["rebuilt"])
        self.assertEqual(set(outputs), {"index", "rules", "usage"})
        self.assertIn('travelmag.example', outputs["rules"])

    def test_node_api_contract_and_generated_scripts(self):
        node = os.getenv("NODE_BINARY") or shutil.which("node")
        if not node:
            self.skipTest("Node is required for demo API contract checks")
        with FeedbackStore(self.feedback) as fb:
            fixture = {"seed": demo.feedback_seed(fb),
                       "contract": {"labels": LABELS, "reasons": REASONS, "marks": MARKS},
                       "theme": demo.demo_page(THEME_JS)}
        result = subprocess.run([node, str(demo.HERE / "test_demo_api.cjs")],
                                input=json.dumps(fixture, ensure_ascii=False),
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        syntax_script = "const vm=require('node:vm'),fs=require('node:fs');for(const s of JSON.parse(fs.readFileSync(0,'utf8')))new vm.Script(s);"
        scripts = []
        for page in (self.dashboard, self.usage, self.rules):
            scripts.extend(re.findall(r"<script>(.*?)</script>", page, flags=re.S))
        result = subprocess.run([node, "-e", syntax_script], input=json.dumps(scripts),
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
