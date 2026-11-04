"""AI 전송용 신원 별칭과 PC 안의 대응표. 전달받은 메일 외의 자료는 조회하지 않는다."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from email.utils import getaddresses
from functools import wraps
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_DB = ROOT / "identity_map.db"
IDENTITY_VERSION = "2026-10-19.1"


class IdentityMapError(RuntimeError):
    """대응표 오류. 주소·이름·본문·경로를 오류 메시지에 포함하지 않는다."""


def _mapping_guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except IdentityMapError:
            raise
        except Exception:
            raise IdentityMapError("로컬 신원 대응표를 처리하지 못해 AI 전송을 중단합니다.") from None
    return guarded

# 메일 주소는 특정 TLD 목록에 의존하지 않는다. SMTP의 일반 ASCII 주소와 mailto를 포함한다.
_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+"
_DOMAIN = r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*"
EMAIL_RE = re.compile(rf"(?<![A-Za-z0-9!#$%&'*+/=?^_`{{|}}~.\-])(?P<email>{_LOCAL}@{_DOMAIN})(?![A-Za-z0-9\-])")
_EMAIL_FULL = re.compile(rf"{_LOCAL}@{_DOMAIN}\Z")
_ALIAS_RE = re.compile(r"담당자_[0-9]{4,}(?![0-9])")
_ANGLE_NAME = re.compile(rf"(?P<name>[^\r\n<>]{{1,100}})<\s*(?P<email>{_LOCAL}@{_DOMAIN})\s*>")
_AFTER_NAME = re.compile(rf"(?P<email>{_LOCAL}@{_DOMAIN})\s*\((?P<name>[^()\r\n]{{1,80}})\)")
_HEADER_LABEL = re.compile(r"^(?:[> \t]*(?:from|to|cc|보낸\s*사람|받는\s*사람|참조|발신자|수신자)\s*[:：]\s*)", re.I)
_TITLE = r"(?:매니저|선임|책임|대리|과장|차장|부장|이사|사원|대표|수석|팀장|실장|본부장|님|씨)"
_NAME_TITLES = re.compile(rf"(?:\s*{_TITLE})+$")
_GENERIC_NAMES = {
    "확인", "요청", "검토", "감사", "일정", "회의", "자료", "계약", "내용", "수정", "운영", "개발", "디자인",
    "홍보", "메일", "발신자", "수신자", "담당자", "관리자", "고객지원", "고객센터", "시스템", "공지", "알림",
    "support", "admin", "info", "noreply", "no-reply", "newsletter", "marketing", "sales", "service", "notification",
    "system", "team", "office", "finance", "customer support", "best regards", "regards", "thanks",
}
_DEPARTMENT = re.compile(r"(?:팀|본부|사업부|센터|그룹|연구소|사무국|\s(?:team|department|division|support|office|group|cent(?:er|re)))$", re.I)
_SUFFIXES = sorted({"님", "씨", "선생님", "담당자", "매니저", "대리", "과장", "차장", "부장", "이사", "책임", "선임", "수석",
                    "대표", "팀장", "실장", "본부장", "께서", "에게", "한테", "께", "으로", "로", "에서", "은", "는", "이",
                    "가", "을", "를", "과", "와", "의", "도", "만", "보다", "처럼", "이라고", "이라"}, key=len, reverse=True)
_NAME_SUFFIX = re.compile(r"(?:" + "|".join(map(re.escape, _SUFFIXES)) + r"){1,4}\Z")
_HANGUL = re.compile(r"[가-힣]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    alias TEXT UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_names (
    name_key TEXT NOT NULL,
    name TEXT NOT NULL,
    identity_id INTEGER NOT NULL,
    PRIMARY KEY(name_key, identity_id)
);
CREATE TABLE IF NOT EXISTS neutral_names (
    name_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    identity_id INTEGER NOT NULL UNIQUE
);
"""


def _clean_name(value: str | None) -> str:
    name = _HEADER_LABEL.sub("", str(value or "")).strip(" \t\"'<>;,:")
    name = re.sub(r"\s+", " ", name)
    if not name or len(name) > 80 or "@" in name or _ALIAS_RE.search(name):
        return ""
    if name.casefold() in _GENERIC_NAMES or _DEPARTMENT.search(name) or re.search(r"[0-9]", name):
        return ""
    # 명시된 표시 이름만 받아들인다. 이메일 아이디에서 이름을 추측하지 않는다.
    if not re.fullmatch(r"[가-힣A-Za-zÀ-ÖØ-öø-ÿ·.'()&/ _\-]+", name) and not re.fullmatch(
            r"[가-힣A-Za-zÀ-ÖØ-öø-ÿ]\*+[가-힣A-Za-zÀ-ÖØ-öø-ÿ]?", name):
        return ""
    return name


def _mail_name(value: str | None) -> str:
    name = _clean_name(value)
    if not name:
        return ""
    # 확인된 header 이름의 직함은 본문 이름과 연결한다. 회사명이 앞에 있는 경우 마지막 인명을 사용한다.
    without_title = _NAME_TITLES.sub("", name).strip()
    if re.fullmatch(r"[가-힣]{2,6}", without_title):
        return without_title
    tokens = without_title.split()
    if len(tokens) > 1 and re.fullmatch(r"[가-힣]{2,6}", tokens[-1]) and not re.fullmatch(r"[가-힣]{2,6}", tokens[0]):
        return tokens[-1]
    return _clean_name(without_title) or name


def _email(value: str) -> str:
    address = str(value or "").strip()
    if not _EMAIL_FULL.fullmatch(address):
        raise ValueError("이메일 주소 형식이 올바르지 않습니다")
    return address.casefold()


def _address_values(value) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        return [str(v) for v in decoded] if isinstance(decoded, list) else [value]
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


class IdentityMap:
    @_mapping_guard
    def __init__(self, path: Path | str | None = None):
        self.path = path if path is not None else IDENTITY_DB
        if str(self.path) != ":memory:":
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._used_aliases: set[str] = set()
        try:
            self.conn.executescript(SCHEMA)
        except Exception:
            self.conn.close()
            raise

    def __enter__(self) -> IdentityMap:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _new_identity(self, email: str | None, name: str = "") -> tuple[int, str]:
        row = self.conn.execute("INSERT INTO identities(email,display_name,created_at) VALUES (?,?,?)",
                                (email, name, datetime.now(timezone.utc).isoformat()))
        identity_id = row.lastrowid
        alias = f"담당자_{identity_id:04d}"
        self.conn.execute("UPDATE identities SET alias=? WHERE id=?", (alias, identity_id))
        return identity_id, alias

    @_mapping_guard
    def register(self, email: str, name: str = "") -> str:
        address, display = _email(email), _clean_name(name)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT * FROM identities WHERE email=?", (address,)).fetchone()
                if row:
                    identity_id, alias = row["id"], row["alias"]
                    if display and not row["display_name"]:
                        self.conn.execute("UPDATE identities SET display_name=? WHERE id=?", (display, identity_id))
                else:
                    identity_id, alias = self._new_identity(address, display)
                if display:
                    variants = {display, _NAME_TITLES.sub("", display).strip()}
                    for variant in filter(None, map(_clean_name, variants)):
                        self.conn.execute("INSERT OR IGNORE INTO identity_names VALUES (?,?,?)",
                                          (variant.casefold(), variant, identity_id))
                self.conn.execute("COMMIT")
                return alias
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    @_mapping_guard
    def register_mail(self, mail) -> None:
        def field(key, default=""):
            return mail.get(key, default) if isinstance(mail, dict) else getattr(mail, key, default)

        sender = field("from_email")
        if sender:
            for display, address in getaddresses([str(sender)]):
                if _EMAIL_FULL.fullmatch(address):
                    self.register(address, _mail_name(field("from_name") or display))
        for key in ("to", "cc", "to_addrs", "cc_addrs"):
            for display, address in getaddresses(_address_values(field(key, []))):
                if _EMAIL_FULL.fullmatch(address):
                    self.register(address, _mail_name(display))
        body = str(field("body") or "")
        for match in EMAIL_RE.finditer(body):
            self.register(match.group("email"))
        for pattern in (_ANGLE_NAME, _AFTER_NAME):
            for match in pattern.finditer(body):
                name = _mail_name(match.group("name"))
                if name:
                    self.register(match.group("email"), name)

    @_mapping_guard
    def sender_alias(self, value: str) -> str:
        """발신자 칸에서는 유효 주소의 별칭만 허용하고 표시명/기호를 노출하지 않는다."""
        aliases = []
        for _, address in getaddresses([str(value or "")]):
            if _EMAIL_FULL.fullmatch(address):
                alias = self.redact(address)
                if alias not in aliases:
                    aliases.append(alias)
        return ", ".join(aliases) if aliases else "알 수 없음"

    def _name_aliases(self) -> dict[str, str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT n.name_key, n.name, n.identity_id, i.alias FROM identity_names n "
                "JOIN identities i ON i.id=n.identity_id ORDER BY n.identity_id").fetchall()
            grouped: dict[str, list] = {}
            for row in rows:
                grouped.setdefault(row["name_key"], []).append(row)
            aliases = {}
            for key, matches in grouped.items():
                if len({row["identity_id"] for row in matches}) == 1:
                    alias = matches[0]["alias"]
                else:
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        neutral = self.conn.execute(
                            "SELECT i.alias FROM neutral_names n JOIN identities i ON i.id=n.identity_id WHERE n.name_key=?",
                            (key,)).fetchone()
                        if neutral:
                            alias = neutral["alias"]
                        else:
                            identity_id, alias = self._new_identity(None, matches[0]["name"])
                            self.conn.execute("INSERT INTO neutral_names VALUES (?,?,?)", (key, matches[0]["name"], identity_id))
                        self.conn.execute("COMMIT")
                    except Exception:
                        self.conn.execute("ROLLBACK")
                        raise
                for row in matches:
                    aliases[row["name"]] = alias
            return aliases

    @staticmethod
    def _name_boundary(text: str, start: int, end: int) -> bool:
        if start and (text[start - 1].isalnum() or text[start - 1] == "_"):
            return False
        if end == len(text) or not (text[end].isalnum() or text[end] == "_"):
            return True
        if not _HANGUL.match(text[end]):
            return False
        tail = re.match(r"[가-힣]+", text[end:]).group()
        return bool(_NAME_SUFFIX.fullmatch(tail))

    def _explicit_pair(self, label: str, email: str) -> tuple[str, str] | None:
        """이메일과 함께 명시된 이름은 동명이인 이름단독 별칭보다 우선한다."""
        address = _email(email)
        with self._lock:
            rows = self.conn.execute(
                "SELECT n.name,i.alias FROM identity_names n JOIN identities i ON i.id=n.identity_id WHERE i.email=?",
                (address,)).fetchall()
        for row in sorted(rows, key=lambda value: len(value["name"]), reverse=True):
            suffix = re.search(r"[\"']?" + re.escape(row["name"]) + rf"[\"']?(?:\s*{_TITLE})*\s*$", label)
            if suffix and (suffix.start() == 0 or not label[suffix.start() - 1].isalnum()):
                return label[:suffix.start()], row["alias"]
        return None

    @_mapping_guard
    def redact(self, text: str) -> str:
        original = str(text or "")
        emails = {match.group("email"): self.register(match.group("email")) for match in EMAIL_RE.finditer(original)}
        used = set()
        def replace_angle_pair(match):
            pair = self._explicit_pair(match.group("name"), match.group("email"))
            if pair is None:
                return match.group()
            prefix, alias = pair
            used.add(alias)
            return prefix + alias
        def replace_after_pair(match):
            pair = self._explicit_pair(match.group("name"), match.group("email"))
            if pair is None or pair[0].strip():
                return match.group()
            used.add(pair[1])
            return pair[1]
        paired = _ANGLE_NAME.sub(replace_angle_pair, original)
        paired = _AFTER_NAME.sub(replace_after_pair, paired)
        def replace_email(match):
            alias = emails[match.group("email")]
            used.add(alias)
            return alias
        redacted = EMAIL_RE.sub(replace_email, paired)
        names = self._name_aliases()
        if names:
            pattern = re.compile("|".join(map(re.escape, sorted(names, key=len, reverse=True))))
            def replace_name(match):
                if not self._name_boundary(redacted, match.start(), match.end()):
                    return match.group()
                alias = names[match.group()]
                used.add(alias)
                return alias
            redacted = pattern.sub(replace_name, redacted)
        # header '이름 <주소>' / '주소 (이름)'가 같은 별칭으로 중복될 때는 한 번만 표시한다.
        redacted = re.sub(r"(담당자_[0-9]{4,})\s*<\s*\1\s*>", r"\1", redacted)
        redacted = re.sub(r"(담당자_[0-9]{4,})\s*\(\s*\1\s*\)", r"\1", redacted)
        self._used_aliases.update(used)
        return redacted

    @_mapping_guard
    def restore(self, value):
        with self._lock:
            replacements = {row["alias"]: row["display_name"] or row["email"] or row["alias"]
                            for row in self.conn.execute("SELECT alias,display_name,email FROM identities")
                            if row["alias"] in self._used_aliases}

        def visit(item):
            if isinstance(item, str):
                return _ALIAS_RE.sub(lambda match: replacements.get(match.group(), match.group()), item)
            if isinstance(item, dict):
                return {copy.deepcopy(key): visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, tuple):
                return tuple(visit(child) for child in item)
            return copy.deepcopy(item)

        return visit(value)
