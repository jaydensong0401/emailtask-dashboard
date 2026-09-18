"""네이버웍스 Mail API 클라이언트.

문서: https://developers.worksmobile.com/kr/docs/mail
Base URL: https://www.worksapis.com/v1.0
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests
from bs4 import BeautifulSoup

from .config import API_BASE

INBOX_FOLDER_ID = 0   # 받은메일함 (문서 응답 예시 기준 기본값)
ALL_FOLDER_ID = -1    # 전체메일함 (search API 기본값)
INBOX_NAMES = {"받은메일함", "inbox", "INBOX", "受信トレイ"}


class MailApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body
        super().__init__(f"HTTP {status}: {body[:400]}")


@dataclass
class Mail:
    mail_id: str
    subject: str
    from_name: str
    from_email: str
    received_time: str
    status: str = ""
    folder_id: int | None = None
    attach_count: int = 0
    body: str = ""
    to: list[str] = field(default_factory=list)

    # -- 스레드/필터용 헤더 (IMAP 경로에서 채워진다) --
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    list_unsubscribe: bool = False

    # -- 로컬 저장소용 --
    folder: str = ""          # 이 메일을 받아온 메일함 (IMAP 이름)
    thread_hint: str = ""     # 제목으로 찾은 과거 메일을 붙일 스레드 (헤더로 못 묶는 경우)

    @property
    def is_unread(self) -> bool:
        return str(self.status).lower() in {"unread", "0"}

    def is_direct_to(self, me: str) -> bool:
        """To/Cc 에 내 주소가 있으면 직접 수신. 없으면 그룹메일로 본다."""
        me = me.strip().lower()
        return any(a.strip().lower() == me for a in (*self.to, *self.cc))


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    if "<" not in html:
        return re.sub(r"\n{3,}", "\n\n", html).strip()
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


def _addr(obj: Any) -> tuple[str, str]:
    if isinstance(obj, dict):
        return str(obj.get("name") or ""), str(obj.get("email") or "")
    return "", str(obj or "")


class NaverWorksMail:
    def __init__(self, access_token: str, user_id: str = "me", timeout: int = 30):
        self.user_id = user_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        )

    # ── 저수준 ───────────────────────────────────────────────────────────
    def _get(self, path: str, **params) -> dict:
        url = f"{API_BASE}/users/{self.user_id}{path}"
        params = {k: v for k, v in params.items() if v is not None}
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise MailApiError(r.status_code, r.text)
        return r.json()

    # ── 고수준 ───────────────────────────────────────────────────────────
    def unread_count(self) -> int:
        """GET /users/{userId}/mail/unread-count"""
        data = self._get("/mail/unread-count")
        return int(data.get("count", data.get("unreadCount", 0)))

    def folders(self) -> list[dict]:
        """GET /users/{userId}/mail/mailfolders"""
        return self._get("/mail/mailfolders").get("mailFolders", [])

    def inbox_folder_id(self) -> int:
        """받은메일함 folderId 를 이름으로 찾는다. 못 찾으면 문서 기본값(0)."""
        try:
            for f in self.folders():
                if str(f.get("folderName", "")).strip() in INBOX_NAMES:
                    return int(f["folderId"])
        except MailApiError:
            pass
        return INBOX_FOLDER_ID

    def list_mails(
        self,
        folder_id: int = INBOX_FOLDER_ID,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[Mail]:
        """GET /users/{userId}/mail/mailfolders/{folderId}/children (커서 페이지네이션)."""
        out: list[Mail] = []
        cursor = None
        while len(out) < limit:
            page = self._get(
                f"/mail/mailfolders/{folder_id}/children",
                count=min(200, max(5, limit - len(out))),
                cursor=cursor,
                isUnread=str(unread_only).lower() if unread_only else None,
            )
            for m in page.get("mails", []):
                out.append(self._to_mail(m))
                if len(out) >= limit:
                    break
            cursor = (page.get("responseMetaData") or {}).get("nextCursor")
            if not cursor:
                break
        return out

    def search(self, query: str, limit: int = 20, folder_id: int = ALL_FOLDER_ID) -> list[Mail]:
        """GET /users/{userId}/mail/search"""
        page = self._get(
            "/mail/search", query=query, folderId=folder_id, count=min(500, limit)
        )
        return [self._to_mail(m) for m in page.get("mails", [])][:limit]

    def get_mail(self, mail_id: str) -> Mail:
        """GET /users/{userId}/mail/{mailId} — 본문(body) 포함 상세."""
        data = self._get(f"/mail/{mail_id}")
        raw = data.get("mail", data)
        mail = self._to_mail(raw)
        mail.body = _html_to_text(raw.get("body", ""))
        return mail

    def iter_with_bodies(self, mails: list[Mail]) -> Iterator[Mail]:
        """목록의 각 메일에 대해 상세 조회로 본문을 채워서 내보낸다."""
        for m in mails:
            try:
                yield self.get_mail(m.mail_id)
            except MailApiError as e:
                m.body = f"[본문 조회 실패: {e}]"
                yield m

    @staticmethod
    def _to_mail(m: dict) -> Mail:
        name, email = _addr(m.get("from"))
        to = [_addr(t)[1] for t in (m.get("to") or []) if _addr(t)[1]]
        return Mail(
            mail_id=str(m.get("mailId", "")),
            subject=str(m.get("subject") or "(제목 없음)"),
            from_name=name,
            from_email=email,
            received_time=str(m.get("receivedTime") or m.get("sentTime") or ""),
            status=str(m.get("status") or ""),
            folder_id=m.get("folderId"),
            attach_count=int(m.get("attachCount") or len(m.get("attachList") or [])),
            body=_html_to_text(m.get("body", "")),
            to=to,
        )
