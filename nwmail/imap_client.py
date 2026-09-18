"""네이버웍스 메일 IMAP 클라이언트 (Developer Console 앱 없이 쓰는 경로).

관리자가 Admin > 보안 > 서비스 권한 에서 IMAP/SMTP 를 허용해두면,
본인 계정 ID 와 외부 앱 비밀번호만으로 메일을 읽을 수 있다. API 앱 등록이 불필요하다.

서버: imap.worksmobile.com:993 (SSL) — 실측 확인됨

메일은 UID 로 다룬다. 메일함 안 순번(sequence number)은 메일이 지워지면 당겨지므로,
순번을 저장해두고 증분 수집하면 새 메일을 '이미 받은 메일'로 착각해 건너뛸 수 있다.
모든 접속은 읽기 전용(EXAMINE)이라 읽음 상태를 바꾸지 않는다.
"""
from __future__ import annotations

import base64
import email
import email.utils
import imaplib
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import Message

from .client import Mail, _html_to_text

IMAP_HOST = "imap.worksmobile.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.worksmobile.com"
SMTP_PORT = 587

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# 과거 이력을 찾을 때 제외할 메일함
SKIP_FLAGS = {"\\noselect", "\\trash", "\\junk", "\\drafts"}
SKIP_NAMES = ("휴지통", "스팸", "임시보관", "임시 보관", "trash", "spam", "junk",
              "draft", "deleted")


class ImapError(RuntimeError):
    pass


def imap_utf7_decode(name: str) -> str:
    """IMAP modified UTF-7 (RFC 3501) 메일함 이름을 사람이 읽는 문자열로.

    한글 메일함은 '&vPSwuMS4-' 같은 형태로 오기 때문에, 이를 디코딩하지 않으면
    '보낸메일함' 을 이름으로 찾을 수 없다.
    """
    if "&" not in name:
        return name
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            end = len(name)
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")                       # '&-' 는 리터럴 '&'
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])       # 디코딩 실패 시 원문 유지
        i = end + 1
    return "".join(out)


def imap_utf7_encode(name: str) -> str:
    """사람이 읽는 이름을 IMAP modified UTF-7 로. (SELECT 용)"""
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode("ascii")
                       .rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def quote_arg(value: str) -> str:
    """IMAP 명령 인자를 따옴표 문자열로. (공백·특수문자가 있어도 안전)"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(d: date) -> str:
    """SINCE 검색용 날짜 (01-Sep-2026). strftime('%b') 는 로케일을 타므로 쓰지 않는다."""
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def build_or_query(criteria: list[str]) -> str:
    """검색 조건들을 OR 로 묶는다. IMAP 의 OR 는 인자 2개짜리라 앞에 겹쳐 쌓는다.

    ['A', 'B', 'C'] -> 'OR OR A B C'  (= (A or B) or C)
    """
    if not criteria:
        raise ValueError("검색 조건이 비어 있습니다.")
    return " ".join(["OR"] * (len(criteria) - 1) + criteria)


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _extract_body(msg: Message) -> str:
    """multipart 를 훑어 text/plain 우선, 없으면 text/html 을 텍스트로 변환."""
    plain, html = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    body = plain or _html_to_text(html)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _count_attachments(msg: Message) -> int:
    return sum(
        1 for p in msg.walk()
        if "attachment" in str(p.get("Content-Disposition", "")).lower()
    )


def _uids(typ: str, data) -> list[str]:
    if typ != "OK" or not data or not data[0]:
        return []
    return [u.decode() if isinstance(u, bytes) else str(u) for u in data[0].split()]


class NaverWorksImap:
    """API 클라이언트와 같은 Mail 객체를 돌려주므로 요약 레이어를 그대로 쓴다."""

    def __init__(self, user: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT):
        self.user, self.password = user, password
        self.host, self.port = host, port
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None
        self._entries: list[dict] | None = None

    def __enter__(self) -> NaverWorksImap:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
            self._conn.login(self.user, self.password)
        except imaplib.IMAP4.error as e:
            raise ImapError(
                f"IMAP 로그인 실패: {e}\n"
                "  - 계정 비밀번호가 아니라 '외부 앱 비밀번호' 여야 합니다. "
                "네이버웍스는 v3.6 부터 이를 의무화했습니다.\n"
                "    발급: PC웹 로그인 > 우측 상단 프로필 > 보안 > "
                "외부 앱 비밀번호 생성하기 > 새 비밀번호 생성\n"
                "  - 관리자가 Admin > 보안 > 서비스 권한 에서 IMAP/SMTP 를 허용했는지도 "
                "확인하세요 (반영 5~10분)."
            ) from e
        except Exception as e:
            raise ImapError(f"IMAP 접속 실패 ({self.host}:{self.port}): {e}") from e

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None
            self._selected = None

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if not self._conn:
            raise ImapError("connect() 를 먼저 호출하세요.")
        return self._conn

    # ── 메일함 ───────────────────────────────────────────────────────────
    def _select(self, folder: str) -> None:
        """읽기 전용으로 메일함을 연다. 이미 열려 있으면 다시 열지 않는다."""
        if self._selected == folder:
            return
        arg = folder if folder.upper() == "INBOX" else quote_arg(folder)
        typ, data = self.conn.select(arg, readonly=True)
        if typ != "OK":
            raise ImapError(f"메일함 선택 실패: {imap_utf7_decode(folder)} ({data})")
        self._selected = folder

    def folder_entries(self) -> list[dict]:
        """[{wire: 서버용 이름, name: 한글 이름, flags: {'\\sent', ...}}]"""
        if self._entries is not None:
            return self._entries
        typ, data = self.conn.list()
        if typ != "OK":
            raise ImapError(f"메일함 목록 조회 실패: {typ}")
        out = []
        for raw in data:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            flags_m = re.match(r"\s*\(([^)]*)\)", line)
            flags = {f.lower() for f in (flags_m.group(1).split() if flags_m else [])}
            if m := re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line):
                wire = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
            else:
                wire = line.rsplit(" ", 1)[-1].strip()       # 따옴표 없는 응답 대비
            out.append({"wire": wire, "name": imap_utf7_decode(wire), "flags": flags})
        self._entries = out
        return out

    def folders(self, decoded: bool = True) -> list[str]:
        """메일함 목록. 한글 이름은 IMAP modified UTF-7 로 오므로 디코딩한다."""
        return [e["name"] if decoded else e["wire"] for e in self.folder_entries()]

    def find_folder(self, *keywords: str) -> str | None:
        """이름에 keyword 가 들어간 메일함의 '서버가 이해하는' 이름을 돌려준다."""
        for e in self.folder_entries():
            if any(k.lower() in e["name"].lower() for k in keywords):
                return e["wire"]
        return None

    def sent_folder(self) -> str | None:
        for e in self.folder_entries():
            if "\\sent" in e["flags"]:
                return e["wire"]
        return self.find_folder("보낸", "sent", "送信")

    def context_folders(self) -> list[str]:
        """과거 이력을 찾을 메일함. 휴지통·스팸·임시보관함은 뺀다. 받은/보낸메일함이 먼저."""
        sent = self.sent_folder()
        picked = []
        for e in self.folder_entries():
            if e["flags"] & SKIP_FLAGS:
                continue
            if any(s in e["name"].lower() for s in SKIP_NAMES):
                continue
            picked.append(e["wire"])
        head = [f for f in ("INBOX", sent) if f and f in picked]
        return head + [f for f in picked if f not in head]

    def unread_count(self, folder: str = "INBOX") -> int:
        self._select(folder)
        typ, data = self.conn.uid("SEARCH", "UNSEEN")
        return len(_uids(typ, data))

    # ── 메일 가져오기 ────────────────────────────────────────────────────
    def fetch(self, folder: str, uid: str) -> Mail | None:
        """UID 로 메일 한 통을 본문까지 가져온다."""
        self._select(folder)
        typ, data = self.conn.uid("FETCH", str(uid), "(RFC822 FLAGS)")
        if typ != "OK" or not data:
            return None
        raw = next((p[1] for p in data if isinstance(p, tuple) and len(p) > 1), None)
        if raw is None:
            return None
        flags_blob = b" ".join(
            p if isinstance(p, bytes) else p[0] for p in data if p
        )
        is_unread = b"\\Seen" not in flags_blob

        msg = email.message_from_bytes(raw)
        name, addr = email.utils.parseaddr(_decode(msg.get("From")))
        received = ""
        if raw_date := msg.get("Date"):
            try:
                received = email.utils.parsedate_to_datetime(raw_date).isoformat()
            except Exception:
                received = raw_date

        return Mail(
            mail_id=str(uid),
            subject=_decode(msg.get("Subject")) or "(제목 없음)",
            from_name=name,
            from_email=addr,
            received_time=received,
            status="Unread" if is_unread else "Read",
            attach_count=_count_attachments(msg),
            body=_extract_body(msg),
            to=[a for _, a in email.utils.getaddresses(msg.get_all("To") or []) if a],
            cc=[a for _, a in email.utils.getaddresses(msg.get_all("Cc") or []) if a],
            message_id=(msg.get("Message-ID") or "").strip(),
            in_reply_to=(msg.get("In-Reply-To") or "").strip(),
            references=(msg.get("References") or "").split(),
            list_unsubscribe=bool(msg.get("List-Unsubscribe")),
            folder=folder,
        )

    def list_mails(
        self, limit: int = 15, unread_only: bool = False, folder: str = "INBOX"
    ) -> list[Mail]:
        """최신 메일부터 limit 통을 본문까지 채워서 돌려준다."""
        self._select(folder)
        typ, data = self.conn.uid("SEARCH", "UNSEEN" if unread_only else "ALL")
        uids = _uids(typ, data)
        return [m for uid in reversed(uids[-limit:]) if (m := self.fetch(folder, uid))]

    def search(self, query: str, limit: int = 15, folder: str = "INBOX") -> list[Mail]:
        """제목 또는 본문에 query 가 들어간 메일. 한글은 UTF-8 리터럴로 보낸다."""
        self._select(folder)
        try:
            if query.isascii():
                typ, data = self.conn.uid("SEARCH", "TEXT", quote_arg(query))
            else:
                self.conn.literal = query.encode("utf-8")
                typ, data = self.conn.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
        except imaplib.IMAP4.error:
            return []
        uids = _uids(typ, data)
        return [m for uid in reversed(uids[-limit:]) if (m := self.fetch(folder, uid))]

    # ── 기간 기반 수집 (대시보드용) ──────────────────────────────────────
    def fetch_since(self, since: date, folder: str = "INBOX",
                    skip_uids: set[str] | None = None) -> list[Mail]:
        """since 날짜 이후 메일. skip_uids 에 있는 UID 는 건너뛴다(증분 수집)."""
        self._select(folder)
        typ, data = self.conn.uid("SEARCH", "SINCE", imap_date(since))
        if typ != "OK":
            raise ImapError(f"기간 검색 실패({imap_date(since)}): {typ}")
        skip = skip_uids or set()
        return [m for uid in _uids(typ, data)
                if uid not in skip and (m := self.fetch(folder, uid))]

    # ── 과거 이력 검색 (nwmail.context 가 사용) ──────────────────────────
    def search_header(self, folder: str, pairs: list[tuple[str, str]],
                      chunk: int = 8) -> list[str]:
        """헤더 값으로 메일을 찾는다. pairs = [('Message-ID', '<..>'), ('References', '<..>')]

        IMAP 헤더 검색은 부분 일치라서 References 에 뿌리 ID 가 들어 있는 답장을 모두
        찾을 수 있다. 조건이 많으면 명령이 길어지므로 chunk 개씩 OR 로 묶어 보낸다.
        """
        pairs = [(n, v) for n, v in pairs if v and v.isascii()]
        if not pairs:
            return []
        self._select(folder)
        found: list[str] = []
        for i in range(0, len(pairs), chunk):
            query = build_or_query(
                [f"HEADER {name} {quote_arg(value)}" for name, value in pairs[i:i + chunk]])
            typ, data = self.conn.uid("SEARCH", query)
            if typ != "OK":
                raise ImapError(f"헤더 검색 실패: {typ} {data}")
            found += [u for u in _uids(typ, data) if u not in found]
        return found

    def search_subject(self, folder: str, subject: str, since_days: int) -> list[str]:
        """제목에 subject 가 들어간 최근 since_days 일 메일의 UID."""
        self._select(folder)
        since = imap_date(date.today() - timedelta(days=since_days))
        if subject.isascii():
            typ, data = self.conn.uid("SEARCH", "SINCE", since, "SUBJECT", quote_arg(subject))
        else:
            self.conn.literal = subject.encode("utf-8")
            typ, data = self.conn.uid("SEARCH", "CHARSET", "UTF-8", "SINCE", since, "SUBJECT")
        return _uids(typ, data)
