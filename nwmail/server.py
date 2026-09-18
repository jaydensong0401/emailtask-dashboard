"""대시보드 로컬 서버. 이 PC 에서만 접속된다 (127.0.0.1).

  GET  /               out/dashboard.html (캐시하지 않음)
  GET  /api/ping       켜져 있는지 확인 (파일로 연 대시보드가 서버 주소로 옮겨 갈 때)
  GET  /api/feedback   내 할 일 · 대화 · 프로젝트 표시 상태 + 직접 정한 기한
  POST /api/feedback   완료 · 제외 · 업무삭제 · 되돌리기 기록
  POST /api/mark       답을 기다리는 메일 답변완료 · 프로젝트 완료 · 삭제 · 되돌리기 기록
  POST /api/due        할 일 기한 직접 정하기 · 메일 기한으로 되돌리기 (일정 탭)

다른 웹사이트가 브라우저를 통해 이 서버로 기록을 보내지 못하도록
Host 헤더(127.0.0.1/localhost)와 대시보드에 심은 토큰을 확인한다.
"""
from __future__ import annotations

import json
import socket
import socketserver
import threading
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .feedback import MARKS, FeedbackError, FeedbackStore

BODY_MAX = 64_000
KST = timezone(timedelta(hours=9))
TOKEN_HEADER = "X-Nwmail-Token"


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False      # 윈도우에서 켜 두면 같은 포트에 서버가 두 번 뜬다
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)     # 호스트 이름 조회(느림)는 건너뛴다
        self.server_name, self.server_port = self.server_address[:2]


def make_server(port: int, page: Path, feedback: FeedbackStore,
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    token = feedback.token()
    lock = threading.Lock()                # sqlite 연결 하나를 요청 스레드들이 나눠 쓴다

    class Handler(BaseHTTPRequestHandler):
        server_version = "nwmail"

        def log_message(self, *args) -> None:      # 창 없이 돌므로 요청마다 남기지 않는다
            pass

        # ── 응답 ──────────────────────────────────────────────────────
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _local_host(self) -> bool:
            """DNS 리바인딩 방지: 주소창이 127.0.0.1/localhost 일 때만 받는다."""
            bound = self.server.server_address[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{bound}", f"localhost:{bound}"}

        def _authorized(self) -> bool:
            return self._local_host() and self.headers.get(TOKEN_HEADER) == token

        # ── 요청 ──────────────────────────────────────────────────────
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._local_host():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "허용되지 않은 주소"})
            if path == "/api/ping":
                return self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")
            if path == "/api/feedback":
                if not self._authorized():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "토큰이 맞지 않습니다"})
                with lock:
                    rows = feedback.states()
                    marks = {t: feedback.marks(t) for t in MARKS}
                    dues = feedback.dues()
                states = {k: {"label": v["label"], "reason": v["reason"],
                              "labeled_at": v["labeled_at"]} for k, v in rows.items()}
                return self._json(HTTPStatus.OK, {
                    "states": states, "dues": {k: _due_json(v) for k, v in dues.items()}, **{
                        f"{t}s": {k: _mark_json(v) for k, v in m.items()}
                        for t, m in marks.items()}})
            if path in ("/", "/dashboard.html"):
                try:
                    body = page.read_bytes()
                except OSError:
                    return self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                                      "대시보드가 아직 없습니다. python dashboard.py 를 실행하세요."
                                      .encode("utf-8"), "text/plain; charset=utf-8")
                return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소"})

        def _post_mark(self) -> None:
            """{target: thread|project, key, label, ref_at, title} 하나를 기록한다."""
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "토큰이 맞지 않습니다"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= BODY_MAX:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "본문 크기가 올바르지 않습니다"})
            try:
                it = json.loads(self.rfile.read(length).decode("utf-8"))
                with lock:
                    state = feedback.mark(str(it.get("target") or ""), str(it.get("key") or ""),
                                          str(it.get("label") or ""), str(it.get("ref_at") or ""),
                                          str(it.get("title") or ""))
            except (ValueError, AttributeError, UnicodeDecodeError) as e:
                msg = str(e) if isinstance(e, FeedbackError) else "요청 형식이 올바르지 않습니다"
                return self._json(HTTPStatus.BAD_REQUEST, {"error": msg})
            return self._json(HTTPStatus.OK, {"ok": True, "state": _mark_json(state)})

        def _post_due(self) -> None:
            """{task_id, due: YYYY-MM-DD | '', snapshot} 하나를 기록한다. 빈 due 는 메일 기한으로 되돌리기."""
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "토큰이 맞지 않습니다"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= BODY_MAX:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "본문 크기가 올바르지 않습니다"})
            try:
                it = json.loads(self.rfile.read(length).decode("utf-8"))
                snap = it.get("snapshot") if isinstance(it.get("snapshot"), dict) else {}
                with lock:
                    state = feedback.set_due(str(it.get("task_id") or ""), str(it.get("due") or ""),
                                             snap)
            except (ValueError, AttributeError, UnicodeDecodeError) as e:
                msg = str(e) if isinstance(e, FeedbackError) else "요청 형식이 올바르지 않습니다"
                return self._json(HTTPStatus.BAD_REQUEST, {"error": msg})
            return self._json(HTTPStatus.OK, {"ok": True, "state": _due_json(state)})

        def _post_filters(self) -> None:
            """차단 규칙 · 보호 도메인 변경. 처리는 serve.py 가 붙인 filters_handler 가 한다."""
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "토큰이 맞지 않습니다"})
            handler = getattr(self.server, "filters_handler", None)
            if handler is None:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                                  {"error": "이 서버에서는 차단 설정을 바꿀 수 없습니다"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= BODY_MAX:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "본문 크기가 올바르지 않습니다"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("요청 형식이 올바르지 않습니다")
                result = handler(data)
            except ValueError as e:              # FilterError 포함: 보호 도메인 겨냥 등
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:               # 저장소 잠금 등. 서버는 계속 살아 있어야 한다
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  {"error": f"처리하지 못했습니다: {type(e).__name__}"})
            return self._json(HTTPStatus.OK, result)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] == "/api/mark":
                return self._post_mark()
            if self.path.split("?", 1)[0] == "/api/due":
                return self._post_due()
            if self.path.split("?", 1)[0] == "/api/filters":
                return self._post_filters()
            if self.path.split("?", 1)[0] != "/api/feedback":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소"})
            if not self._authorized():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "토큰이 맞지 않습니다"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= BODY_MAX:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                                  if length > BODY_MAX else HTTPStatus.BAD_REQUEST,
                                  {"error": "본문 크기가 올바르지 않습니다"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                items = data["items"] if isinstance(data.get("items"), list) else [data]
                states = {}
                for it in items[:500]:
                    snap = it.get("snapshot") if isinstance(it.get("snapshot"), dict) else {}
                    with lock:
                        state = feedback.apply(str(it.get("task_id") or ""),
                                               str(it.get("label") or ""),
                                               str(it.get("reason") or ""), snap,
                                               now=_past_day(it.get("on")))
                    states[it["task_id"]] = (
                        {"label": state["label"], "reason": state["reason"],
                         "labeled_at": state["labeled_at"]} if state else None)
            except (ValueError, KeyError, AttributeError, UnicodeDecodeError) as e:
                msg = str(e) if isinstance(e, FeedbackError) else "요청 형식이 올바르지 않습니다"
                return self._json(HTTPStatus.BAD_REQUEST, {"error": msg})
            return self._json(HTTPStatus.OK, {"ok": True, "states": states})

    return _Server((host, port), Handler)


def _mark_json(state: dict | None) -> dict | None:
    return ({"label": state["label"], "ref_at": state["ref_at"], "labeled_at": state["labeled_at"]}
            if state else None)


def _due_json(state: dict | None) -> dict | None:
    return {"due": state["due"], "set_at": state["set_at"]} if state else None


def _past_day(value) -> datetime | None:
    """예전 체크를 옮길 때 표시 날짜(YYYY-MM-DD). 없거나 미래면 지금 시각을 쓴다."""
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return None
    at = datetime(d.year, d.month, d.day, 12, tzinfo=KST)
    return at if at <= datetime.now(timezone.utc) else None
