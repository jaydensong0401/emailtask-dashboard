"""Claude 사용량 기록 · 집계 (대시보드 맨 아래 '토큰 사용 현황').

토큰을 쓰는 곳은 세 군데뿐이다: 업무 정리(extract.py — 자동 09·13·17시 또는 직접 실행),
메일 요약(run.py, claude_code · api 백엔드일 때), 토큰 확인(set_token.py). 메일 받기 · 차단 ·
프로젝트 분류 · 대시보드는 규칙으로 돌아 토큰을 쓰지 않는다.

  llm_calls  호출 한 번마다: 토큰(입력 · 출력 · 캐시 읽기 · 캐시 쓰기) · 모델 · API 환산 금액 · 걸린 시간,
             결과(실패면 원인: 한도 초과 · 시간 초과 · 인증 · 오류), 보낸 대화별 글자 수
  llm_runs   업무 정리 한 번마다: 다룬 대화 · 내용이 그대로라 다시 묻지 않은 대화 · 새로 정리 · 실패 · 호출 수

- 금액은 CLI 가 계산한 API 환산값이다. 구독 사용량으로 처리되므로 실제 청구는 없고, 호출끼리 무게를
  견주는 참고값이다 (출력 · 캐시 쓰기는 비싸고 캐시 읽기는 싸다).
- 한 호출에 대화 여러 개를 묶어 보내므로 대화 · 프로젝트별 토큰은 글자 수 비율로 나눈 추정치다.
- 구독 한도가 얼마나 남았는지는 CLI 가 알려주지 않는다. 한도 초과로 실패한 호출만 잡는다.
- 기록이 실패해도 업무 정리는 멈추지 않는다. 기록은 mail.db 에 있고 메일을 다시 받아도 지우지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .config import NONINTERACTIVE_ENV
from .store import KST, Store

PURPOSES = {"extract": "업무 정리", "summary": "메일 요약", "verify": "토큰 확인"}
ERROR_KINDS = {"limit": "한도 초과", "timeout": "시간 초과", "auth": "인증 문제", "error": "오류"}
# 어디에 쓰나: (용도, 실행) → 이름. 실행이 None 이면 자동 · 직접을 합친다
USES = ((("extract", "auto"), "업무 정리 · 자동"), (("extract", "manual"), "업무 정리 · 직접 실행"),
        (("summary", None), "메일 요약 (run.py)"), (("verify", None), "토큰 확인 (set_token.py)"))
MANUAL_SLOT = "직접 실행"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

LIMIT_WORDS = ("session limit", "usage limit", "weekly limit", "hit your", "limit reached",
               "rate limit", "rate_limit", "too many requests")
AUTH_WORDS = ("토큰을 거부", "로그인", "not logged in", "authorization", "authentication", "인증")
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def trigger() -> str:
    """자동 실행(작업 스케줄러 → auto.py)이면 auto, 터미널에서 직접 돌리면 manual."""
    return "auto" if os.getenv(NONINTERACTIVE_ENV) else "manual"


def error_kind(message: str | None) -> str:
    """실패 원인: limit(구독 한도 초과) · timeout · auth(토큰 · 로그인) · error."""
    low = (message or "").lower()
    if any(w in low for w in LIMIT_WORDS):
        return "limit"
    if "시간 초과" in low or "timed out" in low or "timeout" in low:
        return "timeout"
    if any(w in low for w in AUTH_WORDS):
        return "auth"
    return "error"


def reset_label(message: str | None) -> str:
    """한도가 풀리는 시각. 'resets 1:30pm (Asia/Seoul)' → '13:30', 'resets Sep 20, 3pm' → '9/20 15:00'."""
    m = RESET_RE.search(message or "")
    if not m:
        return ""
    hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
    at = f"{hour:02d}:{m['m'] or '00'}"
    mon = (m["mon"] or "").lower()
    return f"{MONTHS.index(mon) + 1}/{int(m['day'])} {at}" if mon in MONTHS else at


# ── 기록 ────────────────────────────────────────────────────────────────
def start_run(store: Store, purpose: str = "extract", threads: int = 0,
              skipped: int = 0) -> int | None:
    """업무 정리 한 번을 연다. 기록하지 못하면 None (정리는 그대로 진행)."""
    try:
        with store.tx() as c:
            cur = c.execute(
                "INSERT INTO llm_runs (at, purpose, trigger, threads, skipped) VALUES (?,?,?,?,?)",
                (now_iso(), purpose, trigger(), threads, skipped))
        return cur.lastrowid
    except sqlite3.Error:
        return None


def finish_run(store: Store, run_id: int | None, cached: int = 0, extracted: int = 0,
               failed: int = 0, calls: int = 0) -> None:
    if run_id is None:
        return
    try:
        with store.tx() as c:
            c.execute("UPDATE llm_runs SET cached = ?, extracted = ?, failed = ?, calls = ?"
                      " WHERE id = ?", (cached, extracted, failed, calls, run_id))
    except sqlite3.Error:
        pass


def record_call(store: Store, purpose: str, *, usage: dict | None = None, backend: str = "",
                seconds: float = 0.0, run_id: int | None = None, threads: int = 0, mails: int = 0,
                prompt_chars: int = 0, error: str = "", detail: list | None = None,
                at: str | None = None, mode: str = "") -> int | None:
    """호출 한 번을 남긴다. usage 는 CliResult.usage() 모양 (모델 · 토큰 · cost_usd).

    detail 은 보낸 대화마다 [thread_key, 제목, 프로젝트, 글자 수, 메일 수, 방식] — 대화 · 프로젝트별 추정에 쓴다.
    mode 는 그 호출이 대화 전체를 보냈는지(full) 정리 결과 + 새 메일만 보냈는지(delta) — 둘이 섞이면 mixed.
    """
    u = usage or {}
    try:
        with store.tx() as c:
            cur = c.execute(
                """INSERT INTO llm_calls
                   (at, purpose, trigger, run_id, backend, model, threads, mails, prompt_chars,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
                    seconds, ok, error_kind, error, detail, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (at or now_iso(), purpose, trigger(), run_id, backend, str(u.get("model") or ""),
                 threads, mails, prompt_chars,
                 *(int(u.get(f) or 0) for f in TOKEN_FIELDS),
                 float(u.get("cost_usd") or 0), round(float(seconds or 0), 1),
                 int(not error), error_kind(error) if error else "", (error or "")[:300],
                 json.dumps(detail or [], ensure_ascii=False), mode))
        return cur.lastrowid
    except sqlite3.Error:
        return None


# ── 집계 (대시보드) ──────────────────────────────────────────────────────
def total_tokens(row: dict) -> int:
    return sum(int(row.get(f) or 0) for f in TOKEN_FIELDS)


def _kst(value: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(KST)


def slot_of(row: dict, slot_times: tuple[str, ...]) -> str:
    """자동 정리 회차 이름 ('09시'). 그 날 지난 회차 중 가장 늦은 것, 첫 회차 전이면 전날 마지막 회차.

    직접 실행은 '직접 실행'.
    """
    if row.get("trigger") != "auto":
        return MANUAL_SLOT
    local = _kst(row.get("at"))
    hm = f"{local:%H:%M}" if local else "00:00"
    passed = [t for t in slot_times if t <= hm] or list(slot_times[-1:])
    return f"{passed[-1][:2]}시" if passed else MANUAL_SLOT


def _bucket(rows: list[dict]) -> dict:
    out = {"tokens": 0, "calls": len(rows), "fails": 0, "limit": 0, "cost": 0.0}
    for f in TOKEN_FIELDS:
        out[f] = sum(int(r.get(f) or 0) for r in rows)
    for r in rows:
        out["tokens"] += total_tokens(r)
        out["cost"] += float(r.get("cost_usd") or 0)
        out["fails"] += not r.get("ok")
        out["limit"] += r.get("error_kind") == "limit"
    out["cost"] = round(out["cost"], 4)
    return out


def delta(cur: dict, prev: dict) -> dict:
    """증가량: 이전 같은 길이의 기간과 견준 차이.

    pct 는 이전이 0 이면 None (몇 % 늘었다고 말할 수 없다). dir 은 up · down · flat · new.
    """
    a, b = int(cur.get("tokens") or 0), int(prev.get("tokens") or 0)
    diff = a - b
    return {"tokens": diff, "calls": int(cur.get("calls") or 0) - int(prev.get("calls") or 0),
            "cost": round(float(cur.get("cost") or 0) - float(prev.get("cost") or 0), 4),
            "prev": b, "pct": (diff / b) if b else None,
            "dir": "new" if not b and a else "flat" if not diff else "up" if diff > 0 else "down"}


def _detail(raw: str | None) -> list[list]:
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    return [x for x in items if isinstance(x, list) and len(x) >= 4]


def _call_view(r: dict, slot_times: tuple[str, ...]) -> dict:
    local = _kst(r.get("at"))
    return {**{k: r.get(k) for k in ("id", "at", "purpose", "trigger", "model", "threads", "mails",
                                      "seconds", "error_kind", "error", *TOKEN_FIELDS)},
            "ok": bool(r.get("ok")), "tokens": total_tokens(r),
            "cost": float(r.get("cost_usd") or 0), "slot": slot_of(r, slot_times),
            "when": f"{local:%m/%d %H:%M}" if local else "", "reset": reset_label(r.get("error")),
            "purpose_label": PURPOSES.get(r.get("purpose") or "", r.get("purpose") or "")}


def summary(store: Store, now: datetime | None = None, days: int = 7,
            slot_times: tuple[str, ...] = ("09:00", "13:00", "17:00"), recent: int = 30) -> dict:
    """대시보드 '토큰 사용 현황'에 필요한 것. 기간은 한국 날짜 기준 (오늘 포함 최근 days 일 · 이번 달).

    증가량을 말하려면 직전 기간도 같이 읽어야 한다 — 어제 · 지난 days 일 · 지난달.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(KST).date()
    first, month0 = today - timedelta(days=days - 1), today.replace(day=1)
    # 직전 기간(증가량을 재는 자): 지난 days 일 · 지난달 1일
    prev_first, prev_month0 = first - timedelta(days=days), (month0 - timedelta(days=1)).replace(day=1)
    since = min(prev_first, prev_month0)
    start = datetime(since.year, since.month, since.day, tzinfo=KST).astimezone(timezone.utc)
    try:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM llm_calls WHERE at >= ? ORDER BY at, id", (start.isoformat(),))]
        log = [dict(r) for r in store.conn.execute(
            "SELECT * FROM llm_calls ORDER BY at DESC, id DESC LIMIT ?", (recent,))]
        runs = [dict(r) for r in store.conn.execute(
            "SELECT * FROM llm_runs WHERE at >= ? ORDER BY at, id",
            (datetime(first.year, first.month, first.day, tzinfo=KST).astimezone(timezone.utc).isoformat(),))]
        first_at = store.conn.execute("SELECT MIN(at) FROM llm_calls").fetchone()[0]
        probe = store.conn.execute(
            "SELECT * FROM llm_calls WHERE purpose = 'verify' AND ok = 1 ORDER BY at DESC LIMIT 1").fetchone()
        # 누적은 행을 다 읽지 않고 합계만 받는다 (기록이 쌓일수록 길어지므로)
        tot = dict(store.conn.execute(
            f"SELECT COUNT(*) AS calls, COALESCE(SUM({' + '.join(TOKEN_FIELDS)}), 0) AS tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(ok = 0), 0) AS fails FROM llm_calls"
        ).fetchone())
    except sqlite3.Error:
        rows, log, runs, first_at, probe, tot = [], [], [], None, None, None
    for r in rows:
        r["day"] = (_kst(r["at"]) or datetime.min.replace(tzinfo=KST)).date()
    week = [r for r in rows if r["day"] >= first]

    day_list = []
    for i in range(days):
        d = first + timedelta(days=i)
        day_list.append({"day": d, **_bucket([r for r in week if r["day"] == d])})

    slots: dict[str, list[dict]] = {f"{t[:2]}시": [] for t in slot_times}
    for r in week:
        slots.setdefault(slot_of(r, slot_times), []).append(r)
    slot_list = [{"label": k, **_bucket(v)} for k, v in slots.items()
                 if v or k != MANUAL_SLOT]

    week_tokens = sum(total_tokens(r) for r in week) or 0
    uses = []
    for (purpose, trig), label in USES:
        picked = [r for r in week if r.get("purpose") == purpose and (trig is None or r.get("trigger") == trig)]
        b = _bucket(picked)
        uses.append({"label": label, "purpose": purpose, **b,
                     "share": b["tokens"] / week_tokens if week_tokens else 0.0})

    # 대화별 추정: 호출 토큰을 그 호출에 담긴 대화들의 글자 수 비율로 나눈다
    threads: dict[str, dict] = {}
    for r in week:
        items = _detail(r.get("detail"))
        chars = sum(max(int(x[3] or 0), 0) for x in items)
        for key, subject, project, size, *rest in items:
            t = threads.setdefault(key, {"key": key, "subject": subject, "project": project,
                                         "tokens": 0, "sent": 0, "chars": 0, "mails": 0})
            t["sent"] += 1
            t["chars"] = int(size or 0)
            t["mails"] = int(rest[0] or 0) if rest else t["mails"]
            t["subject"], t["project"] = subject or t["subject"], project or t["project"]
            if r.get("ok") and chars:
                t["tokens"] += total_tokens(r) * int(size or 0) / chars
    for t in threads.values():
        t["tokens"] = round(t["tokens"])

    limits = [r for r in week if r.get("error_kind") == "limit"]
    last_limit = max(limits, key=lambda r: r["at"]) if limits else None
    last_run = runs[-1] if runs else None
    models: dict[str, int] = {}
    for r in week:
        if r.get("model"):
            models[r["model"]] = models.get(r["model"], 0) + total_tokens(r)
    probe = dict(probe) if probe else None

    # 사용량과 증가량: 오늘 · 최근 days 일 · 이번 달 을 각각 어제 · 지난 days 일 · 지난달 과 견준다
    cur = {"today": _bucket([r for r in rows if r["day"] == today]), "week": _bucket(week),
           "month": _bucket([r for r in rows if month0 <= r["day"] <= today])}
    prev = {"today": _bucket([r for r in rows if r["day"] == today - timedelta(days=1)]),
            "week": _bucket([r for r in rows if prev_first <= r["day"] < first]),
            "month": _bucket([r for r in rows if prev_month0 <= r["day"] < month0]),
            # 일별 막대 첫 줄의 증가량을 재려면 창 하루 앞도 있어야 한다
            "day0": _bucket([r for r in rows if r["day"] == first - timedelta(days=1)])}
    span = (today - (kst_day(first_at) or today)).days + 1 if first_at else 0
    total = {"tokens": int((tot or {}).get("tokens") or 0), "calls": int((tot or {}).get("calls") or 0),
             "cost": round(float((tot or {}).get("cost") or 0), 4), "fails": int((tot or {}).get("fails") or 0),
             "span": span, "per_day": round(int((tot or {}).get("tokens") or 0) / span) if span else 0}
    from .usage_report import build_report
    return {
        "has_data": bool(first_at), "first_at": first_at, "days": days,
        "today": cur["today"], "week": cur["week"], "month": cur["month"],
        "prev": prev, "total": total,
        "delta": {k: delta(cur[k], prev[k]) for k in cur},
        "day_list": day_list, "slots": slot_list, "uses": uses, "threads": threads,
        "limit": {"count": len(limits), "at": last_limit["at"] if last_limit else None,
                  "reset": reset_label(last_limit.get("error")) if last_limit else ""},
        "runs": {"count": len(runs), "cached": sum(r["cached"] or 0 for r in runs),
                 "extracted": sum(r["extracted"] or 0 for r in runs),
                 "failed": sum(r["failed"] or 0 for r in runs),
                 "calls": sum(r["calls"] or 0 for r in runs), "last": last_run},
        "models": sorted(models, key=lambda k: -models[k]),
        "probe": {"tokens": probe["input_tokens"] + probe["cache_read_tokens"] + probe["cache_write_tokens"],
                  "at": probe["at"], "model": probe["model"]} if probe else None,
        "log": [_call_view(r, slot_times) for r in log],
        "reduction_report": build_report(store, now=now, days=days),
    }


def kst_day(value: str | None) -> date | None:
    d = _kst(value)
    return d.date() if d else None
