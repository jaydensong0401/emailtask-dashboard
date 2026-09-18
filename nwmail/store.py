"""로컬 메일 저장소 (SQLite).

매번 IMAP 을 다시 훑지 않기 위한 캐시이자, 대시보드가 유지해야 하는 상태
(차단 이력, 신규 항목 판별)의 보관소.

보관 범위
  - 받은메일함의 최근 N일(기본 7일) 메일
  - 그 메일이 속한 대화의 과거 메일과 내가 보낸 답장 (기간 제한 없음)
대시보드·추출은 '최근 N일 안에 받은 메일이 있는 대화'만 다루되, 대화 전체를 본다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .client import Mail
from .config import ROOT

DB_PATH = ROOT / "mail.db"
KST = timezone(timedelta(hours=9))     # 한국은 서머타임이 없어 고정 오프셋으로 충분

# 2: 메일 키를 '메일함:UID' 로 변경(예전엔 메일함 안 순번), 내가 보낸 메일 본문 저장,
#    시각을 UTC 로 통일. 예전 형식 데이터는 sync.py 가 자동으로 다시 수집한다.
SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    uid              TEXT PRIMARY KEY,      -- '메일함:UID'
    folder           TEXT NOT NULL DEFAULT 'INBOX',
    message_id       TEXT,
    in_reply_to      TEXT,
    refs             TEXT,
    thread_key       TEXT NOT NULL,
    subject          TEXT,
    from_name        TEXT,
    from_email       TEXT,
    to_addrs         TEXT,
    cc_addrs         TEXT,
    sent_at          TEXT,                  -- UTC ISO
    body             TEXT,
    attach_count     INTEGER DEFAULT 0,
    is_direct        INTEGER DEFAULT 0,
    is_unread        INTEGER DEFAULT 0,
    is_mine          INTEGER DEFAULT 0,
    list_unsubscribe INTEGER DEFAULT 0,
    blocked_by       INTEGER,
    fetched_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_mails_thread ON mails(thread_key);
CREATE INDEX IF NOT EXISTS ix_mails_sent   ON mails(sent_at);
CREATE INDEX IF NOT EXISTS ix_mails_block  ON mails(blocked_by);
CREATE INDEX IF NOT EXISTS ix_mails_msgid  ON mails(message_id);

CREATE TABLE IF NOT EXISTS thread_alias (
    alias      TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alias_key ON thread_alias(thread_key);

CREATE TABLE IF NOT EXISTS filters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- subject | sender | domain
    pattern     TEXT NOT NULL,
    note        TEXT,
    enabled     INTEGER DEFAULT 1,
    hits        INTEGER DEFAULT 0,
    created_at  TEXT,
    category    TEXT DEFAULT '기타',     -- 광고·홍보 | 뉴스레터·구독 | 행사·안내 | 자동 알림 | 기타
    UNIQUE(kind, pattern)
);

-- 차단하지 않을 도메인 (코드에 고정한 회사·거래처 도메인 외에 사용자가 더한 것)
CREATE TABLE IF NOT EXISTS protected_domains (
    domain      TEXT PRIMARY KEY,
    note        TEXT DEFAULT '',
    created_at  TEXT
);

-- 차단 후보에서 뺀 것 (다시 제안하지 않음)
CREATE TABLE IF NOT EXISTS filter_dismissed (
    kind        TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    created_at  TEXT,
    PRIMARY KEY (kind, pattern)
);

CREATE TABLE IF NOT EXISTS extractions (
    thread_key   TEXT PRIMARY KEY,
    project      TEXT,
    payload      TEXT,
    source_hash  TEXT,
    backend      TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS task_state (
    task_id  TEXT PRIMARY KEY,
    done     INTEGER DEFAULT 0,
    done_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    new_mails  INTEGER DEFAULT 0,
    kind       TEXT DEFAULT 'sync'          -- sync | extract
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 제목 앞머리의 회신/전달 표식 (스레드 묶기용).
# 네이버웍스는 "RE: " 뿐 아니라 "[RE]" 형태로도 붙이므로 둘 다 걷어내야
# "[RE][누리마케팅] VX" 와 "[누리마케팅] VX" 가 같은 스레드로 묶인다.
REPLY_WORD = r"re|rre|fw|fwd|답장|회신|전달"
REPLY_PREFIX = re.compile(
    rf"^\s*(?:"
    rf"(?:{REPLY_WORD})\s*[:：]\s*"       # RE: / 답장:
    rf"|\[\s*(?:{REPLY_WORD})\s*\]\s*"    # [RE] / [답장]
    rf"|\({REPLY_WORD}\)\s*"              # (RE)
    rf")+",
    re.I,
)


def normalize_subject(subject: str) -> str:
    """RE:/[RE]/FW: 등을 걷어낸 제목. 스레드 묶기의 최후 수단."""
    prev = None
    s = subject or ""
    while prev != s:
        prev = s
        s = REPLY_PREFIX.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip().lower()


def thread_aliases(mail: Mail) -> list[str]:
    """이 메일이 속할 수 있는 스레드 식별자 후보를 우선순위대로.

    루트 메일은 자기 Message-ID 가 곧 스레드 뿌리다(답장들이 이걸 참조한다).
    제목 별칭은 답장 헤더(References/In-Reply-To)가 없는 메일에만 붙인다. 헤더가 있는
    메일까지 제목으로 묶으면 "RE: 문의" 처럼 흔한 제목의 서로 다른 대화가 합쳐진다.
    """
    out: list[str] = []
    for cand in (*mail.references, mail.in_reply_to, mail.message_id):
        if (c := (cand or "").strip()) and c not in out:
            out.append(c)
    linked = bool(mail.references) or bool((mail.in_reply_to or "").strip())
    if not linked and (subj := normalize_subject(mail.subject)):
        out.append(f"subj:{subj}")
    return out or [f"uid:{mail.mail_id}"]


def thread_key_for(mail: Mail) -> str:
    """DB 없이 쓰는 단순 키. 저장 시에는 Store 가 별칭으로 병합까지 처리한다."""
    return thread_aliases(mail)[0]


def to_utc_iso(value: str | None) -> str:
    """시각을 UTC ISO 로. 발신 서버마다 +09:00, -04:00 처럼 제각각이라 문자열 비교가 틀린다."""
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return str(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat()


MAX_READ_DAYS = 30      # 오래 비었어도 이 이상은 한 번에 읽지 않는다


def weekday_window_days(today: date) -> int:
    """직전 평일까지 거슬러 가는 일수. 월 3(금), 일 2(금), 나머지 1(어제)."""
    return {0: 3, 6: 2}.get(today.weekday(), 1)


def window_start(days: int, now: datetime | None = None) -> datetime:
    """최근 N일 창의 시작 (한국 날짜 기준 N일 전 0시). IMAP SINCE 와 같은 기준이다."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(KST).date() - timedelta(days=days)
    return datetime(day.year, day.month, day.day, tzinfo=KST).astimezone(timezone.utc)


@dataclass
class SyncResult:
    fetched: int
    inserted: int
    skipped: int
    blocked: int
    thread_keys: set[str] = field(default_factory=set)


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """예전 스키마로 만들어진 DB 에 새 컬럼을 덧붙인다."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(mails)")}
        for col, ddl in (("in_reply_to", "TEXT"), ("refs", "TEXT"),
                         ("is_mine", "INTEGER DEFAULT 0")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE mails ADD COLUMN {col} {ddl}")
        if "kind" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}:
            self.conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'sync'")
        if "category" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(filters)")}:
            self.conn.execute("ALTER TABLE filters ADD COLUMN category TEXT DEFAULT '기타'")
        if not self.conn.execute("SELECT 1 FROM mails LIMIT 1").fetchone():
            self._set_meta("schema_version", SCHEMA_VERSION)     # 빈 DB 는 바로 최신

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key)"
                          " DO UPDATE SET value = excluded.value", (key, value))

    def needs_rebuild(self) -> bool:
        """예전 형식으로 저장된 메일이 남아 있어 다시 수집해야 하는가."""
        return self._meta("schema_version") != SCHEMA_VERSION

    # ── 스레드 해석 ──────────────────────────────────────────────────────
    def resolve_thread(self, mail: Mail, hint: str | None = None) -> str:
        """별칭 표를 보고 스레드를 결정한다.

        메일이 도착 순서와 무관하게 같은 건으로 묶이도록, 후보 식별자 중 하나라도
        기존 스레드에 등록돼 있으면 그 스레드에 합류시킨다. 서로 다른 스레드가
        하나로 이어지는 경우에는 기존 스레드끼리 병합한다. hint 가 있으면 그 스레드로
        붙인다 (제목으로 찾은 과거 메일처럼 헤더로는 이어지지 않는 경우).
        """
        c = self.conn
        aliases = thread_aliases(mail)
        rows = c.execute(
            f"SELECT DISTINCT thread_key FROM thread_alias WHERE alias IN "
            f"({','.join('?' * len(aliases))})", aliases
        ).fetchall()
        found = [r["thread_key"] for r in rows]
        if hint:
            found = [hint] + [k for k in found if k != hint]

        key = found[0] if found else aliases[0]
        for stale in found[1:]:                       # 갈라져 있던 스레드 병합
            c.execute("UPDATE mails SET thread_key = ? WHERE thread_key = ?",
                      (key, stale))
            c.execute("UPDATE thread_alias SET thread_key = ? WHERE thread_key = ?",
                      (key, stale))
        for a in aliases:
            c.execute("INSERT OR IGNORE INTO thread_alias (alias, thread_key)"
                      " VALUES (?,?)", (a, key))
        return key

    # ── 수집 ─────────────────────────────────────────────────────────────
    def known_uids(self, folder: str = "INBOX") -> set[str]:
        """해당 메일함에서 이미 받은 IMAP UID."""
        prefix = f"{folder}:"
        rows = self.conn.execute(
            "SELECT uid FROM mails WHERE folder = ?", (folder,)).fetchall()
        return {r["uid"][len(prefix):] for r in rows if r["uid"].startswith(prefix)}

    def known_keys(self) -> set[str]:
        return {r["uid"] for r in self.conn.execute("SELECT uid FROM mails")}

    def known_message_ids(self) -> set[str]:
        return {r["message_id"] for r in self.conn.execute(
            "SELECT message_id FROM mails WHERE message_id IS NOT NULL AND message_id != ''")}

    def save_mails(self, mails: list[Mail], me: str, folder: str = "INBOX",
                   matcher=None) -> SyncResult:
        """메일을 저장한다. matcher(mail) 가 필터 id 를 돌려주면 차단으로 표시.

        같은 메일이 여러 메일함에 있으면(나를 참조에 넣어 보낸 경우 등) 한 번만 저장한다.
        내가 보낸 메일은 차단하지 않는다.
        """
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        me_l = (me or "").strip().lower()
        inserted = blocked = 0
        keys: set[str] = set()
        with self.tx() as c:
            for m in mails:
                fold = m.folder or folder
                key = f"{fold}:{m.mail_id}"
                if c.execute("SELECT 1 FROM mails WHERE uid = ?", (key,)).fetchone():
                    continue
                mid = (m.message_id or "").strip()
                if mid and c.execute("SELECT 1 FROM mails WHERE message_id = ?",
                                     (mid,)).fetchone():
                    continue
                mine = bool(me_l) and (m.from_email or "").strip().lower() == me_l
                blocked_by = matcher(m) if (matcher and not mine) else None
                tkey = self.resolve_thread(m, hint=m.thread_hint or None)
                c.execute(
                    """INSERT INTO mails
                       (uid, folder, message_id, in_reply_to, refs, thread_key,
                        subject, from_name, from_email, to_addrs, cc_addrs, sent_at, body,
                        attach_count, is_direct, is_unread, is_mine, list_unsubscribe,
                        blocked_by, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, fold, mid, m.in_reply_to,
                     json.dumps(m.references, ensure_ascii=False), tkey, m.subject,
                     m.from_name, m.from_email, json.dumps(m.to, ensure_ascii=False),
                     json.dumps(m.cc, ensure_ascii=False), to_utc_iso(m.received_time),
                     m.body, m.attach_count, int(m.is_direct_to(me) and not mine),
                     int(m.is_unread and not mine), int(mine), int(m.list_unsubscribe),
                     blocked_by, now),
                )
                inserted += 1
                keys.add(tkey)
                if blocked_by:
                    blocked += 1
                    c.execute("UPDATE filters SET hits = hits + 1 WHERE id = ?",
                              (blocked_by,))
        # 병합으로 키가 바뀌었을 수 있으니 저장 후 기준으로 다시 모은다
        final = {self._current_key(k) for k in keys}
        return SyncResult(len(mails), inserted, len(mails) - inserted, blocked, final)

    def _current_key(self, key: str) -> str:
        row = self.conn.execute("SELECT thread_key FROM thread_alias WHERE alias = ?",
                                (key,)).fetchone()
        return row["thread_key"] if row else key

    def start_run(self, new_mails: int, kind: str = "sync",
                  started_at: datetime | None = None) -> int:
        """실행 기록. kind 는 sync(메일 받기) / extract(업무 추출)."""
        when = (started_at or datetime.now(timezone.utc)).isoformat()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, new_mails, kind) VALUES (?,?,?)",
                (when, new_mails, kind),
            )
        return cur.lastrowid

    def last_run_at(self, kind: str = "sync", offset: int = 1) -> str | None:
        """kind 실행 기록 중 최근에서 offset 번째 (0 = 가장 최근, 1 = 그 직전)."""
        row = self.conn.execute(
            "SELECT started_at FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (kind, offset)).fetchone()
        return row["started_at"] if row else None

    def last_extracted_at(self) -> str | None:
        """업무가 마지막으로 정리된 시각. 일부 대화가 실패하면 실행 기록은 남지 않으므로 결과로 본다."""
        row = self.conn.execute("SELECT MAX(extracted_at) FROM extractions").fetchone()
        return row[0] if row else None

    def read_window_days(self, kind: str, now: datetime | None = None) -> int:
        """이번 실행에서 읽을 기간(일). 직전 평일 0시부터, 실행이 비었으면 그 날부터.

        평일 규칙: 화~금·토는 D-1, 월요일은 D-3(금요일부터), 일요일은 D-2(금요일부터).
        휴가·PC 꺼짐으로 마지막 kind 실행이 그보다 오래됐으면 그 날짜부터 읽어 빠뜨리지 않는다.
        """
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(KST).date()
        days = weekday_window_days(today)
        if last := self.last_run_at(kind, offset=0):
            try:
                gap = (today - datetime.fromisoformat(last).astimezone(KST).date()).days
            except ValueError:
                gap = 0
            days = max(days, min(gap, MAX_READ_DAYS))
        return days

    def header_coverage(self) -> float:
        """References/In-Reply-To 가 저장된 메일의 비율."""
        total = self.conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        if not total:
            return 1.0
        have = self.conn.execute(
            "SELECT COUNT(*) FROM mails WHERE (refs IS NOT NULL AND refs != '[]')"
            " OR (in_reply_to IS NOT NULL AND in_reply_to != '')"
        ).fetchone()[0]
        return have / total

    def rethread(self, force: bool = False) -> dict:
        """저장된 메일 전체를 현재 규칙으로 다시 묶는다.

        헤더(References)가 저장되기 전에 수집된 메일에 대해 이걸 돌리면 제목만으로
        묶게 되어 오히려 스레드가 쪼개진다. 그래서 헤더 보유율이 낮으면 거부한다.
        """
        cov = self.header_coverage()
        if cov < 0.3 and not force:
            return {"skipped": True, "coverage": round(cov, 2),
                    "reason": "헤더 정보가 부족해 재묶기를 건너뜁니다. "
                              "`python sync.py --rebuild` 로 다시 수집하세요."}
        rows = self.conn.execute("SELECT * FROM mails ORDER BY sent_at").fetchall()
        before = len({r["thread_key"] for r in rows})
        with self.tx() as c:
            c.execute("DELETE FROM thread_alias")
            for r in rows:
                try:
                    refs = json.loads(r["refs"]) if r["refs"] else []
                except (json.JSONDecodeError, TypeError):
                    refs = []
                m = Mail(mail_id=r["uid"], subject=r["subject"] or "",
                         from_name=r["from_name"] or "",
                         from_email=r["from_email"] or "",
                         received_time=r["sent_at"] or "",
                         message_id=r["message_id"] or "",
                         in_reply_to=r["in_reply_to"] or "",
                         references=refs)
                c.execute("UPDATE mails SET thread_key = ? WHERE uid = ?",
                          (self.resolve_thread(m), r["uid"]))
        after = self.conn.execute(
            "SELECT COUNT(DISTINCT thread_key) FROM mails").fetchone()[0]
        return {"skipped": False, "before": before, "after": after,
                "merged": before - after, "coverage": round(cov, 2)}

    def reset_mails(self) -> int:
        """메일과 스레드만 비운다. 필터 규칙은 보존한다."""
        n = self.conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        with self.tx() as c:
            c.execute("DELETE FROM mails")
            c.execute("DELETE FROM thread_alias")
            c.execute("DELETE FROM extractions")
            self._set_meta("schema_version", SCHEMA_VERSION)
        return n

    # ── 조회 ─────────────────────────────────────────────────────────────
    def threads(self, window_days: int | None = 7, now: datetime | None = None,
                include_blocked: bool = False, keys: list[str] | None = None) -> list[dict]:
        """최근 N일 안에 받은 메일이 있는 대화를, 과거 메일까지 포함해 돌려준다.

        window_days=None 이면 기간과 무관하게 모든 대화.
        keys 를 주면 기간과 무관하게 그 대화들만 (기간은 최근 메일 수 · 답장 판단에만 쓴다).
        각 대화에서 계산하는 값:
          replied_by_me     마지막으로 받은 메일 뒤에 내가 보낸 메일이 있다 (상대 차례)
          awaiting_my_reply 최근 N일 안에 나에게 직접 온 메일이 있고, 그 뒤로 내가 답하지 않았다
        """
        cutoff = window_start(window_days, now).isoformat() if window_days is not None else None
        block_sql = "" if include_blocked else " AND blocked_by IS NULL"
        if keys is not None:
            keys = list(dict.fromkeys(keys))
        elif cutoff:
            keys = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT thread_key FROM mails WHERE is_mine = 0 AND sent_at >= ?"
                + block_sql, (cutoff,))]
        else:
            keys = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT thread_key FROM mails WHERE 1 = 1" + block_sql)]

        out = []
        for key in keys:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM mails WHERE thread_key = ?" + block_sql
                + " ORDER BY sent_at, uid", (key,))]
            if not rows:
                continue
            recv = [r for r in rows if not r["is_mine"]]
            mine = [r for r in rows if r["is_mine"]]
            last_recv = max((r["sent_at"] or "" for r in recv), default=None)
            my_last = max((r["sent_at"] or "" for r in mine), default=None)
            last_direct = max((r["sent_at"] or "" for r in recv if r["is_direct"]),
                              default=None)
            recent = [r for r in rows if cutoff is None or (r["sent_at"] or "") >= cutoff]
            out.append({
                "thread_key": key,
                "subject": (recv[-1] if recv else rows[-1])["subject"],
                "mail_count": len(rows),
                "recent_count": len(recent),
                "last_at": rows[-1]["sent_at"],
                "last_received_at": last_recv,
                "last_direct_at": last_direct,
                "my_last_at": my_last,
                "is_direct": last_direct is not None,
                "is_unread": any(r["is_unread"] for r in recv),
                "replied_by_me": my_last is not None and (last_recv is None or my_last > last_recv),
                "awaiting_my_reply": (last_direct is not None
                                      and (cutoff is None or last_direct >= cutoff)
                                      and (my_last is None or last_direct > my_last)),
                "last_fetched": max(r["fetched_at"] or "" for r in rows),
                "mails": rows,
            })
        out.sort(key=lambda t: t["last_at"] or "", reverse=True)
        return out

    def blocked_mails(self, window_days: int | None = 7,
                      now: datetime | None = None) -> list[dict]:
        sql = """SELECT m.*, f.pattern AS filter_pattern, f.kind AS filter_kind,
                        f.category AS filter_category, f.enabled AS filter_enabled
                 FROM mails m LEFT JOIN filters f ON f.id = m.blocked_by
                 WHERE m.blocked_by IS NOT NULL"""
        args: tuple = ()
        if window_days is not None:
            sql += " AND m.sent_at >= ?"
            args = (window_start(window_days, now).isoformat(),)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY m.sent_at DESC", args)]

    def stats(self, window_days: int | None = 7, now: datetime | None = None) -> dict:
        cutoff = window_start(window_days, now).isoformat() if window_days is not None else ""

        def q(where: str) -> int:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM mails WHERE sent_at >= ? AND is_mine = 0 AND {where}",
                (cutoff,)).fetchone()[0]

        threads = self.threads(window_days, now)
        return {
            "total": self.conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0],
            "window": q("blocked_by IS NULL"),
            "blocked": q("blocked_by IS NOT NULL"),
            "direct": q("is_direct = 1 AND blocked_by IS NULL"),
            "unread": q("is_unread = 1 AND blocked_by IS NULL"),
            "threads": len(threads),
            "history": sum(t["mail_count"] - t["recent_count"] for t in threads),
            "mine": sum(1 for t in threads for m in t["mails"] if m["is_mine"]),
        }
