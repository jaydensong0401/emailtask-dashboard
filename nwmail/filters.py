"""메일 차단 필터 (요구 4).

차단은 삭제가 아니다. 저장소에 남기고 태깅만 하므로 대시보드에서 언제든 확인할 수
있고, 잘못 걸린 규칙을 되돌릴 수 있다. 차단된 메일은 LLM 에 보내지 않아 비용도 준다.

보호 도메인
  우리 회사와 주요 고객사(PROTECTED_DOMAINS)는 어떤 규칙에도 차단되지 않는다.
  - 판정: 받을 때(make_matcher)와 다시 적용할 때(reapply) 보호 도메인 메일은 건너뛴다
  - 등록: 보호 도메인을 겨냥한 규칙은 저장 · 다시 켜기를 거부한다 (FilterError)
  - 제안: 보호 도메인과 업무 대화 상대 도메인은 차단 후보에 올리지 않는다
  사용자가 대시보드에서 보호 도메인을 더할 수 있다 (protected_domains 표).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import Mail
from .store import Store

# 사용자 지시로 고정: 이 도메인(하위 도메인 포함)에서 온 메일은 절대 차단하지 않는다
PROTECTED_DOMAINS = ("lumi.example", "aurora.example", "nurimktg.co.example")

KINDS = {"sender": "보낸 주소", "domain": "보낸 도메인", "subject": "제목"}
CATEGORIES = ("광고·홍보", "뉴스레터·구독", "행사·안내", "자동 알림", "기타")
DEFAULT_CATEGORY = "기타"
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class FilterError(ValueError):
    """규칙 · 보호 도메인 요청이 올바르지 않다 (보호 도메인 겨냥, 빈 문구 등)."""


def sender_domain(email: str | None) -> str:
    addr = (email or "").strip().strip("<>").lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def domain_in(domain: str, domains) -> bool:
    """domain 이 목록의 도메인이거나 그 하위 도메인인가."""
    d = (domain or "").lower()
    return bool(d) and any(d == p or d.endswith("." + p) for p in domains)


def normalize_domain(value: str) -> str:
    d = (value or "").strip().lower()
    d = d.rsplit("@", 1)[-1] if "@" in d else d
    d = d.removeprefix("*.").strip(". ")
    if not DOMAIN_RE.match(d):
        raise FilterError(f"도메인 형식이 아닙니다: {value!r} (예: example.com)")
    return d


@dataclass
class Filter:
    id: int
    kind: str        # subject | sender | domain
    pattern: str
    note: str
    enabled: bool
    hits: int
    category: str = DEFAULT_CATEGORY

    def matches(self, mail: Mail) -> bool:
        p = self.pattern.lower()
        if self.kind == "subject":
            return p in (mail.subject or "").lower()
        if self.kind == "domain":
            return domain_in(sender_domain(mail.from_email), (p,))
        return p in (mail.from_email or "").lower()


def load_filters(store: Store, enabled_only: bool = True) -> list[Filter]:
    sql = "SELECT * FROM filters"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return [
        Filter(r["id"], r["kind"], r["pattern"], r["note"] or "",
               bool(r["enabled"]), r["hits"],
               r["category"] if r["category"] in CATEGORIES else DEFAULT_CATEGORY)
        for r in store.conn.execute(sql).fetchall()
    ]


# ── 보호 도메인 ─────────────────────────────────────────────────────────
def protected_domains(store: Store) -> list[dict]:
    """고정(코드) + 사용자가 더한 보호 도메인. fixed 가 True 면 뺄 수 없다."""
    out = [{"domain": d, "note": "고정 (우리 회사·고객사)", "fixed": True}
           for d in PROTECTED_DOMAINS]
    out += [{"domain": r["domain"], "note": r["note"] or "", "fixed": False}
            for r in store.conn.execute(
                "SELECT domain, note FROM protected_domains ORDER BY created_at, domain")
            if r["domain"] not in PROTECTED_DOMAINS]
    return out


def protected_set(store: Store | None = None) -> set[str]:
    return {p["domain"] for p in protected_domains(store)} if store else set(PROTECTED_DOMAINS)


def is_protected(email: str | None, protected) -> bool:
    return domain_in(sender_domain(email), protected)


def add_protected(store: Store, domain: str, note: str = "") -> str:
    d = normalize_domain(domain)
    with store.tx() as c:
        c.execute("INSERT OR IGNORE INTO protected_domains (domain, note, created_at)"
                  " VALUES (?,?,?)", (d, note.strip(), datetime.now(timezone.utc).isoformat()))
    return d


def remove_protected(store: Store, domain: str) -> str:
    d = normalize_domain(domain)
    if d in PROTECTED_DOMAINS:
        raise FilterError(f"{d} 는 고정 보호 도메인이라 뺄 수 없습니다.")
    with store.tx() as c:
        c.execute("DELETE FROM protected_domains WHERE domain = ?", (d,))
    return d


# ── 규칙 ────────────────────────────────────────────────────────────────
def targets_protected(kind: str, pattern: str, protected) -> str | None:
    """규칙이 보호 도메인을 겨냥하면 그 도메인을, 아니면 None.

    제목 규칙은 겨냥하지 않는 것으로 본다 (판정 때 보호 도메인 메일은 건너뛰므로 안전).
    'co.kr' 처럼 보호 도메인을 통째로 덮는 상위 도메인도 겨냥으로 본다.
    """
    if kind == "subject":
        return None
    target = (pattern or "").strip().lower().lstrip("@")
    if kind == "sender":
        target = target.rsplit("@", 1)[-1]
    for p in protected:
        if target == p or target.endswith("." + p) or p.endswith("." + target):
            return p
    return None


def check_rule(kind: str, pattern: str, protected) -> str:
    """등록 전에 규칙을 검사하고 정규화한 문구를 돌려준다."""
    if kind not in KINDS:
        raise FilterError(f"알 수 없는 규칙 종류: {kind}")
    value = (pattern or "").strip()
    if not value:
        raise FilterError("빈 문구는 등록할 수 없습니다.")
    if kind == "domain":
        value = normalize_domain(value)
    elif kind == "sender":
        value = value.lower()
    if hit := targets_protected(kind, value, protected):
        raise FilterError(f"{hit} 는 보호 도메인이라 차단 규칙을 만들 수 없습니다.")
    return value


def add_filter(store: Store, kind: str, pattern: str, note: str = "",
               category: str = DEFAULT_CATEGORY, enabled: bool = True) -> int:
    value = check_rule(kind, pattern, protected_set(store))
    category = category if category in CATEGORIES else DEFAULT_CATEGORY
    with store.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO filters (kind, pattern, note, category, enabled, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, value, note.strip(), category, int(enabled),
             datetime.now(timezone.utc).isoformat()),
        )
        row = c.execute(
            "SELECT id FROM filters WHERE kind = ? AND pattern = ?", (kind, value)
        ).fetchone()
        c.execute("DELETE FROM filter_dismissed WHERE kind = ? AND pattern = ?", (kind, value))
    return row["id"]


def get_filter(store: Store, filter_id: int) -> Filter | None:
    return next((f for f in load_filters(store, enabled_only=False) if f.id == filter_id), None)


def set_enabled(store: Store, filter_id: int, enabled: bool) -> None:
    if enabled and (f := get_filter(store, filter_id)):
        if hit := targets_protected(f.kind, f.pattern, protected_set(store)):
            raise FilterError(f"규칙 {filter_id} 는 보호 도메인({hit})을 겨냥해 켤 수 없습니다.")
    with store.tx() as c:
        c.execute("UPDATE filters SET enabled = ? WHERE id = ?",
                  (int(enabled), filter_id))


def remove_filter(store: Store, filter_id: int) -> None:
    """규칙을 지우면 그 규칙으로 차단됐던 메일도 되살린다."""
    with store.tx() as c:
        c.execute("UPDATE mails SET blocked_by = NULL WHERE blocked_by = ?",
                  (filter_id,))
        c.execute("DELETE FROM filters WHERE id = ?", (filter_id,))


def remove_protected_rules(store: Store) -> list[Filter]:
    """보호 도메인을 겨냥한 규칙을 지운다 (켜져 있든 꺼져 있든). 지운 규칙을 돌려준다."""
    protected = protected_set(store)
    gone = [f for f in load_filters(store, enabled_only=False)
            if targets_protected(f.kind, f.pattern, protected)]
    for f in gone:
        remove_filter(store, f.id)
    return gone


def dismiss_candidate(store: Store, kind: str, pattern: str) -> None:
    with store.tx() as c:
        c.execute("INSERT OR IGNORE INTO filter_dismissed (kind, pattern, created_at)"
                  " VALUES (?,?,?)", (kind, pattern.strip().lower(),
                                      datetime.now(timezone.utc).isoformat()))


def rule_counts(store: Store) -> dict[int, int]:
    """규칙별로 지금 차단하고 있는 메일 수 (저장된 전체)."""
    return {r[0]: r[1] for r in store.conn.execute(
        "SELECT blocked_by, COUNT(*) FROM mails WHERE blocked_by IS NOT NULL GROUP BY blocked_by")}


def make_matcher(filters: list[Filter], protected=None):
    """save_mails 에 넘길 판정 함수. 걸린 규칙 id 를, 없으면 None 을 돌려준다.

    보호 도메인에서 온 메일은 어떤 규칙에도 걸리지 않는다.
    """
    guard = set(protected) if protected is not None else set(PROTECTED_DOMAINS)
    guard |= set(PROTECTED_DOMAINS)

    def match(mail: Mail) -> int | None:
        if is_protected(mail.from_email, guard):
            return None
        for f in filters:
            if f.matches(mail):
                return f.id
        return None
    return match


def reapply(store: Store) -> dict[str, int]:
    """규칙이 바뀐 뒤 이미 저장된 메일에 다시 적용한다.

    내가 보낸 메일과 보호 도메인 메일은 차단하지 않는다.
    """
    match = make_matcher(load_filters(store), protected_set(store))
    rows = store.conn.execute(
        "SELECT uid, subject, from_email, blocked_by, is_mine FROM mails"
    ).fetchall()
    newly, unblocked = 0, 0
    with store.tx() as c:
        for r in rows:
            fake = Mail(mail_id=r["uid"], subject=r["subject"] or "",
                        from_name="", from_email=r["from_email"] or "",
                        received_time="")
            hit = None if r["is_mine"] else match(fake)
            if hit != r["blocked_by"]:
                c.execute("UPDATE mails SET blocked_by = ? WHERE uid = ?",
                          (hit, r["uid"]))
                newly += hit is not None
                unblocked += hit is None
    return {"blocked": newly, "unblocked": unblocked}


# ── 차단 후보 제안 ──────────────────────────────────────────────────────
# 이 회사에서는 '프로모션', '이벤트' 가 광고가 아니라 본업 용어라서 그 단어로는 판단하지 않는다.
AD_MARK = re.compile(r"[(\[]\s*광고\s*[)\]]")
NEWS_SUBJECT = re.compile(r"(뉴스레터|newsletter|뉴스|소식지|zine|digest|weekly|리포트가 도착)", re.I)
EVENT_SUBJECT = re.compile(r"(설명회|세미나|웨비나|webinar|컨퍼런스|conference|박람회|참석 안내|초대합니다)",
                           re.I)
NEWS_LOCAL = re.compile(r"(newsletter|news|marketing|promo)", re.I)
NOTICE_LOCAL = re.compile(r"(no-?reply|donotreply|notification|alert|mailer|bounce)", re.I)


def partner_domains(store: Store, me: str = "") -> set[str]:
    """업무 대화 상대: 우리 회사 사람이 메일을 보낸 곳, 우리 회사 사람이 끼어 있는 대화의 참여자."""
    ours = set(PROTECTED_DOMAINS) | ({sender_domain(me)} if sender_domain(me) else set())
    out: set[str] = set()
    ours_threads: set[str] = set()
    for r in store.conn.execute("SELECT thread_key, from_email, to_addrs, cc_addrs FROM mails"):
        if domain_in(sender_domain(r["from_email"]), ours):
            ours_threads.add(r["thread_key"])
            for raw in (r["to_addrs"], r["cc_addrs"]):
                out |= {sender_domain(a) for a in _addrs(raw)}
    if ours_threads:
        marks = ",".join("?" * len(ours_threads))
        for r in store.conn.execute(
                f"SELECT from_email FROM mails WHERE thread_key IN ({marks})", tuple(ours_threads)):
            out.add(sender_domain(r["from_email"]))
    return {d for d in out if d}


def _addrs(raw) -> list[str]:
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [a for a in items if isinstance(a, str)]


def suggest_filters(store: Store, min_count: int = 2, me: str = "") -> list[dict]:
    """정기 발송(광고 · 뉴스레터 · 행사 안내 · 자동 알림)으로 보이는 곳을 차단 후보로 올린다.

    자동으로 차단하지 않는다. 판단 근거:
      - 제목의 광고 표기 '(광고)' '[광고]' (정보통신망법상 광고 메일 표기) → 제목 규칙
      - 수신거부 헤더, 뉴스레터성 제목, news/marketing 발신 주소 → 뉴스레터·구독
      - 설명회 · 세미나 · 참석 안내 제목 → 행사·안내
      - noreply · notification 발신 주소에서 min_count 통 이상 → 자동 알림
    뚜렷한 신호 없이 제목 키워드만 있는 곳은 제안하지 않는다.
    보호 도메인, 업무 대화 상대 도메인, 이미 있는 규칙, 후보에서 뺀 것은 제외한다.
    """
    protected = protected_set(store)
    partners = partner_domains(store, me)
    existing = {(f.kind, f.pattern.lower()) for f in load_filters(store, enabled_only=False)}
    dismissed = {(r["kind"], r["pattern"]) for r in store.conn.execute(
        "SELECT kind, pattern FROM filter_dismissed")}

    def skip(kind: str, pattern: str) -> bool:
        return (kind, pattern.lower()) in existing or (kind, pattern.lower()) in dismissed

    rows = [r for r in store.conn.execute("""
        SELECT from_email, subject, list_unsubscribe, to_addrs, cc_addrs
        FROM mails WHERE blocked_by IS NULL AND is_mine = 0""").fetchall()
            if (d := sender_domain(r["from_email"]))
            and not domain_in(d, protected) and not domain_in(d, partners)]

    out: list[dict] = []
    ad_rows: dict[str, list] = {}
    for r in rows:
        if m := AD_MARK.search(r["subject"] or ""):
            ad_rows.setdefault("(광고)" if m.group(0).startswith("(") else "[광고]", []).append(r)
    for mark, rs in ad_rows.items():
        if not skip("subject", mark):
            out.append({"kind": "subject", "pattern": mark, "category": "광고·홍보",
                        "count": len(rs), "reasons": ["제목에 광고 표기 (법정 광고 메일 표시)"],
                        "sample": rs[0]["subject"] or ""})
    ad_covered = {id(r) for rs in ad_rows.values() for r in rs}

    by_domain: dict[str, list] = {}
    for r in rows:
        if id(r) not in ad_covered:
            by_domain.setdefault(sender_domain(r["from_email"]), []).append(r)

    for domain, rs in by_domain.items():
        if skip("domain", domain) or skip("sender", "@" + domain):
            continue
        locals_ = {(r["from_email"] or "").lower().split("@")[0] for r in rs}
        subjects = [r["subject"] or "" for r in rs]
        unsub = sum(1 for r in rs if r["list_unsubscribe"])
        reasons: list[str] = []
        if unsub or any(NEWS_SUBJECT.search(s) for s in subjects) or any(NEWS_LOCAL.search(x) for x in locals_):
            category = "뉴스레터·구독"
            if unsub:
                reasons.append(f"수신거부 헤더 {unsub}통")
            if any(NEWS_SUBJECT.search(s) for s in subjects):
                reasons.append("뉴스레터성 제목")
            if any(NEWS_LOCAL.search(x) for x in locals_):
                reasons.append("홍보·뉴스 발송 주소")
        elif any(EVENT_SUBJECT.search(s) for s in subjects):
            category = "행사·안내"
            reasons.append("설명회·세미나 안내 제목")
        elif any(NOTICE_LOCAL.search(x) for x in locals_):
            if len(rs) < min_count:
                continue                      # 알림 한두 통은 업무 도구 알림일 수 있어 두고 본다
            category = "자동 알림"
            reasons.append(f"자동 발송 주소에서 {len(rs)}통")
        else:
            continue                          # 뚜렷한 신호가 없으면 제안하지 않는다 (오탐 방지)
        reasons.append("우리 쪽에서 메일을 보낸 적 없음")
        out.append({"kind": "domain", "pattern": domain, "category": category,
                    "count": len(rs), "reasons": reasons, "sample": subjects[0]})

    out.sort(key=lambda c: (CATEGORIES.index(c["category"]), -c["count"], c["pattern"]))
    return out


def block_overview(store: Store, me: str = "") -> dict:
    """차단됨 탭에 필요한 것: 보호 도메인 · 규칙별 차단 수 · 차단 후보 · 선택지."""
    return {"protected": protected_domains(store), "counts": rule_counts(store),
            "candidates": suggest_filters(store, me=me),
            "categories": list(CATEGORIES), "kinds": dict(KINDS)}
