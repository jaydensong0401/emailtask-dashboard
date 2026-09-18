"""할 일 표시 기록 (완료 · 제외 · 업무삭제) + 대화 · 프로젝트 표시 (답변완료 · 프로젝트 완료 · 삭제).

내 할 일(오늘 탭)과 프로젝트 할 일(프로젝트 탭)을 같은 표에 둔다. kind 로 구분한다.
답을 기다리는 메일과 프로젝트의 ⋮ 메뉴 표시는 marks 표에 따로 둔다.
일정 탭에서 직접 정한 기한은 task_due 표에 둔다 (메일에서 뽑은 기한보다 우선).

메일 DB(mail.db)와 따로 feedback.db 에 둔다.
- 메일을 다시 받아도(저장소 재구성) 기록이 남는다.
- 대시보드 서버가 쓰는 동안 메일 받기·업무 정리와 잠금이 겹치지 않는다.

나중에 업무 정리에 반영(학습)할 수 있도록, 현재 상태와 함께 누른 기록 전체와 당시 문구를 남긴다.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT

FEEDBACK_DB = ROOT / "feedback.db"
LABELS = {"done": "완료", "excluded": "제외", "deleted": "업무삭제"}
REASONS = {
    "excluded": ("남의 일", "참고만 하면 됨", "이미 다른 사람이 처리"),
    "deleted": ("할 일 아님", "중복", "이미 끝난 일", "내용이 틀림"),
}
SNAPSHOT_FIELDS = ("text", "thread_key", "subject", "project", "deadline", "urgency",
                   "kind", "role", "assignee")
KINDS = {"me": "내 할 일", "task": "프로젝트 할 일"}
SNAPSHOT_MAX = 500
TASK_ID = re.compile(r"^[0-9a-f]{12}$")
# 답을 기다리는 메일(대화)과 프로젝트에 붙이는 표시. 삭제는 대시보드에서 숨기기만 한다 (메일은 그대로)
MARKS = {
    "thread": {"answered": "답변완료", "deleted": "삭제"},
    "project": {"done": "완료", "deleted": "삭제"},
}
MARK_KEY_MAX = 500
DUE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DUE_YEARS = 3                                   # 직접 정하는 기한은 올해 앞뒤로 이만큼 안에서만

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_feedback (      -- 할 일 하나당 현재 표시 (없으면 남은 일)
    task_id     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,                  -- done | excluded | deleted
    reason      TEXT DEFAULT '',
    text        TEXT DEFAULT '',                -- 표시할 때의 문구 (학습 예시 · 완료 목록)
    thread_key  TEXT DEFAULT '',
    subject     TEXT DEFAULT '',
    project     TEXT DEFAULT '',
    deadline    TEXT DEFAULT '',
    urgency     TEXT DEFAULT '',                -- 내 할 일만
    kind        TEXT DEFAULT 'me',              -- me(내 할 일) | task(프로젝트 할 일)
    role        TEXT DEFAULT '',                -- 프로젝트 할 일의 역할 (개발/디자인/수정/기타)
    assignee    TEXT DEFAULT '',
    labeled_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_log (       -- 누른 기록 전체 (되돌리기 포함)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    action      TEXT NOT NULL,                  -- done | excluded | deleted | open(되돌리기)
    reason      TEXT DEFAULT '',
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marks (             -- 대화 · 프로젝트 하나당 현재 표시 (없으면 진행 중)
    target      TEXT NOT NULL,                  -- thread | project
    key         TEXT NOT NULL,                  -- 대화 키 | 프로젝트 이름
    label       TEXT NOT NULL,                  -- thread: answered | deleted, project: done | deleted
    ref_at      TEXT DEFAULT '',                -- 표시할 때 마지막으로 받은 메일 시각. 이후 새 메일이
                                                -- 오면 답변완료 · 프로젝트 완료는 풀린다
    title       TEXT DEFAULT '',                -- 표시할 때의 제목 (기록 확인용)
    labeled_at  TEXT NOT NULL,
    PRIMARY KEY (target, key)
);
CREATE TABLE IF NOT EXISTS mark_log (           -- 누른 기록 전체 (되돌리기 포함)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    key         TEXT NOT NULL,
    action      TEXT NOT NULL,                  -- 표시 | open(되돌리기)
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_due (           -- 할 일 하나당 직접 정한 기한 (없으면 메일에서 뽑은 기한)
    task_id     TEXT PRIMARY KEY,
    due         TEXT NOT NULL,                  -- YYYY-MM-DD
    text        TEXT DEFAULT '',                -- 정할 때의 문구 (기록 확인 · 학습 예시)
    thread_key  TEXT DEFAULT '',
    subject     TEXT DEFAULT '',
    project     TEXT DEFAULT '',
    deadline    TEXT DEFAULT '',                -- 그때 메일에서 뽑혀 있던 기한 (직접 정한 기한과 비교)
    urgency     TEXT DEFAULT '',
    kind        TEXT DEFAULT 'me',
    role        TEXT DEFAULT '',
    assignee    TEXT DEFAULT '',
    set_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS due_log (            -- 기한을 정한 기록 전체 (메일 기한으로 되돌리기 포함)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    due         TEXT DEFAULT '',                -- 빈 값이면 메일 기한으로 되돌림
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class FeedbackError(ValueError):
    """잘못된 표시 요청 (알 수 없는 표시 · 사유 · ID)."""


class FeedbackStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else FEEDBACK_DB
        self.conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """예전 feedback.db 에 새 열을 덧붙인다 (내 할 일만 있던 시절의 기록은 kind=me)."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(task_feedback)")}
        with self.conn:
            for col, ddl in (("kind", "TEXT DEFAULT 'me'"), ("role", "TEXT DEFAULT ''"),
                             ("assignee", "TEXT DEFAULT ''")):
                if col not in have:
                    self.conn.execute(f"ALTER TABLE task_feedback ADD COLUMN {col} {ddl}")
            self.conn.execute("UPDATE task_feedback SET kind = 'me' WHERE kind IS NULL OR kind = ''")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> FeedbackStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def token(self) -> str:
        """대시보드에 심는 비밀값. 다른 사이트가 로컬 서버로 기록을 보내지 못하게 한다."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'token'").fetchone()
        if row:
            return row["value"]
        value = secrets.token_urlsafe(24)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('token', ?)",
                              (value,))
        return self.conn.execute("SELECT value FROM meta WHERE key = 'token'").fetchone()["value"]

    def states(self) -> dict[str, dict]:
        return {r["task_id"]: dict(r) for r in self.conn.execute("SELECT * FROM task_feedback")}

    def recent(self, days: int = 30, now: datetime | None = None) -> list[dict]:
        """최근 N일 안에 표시한 할 일, 최근 표시부터."""
        since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM task_feedback WHERE labeled_at >= ? ORDER BY labeled_at DESC",
            (since,))]

    def apply(self, task_id: str, label: str, reason: str = "",
              snapshot: dict | None = None, now: datetime | None = None) -> dict | None:
        """표시를 기록한다. label 이 open 이면 되돌리기(남은 일로). 현재 상태를 돌려준다."""
        if not TASK_ID.match(task_id or ""):
            raise FeedbackError("할 일 ID 가 올바르지 않습니다")
        if label != "open" and label not in LABELS:
            raise FeedbackError(f"알 수 없는 표시: {label}")
        reason = (reason or "").strip()
        if reason and reason not in REASONS.get(label, ()):
            raise FeedbackError(f"알 수 없는 사유: {reason}")
        # 문자열로 비교 · 정렬하므로 항상 UTC 로 적는다
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        snap = {k: str((snapshot or {}).get(k) or "")[:SNAPSHOT_MAX] for k in SNAPSHOT_FIELDS}
        with self.conn:
            self.conn.execute(
                "INSERT INTO feedback_log (task_id, action, reason, at) VALUES (?,?,?,?)",
                (task_id, label, reason, at))
            if label == "open":
                self.conn.execute("DELETE FROM task_feedback WHERE task_id = ?", (task_id,))
                return None
            self.conn.execute(
                f"""INSERT INTO task_feedback (task_id, label, reason, labeled_at,
                        {", ".join(SNAPSHOT_FIELDS)})
                    VALUES (?,?,?,?, {", ".join("?" for _ in SNAPSHOT_FIELDS)})
                    ON CONFLICT(task_id) DO UPDATE SET
                      label=excluded.label, reason=excluded.reason,
                      labeled_at=excluded.labeled_at,
                      {", ".join(f"{k}=CASE WHEN excluded.{k} != '' THEN excluded.{k} ELSE {k} END"
                                 for k in SNAPSHOT_FIELDS)}""",
                (task_id, label, reason, at, *(snap[k] for k in SNAPSHOT_FIELDS)))
        return dict(self.conn.execute(
            "SELECT * FROM task_feedback WHERE task_id = ?", (task_id,)).fetchone())

    # ── 대화 · 프로젝트 표시 (답변완료 · 프로젝트 완료 · 삭제) ─────────────────
    def marks(self, target: str) -> dict[str, dict]:
        return {r["key"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM marks WHERE target = ?", (target,))}

    def mark(self, target: str, key: str, label: str, ref_at: str = "", title: str = "",
             now: datetime | None = None) -> dict | None:
        """표시를 기록한다. label 이 open 이면 되돌리기. 현재 상태를 돌려준다."""
        if target not in MARKS:
            raise FeedbackError(f"알 수 없는 대상: {target}")
        key = (key or "").strip()
        if not key or len(key) > MARK_KEY_MAX:
            raise FeedbackError("대상 키가 올바르지 않습니다")
        if label != "open" and label not in MARKS[target]:
            raise FeedbackError(f"알 수 없는 표시: {label}")
        ref_at = (ref_at or "").strip()
        if ref_at:
            try:
                datetime.fromisoformat(ref_at)
            except ValueError:
                raise FeedbackError("기준 시각 형식이 올바르지 않습니다") from None
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO mark_log (target, key, action, at) VALUES (?,?,?,?)",
                              (target, key, label, at))
            if label == "open":
                self.conn.execute("DELETE FROM marks WHERE target = ? AND key = ?", (target, key))
                return None
            self.conn.execute(
                """INSERT INTO marks (target, key, label, ref_at, title, labeled_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(target, key) DO UPDATE SET label=excluded.label,
                     ref_at=excluded.ref_at, labeled_at=excluded.labeled_at,
                     title=CASE WHEN excluded.title != '' THEN excluded.title ELSE title END""",
                (target, key, label, ref_at, (title or "")[:SNAPSHOT_MAX], at))
        return dict(self.conn.execute("SELECT * FROM marks WHERE target = ? AND key = ?",
                                      (target, key)).fetchone())

    # ── 직접 정한 기한 (일정 탭) ────────────────────────────────────────────
    def dues(self) -> dict[str, dict]:
        return {r["task_id"]: dict(r) for r in self.conn.execute("SELECT * FROM task_due")}

    def set_due(self, task_id: str, due: str, snapshot: dict | None = None,
                now: datetime | None = None) -> dict | None:
        """할 일의 기한을 직접 정한다. due 가 빈 값이면 지운다(메일에서 뽑은 기한으로). 현재 상태를 돌려준다.

        snapshot 의 deadline 에는 그때 메일에서 뽑혀 있던 기한을 싣는다.
        """
        if not TASK_ID.match(task_id or ""):
            raise FeedbackError("할 일 ID 가 올바르지 않습니다")
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        due = (due or "").strip()
        if due:
            try:
                day = date.fromisoformat(due) if DUE_DAY.match(due) else None
            except ValueError:
                day = None
            if day is None:
                raise FeedbackError("기한 형식이 올바르지 않습니다 (YYYY-MM-DD)")
            if abs(day.year - at.year) > DUE_YEARS:
                raise FeedbackError(f"기한은 올해 앞뒤 {DUE_YEARS}년 안에서만 정할 수 있습니다")
        snap = {k: str((snapshot or {}).get(k) or "")[:SNAPSHOT_MAX] for k in SNAPSHOT_FIELDS}
        with self.conn:
            self.conn.execute("INSERT INTO due_log (task_id, due, at) VALUES (?,?,?)",
                              (task_id, due, at.isoformat()))
            if not due:
                self.conn.execute("DELETE FROM task_due WHERE task_id = ?", (task_id,))
                return None
            self.conn.execute(
                f"""INSERT INTO task_due (task_id, due, set_at, {", ".join(SNAPSHOT_FIELDS)})
                    VALUES (?,?,?, {", ".join("?" for _ in SNAPSHOT_FIELDS)})
                    ON CONFLICT(task_id) DO UPDATE SET due=excluded.due, set_at=excluded.set_at,
                      {", ".join(f"{k}=CASE WHEN excluded.{k} != '' THEN excluded.{k} ELSE {k} END"
                                 for k in SNAPSHOT_FIELDS)}""",
                (task_id, due, at.isoformat(), *(snap[k] for k in SNAPSHOT_FIELDS)))
        return dict(self.conn.execute(
            "SELECT * FROM task_due WHERE task_id = ?", (task_id,)).fetchone())


def mark_active(state: dict | None, latest_at: str | None) -> dict | None:
    """표시가 지금도 유효한가. 답변완료 · 프로젝트 완료는 표시한 뒤 새 메일이 오면 풀린다.

    삭제는 새 메일이 와도 유지한다 (광고처럼 다시 볼 필요 없는 것을 숨기는 용도).
    """
    if not state or state["label"] == "deleted" or not latest_at or not state.get("ref_at"):
        return state
    try:
        newer = datetime.fromisoformat(latest_at) > datetime.fromisoformat(state["ref_at"])
    except (TypeError, ValueError):
        return state
    return None if newer else state
