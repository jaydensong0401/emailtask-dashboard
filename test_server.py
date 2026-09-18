"""대시보드 로컬 서버 · 표시 기록 검증.

임시 폴더에 기록 DB 와 대시보드 파일을 만들어 실제 HTTP 로 확인한다. 메일함·LLM 불필요.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from nwmail.feedback import FeedbackError, FeedbackStore
from nwmail.server import KST, TOKEN_HEADER, make_server

ok = total = 0
TID = "0123456789ab"


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        hdr("[1] 표시 기록 저장소")
        fb = FeedbackStore(Path(tmp) / "fb.db")
        token = fb.token()
        again = FeedbackStore(Path(tmp) / "fb.db")
        check("토큰은 한 번 만들고 계속 같음", token == again.token() and len(token) >= 20)
        again.close()

        state = fb.apply(TID, "excluded", "남의 일", {"text": "회신", "project": "루미"})
        check("제외 + 사유 + 문구 저장",
              state["label"] == "excluded" and state["reason"] == "남의 일" and state["text"] == "회신")
        state = fb.apply(TID, "done", snapshot={"text": ""})
        check("다시 표시하면 상태만 바뀌고 문구는 유지 (빈 값으로 덮지 않음)",
              state["label"] == "done" and state["reason"] == "" and state["text"] == "회신")
        check("되돌리기는 현재 상태를 지움", fb.apply(TID, "open") is None and TID not in fb.states())
        actions = [r["action"] for r in fb.conn.execute("SELECT action FROM feedback_log")]
        check("누른 기록은 전부 남음", actions == ["excluded", "done", "open"], str(actions))
        for label, args in (("알 수 없는 표시", (TID, "later")),
                            ("표시와 맞지 않는 사유", (TID, "done", "중복")),
                            ("ID 형식", ("../../etc", "done"))):
            try:
                fb.apply(*args)
                check(f"거부: {label}", False)
            except FeedbackError:
                check(f"거부: {label}", True)
        long = fb.apply("ffffffffffff", "deleted", "중복", {"text": "가" * 2000})
        check("문구 길이 제한", len(long["text"]) == 500)
        fb.apply("ffffffffffff", "open")

        hdr("[1-1] 직접 정한 기한 저장소 (일정 탭)")
        due = fb.set_due(TID, "2026-09-20", {"text": "배너 회신", "deadline": "2026-09-18", "kind": "me"})
        check("기한 + 그때 문구 · 메일 기한 저장",
              due["due"] == "2026-09-20" and due["text"] == "배너 회신" and due["deadline"] == "2026-09-18")
        due = fb.set_due(TID, "2026-09-22", {"text": ""})
        check("다시 정하면 기한만 바뀌고 문구는 유지", due["due"] == "2026-09-22" and due["text"] == "배너 회신")
        check("빈 기한은 지움 (메일 기한으로 되돌리기)", fb.set_due(TID, "") is None and TID not in fb.dues())
        log = [r["due"] for r in fb.conn.execute("SELECT due FROM due_log ORDER BY id")]
        check("정한 기록은 전부 남음", log == ["2026-09-20", "2026-09-22", ""], str(log))
        for label, args in (("날짜 형식", (TID, "9월 20일")), ("없는 날짜", (TID, "2026-02-30")),
                            ("구분자 없는 날짜", (TID, "20260920")), ("너무 먼 해", (TID, "2099-01-01")),
                            ("ID 형식", ("../etc", "2026-09-20"))):
            try:
                fb.set_due(*args)
                check(f"거부: {label}", False)
            except FeedbackError:
                check(f"거부: {label}", True)

        hdr("[2] 로컬 서버")
        page = Path(tmp) / "dashboard.html"
        page.write_text("<!doctype html><title>대시보드</title>", encoding="utf-8")
        server = make_server(0, page, fb)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def req(method: str, path: str, body=None, host=f"127.0.0.1:{port}", tok=token,
                extra: dict | None = None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Host": host, **(extra or {})}
            if tok:
                headers[TOKEN_HEADER] = tok
            data = None
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            conn.close()
            return r.status, dict(r.getheaders()), raw

        try:
            status, headers, raw = req("GET", "/", tok=None)
            check("대시보드 페이지 (캐시 안 함)", status == 200 and "대시보드" in raw.decode()
                  and headers.get("Cache-Control") == "no-store")
            check("켜짐 확인은 토큰 없이", req("GET", "/api/ping", tok=None)[0] == 204)
            check("localhost 주소도 허용", req("GET", "/", host=f"localhost:{port}", tok=None)[0] == 200)
            check("다른 Host 로 오면 거부 (DNS 리바인딩)",
                  req("GET", "/", host=f"evil.example:{port}", tok=None)[0] == 403)
            check("상태 조회는 토큰 필요", req("GET", "/api/feedback", tok=None)[0] == 403)
            check("틀린 토큰 거부", req("GET", "/api/feedback", tok="x")[0] == 403)

            item = {"task_id": TID, "label": "deleted", "reason": "중복",
                    "snapshot": {"text": "배너 회신", "thread_key": "<a@t>", "project": "루미"}}
            check("기록은 토큰 없으면 거부", req("POST", "/api/feedback", item, tok=None)[0] == 403)
            check("기록은 다른 Host 면 거부",
                  req("POST", "/api/feedback", item, host="evil.example")[0] == 403)
            status, _, raw = req("POST", "/api/feedback", item)
            res = json.loads(raw)
            check("업무삭제 기록", status == 200 and res["states"][TID]["label"] == "deleted", raw[:120])
            status, _, raw = req("GET", "/api/feedback")
            check("조회에 반영", json.loads(raw)["states"][TID]["reason"] == "중복")
            check("당시 문구 · 대화 저장", fb.states()[TID]["thread_key"] == "<a@t>")

            status, _, raw = req("POST", "/api/feedback", dict(item, label="done", reason="남의 일"))
            check("잘못된 사유는 400 + 이유", status == 400 and "사유" in json.loads(raw)["error"])
            check("본문이 JSON 이 아니면 400", req("POST", "/api/feedback", b"{oops")[0] == 400)
            big = b"{" + b" " * 70_000 + b"}"
            check("너무 큰 본문은 413", req("POST", "/api/feedback", big)[0] == 413)

            batch = {"items": [{"task_id": "aaaaaaaaaaaa", "label": "done", "on": "2026-09-01"},
                               {"task_id": "bbbbbbbbbbbb", "label": "done", "on": "2999-01-01"}]}
            status, _, raw = req("POST", "/api/feedback", batch)
            states = fb.states()
            check("여러 건 한 번에 (예전 체크 옮기기)",
                  status == 200 and {"aaaaaaaaaaaa", "bbbbbbbbbbbb"} <= set(states))
            moved = datetime.fromisoformat(states["aaaaaaaaaaaa"]["labeled_at"])
            check("옮긴 체크는 그날 날짜로 (UTC 로 저장)",
                  moved.utcoffset().total_seconds() == 0
                  and moved.astimezone(KST).date().isoformat() == "2026-09-01", str(moved))
            future = datetime.fromisoformat(states["bbbbbbbbbbbb"]["labeled_at"])
            check("미래 날짜는 지금 시각으로", future <= datetime.now(timezone.utc))

            status, _, raw = req("POST", "/api/feedback", {"task_id": TID, "label": "open"})
            check("되돌리기", status == 200 and json.loads(raw)["states"][TID] is None
                  and TID not in fb.states())
            check("없는 주소는 404", req("GET", "/api/nothing")[0] == 404)

            hdr("[3] 답을 기다리는 메일 · 프로젝트 표시 (/api/mark)")
            wait = {"target": "thread", "key": "<w@t>", "label": "answered",
                    "ref_at": "2026-09-16T01:00:00+00:00", "title": "[누리마케팅] 문구 확인"}
            check("표시는 토큰 없으면 거부", req("POST", "/api/mark", wait, tok=None)[0] == 403)
            status, _, raw = req("POST", "/api/mark", wait)
            res = json.loads(raw)
            check("답변완료 기록 + 기준 시각",
                  status == 200 and res["state"]["label"] == "answered"
                  and res["state"]["ref_at"] == wait["ref_at"], raw[:160])
            proj = {"target": "project", "key": "오로라 포럼", "label": "done",
                    "ref_at": "2026-09-15T00:00:00+00:00"}
            check("프로젝트 완료 기록", req("POST", "/api/mark", proj)[0] == 200)
            status, _, raw = req("GET", "/api/feedback")
            got = json.loads(raw)
            check("조회에 대화 · 프로젝트 표시가 함께",
                  got["threads"]["<w@t>"]["label"] == "answered"
                  and got["projects"]["오로라 포럼"]["label"] == "done")
            for label, body in (("대상과 맞지 않는 표시", dict(wait, label="done")),
                                ("알 수 없는 대상", dict(wait, target="task")),
                                ("빈 키", dict(wait, key="")),
                                ("기준 시각 형식", dict(wait, ref_at="어제"))):
                status, _, raw = req("POST", "/api/mark", body)
                check(f"거부: {label} (400)", status == 400 and json.loads(raw).get("error"))
            status, _, raw = req("POST", "/api/mark", dict(proj, label="open"))
            check("프로젝트 되돌리기", status == 200 and json.loads(raw)["state"] is None
                  and "오로라 포럼" not in fb.marks("project"))
            log = [r["action"] for r in fb.conn.execute(
                "SELECT action FROM mark_log WHERE key = ? ORDER BY id", ("오로라 포럼",))]
            check("누른 기록은 전부 남음", log == ["done", "open"], str(log))

            hdr("[4] 기한 직접 정하기 (/api/due)")
            body = {"task_id": TID, "due": "2026-09-25", "snapshot": {"text": "배너 회신", "deadline": ""}}
            check("기한은 토큰 없으면 거부", req("POST", "/api/due", body, tok=None)[0] == 403)
            check("기한은 다른 Host 면 거부", req("POST", "/api/due", body, host="evil.example")[0] == 403)
            status, _, raw = req("POST", "/api/due", body)
            check("기한 기록", status == 200 and json.loads(raw)["state"]["due"] == "2026-09-25", raw[:120])
            status, _, raw = req("GET", "/api/feedback")
            check("조회에 직접 정한 기한이 함께", json.loads(raw)["dues"][TID]["due"] == "2026-09-25")
            status, _, raw = req("POST", "/api/due", dict(body, due="다음 주"))
            check("잘못된 날짜는 400 + 이유", status == 400 and "형식" in json.loads(raw)["error"])
            check("본문이 JSON 객체가 아니면 400", req("POST", "/api/due", b"[1]")[0] == 400)
            status, _, raw = req("POST", "/api/due", {"task_id": TID, "due": ""})
            check("메일 기한으로 되돌리기", status == 200 and json.loads(raw)["state"] is None
                  and TID not in fb.dues())

            try:
                make_server(port, page, fb).server_close()
                check("같은 포트에 두 번 뜨지 않음", False)
            except OSError:
                check("같은 포트에 두 번 뜨지 않음", True)
        finally:
            server.shutdown()
            server.server_close()
            fb.close()

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
