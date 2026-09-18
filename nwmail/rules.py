"""LLM 없이 돌아가는 규칙 기반 요약 (비용 0원, 네트워크 불필요).

정확도는 LLM보다 낮지만 분류·우선순위·기한 추출은 실무에서 쓸 만하다.
API 키도, 구독도, 로컬 모델도 필요 없다.
"""
from __future__ import annotations

import re
from datetime import date

from .client import Mail

# -- 기한 표현 ---------------------------------------------------------------
DEADLINE_PATTERNS = [
    r"\d{4}[-./]\d{1,2}[-./]\d{1,2}",           # 2026-09-20
    r"\d{1,2}\s*월\s*\d{1,2}\s*일",              # 9월 20일
    r"\d{1,2}\s*/\s*\d{1,2}\s*(?:까지|한)",       # 9/20까지
    r"(?:오늘|내일|모레|금일|익일)\s*(?:까지|중)",
    r"(?:이번|다음)\s*주\s*(?:월|화|수|목|금)요일",
    r"(?:금주|차주|월말|분기말|연말)\s*까지",
]
DEADLINE_RE = re.compile("|".join(DEADLINE_PATTERNS))

# -- 분류 규칙 (앞에서부터 먼저 맞는 것이 이긴다) ----------------------------
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("승인결재", ["결재", "승인", "전자결재", "품의", "기안", "반려"]),
    ("회의", ["회의", "미팅", "일정", "참석", "회의록", "아젠다", "zoom", "meet"]),
    ("업무요청", ["요청", "부탁", "회신", "확인 부탁", "검토", "제출", "작성", "공유 부탁"]),
    ("공지", ["공지", "안내", "알림", "전사", "사내", "규정", "정책"]),
    ("뉴스레터", ["뉴스레터", "newsletter", "구독", "unsubscribe", "수신거부", "웨비나"]),
    ("외부영업", ["제안", "견적", "도입", "소개", "데모", "무료 체험", "프로모션"]),
    ("스팸의심", ["당첨", "무료", "긴급 확인", "계정 정지", "본인 인증", "클릭하세요"]),
]

HIGH_KEYWORDS = ["긴급", "asap", "즉시", "마감", "오늘까지", "내일까지", "지연",
                 "장애", "critical", "urgent", "재요청", "미제출", "최종"]
ACTION_KEYWORDS = ["요청", "부탁", "회신", "답변", "확인", "검토", "제출", "승인",
                   "결재", "작성", "참석", "등록", "signoff", "review"]
BULK_SENDER = re.compile(r"(no-?reply|donotreply|newsletter|notification|mailer|bounce)",
                         re.I)

SIGNATURE_CUT = re.compile(
    r"(--\s*$|^-{3,}$|^={3,}$|본\s*메일은|수신을?\s*원하지|무단\s*전재|"
    r"Confidentiality\s+Notice|This\s+e?-?mail\s+.{0,30}confidential)",
    re.I | re.M,
)


def _clean_body(body: str) -> str:
    """인사말·서명·면책문구를 잘라낸다."""
    if m := SIGNATURE_CUT.search(body):
        body = body[: m.start()]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    skip = re.compile(r"^(안녕하세요|감사합니다|수고하십니다|고맙습니다|"
                      r"좋은\s*하루|드림|올림|배상)[.!\s]*$")
    return "\n".join(ln for ln in lines if not skip.match(ln))


def _summarize_body(body: str, limit: int = 140) -> str:
    """본문에서 가장 정보량이 많아 보이는 앞부분을 뽑는다."""
    clean = _clean_body(body)
    if not clean:
        return "(본문 없음)"
    sentences = re.split(r"(?<=[.!?。])\s+|\n+", clean)
    out = ""
    for s in sentences:
        s = s.strip()
        if len(s) < 4:
            continue
        out = f"{out} {s}".strip()
        if len(out) >= limit:
            break
    return (out[:limit] + "…") if len(out) > limit else (out or "(본문 없음)")


def _categorize(mail: Mail, haystack: str) -> str:
    if BULK_SENDER.search(mail.from_email):
        return "뉴스레터"
    for name, keywords in CATEGORY_RULES:
        if any(k in haystack for k in keywords):
            return name
    return "기타"


def _classify(mail: Mail) -> dict:
    haystack = f"{mail.subject}\n{_clean_body(mail.body)}".lower()
    category = _categorize(mail, haystack)
    deadline = m.group(0).strip() if (m := DEADLINE_RE.search(
        f"{mail.subject}\n{mail.body}")) else ""

    action_required = (
        any(k in haystack for k in ACTION_KEYWORDS)
        and category not in {"뉴스레터", "스팸의심"}
        and not BULK_SENDER.search(mail.from_email)
    )

    if any(k in haystack for k in HIGH_KEYWORDS) or (deadline and action_required):
        priority = "high"
    elif category in {"뉴스레터", "스팸의심"}:
        priority = "low"
    elif action_required or mail.is_unread:
        priority = "medium"
    else:
        priority = "low"

    if action_required:
        verb = next((k for k in ACTION_KEYWORDS if k in haystack), "확인")
        action = f"{mail.subject} — {verb} 필요"
    else:
        action = ""

    return {
        "mail_id": mail.mail_id,
        "subject": mail.subject,
        "sender": f"{mail.from_name} <{mail.from_email}>".strip(),
        "summary": _summarize_body(mail.body),
        "category": category,
        "priority": priority,
        "action_required": action_required,
        "action": action,
        "deadline": deadline,
    }


class RuleSummarizer:
    """LLM을 쓰지 않는다. 비용 0원, 오프라인 동작."""

    def summarize(self, mails: list[Mail]) -> dict:
        items = [_classify(m) for m in mails]
        todo = sum(1 for i in items if i["action_required"])
        high = sum(1 for i in items if i["priority"] == "high")
        unread = sum(1 for m in mails if m.is_unread)

        buckets: dict[str, int] = {}
        for i in items:
            buckets[i["category"]] = buckets.get(i["category"], 0) + 1
        top = ", ".join(f"{k} {v}건" for k, v in
                        sorted(buckets.items(), key=lambda x: -x[1])[:4])

        return {
            "overview": (
                f"{date.today():%Y-%m-%d} 기준 메일 {len(mails)}통 "
                f"(안 읽음 {unread}통). 처리 필요 {todo}건, 높은 우선순위 {high}건. "
                f"구성: {top}. "
                f"[규칙 기반 요약 — 본문 의미 분석은 하지 않습니다]"
            ),
            "items": items,
        }
