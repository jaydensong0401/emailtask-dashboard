"""오케스트레이션: 메일 수집(API 또는 IMAP) → Claude 요약 → 출력."""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from .client import Mail, NaverWorksMail
from .config import Config, load_config
from .summarizer import make_summarizer

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
BADGE = {"high": "[높음]", "medium": "[보통]", "low": "[낮음]"}


class MailSummaryAgent:
    """MAIL_SOURCE 설정에 따라 API 경로와 IMAP 경로를 자동 선택한다."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.llm = make_summarizer(self.cfg)
        self._stack = ExitStack()

    def __enter__(self) -> MailSummaryAgent:
        return self

    def __exit__(self, *_) -> None:
        self._stack.close()

    # -- 수집 ---------------------------------------------------------------
    def _collect_via_imap(self, limit: int, unread_only: bool, query: str | None) -> list[Mail]:
        from .imap_client import NaverWorksImap

        imap = self._stack.enter_context(
            NaverWorksImap(self.cfg.imap_user, self.cfg.imap_password)
        )
        if query:
            return imap.search(query, limit=limit)
        return imap.list_mails(limit=limit, unread_only=unread_only)

    def _collect_via_api(self, limit: int, unread_only: bool, query: str | None) -> list[Mail]:
        from .auth import get_access_token

        api = NaverWorksMail(get_access_token(self.cfg))
        base = (
            api.search(query, limit=limit)
            if query
            else api.list_mails(api.inbox_folder_id(), limit=limit, unread_only=unread_only)
        )
        return list(api.iter_with_bodies(base))   # API 는 본문을 따로 조회해야 한다

    def collect(
        self, limit: int = 15, unread_only: bool = False, query: str | None = None
    ) -> list[Mail]:
        if self.cfg.mail_source == "imap":
            return self._collect_via_imap(limit, unread_only, query)
        return self._collect_via_api(limit, unread_only, query)

    # -- 실행 ---------------------------------------------------------------
    def run(
        self, limit: int = 15, unread_only: bool = False, query: str | None = None
    ) -> dict:
        mails = self.collect(limit=limit, unread_only=unread_only, query=query)
        if not mails:
            return {"overview": "요약할 메일이 없습니다.", "items": [], "count": 0,
                    "source": self.cfg.mail_source}
        result = self.llm.summarize(mails)
        result["count"] = len(mails)
        result["source"] = self.cfg.mail_source
        return result


def render(result: dict) -> str:
    src = {"imap": "IMAP", "api": "Works API"}.get(result.get("source", ""), "")
    backend = result.get("_backend", "rules")
    cost = "과금" if backend.startswith("api(") else "무료"
    lines = ["=" * 68,
             f"네이버웍스 메일 요약  (수집: {src} / 요약: {backend} · {cost})",
             "=" * 68, ""]
    lines += [result.get("overview", ""), ""]

    items = sorted(
        result.get("items", []),
        key=lambda i: PRIORITY_ORDER.get(i.get("priority", "low"), 3),
    )
    todos = [i for i in items if i.get("action_required")]

    if todos:
        lines += [f"할 일 {len(todos)}건", "-" * 68]
        for i in todos:
            due = f"  (기한: {i['deadline']})" if i.get("deadline") else ""
            lines.append(f"  {BADGE.get(i.get('priority'), '')} {i.get('action', '')}{due}")
            lines.append(f"      <- {i.get('subject', '')} / {i.get('sender', '')}")
        lines.append("")

    lines += [f"메일 {len(items)}통", "-" * 68]
    for i in items:
        lines.append(
            f"{BADGE.get(i.get('priority'), '')} [{i.get('category', '기타')}] "
            f"{i.get('subject', '')}"
        )
        lines.append(f"      보낸사람: {i.get('sender', '')}")
        lines.append(f"      {i.get('summary', '')}")
        lines.append("")

    if usage := result.get("_usage"):
        lines.append(
            f"(토큰: 입력 {usage['input_tokens']:,} / 출력 {usage['output_tokens']:,})"
        )
    return "\n".join(lines)


def save_json(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
