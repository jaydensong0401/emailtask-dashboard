"""대시보드 HTML 생성 (4단계).

정적 HTML 한 장으로 끝나도록 CSS·JS 를 모두 인라인한다. 외부 리소스를 부르지
않으므로 사내망/오프라인에서도 열리고, 메일 내용이 PC 밖으로 나가지 않는다.

완료·제외·업무삭제는 로컬 서버의 feedback.db에 저장한다. 오늘/프로젝트의
동일 업무는 공통 ID로 연결하므로 재생성하거나 브라우저를 바꿔도 상태가 유지된다.
예전 localStorage 완료 기록은 서버에서 열 때 가져온다.
"""
from __future__ import annotations

import html
import json
import re
import zlib
from datetime import date, datetime, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path
from urllib.parse import quote

from .client import INBOX_NAMES
from .colors import BANDS, tones
from .extract import PROMPT_VERSION, load_all, render_thread
from .feedback import (
    DUE_YEARS, EXTRACT_EXCLUSION_REASONS, LABELS, MARKS, REASONS,
    FeedbackError, FeedbackStore, excludes_analysis, mark_active,
)
from .filters import block_overview, load_filters, sender_domain
from .mailtext import preview_text, snippet
from .projects import (
    canonical_org, classify, load_rules, normalize_role, org_for_email, people_orgs, person_key,
    skip_extract, title_key, work_title,
)
from .store import Store, window_start
from .tasklinks import task_id, task_links
from .usage import ERROR_KINDS
from .usage import summary as usage_summary

USAGE_PAGE = "usage.html"               # 토큰 사용 현황 페이지 (대시보드 옆에 같이 놓인다)
RULES_PAGE = "rules.html"
# Mailwork 시안 A의 m 심볼: 머리말과 파비콘이 같은 벡터 도형을 사용한다.
MAILWORK_MARK = (
    '<path d="M0 70 C0 28 28 0 70 0 C91 0 108 8 120 22 '
    'C132 8 149 0 170 0 C212 0 240 28 240 70 V116 Q240 120 236 120 '
    'H194 Q190 120 190 116 V72.5 C190 57.5 182.5 50 167.5 50 '
    'C152.5 50 145 57.5 145 72.5 V206 Q145 210 141 210 H99 Q95 210 95 206 '
    'V72.5 C95 57.5 87.5 50 72.5 50 C57.5 50 50 57.5 50 72.5 '
    'V206 Q50 210 46 210 H4 Q0 210 0 206 Z"/>'
    '<rect x="190" y="160" width="50" height="50" rx="4"/>')

# 검정 타일 + 흰 심볼. data URI로 넣어 파일로 직접 열 때도 표시한다.
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"'
    ' role="img" aria-labelledby="title"><title id="title">Mailwork favicon</title>'
    '<rect width="64" height="64" rx="12.0000" fill="#111111"/>'
    '<g fill="#FFFFFF" transform="translate(8.0000 11.0000) scale(0.20000000)">'
    f'{MAILWORK_MARK}</g></svg>')

# 인라인 SVG의 currentColor가 라이트 · 다크 테마의 글자색을 따라간다.
MAILWORK_LOGO = (Path(__file__).with_name("assets") / "mailwork-logo.svg").read_text(encoding="utf-8").strip()


def _head(title: str) -> str:
    """두 페이지가 같이 쓰는 <head>. 아이콘은 파일 하나로 열어도 보이게 data URI."""
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="application-name" content="Mailwork">'
            '<meta name="apple-mobile-web-app-title" content="Mailwork">'
            f'<link rel="icon" href="data:image/svg+xml,{quote(FAVICON, safe="")}"'
            ' type="image/svg+xml" sizes="any">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head>')


KST = timezone(timedelta(hours=9))     # 한국은 서머타임이 없어 고정 오프셋으로 충분
UNCLASSIFIED = "미분류"
OTHER_CLIENT = "기타"                   # 설정에 없는 고객사(태그로만 알려진 프로젝트 · 미분류)의 묶음 이름
MAX_SLOTS = 9                           # 프로젝트 색 9개 (검증된 범주형 8색 + 자주색 1색). 모자라면 처음부터 다시
COLOR_NAMES = ("회색", "파랑", "주황", "청록", "노랑", "분홍", "초록", "보라", "빨강", "자주")   # 0~9번 칸 (색 고르는 창)
FULL_RUN_TIMES = ("09:00", "13:00", "17:00")   # setup_schedule.ps1 의 Mailwork-full 시각과 맞춘다
WAIT_PREVIEW = 6                        # 답을 기다리는 메일은 이만큼만 펼쳐 둔다
RECENT_DECISIONS = 5
LABELED_DAYS = 30                       # 완료 · 제외 목록은 최근 이만큼만
WEEKDAYS = "월화수목금토일"
DUE_KEEP_DAYS = 30                      # 최근 N일 창 밖 대화의 기한 있는 할 일은 마감이 이만큼 지나면 뺀다
SCHEDULE_WEEK = 7                       # 일정 탭 '7일 안' 과 탭 숫자의 기준
SCHEDULE_CAP = 3                        # 달력 한 칸에 펼쳐 둘 할 일 수 (넘치면 +N건)
SAME_WORK_MIN = 0.6                     # 카드에서 같은 일로 묶을 문구 겹침 비율
ADOPT_MIN = 0.6                         # 문구가 바뀐 같은 일로 보고 표시를 옮길 겹침 비율
# 일정 탭 달력에 표시하는 관공서 공휴일 · 대체공휴일 · 선거일. 음력 명절은 Windows 음력 달력
# (KoreanLunisolarCalendar)로 계산했다. 임시공휴일은 정해지면 더하고, 2028년부터는 해마다 추가한다.
HOLIDAYS = {
    "2026-01-01": "신정", "2026-02-16": "설날 연휴", "2026-02-17": "설날", "2026-02-18": "설날 연휴",
    "2026-03-01": "삼일절", "2026-03-02": "대체공휴일", "2026-05-05": "어린이날",
    "2026-05-24": "부처님오신날", "2026-05-25": "대체공휴일", "2026-06-03": "지방선거",
    "2026-06-06": "현충일", "2026-08-15": "광복절", "2026-08-17": "대체공휴일",
    "2026-09-24": "추석 연휴", "2026-09-25": "추석", "2026-09-26": "추석 연휴",
    "2026-10-03": "개천절", "2026-10-05": "대체공휴일", "2026-10-09": "한글날", "2026-12-25": "성탄절",
    "2027-01-01": "신정", "2027-02-06": "설날 연휴", "2027-02-07": "설날", "2027-02-08": "설날 연휴",
    "2027-02-09": "대체공휴일", "2027-03-01": "삼일절", "2027-05-05": "어린이날",
    "2027-05-13": "부처님오신날", "2027-06-06": "현충일", "2027-08-15": "광복절",
    "2027-08-16": "대체공휴일", "2027-09-14": "추석 연휴", "2027-09-15": "추석",
    "2027-09-16": "추석 연휴", "2027-10-03": "개천절", "2027-10-04": "대체공휴일",
    "2027-10-09": "한글날", "2027-10-11": "대체공휴일", "2027-12-25": "성탄절",
    "2027-12-27": "대체공휴일",
}

URGENCY_RANK = {"high": 0, "medium": 1, "low": 2}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_dt(value) -> str:
    d = parse_dt(value)
    return d.astimezone(KST).strftime("%m/%d %H:%M") if d else str(value or "")


def deadline_info(value: str, today: date) -> dict:
    v = (value or "").strip()
    if not v:
        return {"label": "", "level": "none", "sort": 99999, "raw": ""}
    try:
        d = date.fromisoformat(v[:10])
    except ValueError:
        return {"label": v, "level": "later", "sort": 99998, "raw": v}
    delta = (d - today).days
    if delta < 0:
        return {"label": f"{-delta}일 지남", "level": "overdue", "sort": delta, "raw": v}
    if delta == 0:
        return {"label": "오늘", "level": "today", "sort": 0, "raw": v}
    return {"label": f"D-{delta}", "level": "soon" if delta <= 3 else "later",
            "sort": delta, "raw": v}


def iso_day(raw: str) -> str:
    """기한 값에서 달력에 올릴 날짜 'YYYY-MM-DD'. 날짜로 읽히지 않으면 빈 값 (deadline_info 와 같은 기준)."""
    try:
        return date.fromisoformat((raw or "").strip()[:10]).isoformat()
    except ValueError:
        return ""


def due_info(mail_deadline: str, set_due: dict | None, today: date) -> dict:
    """실제 기한: 일정 탭에서 직접 정한 기한이 있으면 그것, 없으면 메일에서 뽑은 기한.

    mail 은 메일에서 뽑힌 기한 그대로(직접 정한 기한을 지우면 돌아갈 값), manual 은 직접 정했는지.
    날짜로 읽히는 기한은 raw 를 'YYYY-MM-DD' 로 맞춘다 (페이지 JS 가 같은 꼴로 읽는다).
    """
    mail = (mail_deadline or "").strip()
    d = deadline_info(set_due["due"] if set_due else mail, today)
    if day := iso_day(d["raw"]):
        d["raw"] = day
    return {**d, "mail": mail, "manual": bool(set_due)}


def new_since(store: Store) -> str | None:
    """NEW 표시 기준 시각: 직전 정리(추출) 이후에 온 메일.

    자동 실행이면 10분마다 동기화하므로 '직전 동기화 이후' 는 10분 안쪽이라 쓸모가 없다.
    그래서 정리 주기를 기준으로 한다. 가장 최근 정리 직전의 정리부터 세므로, 방금 정리가
    끝나도 그 정리에 새로 반영된 메일은 NEW 로 남는다. 정리 기록이 없으면 직전 동기화.
    """
    return (store.last_run_at("extract", offset=1) or store.last_run_at("extract", offset=0)
            or store.last_run_at("sync", offset=1))


def next_full_run(now: datetime) -> datetime:
    """다음 자동 정리(Mailwork-full) 시각 (KST)."""
    local = now.astimezone(KST)
    for day in (0, 1):
        base = local + timedelta(days=day)
        for t in FULL_RUN_TIMES:
            hh, mm = map(int, t.split(":"))
            at = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at > local:
                return at
    return local


def addr_set(raw) -> set[str]:
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        items = []
    return {str(a).strip().lower() for a in items if a}


def awaiting_to_me(thread: dict, me: str, cutoff: str) -> str | None:
    """받는사람(To)에 내 주소가 있는 최근 메일 중 내가 아직 답하지 않은 가장 늦은 메일 시각.

    참조(Cc)로만 받은 메일은 세지 않는다. 대시보드의 '답을 기다리는 메일' 기준이며,
    업무 정리(추출)가 쓰는 '내 회신 필요' 판단(To/Cc)과는 따로 둔다.
    """
    me = me.strip().lower()
    asked = max((m["sent_at"] or "" for m in thread["mails"]
                 if not m.get("is_mine") and (m["sent_at"] or "") >= cutoff
                 and me in addr_set(m.get("to_addrs"))), default=None)
    mine = thread.get("my_last_at")
    return asked if asked and (mine is None or asked > mine) else None


def sender_parts(name: str | None, email: str | None, rules=None) -> tuple[str, str]:
    """보낸 사람 표시를 이름(직함)과 소속으로 나눈다. 소속은 메일 주소로 확인되면 그걸 쓴다.

    '서나윤 대리_누리마케팅' → ('서나윤 대리', '누리'), '루미 강민호 이사' → ('강민호 이사', '루미'),
    '유나래 Narae Yoo 책임매니저 오로라고객경험팀' → ('유나래 책임매니저', '오로라').
    """
    raw = " ".join((name or "").split())
    head, _, tail = raw.partition("_")
    org = org_for_email(email, rules) or canonical_org(tail, rules)
    words = [w for w in head.split() if not (org and org.lower() in w.lower())]
    hangul = [w for w in words if re.search(r"[가-힣]", w)]
    words = hangul or words                     # 한글 이름이 있으면 'Narae Yoo' 같은 영문 병기는 뺀다
    return " ".join(words) or head or (email or ""), org


def latest(*values) -> str | None:
    dts = [(d, v) for v in values if (d := parse_dt(v))]
    return max(dts)[1] if dts else None


def _kst_day(value) -> date | None:
    d = parse_dt(value)
    return d.astimezone(KST).date() if d else None


def _snapshot_action(row: dict, slot_of, today: date, roles: list[str],
                     dues: dict | None = None) -> dict:
    """지금 대시보드에 없는 할 일(최근 7일을 벗어난 대화 등)을 표시 기록의 문구로 되살린다."""
    project = row.get("project") or UNCLASSIFIED
    urgency = row.get("urgency") if row.get("urgency") in URGENCY_RANK else "medium"
    role = row.get("role") or ""
    return {
        "id": row["task_id"], "text": row.get("text") or "(문구 없음)", "urgency": urgency,
        "kind": row.get("kind") or "me", "role": role, "fix": "",
        "rslot": roles.index(role) + 1 if role in roles else 0,
        "assignee": row.get("assignee") or "", "org": "",
        "deadline": due_info(row.get("deadline") or "", (dues or {}).get(row["task_id"]), today),
        "project": project, "slot": slot_of(project),
        "thread": {"key": row.get("thread_key") or "", "subject": row.get("subject") or "",
                   "direct": False, "new": False},
    }


def _schedule_items(open_actions: list[dict], projects: list[dict], stale_tasks: list[dict],
                    done: list[dict]) -> list[dict]:
    """일정 탭에 올릴 할 일: 남은 내 할 일(기한이 없어도) + 기한 있는 남은 프로젝트 할 일 + 기한 있는 완료한 일.

    프로젝트 할 일은 진행 중 프로젝트 것만 넣고, 내 할 일과 공통 ID로 연결됐으면 뺀다.
    기한순(없으면 맨 뒤) → 남은 일 → 내 할 일 → 긴급도 순. 페이지 JS 의 byDue 와 같은 순서다.
    """
    mine = {a["id"] for a in open_actions}
    tasks = [t for p in projects if p["pstate"] in ("open", "delayed") for i in p["items"]
             for ts in i["roles"].values() for t in ts if open_task(t)] + stale_tasks
    rows = list(open_actions) + [
        t for t in tasks if iso_day(t["deadline"]["raw"])
        and t["id"] not in mine]
    rows += [d for d in done if iso_day(d["deadline"]["raw"])]
    return sorted(rows, key=lambda r: (iso_day(r["deadline"]["raw"]) or "9999", not open_task(r),
                                       r.get("kind") == "task", URGENCY_RANK.get(r.get("urgency"), 1)))


def state_label(item: dict) -> str:
    """할 일의 현재 표시. 없으면 open(남은 일)."""
    return ((item.get("state") or {}).get("label") or "open")


def open_task(item: dict) -> bool:
    return state_label(item) == "open"


def dropped_task(item: dict) -> bool:
    return state_label(item) in ("excluded", "deleted")


def project_progress(items: list[dict], ready: bool, mark: dict | None) -> dict:
    """공통 ID는 한 번만 센다. 미추출 대화가 있으면 빈 목록을 완료로 오인하지 않는다."""
    unique = {i["id"]: i for i in items}
    remaining = [i for i in unique.values() if open_task(i)]
    total = sum(not dropped_task(i) for i in unique.values())
    state = (mark or {}).get("label")
    if not state:
        state = ("delayed" if any(i["deadline"]["level"] == "overdue" for i in remaining)
                 else "done" if ready and not remaining else "open")
    return {"state": state, "total": total, "open": len(remaining)}


def project_slots(rules) -> dict[str, int]:
    """색은 세부 프로젝트마다. projects.json 순서(고객사 → 세부 프로젝트 → 기본 프로젝트)대로 1번 색부터
    차례로 주고, 색이 모자라 처음부터 다시 쓸 때도 같은 고객사 안에서는 아직 안 쓴 색을 먼저 고른다.

    설정 순서만 보므로 최근 7일에 어떤 프로젝트가 보이든 색이 바뀌지 않는다 (보이는 수가 달라도 다시 칠하지 않음).
    """
    slots: dict[str, int] = {}
    used: dict[str, set[int]] = {}
    turn = 0                                # 다음 프로젝트가 받을 색 차례 (0부터)
    for name, client in rules.client_of.items():
        taken = used.setdefault(client, set())
        order = [(turn + k) % MAX_SLOTS + 1 for k in range(MAX_SLOTS)]
        slot = next((s for s in order if s not in taken), order[0])
        slots[name] = slot
        taken.add(slot)
        turn = slot % MAX_SLOTS
    return slots


def name_slot(name: str) -> int:
    """설정에 없는 프로젝트(제목 태그 · 메일 도메인으로 알게 된 곳)는 이름으로 정해지는 고정 색."""
    return zlib.crc32(name.encode("utf-8")) % MAX_SLOTS + 1


def extra_slots(extra: dict[str, list[str]], slots: dict[str, int], client_of: dict[str, str]) -> dict[str, int]:
    """설정에 없는 프로젝트의 색 (고객사 → 이름들). 이름으로 정해지는 색(name_slot)을 먼저 쓰고, 같은 고객사
    묶음에서 이미 쓴 색이면 이름순으로 뒤의 것이 아직 안 쓴 다음 색을 쓴다. 보이는 프로젝트가 같으면 늘 같은 색."""
    out: dict[str, int] = {}
    for client, names in extra.items():
        taken = {s for n, s in slots.items() if client_of.get(n) == client}
        for name in sorted(names):
            want = name_slot(name)
            order = [(want - 1 + k) % MAX_SLOTS + 1 for k in range(MAX_SLOTS)]
            out[name] = next((s for s in order if s not in taken), want)
            taken.add(out[name])
    return out


def picked_slots(auto: dict[str, int], picked: dict[str, str]) -> tuple[dict[str, int], dict[int, str]]:
    """사용자가 고른 색(feedback.db)을 자동 색 위에 얹는다. s1~s9 는 그 팔레트 칸, 직접 고른 #rrggbb 는
    10번 칸부터 하나씩 새로 만든다 (페이지가 그 칸의 색을 테마별로 정의). 돌려주는 값: (프로젝트 → 칸, 새 칸 → 색)."""
    slots, custom = dict(auto), {}
    for name, color in sorted(picked.items()):
        if color.startswith("s") and color[1:].isdigit() and 1 <= int(color[1:]) <= MAX_SLOTS:
            slots[name] = int(color[1:])
        elif color.startswith("#") and len(color) == 7:
            custom[MAX_SLOTS + 1 + len(custom)] = color
            slots[name] = MAX_SLOTS + len(custom)
    return slots, custom


def _received_at(mails: list[dict]) -> datetime | None:
    """마지막으로 받은 메일 시각 (내가 보낸 메일은 빼고). 완료 표시가 풀리는 기준."""
    return max((d for m in mails if not m.get("is_mine") and (d := parse_dt(m.get("sent_at")))),
               default=None)


FIX_TYPES = ("개발", "디자인", "데이터")      # _role_tag 가 역할 뒤에 붙이는 수정 종류


def _plain_text(row: dict) -> str:
    """표시 기록의 문구에서 앞에 붙은 역할 태그를 걷어낸다.

    예전 기록은 화면의 .t 를 통째로 긁어 저장해서 '기타ATR 투어…' 처럼 역할이 문구 앞에 붙어 있다
    (지금은 _state_attrs 의 data-text 로 문구만 싣는다). 걷어낸 쪽이 늘 맞지는 않으므로
    ('개발계에 반영' 처럼 문구가 역할 이름으로 시작할 수 있다) 비교할 때 원문과 함께 쓴다.
    """
    text = (row.get("text") or "").strip()
    role = row.get("role") or ""
    if not role or (row.get("kind") or "me") != "task":
        return text
    for tag in [f"{role}·{f}" for f in FIX_TYPES] + [role]:
        if text.startswith(tag):
            return text[len(tag):].strip()
    return text


def adopt_marks(feedback: FeedbackStore, extractions: dict, last_seen: dict[str, str],
                states: dict[str, dict]) -> dict[str, dict]:
    """문구가 바뀌어 떨어져 나간 완료 · 제외 표시를 지금 할 일에 다시 이어붙인다.

    할 일 ID 는 (대화, 종류, 문구)로 만들기 때문에(task_id), 업무 정리를 다시 돌려 같은 일의
    문구가 조금이라도 달라지면 표시가 떨어져 나가고 끝낸 일이 '남은 일'로 다시 올라온다.

    표시한 뒤 그 대화에 새로 받은 메일이 없으면 달라진 것은 문구뿐이므로 표시를 새 ID 로 옮긴다.
    새 메일이 있으면 같은 일을 다시 요청받은 것일 수 있으므로 옮기지 않는다. 그런 할 일은 남은
    일로 그대로 두고 '전에 처리한 일과 비슷하다'고만 알려, 사람이 보고 판단하게 한다.

    states 는 그 자리에서 고친다. 돌려주는 값은 {지금 할 일 ID: 비슷한 표시 기록}.
    """
    live: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, data in extractions.items():
        if not isinstance(data, dict):
            continue
        for kind, field in (("me", "my_actions"), ("task", "tasks")):
            for it in data.get(field) or []:
                if isinstance(it, dict) and (text := (it.get("text") or "").strip()):
                    live.setdefault((key, kind), []).append((task_id(key, kind, text), text))

    live_ids = {tid for items in live.values() for tid, _ in items}
    orphans: dict[tuple[str, str], list[dict]] = {}
    for m in states.values():
        if m["task_id"] not in live_ids and m.get("thread_key"):
            orphans.setdefault((m["thread_key"], m.get("kind") or "me"), []).append(m)

    similar: dict[str, dict] = {}
    for spot, marks in orphans.items():
        items = [(tid, text) for tid, text in live.get(spot, ()) if tid not in states]
        pairs = [(r, m["task_id"], tid, text)
                 for m in marks for tid, text in items
                 if (r := max(work_ratio(m.get("text") or "", text),
                              work_ratio(_plain_text(m), text))) >= ADOPT_MIN]
        taken_marks: set[str] = set()
        taken_tasks: set[str] = set()
        for _, old, new, text in sorted(pairs, key=lambda p: -p[0]):
            if old in taken_marks or new in taken_tasks:
                continue        # 표시 하나에 할 일 하나 (겹침이 큰 짝부터 가져간다)
            taken_marks.add(old)
            taken_tasks.add(new)
            mark = states[old]
            if (last_seen.get(spot[0]) or "") > (mark.get("labeled_at") or ""):
                similar[new] = mark
                continue
            try:
                moved = feedback.move(old, new, text)
            except FeedbackError:
                continue        # 형식이 이상한 옛 기록은 건드리지 않는다
            if moved:
                states[new] = moved
                states.pop(old, None)
    return similar


# ── 데이터 모델 ─────────────────────────────────────────────────────────────
def recipient_alerts(store: Store, states: dict[str, dict], me: str) -> list[dict]:
    """제외 표시 뒤 받은메일함의 To에 본인 주소가 포함된 최신 메일을 대화마다 알린다."""
    if not me.strip():
        return []
    excluded = {}
    for state in states.values():
        at = parse_dt(state.get("labeled_at"))
        if not excludes_analysis(state) or not at:
            continue
        key = store._current_key(state["thread_key"])
        excluded[key] = min(excluded.get(key, at), at)
    if not excluded:
        return []
    alerts = []
    inboxes = {name.casefold() for name in INBOX_NAMES}
    for thread in store.threads(None, keys=list(excluded)):
        candidates = []
        for mail in thread["mails"]:
            at = parse_dt(mail.get("sent_at"))
            if (mail.get("is_mine") or (mail.get("folder") or "").casefold() not in inboxes
                    or not at or at <= excluded[thread["thread_key"]]):
                continue
            try:
                raw = json.loads(mail.get("to_addrs") or "[]")
            except (ValueError, TypeError):
                continue
            if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
                continue
            if me.strip().casefold() in {addr.casefold() for _, addr in getaddresses(raw)}:
                candidates.append(mail)
        if candidates:
            mail = max(candidates, key=lambda m: parse_dt(m["sent_at"]))
            alerts.append({"thread_key": thread["thread_key"], "uid": mail["uid"],
                           "subject": mail.get("subject") or thread["subject"],
                           "at": mail["sent_at"],
                           "sender": mail.get("from_name") or mail.get("from_email") or ""})
    return sorted(alerts, key=lambda a: parse_dt(a["at"]), reverse=True)


def build_model(store: Store, me: str, today: date | None = None,
                now: datetime | None = None, window_days: int = 7,
                feedback: FeedbackStore | None = None) -> dict:
    """최근 window_days 일 안에 받은 메일이 있는 대화만 담는다 (과거 이력은 대화에 포함).

    feedback 이 있으면 내 할 일을 표시 기록(완료 · 제외 · 업무삭제)에 따라 나누고,
    답을 기다리는 메일(답변완료 · 삭제)과 프로젝트(완료 · 삭제) 표시도 반영한다.
    할 일의 기한은 일정 탭에서 직접 정한 기한이 있으면 그것을 쓴다. 창 밖 대화라도 기한이 있는
    남은 할 일은 오늘 탭 · 일정 탭에 남긴다 (프로젝트 탭에는 없음).
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(KST).date()
    cutoff = window_start(window_days, now).isoformat()
    rules = load_rules()
    auto = project_slots(rules)                 # 자동 색 (설정 순서) — 색 고르는 창의 '자동 색으로'
    picked = feedback.colors() if feedback else {}
    slots, custom = picked_slots(auto, picked)
    extractions = load_all(store)
    identity_threads = store.threads(window_days=window_days, now=now, keys=list(extractions)) if extractions else []
    links = task_links(extractions, [me, *_my_names(identity_threads)])
    linked_actions = {v["task_id"]: v for v in links.values()}
    if feedback:
        feedback.link_tasks(links)

    def identity(key: str, kind: str, text: str) -> str:
        original = task_id(key, kind, text)
        return links.get(original, {}).get("task_id", original)

    states = feedback.states() if feedback else {}
    # 문구가 바뀌어 떨어져 나간 표시를 먼저 이어붙인다 (states 를 고치고, 새 메일이 온 뒤라
    # 옮기지 않은 것은 '비슷한 일' 로만 알려 준다)
    similar = (adopt_marks(feedback, extractions, store.last_received(), states)
               if feedback else {})
    marked: dict[str, list[dict]] = {}          # 대화별 표시 기록 (업무 정리에 보내는 크기 계산용)
    for mk in states.values():
        marked.setdefault(store._current_key(mk.get("thread_key") or ""), []).append(mk)
    analysis_excluded = {key for key, items in marked.items()
                         if any(excludes_analysis(state) for state in items)}
    dues = feedback.dues() if feedback else {}
    thread_marks = feedback.marks("thread") if feedback else {}
    project_marks = feedback.marks("project") if feedback else {}
    prev_run = parse_dt(new_since(store))

    projects: dict[str, dict] = {}
    my_actions: list[dict] = []
    unanswered: list[dict] = []
    handled: list[dict] = []
    direct_threads: list[dict] = []
    sizes: dict[str, dict] = {}            # 대화별 지금 크기 (업무 정리에 보내는 글자 수) — 토큰 사용 현황
    extracted = new_threads = excluded = window_mails = 0

    def slot_of(project: str) -> int:
        """프로젝트 색 (고른 색이 있으면 그 색). 미분류는 회색(0), 설정에 없는 이름은 이름으로 정해지는 색.
        예전 표시 기록의 이름('루미' 등)은 지금 규칙의 세부 프로젝트로 맞춰 본다."""
        if project in slots:
            return slots[project]
        if not project or project == UNCLASSIFIED:
            return 0
        return slots.get(rules.resolve(project)) or name_slot(project)

    def project(name: str, client: str) -> dict:
        if name not in projects:
            projects[name] = {"name": name, "client": client, "slot": slot_of(name),
                              "threads": [], "items": {}, "new": False, "latest": None,
                              "received": None}
        return projects[name]

    def work_item(p: dict, subject: str) -> dict:
        """같은 업무명(메일명)의 대화는 대화가 달라도 한 묶음. 대화는 최신순으로 들어온다."""
        title = work_title(subject, "" if p["name"] == UNCLASSIFIED else p["name"], rules)
        key = title_key(title) or title
        if key not in p["items"]:
            p["items"][key] = {"title": title, "threads": [], "pending": [], "decisions": [],
                               "excluded": [], "roles": {r: [] for r in rules.roles},
                               "status": "",
                               "new": False, "latest": None}
        return p["items"][key]

    def thread_view(t: dict, data: dict | None, name: str, is_new: bool) -> dict:
        last_dt, last = parse_dt(t["last_at"]), t["mails"][-1]
        return {
            "key": t["thread_key"], "subject": t["subject"], "count": t["mail_count"],
            "recent": t["recent_count"],
            "last_at": last_dt.isoformat() if last_dt else t["last_at"],
            "direct": t["is_direct"], "replied": t["replied_by_me"], "new": is_new,
            "from": "나" if last.get("is_mine") else (
                last.get("from_name") or last.get("from_email") or ""),
            "summary": (data or {}).get("summary", ""),
            "status": (data or {}).get("status", ""),
            "snippet": snippet(last.get("body")), "extracted": bool(data),
            "preview": preview_text(last.get("body")),
            "project": name,
        }

    def project_tasks(t: dict, data: dict, th: dict, name: str, slot: int,
                      people: dict[str, str]) -> list[dict]:
        out = []
        for task in data.get("tasks") or []:
            text = (task.get("text") or "").strip() if isinstance(task, dict) else ""
            if not text:
                continue
            role, fix = normalize_role(task.get("role"), task.get("fix_type"), rules)
            assignee = (task.get("assignee") or "").strip()
            # 소속은 메일 주소로 확인되면 그걸, 아니면 모델이 적은 회사명을 쓴다
            org = people.get(person_key(assignee)) or canonical_org(
                task.get("assignee_org"), rules)
            tid = identity(t["thread_key"], "task", text)
            linked = links.get(task_id(t["thread_key"], "task", text), {})
            out.append({
                "id": tid, "text": text, "kind": "task",
                "role": role, "fix": fix, "rslot": rules.roles.index(role) + 1,
                "assignee": assignee, "org": org,
                "deadline": due_info(linked.get("deadline", task.get("deadline", "")), dues.get(tid), today),
                "project": name, "slot": slot,
                "state": states.get(tid), "similar": similar.get(tid), "thread": th,
            })
        return out

    def my_items(t: dict, data: dict, th: dict, name: str, slot: int) -> list[dict]:
        out = []
        for act in data.get("my_actions") or []:
            text = (act.get("text") or "").strip() if isinstance(act, dict) else ""
            if not text:
                continue
            urg = (act.get("urgency") or "medium").strip().lower()
            aid = task_id(t["thread_key"], "me", text)
            out.append({
                "id": aid, "text": text, "kind": "me",
                "urgency": urg if urg in URGENCY_RANK else "medium",
                "deadline": due_info(linked_actions.get(aid, {}).get("deadline", act.get("deadline", "")),
                                     dues.get(aid), today),
                "project": name, "slot": slot, "similar": similar.get(aid), "thread": th,
            })
        return out

    threads = store.threads(window_days=window_days, now=now)
    for t in threads:
        mails = t["mails"]
        received = [m for m in mails if not m.get("is_mine")] or mails
        last_dt = parse_dt(t["last_at"])
        is_new = bool(prev_run) and any(
            (d := parse_dt(m.get("sent_at"))) and d > prev_run for m in received)
        new_threads += is_new
        skipped = skip_extract(t["subject"], rules)
        # 정기 메일은 추출하지 않으므로, 예전에 뽑아 둔 결과가 있어도 쓰지 않는다 (갱신이 안 됨)
        data = None if skipped else extractions.get(t["thread_key"])
        extracted += bool(data)
        excluded += skipped

        client, name, basis = classify(t["subject"], received[0].get("from_email", ""),
                                       (data or {}).get("project", ""), rules)
        name = name or UNCLASSIFIED
        th = dict(thread_view(t, data, name, is_new), basis=basis,
                  analysis_excluded=t["thread_key"] in analysis_excluded)
        window_mails += sum(not m.get("is_mine") and (m.get("sent_at") or "") >= cutoff for m in mails)
        if not skipped and t["thread_key"] not in analysis_excluded:
            # 업무 정리에 실제로 보내는 글자 수 ('처리한 일' 까지 포함해야 실제와 맞는다)
            sizes[t["thread_key"]] = {"chars": len(render_thread(t, me, marks=marked.get(t["thread_key"]))),
                                      "mails": t["mail_count"],
                                      "subject": t["subject"], "project": name}

        p = project(name, client)
        p["threads"].append(th)
        p["new"] |= is_new
        if last_dt and (p["latest"] is None or last_dt > p["latest"]):
            p["latest"] = last_dt
        if (got := _received_at(mails)) and (p["received"] is None or got > p["received"]):
            p["received"] = got
        item = work_item(p, t["subject"])
        item["threads"].append(th)
        item["new"] |= is_new
        if last_dt and (item["latest"] is None or last_dt > item["latest"]):
            item["latest"] = last_dt

        if data:
            item["status"] = item["status"] or th["status"]
            people = people_orgs(mails, rules)
            for task in project_tasks(t, data, th, name, p["slot"], people):
                item["roles"][task["role"]].append(task)
            for dec in data.get("decisions") or []:
                text = (dec.get("text") if isinstance(dec, dict) else str(dec or "")).strip()
                if text:
                    by = (dec.get("by") or "").strip() if isinstance(dec, dict) else ""
                    item["decisions"].append({
                        "text": text, "by": by, "org": people.get(person_key(by), ""),
                        "date": (dec.get("date") or "").strip() if isinstance(dec, dict) else "",
                        "project": name, "thread": th,
                    })
            my_actions += my_items(t, data, th, name, p["slot"])
        elif skipped:
            item["excluded"].append(th)
        else:
            item["pending"].append(th)

        if t["is_direct"]:
            direct_threads.append(th)
        if asked := awaiting_to_me(t, me, cutoff):
            # 답변완료는 표시한 뒤 나에게 새 메일이 오면 풀린다
            mark = mark_active(thread_marks.get(t["thread_key"]), asked)
            # 기다리는 메일 = 나에게(To) 온 그 메일. 보낸 사람 · 본문 미리보기는 대화의 마지막 메일이 아니라 이것으로
            ask = next((x for x in reversed(mails) if x["sent_at"] == asked and not x.get("is_mine")), {})
            who, org = sender_parts(ask.get("from_name"), ask.get("from_email"), rules)
            (handled if mark else unanswered).append({
                "thread": th, "asked": asked, "mark": mark, "name": who, "org": org,
                "body": preview_text(ask.get("body")),
                "waited": max((now - parse_dt(asked)).days, 0)})

    # 창 밖으로 나간 대화라도 기한이 있는 남은 할 일은 완료 · 제외할 때까지 남긴다. 마감이
    # DUE_KEEP_DAYS 일 넘게 지난 것은 뺀다. 먼저 추출 결과만으로 거르고, 남은 대화만 불러온다
    keep_from = (today - timedelta(days=DUE_KEEP_DAYS)).isoformat()

    def still_due(key: str, kind: str, text: str, deadline) -> bool:
        tid = identity(key, kind, text)
        return tid not in states and iso_day(
            dues[tid]["due"] if tid in dues else str(deadline or "")) >= keep_from

    def has_due(key: str, data: dict) -> bool:
        return any(still_due(key, kind, text, it.get("deadline"))
                   for kind, field in (("me", "my_actions"), ("task", "tasks"))
                   for it in data.get(field) or [] if isinstance(it, dict)
                   and (text := (it.get("text") or "").strip()))

    in_window = {t["thread_key"] for t in threads}
    stale_keys = [k for k, data in extractions.items()
                  if k not in in_window and isinstance(data, dict) and has_due(k, data)]
    stale_tasks: list[dict] = []
    for t in store.threads(window_days=window_days, now=now, keys=stale_keys) if stale_keys else []:
        if skip_extract(t["subject"], rules):
            continue
        data = extractions[t["thread_key"]]
        received = [m for m in t["mails"] if not m.get("is_mine")] or t["mails"]
        _, name, _ = classify(t["subject"], received[0].get("from_email", ""),
                              data.get("project", ""), rules)
        name = name or UNCLASSIFIED
        th = dict(thread_view(t, data, name, False), stale=True)
        kept = [x for x in my_items(t, data, th, name, slot_of(name))
                + project_tasks(t, data, th, name, slot_of(name),
                                people_orgs(t["mails"], rules))
                if still_due(t["thread_key"], x["kind"], x["text"], x["deadline"]["raw"])]
        my_actions += [x for x in kept if x["kind"] == "me"]
        stale_tasks += [x for x in kept if x["kind"] == "task"]

    # 설정에 없는 프로젝트는 다 모은 뒤에 색을 정한다 (같은 묶음 안에서 겹치지 않게).
    # 그 전에 만든 할 일 · 프로젝트에는 이름으로 정해지는 색이 들어가 있으니 다시 칠한다
    extra: dict[str, list[str]] = {}
    for p in projects.values():
        if p["name"] != UNCLASSIFIED and p["name"] not in auto:
            extra.setdefault(p["client"], []).append(p["name"])
    found = extra_slots(extra, auto, rules.client_of)
    auto.update(found)
    for name, slot in found.items():
        slots.setdefault(name, slot)            # 고른 색이 있으면 그대로
    for item in [*my_actions, *stale_tasks, *(t for p in projects.values() for i in p["items"].values()
                                              for ts in i["roles"].values() for t in ts)]:
        item["slot"] = slot_of(item["project"])
    for p in projects.values():
        p["slot"] = slot_of(p["name"])
        p["auto_slot"] = auto.get(p["name"], 0)
        p["color"] = picked.get(p["name"], "")

    for p in projects.values():
        p["threads"].sort(key=lambda x: x["last_at"] or "", reverse=True)
        # 새 메일이 온 묶음 → 남은 업무가 있는 묶음 → 최근 순
        p["items"] = sorted(
            p["items"].values(),
            key=lambda i: (not i["new"], not any(i["roles"].values()),
                           -(i["latest"].timestamp() if i["latest"] else 0)))
        tasks = [t for i in p["items"] for v in i["roles"].values() for t in v]
        p["task_count"] = sum(not dropped_task(t) for t in tasks)   # 삭제·제외는 빼고 센다
        p["open_count"] = sum(open_task(t) for t in tasks)
        p["decision_count"] = sum(len(i["decisions"]) for i in p["items"])
        # 프로젝트 완료는 표시한 뒤 새 메일을 받으면 풀려 진행 중으로 돌아온다
        received = p["received"].isoformat() if p["received"] else ""
        p["mark"] = mark_active(project_marks.get(p["name"]), received)
        p["ready"] = bool(p["threads"]) and all(t["extracted"] for t in p["threads"])
        own = [dict(a, state=states.get(a["id"])) for a in my_actions if a["project"] == p["name"]]
        p["progress_items"] = {i["id"]: {"state": state_label(i), "deadline": i["deadline"]["raw"]}
                               for i in tasks + own}
        p["progress"] = project_progress(tasks + own, p["ready"], p["mark"])
        p["pstate"] = p["progress"]["state"]
        p["received_at"] = received
    # 남은 할 일이 있는 프로젝트 → 고객사 → 새 메일 → 최근 순.
    rank = {c: i for i, c in enumerate(rules.clients)}
    ordered = sorted(
        projects.values(),
        key=lambda p: (not (p["progress"]["open"] and p["pstate"] in ("open", "delayed")),
                       rank.get(p["client"], len(rank)), p["name"] == UNCLASSIFIED,
                       rules.is_default(p["name"]), not p["new"],
                       -(p["latest"].timestamp() if p["latest"] else 0)),
    )
    for index, project in enumerate(ordered):
        project["order"] = index
    my_actions.sort(key=lambda a: (a["deadline"]["sort"], URGENCY_RANK[a["urgency"]],
                                   not a["thread"]["new"]))
    unanswered.sort(key=lambda u: -u["waited"])
    handled.sort(key=lambda u: u["mark"]["labeled_at"], reverse=True)
    direct_threads.sort(key=lambda x: x["last_at"] or "", reverse=True)

    # 전 프로젝트의 결정사항을 최근 순으로. 같은 결론이 여러 대화에서 뽑혔으면 한 번만
    seen: set[tuple[str, str]] = set()
    recent: list[dict] = []
    for d in sorted((d for p in ordered for i in p["items"] for d in i["decisions"]),
                    key=lambda d: d["date"], reverse=True):
        key = (d["project"], d["text"])
        if key not in seen:
            seen.add(key)
            recent.append(d)

    # 표시한 할 일은 남은 일에서 빼고 완료 / 제외·삭제 목록으로. 7일 창을 벗어난 대화의
    # 기록도 표시할 때 남긴 문구로 보여준다
    # 완료 · 삭제한 프로젝트의 할 일은 달력에 올리지 않는다. 창 안에 있는 프로젝트는 새 메일로 풀린 표시를
    # 따르고(pstate), 창 밖에만 있는 프로젝트는 새 메일이 없으니 표시가 그대로 유효하다
    pstates = {p["name"]: p["pstate"] for p in ordered}
    stale_tasks = [t for t in stale_tasks if pstates.get(
        t["project"], "done" if t["project"] in project_marks else "open") in ("open", "delayed")]
    live = {a["id"]: a for a in my_actions}
    live.update({t["id"]: t for p in ordered for i in p["items"]
                 for ts in i["roles"].values() for t in ts})
    live.update({t["id"]: t for t in stale_tasks})
    # 한 업무의 오늘 카드에는 내 할 일의 문구/종류를 유지한다 (되돌리면 오늘 목록으로 복귀).
    live.update({a["id"]: a for a in my_actions})
    hidden_actions = [a for a in my_actions if a["id"] not in states
                      and pstates.get(a["project"]) == "not_mine"]
    open_actions = [a for a in my_actions if a["id"] not in states
                    and pstates.get(a["project"]) != "not_mine"]
    done: list[dict] = []
    dropped: list[dict] = []
    recorded = {r["task_id"]: r for r in feedback.recent(LABELED_DAYS, now)} if feedback else {}
    # 분석 제외는 기간이 지나도 되돌릴 수 있어야 한다.
    recorded.update({tid: row for tid, row in states.items() if excludes_analysis(row)})
    for row in sorted(recorded.values(), key=lambda r: r["labeled_at"], reverse=True):
        item = dict(live.get(row["task_id"])
                    or _snapshot_action(row, slot_of, today, rules.roles, dues), state=row)
        (done if row["label"] == "done" else dropped).append(item)

    # 일정 탭 요약 · 탭 숫자는 남은 내 할 일 기준 (프로젝트 할 일은 페이지에서 보기를 바꿀 때만 센다)
    # 숨긴 내 업무도 DOM에 보관하여 프로젝트 상태를 되돌리면 즉시 복구한다.
    schedule = _schedule_items(open_actions + hidden_actions, ordered, stale_tasks, done)
    today_iso = today.isoformat()
    week_end = (today + timedelta(days=SCHEDULE_WEEK)).isoformat()
    days = [iso_day(a["deadline"]["raw"]) for a in open_actions]
    sched = {"overdue": sum(bool(d) and d < today_iso for d in days),
             "today": days.count(today_iso),
             "week": sum(today_iso < d <= week_end for d in days),
             "none": days.count("")}

    filters = load_filters(store, enabled_only=False)
    blocked = store.blocked_mails(window_days=window_days, now=now)
    levels = [r["act"]["deadline"]["level"] for r in _merge_same(open_actions)]
    return {
        "generated_at": now, "today": today, "me": me, "window_days": window_days,
        "task_aliases": {k: v["task_id"] for k, v in links.items()},
        "me_names": _my_names(threads),
        "last_sync": store.last_run_at("sync", offset=0),
        "last_extract": latest(store.last_run_at("extract", offset=0),
                               store.last_extracted_at()),
        "next_extract": next_full_run(now),
        "projects": ordered, "my_actions": open_actions, "done_actions": done,
        "hidden_actions": hidden_actions,
        "project_actions": {p["name"]: [a["id"] for a in my_actions if a["project"] == p["name"]]
                            for p in ordered},
        "dropped_actions": dropped, "schedule": schedule,
        "unanswered": unanswered, "handled": handled,
        "direct_threads": direct_threads, "blocked": blocked,
        "recipient_alerts": recipient_alerts(store, states, me),
        "recent_decisions": recent[:RECENT_DECISIONS],
        "custom_colors": custom,
        "filters": filters, "blocking": block_overview(store, me), "roles": list(rules.roles),
        # 맨 아래 2depth: 분류 규칙(지금 설정 그대로) · 토큰 사용 현황(llm_calls 기록) · 대화별 지금 크기
        "rules": rules, "sizes": sizes,
        "usage": usage_summary(store, now=now, slot_times=FULL_RUN_TIMES),
        "stats": {"threads": len(threads), "blocked": len(blocked), "rules": len(filters),
                  "window_mails": window_mails,
                  "history": sum(t["mail_count"] - t["recent_count"] for t in threads),
                  "unanswered": len(unanswered), "new": new_threads,
                  "extracted": extracted, "excluded": excluded,
                  # 오늘 탭은 다른 대화에 똑같이 잡힌 할 일을 한 줄로 합쳐 보여 주므로 그 줄 수로 센다
                  "my_open": len(levels), "due_today": levels.count("today"),
                  "overdue": levels.count("overdue"), "done": len(done),
                  "done_today": sum(_kst_day(d["state"]["labeled_at"]) == today for d in done),
                  "dropped": len(dropped),
                  **{f"sched_{k}": v for k, v in sched.items()},
                  "sched_badge": sched["overdue"] + sched["today"] + sched["week"],
                  "oldest_wait": max((u["waited"] for u in unanswered), default=0),
                  "named_projects": sum(p["name"] != UNCLASSIFIED and p["pstate"] in ("open", "delayed")
                                        for p in ordered),
                  **{f"projects_{k}": sum(p["pstate"] == k for p in ordered)
                     for k in ("open", "delayed", *MARKS["project"])}},
    }


# ── 렌더링 ─────────────────────────────────────────────────────────────────
GLYPH = {"overdue": ("▲", "crit"), "today": ("◆", "serious"),
         "soon": ("◆", "warn"), "later": ("·", "muted")}
URG_LABEL = {"high": ("▲", "높음"), "medium": ("◆", "보통"), "low": ("▽", "낮음")}


def _deadline(d: dict) -> str:
    if d["level"] == "none":
        return ""
    glyph, tone = GLYPH[d["level"]]
    return (f'<span class="dl" title="{esc(d["raw"])}"><i class="{tone}" aria-hidden="true">'
            f'{glyph}</i>{esc(d["label"])}</span>')


def _badges(th: dict, direct: bool = True) -> str:
    """대화 배지. 오늘 탭 대화 카드는 할 일이 모두 직접 받은 대화라 '직접' 을 빼고 단다 (direct=False)."""
    out = []
    if th["new"]:
        out.append('<span class="new">NEW</span>')
    if direct and th["direct"]:
        out.append('<span class="chip">직접</span>')
    if th.get("stale"):
        out.append('<span class="chip" title="최근 메일이 없는 대화 — 기한이 있어 완료 · 제외할 때까지 남겨 둡니다">'
                   '지난 대화</span>')
    if th.get("analysis_excluded"):
        out.append('<span class="chip">AI 분석 제외</span>')
    return "".join(out)


NAME_SEP = re.compile(r"\s*[,，、/]\s*")


def _names(assignee: str | None) -> list[str]:
    """여럿이 적힌 담당자('문지호, 배건우')를 사람별로 나눈다. 화면에는 ' · ' 로 이어 보인다."""
    return [x for x in NAME_SEP.split((assignee or "").strip()) if x]


def _who(item: dict) -> str:
    """담당자 이름 + 소속. 이름이 없으면 '미지정' 으로 비어 있음을 드러낸다."""
    name = (f'<span class="nm">{esc(" · ".join(_names(item["assignee"])))}</span>' if item.get("assignee")
            else '<span class="nm none">미지정</span>')
    org = f'<span class="org">{esc(item["org"])}</span>' if item.get("org") else ""
    return f'<span class="who">{name}{org}</span>'


def _role_tag(item: dict) -> str:
    label = item["role"] + (f'·{item["fix"]}' if item.get("fix") else "")
    return f'<b class="rtag rt{item.get("rslot", 0)}">{esc(label)}</b>'


def _repeat_tag(item: dict) -> str:
    """전에 완료 · 제외한 일과 문구가 비슷할 때 붙인다.

    표시한 뒤 새 메일이 온 대화라 표시를 그대로 옮기지 않은 것이다 (adopt_marks).
    같은 일을 다시 요청받았을 수도, 이어지는 다른 일일 수도 있어 사람이 보고 정한다.
    """
    mark = item.get("similar")
    if not mark:
        return ""
    label = LABELS.get(mark.get("label") or "", "처리")
    return (f'<span class="sim" title="{esc((mark.get("labeled_at") or "")[:10])} {esc(label)}: '
            f'{esc(mark.get("text") or "")}">전에 {esc(label)}한 일과 비슷</span>')


def _controls(item: dict) -> str:
    """완료 · 제외 · 업무삭제 · 되돌리기. 오늘 탭과 프로젝트 탭이 같은 버튼을 쓴다."""
    return (f'<span class="st">{esc(_status_text(item.get("state")))}</span>'
            '<span class="ctl"><button type="button" class="btn ok" data-act="done">완료</button>'
            '<button type="button" class="btn" data-act="excluded">제외</button>'
            '<button type="button" class="btn" data-act="deleted">업무삭제</button>'
            '<button type="button" class="btn undo" data-act="open">되돌리기</button></span>')


def _state_attrs(item: dict) -> dict:
    """버튼이 서버에 보낼 값. 대시보드가 다시 만들어져도 문구가 남도록 함께 싣는다."""
    state, th, d = item.get("state"), item["thread"], item["deadline"]
    return {
        "data-task": item["id"], "data-kind": item.get("kind", "me"),
        "data-state": state_label(item), "data-level": d["level"],
        "data-at": (state or {}).get("labeled_at", ""),
        "data-reason": (state or {}).get("reason", ""),
        "data-thread": th["key"], "data-subject": th["subject"],
        "data-pname": item.get("project", ""), "data-deadline": d["raw"],
        "data-urgency": item.get("urgency", ""), "data-role": item.get("role", ""),
        "data-assignee": item.get("assignee", ""),
        # 문구는 여기서 싣는다. 화면 글자를 긁으면 배지 · 태그 글자가 섞여 기록될 수 있다
        "data-text": item.get("text", ""),
        # 일정 탭에서 기한을 바꾸면 페이지가 세 탭의 같은 할 일을 함께 고친다 (메일 기한은 되돌릴 값)
        "data-mdl": d.get("mail", d["raw"]), "data-manual": "1" if d.get("manual") else "",
    }


def _task(item: dict, show_src: bool = True) -> str:
    """프로젝트 업무 한 줄: 역할 태그(같은 폭 왼쪽 칸) | 내용 / 담당자 · 기한 · 출처 + 표시 버튼.

    태그를 문구 밖 칸에 두어, 태그 길이와 상관없이 문구 · 넘친 둘째 줄 · 담당자 줄이 한 선에서 시작한다.
    """
    meta = [_who(item)]
    if dl := _deadline(item["deadline"]):
        meta.append(dl)
    if show_src:
        meta.append(f'<span class="src" title="{esc(item["thread"]["subject"])}">'
                    f'{esc(item["thread"]["subject"])}</span>')
    attrs = {"class": "task" + (" done" if state_label(item) == "done" else ""),
             **_state_attrs(item)}
    return (
        "<li " + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items()) + ">"
        f'{_role_tag(item)}<div class="tb"><div class="th"><p class="t">{esc(item["text"])}</p>'
        f'{_repeat_tag(item)}'
        f'{_badges(item["thread"]) if item["thread"]["new"] else ""}</div>'
        f'<div class="meta">{"".join(meta)}{_controls(item)}</div></div></li>'
    )


def _thread_row(th: dict, extra: str = "") -> str:
    body = th["summary"] or th["snippet"]
    return (
        f'<li class="thr"><div class="thr-h"><span class="subj">{esc(th["subject"])}</span>'
        f'{_badges(th)}</div><div class="meta"><span>{esc(th["from"])}</span>'
        f'<span class="num">{esc(fmt_dt(th["last_at"]))}</span>'
        f'<span class="num">{_count_label(th)}</span>{extra}</div>'
        + (f'<p class="snip" data-preview="{esc(th["summary"] or th.get("preview") or body)}" '
           f'data-preview-label="{"메일 요약" if th["summary"] else "최근 메일 내용"}">{esc(body)}</p>' if body else "") + "</li>"
    )


def _count_label(th: dict) -> str:
    if th.get("recent") is not None and th["recent"] != th["count"]:
        return f'최근 {th["recent"]}통 · 전체 {th["count"]}통'
    return f'{th["count"]}통'


def _decisions(decisions: list[dict], merged: bool) -> str:
    """결정한 사람별로 결론을 모은다. 사람 안에서는 최근 결정부터, 사람은 최근에 결정한 순.

    이름 뒤 직함이 달라도('김하늘', '김하늘 책임') 같은 사람으로 본다. 결정자를 모르면 맨 뒤.
    """
    groups: dict[str, list[dict]] = {}
    for d in decisions:
        groups.setdefault(person_key(d["by"]), []).append(d)
    for ds in groups.values():
        ds.sort(key=lambda d: d["date"], reverse=True)
    ordered = sorted(groups.items(), key=lambda kv: kv[1][0]["date"], reverse=True)
    ordered.sort(key=lambda kv: kv[0] == "")

    # 요약 줄에는 결정한 사람 이름만 (사람별 건수는 펼치면 이름 옆에 보인다)
    names = [name or "결정자 미기재" for name, _ in ordered]
    summary = " · ".join(names[:3]) + (f" 외 {len(names) - 3}명" if len(names) > 3 else "")
    blocks = []
    for name, ds in ordered:
        org = next((d["org"] for d in ds if d["org"]), "")
        rows = "".join(
            f'<li class="dec"><span class="dd num" title="{esc(d["date"])}">'
            + (esc(d["date"][5:].replace("-", "/")) if d["date"]
               else '<span class="none">일자 미확인</span>')
            + f'</span><div class="dt"><span class="t">{esc(d["text"])}</span>'
            + (f'<div class="meta"><span class="src" title="{esc(d["thread"]["subject"])}">'
               f'{esc(d["thread"]["subject"])}</span></div>' if merged else "")
            + "</div></li>"
            for d in ds)
        blocks.append(
            f'<div class="dec-g" data-by="{esc(name)}"><div class="dec-by">'
            f'<span class="nm{"" if name else " none"}">{esc(name or "결정자 미기재")}</span>'
            + (f'<span class="org">{esc(org)}</span>' if org else "")
            + f'<span class="cnt">{len(ds)}</span></div><ul>{rows}</ul></div>')
    return (f'<details class="decs" data-col="결정사항"><summary>결정사항 {len(decisions)}개'
            f' <span class="ds">· {esc(summary)}</span></summary>{"".join(blocks)}</details>')


def _work_item(item: dict, roles: list[str]) -> str:
    """업무 묶음 하나 = 프로젝트 안의 카드 한 장.

    머리 띠(업무명 · 진행 상태 · 완료한 할 일 · 메일 수) 아래에 할 일 목록(역할 태그, 역할 순) ·
    결정사항 · 원본 대화를 둔다. 역할별 열로 나누면 쓰지 않는 역할 칸이 공백으로 남아 한 목록으로 둔다.
    원본 대화는 카드 안쪽 상자에 넣어, 대화 제목이 따로 된 업무처럼 읽히지 않게 한다.
    """
    threads = item["threads"]
    merged = len(threads) > 1              # 여러 대화가 묶였을 때만 업무마다 출처 제목을 보인다
    mails = sum(th["count"] for th in threads)
    extracted = len(item["pending"]) + len(item["excluded"]) < len(threads)

    tasks = [t for role in roles for t in item["roles"].get(role, [])]
    shown = sorted((t for t in tasks if not dropped_task(t)),
                   key=lambda t: state_label(t) != "open")   # 남은 일 먼저, 완료는 아래로
    gone = [t for t in tasks if dropped_task(t)]

    if extracted and item["status"]:
        chip = (f'<span class="stt {THREAD_STATUS.get(item["status"], "s-ing")}">'
                f'{esc(item["status"])}</span>')
    elif not extracted and item["pending"]:
        chip = '<span class="wchip">정리 대기</span>'
    else:
        chip = ""
    info = []
    if extracted and shown:     # 완료 / 전체는 표시할 때마다 페이지가 다시 센다 (data-wdone · data-wtotal)
        info.append(f'완료 <b class="num" data-wdone>{sum(state_label(t) == "done" for t in shown)}</b>'
                    f'/<span data-wtotal>{len(shown)}</span>')
    info.append(f'대화 {len(threads)}개 · 메일 {mails}통' if merged else f'메일 {mails}통')
    parts = [
        f'<div class="work-h"><h4 class="wt">{esc(item["title"])}</h4>'
        f'{"<span class=new>NEW</span>" if item["new"] else ""}{chip}'
        f'<span class="wm">{" · ".join(info)}</span></div>'
    ]

    if extracted and not shown:
        parts.append('<p class="no-task">남은 할 일 없음</p>')
    elif extracted:
        parts.append('<ul class="tasks">'
                     + "".join(_task(t, show_src=merged) for t in shown) + "</ul>")
    if extracted and gone:
        parts.append(f'<details class="gone"><summary>제외·삭제한 할 일 {len(gone)}개</summary>'
                     '<ul class="tasks">'
                     + "".join(_task(t, show_src=merged) for t in gone) + "</ul></details>")
    if extracted and item["decisions"]:
        parts.append(_decisions(item["decisions"], merged))

    if item["pending"]:
        rows = "".join(_thread_row(th) for th in item["pending"])
        if extracted:
            parts.append(f'<details class="pend"><summary>추출 대기 대화 {len(item["pending"])}개'
                         f'</summary><ul class="thr-list">{rows}</ul></details>')
        else:
            parts.append(f'<div class="pend-only"><p class="pend-note">업무 추출 대기 — '
                         f'원본 대화 {len(item["pending"])}개</p><ul class="thr-list">{rows}</ul></div>')

    if item["excluded"]:
        rows = "".join(_thread_row(th) for th in item["excluded"])
        parts.append(f'<details class="excl"><summary>정기 메일 — 추출 안 함 · 대화 '
                     f'{len(item["excluded"])}개</summary><ul class="thr-list">{rows}</ul></details>')

    done = [th for th in threads if th["extracted"]]
    if done:
        rows = "".join(_thread_row(th, f'<span>{esc(th["status"])}</span>' if th["status"] else "")
                       for th in done)
        parts.append(f'<details class="rel"><summary>메일 대화 {len(done)}개 · 요약</summary>'
                     f'<ul class="thr-list">{rows}</ul></details>')
    return f'<section class="work" data-work="{esc(item["title"])}">{"".join(parts)}</section>'


KEBAB = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
         '<circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/>'
         '<circle cx="8" cy="13" r="1.5"/></svg>')
WAIT_MENU = (("deleted", "삭제"),)      # 답변완료는 줄에 바로 보이는 버튼으로
# 상태에 맞는 것만 보인다 (진행 중: 완료 · 삭제 / 완료 · 삭제: 되돌리기)
PROJECT_MENU = (("done", "완료"), ("not_mine", "내 일 아님"),
                ("deleted", "삭제"), ("open", "자동 분류로 되돌리기"))
COLOR_ITEM = '<button type="button" role="menuitem" data-color-open>색 바꾸기…</button>'   # 프로젝트 ⋮ 맨 위


def _kebab(items, attr: str, label: str, first: str = "") -> str:
    """세로 ⋮ 메뉴. 메뉴 칸은 스크롤 영역에 잘리지 않도록 페이지가 버튼 옆에 띄운다. first 는 맨 위에 둘 항목."""
    buttons = first + "".join(f'<button type="button" role="menuitem" data-{attr}="{k}">{esc(t)}</button>'
                              for k, t in items)
    return (f'<span class="kb"><button type="button" class="kb-btn" aria-haspopup="menu" '
            f'aria-expanded="false" aria-label="{esc(label)}" title="메뉴">{KEBAB}</button>'
            f'<span class="kb-menu" role="menu" hidden>{buttons}</span></span>')


def _mark_text(state: dict | None, target: str) -> str:
    """'09/17 답변완료', '09/17 완료', '삭제 · 09/17'."""
    if not state:
        return ""
    day = _kst_day(state["labeled_at"])
    when = f"{day:%m/%d}" if day else ""
    name = MARKS[target][state["label"]]
    return " · ".join(x for x in (name, when) if x) if state["label"] == "deleted" \
        else f"{when} {name}".strip()


def _project_attrs(p: dict) -> str:
    """프로젝트 카드 · 진행 줄이 함께 쓰는 표시 상태. 버튼을 누르면 페이지가 둘을 같이 고친다."""
    return (f'data-pstate="{p["pstate"]}" data-latest="{esc(p["received_at"])}" '
            f'data-manual-state="{esc((p["mark"] or {}).get("label", ""))}" data-ready="{int(p["ready"])}" '
            f'data-at="{esc((p["mark"] or {}).get("labeled_at", ""))}" '
            f'data-order="{p.get("order", 0)}"')


def _project_card(p: dict, roles: list[str]) -> str:
    """접고 펴는 프로젝트 한 칸. 머리줄에 이름 · 완료한 할 일 · 최근 메일 날짜.

    완료 / 전체로 적는다 (오늘 탭 프로젝트별 진행 막대와 같은 뜻). 전체에서 제외 · 업무삭제한 일은 뺀다.
    프로젝트는 상자로 감싸지 않는다 — 펼치면 머리줄 색 막대가 세로줄로 이어져 업무 상자들을 묶는다 (--pc).
    """
    meta = (f'업무 {len(p["items"])} · 완료 '
            f'<b class="num" data-done="{esc(p["name"])}">{p["progress"]["total"] - p["progress"]["open"]}</b>/'
            f'<span data-ptotal>{p["progress"]["total"]}</span> · '
            f'결정 {p["decision_count"]} · 대화 {len(p["threads"])}')
    if p["latest"]:
        meta += f' · 최근 <span class="num">{p["latest"].astimezone(KST):%m/%d}</span>'
    head = (
        # 색 막대는 색 고르는 버튼 (⋮ '색 바꾸기…' 도 같은 창). data-color 는 고른 색('' = 자동), data-auto 는 자동 색 칸
        f'<summary class="proj-h"><button type="button" class="bar s{p["slot"]}" data-color="{esc(p.get("color", ""))}" '
        f'data-auto="{p.get("auto_slot", 0)}" aria-haspopup="dialog" aria-expanded="false" '
        f'aria-label="{esc(p["name"])} 색 바꾸기" title="색 바꾸기"></button>'
        f'<h3>{esc(p["name"])}</h3>{"<span class=new>NEW</span>" if p["new"] else ""}'
        f'<span class="pm">{meta}</span>'
        f'<span class="st">{esc(_mark_text(p["mark"], "project"))}</span>'
        f'{_kebab(PROJECT_MENU, "pmark", p["name"] + " 메뉴", COLOR_ITEM)}</summary>'
    )
    body = "".join(_work_item(i, roles) for i in p["items"])
    hidden = "" if p["pstate"] == "open" else " hidden"
    return (f'<details class="proj" data-project="{esc(p["name"])}" {_project_attrs(p)} '
            f'style="--pc:var(--s{p["slot"]})"{hidden}>{head}<div class="proj-b">{body}</div></details>')


def _projects_tab(m: dict) -> str:
    """프로젝트 탭: 진행 중 · 완료 · 삭제 보기 + 고객사별 묶음 + 접고 펴는 프로젝트."""
    projects, s = m["projects"], m["stats"]
    if not projects:
        return '<div class="pad"><p class="empty-state">스레드가 없습니다.</p></div>'
    views = [
        ("open", "진행 중", "남은 할 일이 없으면 완료로, 마감이 지난 할 일이 있으면 지연으로 자동 분류됩니다. 업무 정리 전인 프로젝트는 진행 중에 표시됩니다.",
         "진행 중인 프로젝트가 없습니다."),
        ("done", "완료", "남은 할 일이 없는 프로젝트입니다. 새 할 일이 생기거나 완료를 되돌리면 진행 중 또는 지연으로 이동합니다.",
         "완료한 프로젝트가 없습니다."),
        ("delayed", "지연", "마감이 지난 미완료 업무가 있는 프로젝트입니다. 해당 업무를 처리하거나 기한을 변경하면 자동으로 다시 분류됩니다.",
         "지연된 프로젝트가 없습니다."),
        ("not_mine", "내 일 아님", "내 담당이 아닌 프로젝트입니다. 오늘·일정의 내 할 일에서 제외하며, 자동 분류로 되돌리면 다시 표시됩니다. 새 메일이 와도 이 상태를 유지합니다.",
         "내 일 아님으로 표시한 프로젝트가 없습니다."),
        ("deleted", "삭제", "대시보드에서만 숨깁니다. 메일은 지우지 않고, 새 메일이 와도 숨겨 둡니다.",
         "삭제한 프로젝트가 없습니다."),
    ]
    seg = "".join(
        f'<button type="button" data-pview="{k}" data-note="{esc(note)}" data-empty="{esc(empty)}" '
        f'aria-pressed="{"true" if k == "open" else "false"}">{label} '
        f'<b class="num" id="pcnt-{k}">{s[f"projects_{k}"]}</b></button>'
        for k, label, note, empty in views)
    groups: list[str] = []
    clients = list(dict.fromkeys(p["client"] for p in projects))
    for band, client in ((band, client) for band in (0, 1) for client in clients):
        ps = [p for p in projects if p["client"] == client
              and int(not (p["progress"]["open"] and p["pstate"] in ("open", "delayed"))) == band]
        shown = sum(p["pstate"] == "open" for p in ps)
        cards = "".join(_project_card(p, m["roles"]) for p in ps)
        groups.append(
            f'<section class="client" data-client="{esc(client)}" data-band="{band}"{"" if shown else " hidden"}>'
            # 색은 세부 프로젝트마다라 고객사 머리 네모는 중립 회색
            '<div class="client-h"><i class="sw s0" aria-hidden="true"></i>'
            f'<h2>{esc(client or OTHER_CLIENT)}</h2><span class="cnt num" data-ccnt>{shown}</span>'
            f'</div><div class="projs">{cards}</div></section>')
    return (
        f'<div class="pad">{_recent_decisions(m)}<div class="ptools">'
        f'<div class="seg" role="group" aria-label="프로젝트 보기">{seg}</div>'
        f'<p class="note" id="pview-note">{esc(views[0][2])}</p></div>'
        f'<div class="pgroups">{"".join(groups)}</div>'
        f'<p class="empty-state" id="pempty"{" hidden" if s["projects_open"] else ""}>'
        f'{esc(views[0][3])}</p></div>'
    )


# ── 머리 요약 · 오늘 탭 (할 일 · 진행) · 답장 대기 탭 · 최근 결정사항(프로젝트 탭 맨 위) ──
def _next_label(m: dict) -> str:
    """다음 자동 정리 시각: 오늘이면 '17:00', 아니면 '내일 09:00'."""
    nxt = m["next_extract"]
    return f"{nxt:%H:%M}" if nxt.date() == m["generated_at"].astimezone(KST).date() else f"내일 {nxt:%H:%M}"


def _hero(m: dict) -> str:
    s = m["stats"]
    wait = f'답 기다리는 메일 <em class="c-warn"><span id="hl-wait">{s["unanswered"]}</span>건</em>'
    title = (f'처리할 일이 <em class="c-acc"><span id="hl-me">{s["my_open"]}</span>건</em>, {wait}'
             if s["extracted"] else f'업무 정리 전 · {wait}')
    d = m["today"]
    local = m["generated_at"].astimezone(KST)
    return (
        '<header class="hero"><div class="hero-t">'
        f'<div class="brand-identity">{MAILWORK_LOGO}<div class="brand-account">'
        f'<p class="sub brand-email" title="생성 {local:%Y-%m-%d %H:%M}">{esc(m["me"])}</p>'
        f'<p class="brand-date"><time datetime="{d:%Y-%m-%d}">{d:%m/%d} {WEEKDAYS[d.weekday()]}</time></p>'
        '</div></div>'
        f'<h1 class="sr-only">{title}</h1>'
        '</div>'
        f'<div class="hero-acts">{_usage_link(m)}{_rules_link()}'
        '<button type="button" id="theme" class="pill" aria-label="테마 전환">테마: 시스템</button>'
        '</div></header>'
    )


def _usage_link(m: dict) -> str:
    """테마 버튼 옆에 놓이는 토큰 사용 현황 버튼. 누르면 따로 만든 페이지로 간다.

    버튼에 오늘 쓴 양과 어제보다 얼마나 늘었는지를 같이 적어, 들어가 보지 않아도 대충 알 수 있게 한다.
    """
    u = m["usage"]
    if not u["has_data"]:
        chip = '<span class="pill-n">기록 전</span>'
    else:
        d = u["delta"]["today"]
        chip = (f'<span class="pill-n num">오늘 {_tok(u["today"]["tokens"])}</span>'
                + (f'<span class="pill-d {d["dir"]}">{_arrow(d)}{_tok(abs(d["tokens"]))}</span>'
                   if d["dir"] in ("up", "down") else ""))
    return (f'<a class="pill usage-link" href="{USAGE_PAGE}">토큰 사용 현황{chip}'
            '<span class="pill-go" aria-hidden="true"></span></a>')


def _rules_link() -> str:
    return f'<a class="pill rules-link" href="{RULES_PAGE}">메일 분류 규칙</a>'


def _kpis(m: dict) -> str:
    s = m["stats"]
    todo = s["threads"] - s["excluded"]
    left = max(0, todo - s["extracted"])
    if s["extracted"]:
        value = f'<b class="num" id="kpi-me">{s["my_open"]}</b><span class="u">건 남음</span>'
        caption = (f'<span>지난 마감 <span id="kpi-over">{s["overdue"]}</span></span>'
                   f'<span>오늘 마감 <span id="kpi-today">{s["due_today"]}</span></span>'
                   f'<span>오늘 완료 <span id="kpi-done">{s["done_today"]}</span></span>')
    else:
        value = '<b class="num">—</b>'
        caption = '업무 정리 대기'
    wait_caption = (f'가장 오래된 건 {s["oldest_wait"]}일 경과' if s["oldest_wait"] else
                    '모두 오늘 온 메일' if s["unanswered"] else '모두 답했습니다')
    warn = " c-warn" if s["unanswered"] else ""
    local = m["generated_at"].astimezone(KST)
    nxt = m["next_extract"]
    nxt_label = f"{nxt:%H:%M}" if nxt.date() == local.date() else f"내일 {nxt:%H:%M}"
    sync = fmt_dt(m["last_sync"]) if m["last_sync"] else "없음"
    ext = fmt_dt(m["last_extract"]) if m.get("last_extract") else "없음"
    summary = f"남은 {left}건은 다음 정리 때" if left else "모두 정리됨"
    excluded = f' · 정기 메일 {s["excluded"]}건 제외' if s["excluded"] else ""
    return (
        '<section class="digest-overview" aria-label="메일 업무 요약"><div class="work-overview">'
        '<div class="kpis"><div class="kpi"><h2 class="l">내 할 일</h2>'
        f'<p class="v">{value}</p><p class="c deadline-counts">{caption}</p></div>'
        '<div class="kpi"><h2 class="l">답장 대기</h2>'
        f'<p class="v"><b class="num{warn}" id="kpi-wait">{s["unanswered"]}</b><span class="u">건 대기</span></p>'
        f'<p class="c" id="kpi-wait-c">{wait_caption}</p></div></div>'
        f'<p class="project-summary">진행 프로젝트 <b class="num" id="kpi-proj">{s["named_projects"]}</b>개'
        f' · 새로 온 대화 <b class="num">{s["new"]}건</b></p></div>'
        '<aside class="system-summary" aria-labelledby="system-summary-title">'
        '<h2 id="system-summary-title">자동 정리</h2><dl>'
        f'<div><dt>동기화</dt><dd class="num">{esc(sync)}</dd></div>'
        f'<div><dt>정리</dt><dd>마지막 <b class="num">{esc(ext)}</b> · 다음 <b class="num">{esc(nxt_label)}</b></dd></div>'
        f'<div><dt>요약</dt><dd><span class="num">{s["extracted"]}<span class="den">/{todo}</span></span> · {summary}</dd></div>'
        f'<div><dt>범위</dt><dd>최근 {m["window_days"]}일 대화 {s["threads"]}건{excluded}</dd></div>'
        '</dl></aside></section>'
    )


def _status_text(state: dict | None) -> str:
    if not state:
        return ""
    day = _kst_day(state["labeled_at"])
    when = f"{day:%m/%d}" if day else ""
    if state["label"] == "done":
        return f"{when} 완료"
    return " · ".join(x for x in (LABELS[state["label"]], state.get("reason"), when) if x)


RELATED_PREVIEW = 4                     # 대화 카드 '다른 담당자' 를 펼쳤을 때 보일 업무 수
SAME_TASK_MIN = 15                      # 다른 대화의 같은 할 일로 합칠 최소 글자 수 (띄어쓰기 뺀). '확인 후 회신' 류는 안 합친다
THREAD_STATUS = {"진행중": "s-ing", "보류": "s-hold", "완료": "s-done"}


def _my_names(threads: list[dict]) -> list[str]:
    """내가 보낸 메일의 이름('홍길동 선임' → '홍길동'). 업무 담당자가 나인지 가리는 데 쓴다."""
    return sorted({key for t in threads for m in t["mails"]
                   if m.get("is_mine") and (key := person_key(m.get("from_name")))})


def _day_label(raw: str) -> str:
    """'2026-09-15' → '9/15(화)'. 날짜 꼴이 아니면 적힌 그대로."""
    try:
        d = date.fromisoformat((raw or "")[:10])
    except ValueError:
        return raw or ""
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def work_ratio(a: str, b: str) -> float:
    """두 업무 문구가 얼마나 같은가: 띄어쓰기를 뺀 두 글자 조각의 짧은 쪽 기준 겹침 비율."""
    def grams(s: str) -> set[str]:
        s = "".join(s.split()).lower()
        return {s[i:i + 2] for i in range(len(s) - 1)}
    x, y = grams(a), grams(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


def _same_work(a: str, b: str) -> bool:
    """사실상 같은 일인가.

    업무 정리가 내 할 일을 남의 업무로 한 번 더 적는 경우가 있어, 카드의 다른 담당자 줄에서 뺀다.
    """
    return work_ratio(a, b) >= SAME_WORK_MIN


def _act_context(m: dict) -> dict[str, dict]:
    """대화별 맥락: 이름이 적힌 남의 남은 업무(나는 빼고) · 받는사람으로 온 메일의 답장 대기 일수.

    담당자 이름이 없는 업무는 대개 우리 쪽 일이라 내 할 일과 겹치므로 넣지 않는다 (프로젝트 탭에는 있음).
    """
    mine = set(m.get("me_names") or ())
    ctx: dict[str, dict] = {}
    for p in m["projects"]:
        for item in p["items"]:
            for tasks in item["roles"].values():
                for t in tasks:
                    who = person_key(t.get("assignee"))
                    if who and who not in mine and open_task(t):
                        ctx.setdefault(t["thread"]["key"], {}).setdefault("others", []).append(t)
    for u in m["unanswered"]:
        ctx.setdefault(u["thread"]["key"], {})["waited"] = u["waited"]
    return ctx


def _mail_line(a: dict, ctx: dict, tip: str = "tip") -> str:
    """출처 메일 한 줄: 마지막 보낸 사람(이름만) · 시각 · 통수 · 답장 대기.

    제목은 길고 말머리가 많아 줄에 늘어놓지 않고, 마우스를 올리면(키보드는 포커스) 말풍선으로 띄운다.
    같은 할 일이 다른 탭에도 있으면 tip 으로 말풍선 id 가 겹치지 않게 한다.
    """
    th = a["thread"]
    if not th.get("subject"):
        return '<span class="mail"></span>'
    bits = " · ".join(x for x in (
        person_key(th.get("from")), fmt_dt(th["last_at"]) if th.get("last_at") else "",
        f'메일 {th["count"]}통' if th.get("count") else "") if x)
    waited = ctx.get("waited")
    wait = ("" if waited is None else
            f'<b class="rep">{f"답장 대기 {waited}일" if waited else "오늘 받음 · 답장 전"}</b>')
    tip = f'{tip}-{a["id"]}'
    return (f'<span class="mail" tabindex="0" aria-describedby="{tip}">'
            f'<i class="ic" aria-hidden="true">✉</i><span class="mi">{esc(bits or "원본 메일")}</span>'
            f'{wait}<span class="tip" role="tooltip" id="{tip}"><span class="tip-l">메일 제목</span>'
            f'{esc(th["subject"])}</span></span>')


MANUAL_TAG = '<span class="man" title="일정 탭에서 직접 정한 기한">직접 정함</span>'


def _due_text(d: dict) -> str:
    """'2일 지남 · 9/15(화)' · '오늘 마감 · 9/17(목)' · 'D-3 · 9/20(일)'. 페이지 JS 의 dueInfo 와 같은 모양."""
    if d["level"] == "none":
        return ""
    due = d["label"] + (" 마감" if d["level"] == "today" else "")
    if (day := _day_label(d["raw"])) != d["label"]:
        due += f" · {day}"
    return due


def _act(a: dict, ctx: dict | None = None) -> str:
    """완료 · 제외·삭제 목록의 할 일 카드. 위에서부터 한눈에:
    긴급도 · 기한(날짜) · NEW/직접 · 프로젝트 / 할 일 / 출처 메일(제목은 말풍선) + 버튼.

    상태는 data-state 하나로 두고, 버튼을 누르면 페이지가 카드를 해당 목록으로 옮긴다.
    남은 내 할 일은 이 카드 대신 대화 카드 안의 한 줄(_act_row)로 보인다. 그래서 카드가 남은 일이 되면
    페이지가 빼 두고(parked), 현황 · 다른 담당자는 대화 카드에만 그린다.
    """
    th, d = a["thread"], a["deadline"]
    here = (ctx or {}).get(th["key"], {})
    if a.get("kind") == "task":
        tags = [_role_tag(a)]
        if a.get("assignee"):
            tags.append(f'<span class="tag">{esc(" · ".join(_names(a["assignee"])))}</span>')
    else:
        glyph, label = URG_LABEL[a["urgency"]]
        tags = [f'<span class="tag u-{a["urgency"]}"><i aria-hidden="true">{glyph}</i>'
                f'{label}</span>']
    # 기한이 없어도 자리는 둔다 (일정 탭에서 기한을 정하면 페이지가 채운다)
    tags.append(f'<span class="due lv-{d["level"]}">{esc(_due_text(d))}</span>{MANUAL_TAG}')
    tags.append(_badges(th))
    tags.append(f'<span class="pj"><i class="sw s{a["slot"]}" aria-hidden="true"></i>'
                f'{esc(a["project"])}</span>')
    tags.append(_repeat_tag(a))
    return (
        '<li class="act" ' + " ".join(f'{k}="{esc(v)}"' for k, v in _state_attrs(a).items()) + ">"
        f'<div class="tg">{"".join(tags)}</div><p class="t">{esc(a["text"])}</p>'
        f'<div class="tf">{_mail_line(a, here)}{_controls(a)}</div></li>'
    )


def _merge_same(acts: list[dict]) -> list[dict]:
    """남은 내 할 일을 오늘 탭 줄 단위로: 같은 프로젝트에 띄어쓰기만 다른 같은 문구가 또 있으면 한 줄로.

    업무 정리가 한 요청을 이어진 두 대화에서 한 번씩 뽑는 경우가 있다. 앞(마감이 급한) 쪽에 남기고
    뒤의 할 일은 dups 로 붙인다. 그 줄을 표시하면 페이지가 붙은 할 일도 같은 표시로 함께 기록한다.
    """
    rows: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for a in acts:
        key = (a.get("project", ""), "".join(a["text"].split()).lower())
        if len(key[1]) >= SAME_TASK_MIN and key in seen:
            seen[key]["dups"].append(a)
            continue
        row = {"act": a, "dups": []}
        seen.setdefault(key, row)
        rows.append(row)
    return rows


def _conversations(rows: list[dict]) -> list[dict]:
    """줄을 대화(스레드)별로 묶는다. 대화 순서는 가장 급한 할 일 순 (my_actions 가 마감일순이라 처음 나온 순서)."""
    convs: dict[str, dict] = {}
    for r in rows:
        a = r["act"]
        convs.setdefault(a["thread"]["key"], {"thread": a["thread"], "project": a.get("project", ""),
                                              "slot": a.get("slot", 0), "rows": []})["rows"].append(r)
    return list(convs.values())


def _act_title(th: dict) -> str:
    """대화 카드 제목: 메일 제목에서 회신 표식 · 앞머리 [태그] · 날짜 꼬리 · '의 건' 을 걷어낸 것.

    업무명 묶기(work_title)의 날짜 꼬리는 6~8자리만 떼므로 '_202601001' 같은 긴 관리번호는 여기서 먼저 뗀다
    (묶기 규칙은 그대로 둔다).
    """
    subject = re.sub(r"\s*_\s*\d{6,}\s*$", "", th.get("subject") or "")
    return work_title(subject) or th.get("subject") or "(제목 없음)"


def _act_row(r: dict, order: int) -> str:
    """대화 카드 안의 내 할 일 한 줄: 할 일 / 기한 · 완료 · 제외 · 업무삭제.

    버튼은 프로젝트 탭 할 일과 똑같이 — 같은 테두리 단추를 그 줄 기한 칩과 같은 줄 오른쪽 끝에 늘 띄운다.

    표시해도 줄은 그 자리에 둔다 (취소선 · 상태 + 되돌리기). 완료 / 제외·삭제 목록에는 페이지가
    카드 틀(act-{id})을 복사해 올리고, 다음 갱신 때 서버가 그린 목록으로 바뀐다.
    버튼으로만 표시한다 — 문구를 긁어 쓰다가(IP · 주소) 체크가 눌리는 일이 없도록 체크 상자는 두지 않는다.
    """
    a = r["act"]
    d = a["deadline"]
    attrs = {**_state_attrs(a), "data-order": str(order), "data-new": "1" if a["thread"].get("new") else "",
             "data-dups": " ".join(x["id"] for x in r["dups"])}
    dup = ""
    if r["dups"]:
        where = " / ".join(_act_title(x["thread"]) for x in r["dups"])
        dup = (f'<span class="dup" title="{esc(where)}">다른 대화 {len(r["dups"])}곳의 같은 할 일도 '
               '함께 표시됩니다</span>')
    tid = f'tkt-{a["id"]}'
    return (
        '<li class="tk" ' + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items()) + ">"
        f'<div class="tk-b"><p class="tk-t" id="{tid}">{esc(a["text"])}</p>{dup}{_repeat_tag(a)}</div>'
        f'<div class="tk-r"><span class="due lv-{d["level"]}">{esc(_due_text(d))}</span>{MANUAL_TAG}'
        f'<span class="st">{esc(_status_text(a.get("state")))}</span>'
        f'<span class="ctl"><button type="button" class="btn ok" data-act="done" '
        f'aria-describedby="{tid}">완료</button>'
        f'<button type="button" class="btn" data-act="excluded" aria-describedby="{tid}">제외</button>'
        f'<button type="button" class="btn" data-act="deleted" aria-describedby="{tid}">업무삭제</button>'
        f'<button type="button" class="btn undo" data-act="open" aria-describedby="{tid}">되돌리기'
        '</button></span></div></li>'
    )


def _cv_date(c: dict) -> str:
    """대화 줄 제목 위 날짜 칩: 기한 있는 할 일이 있으면(= '마감 있는 일' 칸) 가장 급한 마감일 · D-day 를
    기한 등급 색으로 ('10/16 금 · D-10'). 기한 없는 대화(= 프로젝트 묶음)는 빈 줄로 두고, 최근 메일 시각은 메일 줄에 있다.

    빈 줄도 늘 그려 둔다 — 넓은 화면에서 이 줄이 할 일 칸 위 머리 자리를 잡는다.
    할 일을 표시하거나 기한이 바뀌어 대화가 칸을 옮기면 페이지 JS(paintDate)가 같은 모양으로 다시 그린다.
    """
    dues = [r["act"]["deadline"] for r in c["rows"] if r["act"]["deadline"]["level"] != "none"]
    if not dues:
        return '<p class="cv-due"></p>'
    d = min(dues, key=lambda x: x["sort"])
    day = date.fromisoformat(iso) if (iso := iso_day(d["raw"])) else None
    big = f"{day.month}/{day.day} {WEEKDAYS[day.weekday()]}" if day else "기한"
    return (f'<p class="cv-due"><span class="cv-dc lv-{d["level"]}"><b class="num">{big}</b> · '
            f'{esc(d["label"])}</span></p>')


def _cv_others(c: dict, here: dict) -> str:
    """같은 대화에서 남이 맡은 남은 일. 접어 두고 머리에 건수 · 이름(소속)만."""
    texts = [r["act"]["text"] for r in c["rows"]]
    others = sorted((t for t in here.get("others", ()) if not any(_same_work(t["text"], x) for x in texts)),
                    key=lambda t: t["deadline"]["sort"])
    if not others:
        return ""
    # 담당자가 여럿이면('문지호, 배건우') 사람마다 이름만: '문지호 · 배건우'
    names = list(dict.fromkeys(person_key(n) + (f'({t["org"]})' if t.get("org") else "")
                               for t in others for n in _names(t.get("assignee"))))
    late = sum(t["deadline"]["level"] == "overdue" for t in others)
    head = (f'다른 담당자 {len(others)}건 · {esc(" · ".join(names[:2]))}{" 외" if len(names) > 2 else ""}'
            + (f' <b class="late">기한 지남 {late}</b>' if late else ""))
    lines = "".join(f'<p class="oth">{_who(t)}<span class="rx">{esc(t["text"])}</span>'
                    f'{_deadline(t["deadline"])}</p>' for t in others[:RELATED_PREVIEW])
    if len(others) > RELATED_PREVIEW:
        lines += f'<p class="oth more">외 {len(others) - RELATED_PREVIEW}건은 프로젝트 탭에서</p>'
    return (f'<details class="cv-oth"><summary><span class="chv" aria-hidden="true"></span>{head}</summary>'
            f'<div>{lines}</div></details>')


def _act_cv(c: dict, ctx: dict) -> str:
    """대화 줄(패널 안 한 줄): 날짜 칩(마감 있는 일) / 제목 · 배지 · 진행 상태 / 프로젝트 · 메일 줄 · 답장 대기 /
    현황 두 줄 / 내 할 일 줄 / 다른 담당자(접힘).

    진행 상태 칩은 제목 옆에 둔다 (프로젝트 탭 업무 카드와 같은 자리). 현황 문단 앞에 두면 첫 줄만 칩 폭만큼
    들여 쓰여 둘째 줄과 시작선이 어긋난다.

    프로젝트 이름은 늘 싣고, 프로젝트 묶음 안에서는 CSS 가 숨긴다 (머리줄에 있으므로). 메일 시각은 늘 보인다.
    페이지가 기한을 바꿔 대화를 칸 사이로 옮겨도 그대로 맞는다. 현황은 두 줄까지, 넘치면 페이지가 '더보기' 를 보인다.
    """
    th = c["thread"]
    here = ctx.get(th["key"], {})
    who, count = esc(person_key(th.get("from"))), f'메일 {th["count"]}통' if th.get("count") else ""
    at = esc(fmt_dt(th["last_at"])) if th.get("last_at") else ""
    # 숨기는 시각 앞뒤의 구분점도 같이 숨도록 span 안에 둔다
    bits = who + (f'<span class="cv-at">{" · " if who else ""}{at}{" · " if count and not who else ""}</span>'
                  if at else "") + (f'{" · " if who else ""}{count}' if count else "")
    waited = here.get("waited")
    meta = "".join((
        f'<span class="pj"><i class="sw s{c["slot"]}" aria-hidden="true"></i>{esc(c["project"])}</span>',
        f'<span class="cv-mail"><i aria-hidden="true">✉</i>{bits}</span>' if bits else "",
        "" if waited is None else
        f'<b class="rep">{f"답장 대기 {waited}일" if waited else "오늘 받음 · 답장 전"}</b>'))
    meta = f'<p class="cv-m">{meta}</p>' if meta else ""
    summary, status = "", th.get("status") or ""
    chip = f'<span class="stt {THREAD_STATUS.get(status, "s-ing")}">{esc(status)}</span>' if status else ""
    if body := th.get("summary") or th.get("snippet"):
        if th.get("summary"):
            label = preview = ""
        else:
            label = '<span class="cv-l">최근 메일</span>'
            preview = f' data-preview="{esc(th.get("preview") or body)}" data-preview-label="최근 메일 내용"'
        summary = (f'<div class="cv-s"><p class="tx"{preview}>{label}{esc(body)}</p>'
                   '<button type="button" class="cv-more" aria-expanded="false">더보기</button></div>')
    last = _kst_day(th.get("last_at"))
    attrs = {"data-thread": th["key"], "data-pg": c["project"], "data-slot": str(c["slot"]),
             "data-order": str(c.get("order", 99999)), "data-last": last.isoformat() if last else ""}
    return (
        '<article class="cv" ' + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items()) + ">"
        f'<div class="cv-b">{_cv_date(c)}'
        f'<div class="cv-h"><h3 title="{esc(th.get("subject") or "")}">{esc(_act_title(th))}'
        f'{_badges(th, direct=False)}{chip}</h3></div>'
        f'{meta}{summary}'
        f'<ul class="tks">{"".join(_act_row(r, r.get("order", 99999)) for r in c["rows"])}</ul>'
        f'{_cv_others(c, here)}<p class="cv-fin">이 대화의 할 일을 모두 정리했습니다</p></div></article>'
    )


def _act_group(name: str, convs: list[dict], ctx: dict) -> str:
    """기한 없는 대화의 프로젝트 묶음. 접고 펴기는 페이지가 브라우저에 기억한다 (처음에는 펼침).
    페이지 JS(makeGroup)도 같은 모양으로 만든다."""
    rows = sum(len(c["rows"]) for c in convs)
    return (f'<details class="apg" data-pg="{esc(name)}" open><summary><span class="chv" aria-hidden="true">'
            f'</span><i class="sw s{convs[0]["slot"]}" aria-hidden="true"></i><span class="apg-n">{esc(name)}'
            f'</span><span class="apg-c num">대화 {len(convs)} · 할 일 {rows}</span></summary>'
            f'<div class="cvs">{"".join(_act_cv(c, ctx) for c in convs)}</div></details>')


def _open_view(acts: list[dict], ctx: dict) -> str:
    """할 일 보기: 기한 있는 대화는 마감순으로 위에, 기한 없는 대화는 프로젝트별로 접어 둘 수 있게.

    대화에 기한 있는 할 일이 하나라도 있으면 대화 카드째 '마감 있는 일' 칸에 둔다.
    두 칸은 비어도 틀을 그려 둔다 (되돌리거나 기한을 바꾸면 페이지가 카드를 옮겨 넣는다).
    """
    convs = _conversations(_merge_same(acts))
    n = 0
    for i, c in enumerate(convs):
        c["order"] = i
        for r in c["rows"]:
            r["order"], n = n, n + 1
    dated = [c for c in convs if any(r["act"]["deadline"]["level"] != "none" for r in c["rows"])]
    groups: dict[str, list[dict]] = {}
    for c in convs:
        if c not in dated:
            groups.setdefault(c["project"], []).append(c)
    if UNCLASSIFIED in groups:                      # 미분류는 맨 아래
        groups[UNCLASSIFIED] = groups.pop(UNCLASSIFIED)
    over = sum(r["act"]["deadline"]["level"] == "overdue" for c in convs for r in c["rows"])
    due_n = sum(len(c["rows"]) for c in dated)
    return (
        f'<p class="acts-sum">대화 <b class="num" id="sum-cv">{len(convs)}</b>개 · 할 일 '
        f'<b class="num" id="sum-n">{n}</b>건<span class="over" id="sum-over"{"" if over else " hidden"}>'
        f' · 기한 지남 <b class="num" id="sum-over-n">{over}</b></span> · 줄 오른쪽 완료 버튼으로 표시합니다</p>'
        f'<section class="az" data-zone="due"{"" if dated else " hidden"}><h3 class="az-h"><span>마감 있는 일'
        f'</span><b class="az-n num">{due_n}</b></h3>'
        f'<div class="cvs">{"".join(_act_cv(c, ctx) for c in dated)}</div></section>'
        f'<section class="az" data-zone="rest"{"" if groups else " hidden"}><h3 class="az-h"><span>'
        f'기한 없는 일</span><b class="az-n num">{n - due_n}</b>'
        '<button type="button" class="az-fold">모두 접기</button></h3>'
        f'<div class="apgs">{"".join(_act_group(k, v, ctx) for k, v in groups.items())}</div></section>'
    )


def _act_templates(m: dict) -> str:
    """오늘 탭에 나중에 올릴 카드 · 대화 카드의 틀. <template> 이라 화면에 안 보이고 문서 검색에도 걸리지 않는다.

    act-{id}: 완료 / 제외·삭제 목록에 카드가 없는 할 일(남은 내 할 일 · 프로젝트 할 일)의 카드.
      어느 탭에서든 표시하면 페이지 JS 가 복사해 해당 목록에 넣는다.
    cv-{id}: 완료 / 제외·삭제한 내 할 일의 대화 카드(그 할 일 한 줄만). 되돌렸는데 할 일 보기에 그 줄이
      없으면 복사해 넣는다. 둘 다 다음 갱신 때 서버가 그린 것으로 바뀐다.
    """
    marked = [*m.get("done_actions", ()), *m.get("dropped_actions", ())]
    hidden = m.get("hidden_actions", ())
    carded = {a["id"] for a in marked}
    tasks = {a["id"]: a for a in [*m["my_actions"], *hidden]}
    tasks.update({t["id"]: t for p in m["projects"] for i in p["items"] for ts in i["roles"].values() for t in ts})
    tasks.update({r["id"]: r for r in m.get("schedule", ()) if r.get("kind") == "task"})
    ctx = _act_context(m)
    cards = "".join(f'<template id="act-{tid}">{_act(t)}</template>'
                    for tid, t in tasks.items() if tid not in carded)
    convs = "".join(
        f'<template id="cv-{a["id"]}">'
        f'{_act_cv(_conversations([{"act": a, "dups": []}])[0], ctx)}</template>'
        for a in [*marked, *hidden] if a.get("kind") != "task")
    return cards + convs


def _acts(m: dict) -> str:
    acts = m["my_actions"]
    if not m["stats"]["extracted"]:
        rows = "".join(_thread_row(th) for th in m["direct_threads"])
        return ('<div class="sec-h"><h2>나에게 직접 온 메일</h2>'
                f'<span class="sec-m">{len(m["direct_threads"])}건</span></div>'
                '<p class="note">업무 정리 전이라 나에게 직접 온 대화를 대신 보여줍니다. '
                '정리가 끝나면 이 자리에 할 일이 마감일순으로 정리됩니다.</p>'
                f'<ul class="thr-list box">{rows or "<li class=empty>없음</li>"}</ul>')
    ctx = _act_context(m)
    views = [
        ("open", "할 일", m["stats"]["my_open"], "남은 내 할 일이 없습니다.",
         "메일 대화별로 묶었습니다 · 기한 있는 일이 위, 기한 없는 일은 프로젝트별.\n"
         "줄 오른쪽 버튼으로 완료·제외·업무삭제. 표시 기록은 업무 정리 기준을 다듬는 데 씁니다."),
        ("done", "완료", len(m.get("done_actions", [])),
         f"최근 {LABELED_DAYS}일 동안 완료한 일이 없습니다.",
         f"최근 {LABELED_DAYS}일 · 최근에 완료한 순"),
        ("dropped", "제외·삭제", len(m.get("dropped_actions", [])),
         f"최근 {LABELED_DAYS}일 동안 제외하거나 삭제한 일이 없습니다.",
         f"최근 {LABELED_DAYS}일 · 최근에 표시한 순"),
    ]
    seg = "".join(
        f'<button type="button" data-view="{key}" data-note="{esc(note)}" '
        f'aria-pressed="{"true" if key == "open" else "false"}">'
        f'{label} <b class="num" id="cnt-{key}">{n}</b></button>'
        for key, label, n, _, note in views)
    marked = {"done": m.get("done_actions", []), "dropped": m.get("dropped_actions", [])}
    lists = (f'<div class="acts-open" data-list="open">{_open_view(acts, ctx)}</div>'
             f'<p class="empty-state" data-empty="open"{"" if not acts else " hidden"}>{views[0][3]}</p>'
             + "".join(f'<ul class="acts" data-list="{key}" hidden>'
                       + "".join(_act(a, ctx) for a in marked[key]) + "</ul>"
                       f'<p class="empty-state" data-empty="{key}" hidden>{empty}</p>'
                       for key, _, _, empty, _ in views[1:]))
    # 보기 설명은 제목 옆 (!) 에 마우스를 올리거나 포커스하면 말풍선으로 (줄바꿈 유지). 보기를 바꾸면 페이지가 문구를 바꾼다
    return (
        '<div class="sec-h has-hint"><div class="sec-t"><h2>내가 해야 할 일</h2>'
        '<span class="infotip" tabindex="0" aria-label="내가 해야 할 일 도움말" aria-describedby="acts-note">'
        '<span class="hi" aria-hidden="true">!</span>'
        f'<span class="tip" role="tooltip" id="acts-note">{esc(views[0][4])}</span></span></div>'
        f'<div class="seg" role="group" aria-label="내 할 일 보기">{seg}</div></div>'
        '<div class="banner" id="srv-note" hidden></div>'
        f'{lists}'
    )


def _progress(m: dict) -> str:
    """프로젝트별 할 일 완료 비율. 표시 기록(feedback.db)으로 채우고, 눌렀을 때 페이지가 고친다."""
    rows = []
    for p in m["projects"]:
        total = p["progress"]["total"]
        remaining = p["progress"]["open"]
        if p["name"] == UNCLASSIFIED and not total:
            continue
        rows.append(
            f'<div class="prow" data-goto="{esc(p["name"])}" {_project_attrs(p)}'
            f'{"" if p["pstate"] == "open" else " hidden"}>'
            '<button type="button" class="pgo" title="프로젝트 탭에서 보기">'
            f'<span class="pn"><span class="pname">{esc(p["name"])}'
            f'</span><span class="pc num">대화 {len(p["threads"])} · 결정 {p["decision_count"]}'
            '</span></span><span class="track" aria-hidden="true"><span class="fill" '
            f'style="width:{round((total - remaining) * 100 / total) if total else 0}%">'
            '</span></span>'
            f'<span class="pv num" data-total="{total}">'
            f'{f"{total - remaining}/{total}" if total else "할 일 없음"}'
            f'</span></button>{_kebab(PROJECT_MENU, "pmark", p["name"] + " 메뉴")}</div>')
    if not rows:
        return ""
    tabs = "".join(f'<button type="button" data-progress-view="{key}" aria-pressed="{str(key == "open").lower()}">'
                   f'{label} <b class="num" id="progress-cnt-{key}">{sum(p["pstate"] == key for p in m["projects"] if p["name"] != UNCLASSIFIED or p["progress"]["total"])}</b></button>'
                   for key, label in (("open", "진행 중"), ("done", "완료"), ("delayed", "지연"),
                                      ("not_mine", "내 일 아님")))
    return ('<div class="sec-h gap"><h2>프로젝트별 진행</h2>'
            '<span class="sec-m">완료한 할 일 / 전체</span></div>'
            f'<div class="seg progress-tabs" role="group" aria-label="프로젝트별 진행 보기">{tabs}</div>'
            f'<div class="progs">{"".join(rows)}</div>'
            f'<p class="empty-state" id="progs-empty"{" hidden" if m["stats"]["projects_open"] else ""}>'
            '진행 중인 프로젝트가 없습니다.</p>')


def _wait(u: dict, extra: bool = False, tasks: int = 0) -> str:
    """답장 대기 탭 한 줄: 경과일 배지 | 보낸 사람 · 소속 · 프로젝트 | 요약 두 줄 | 받은 시각 · 할 일 N건 → |
    답변완료(마우스를 올리면 보임) | ⋮(삭제). 좁은 화면에서는 위아래로 쌓는다. 표시한 줄은 상태 + 되돌리기.

    요약에 마우스를 올리면 나에게 온 그 메일의 본문을 띄운다. 배지 색은 오늘 · 1일 · 2일 이상 세 단계.
    할 일 N건은 같은 대화의 남은 내 할 일 줄 수로, 누르면 오늘 탭의 그 대화 카드로 옮겨 간다 (페이지가 다시 센다).
    """
    th, waited, mark = u["thread"], u["waited"], u.get("mark")
    name, org, project = u.get("name") or th["from"], u.get("org") or "", th["project"]
    if org and org in project:                  # '오로라 · 오로라 운영(기타)' → '오로라 운영(기타)'
        org = ""
    sub = " · ".join(x for x in (org, project) if x and x != UNCLASSIFIED and x != name)
    attrs = {"data-thread": th["key"], "data-asked": u["asked"], "data-waited": str(waited),
             "data-lv": str(min(waited, 2)), "data-title": th["subject"],
             "data-wstate": mark["label"] if mark else "open",
             "data-at": mark["labeled_at"] if mark else ""}
    age = (f'<b class="num">{waited}</b><small>일</small><span class="sr-only"> 경과</span>' if waited
           else '<b>오늘</b><span class="sr-only"> 받음</span>')
    text = th["summary"] or th["subject"]
    preview = (f' data-preview="{esc(u["body"])}" data-preview-label="받은 메일"' if u.get("body") else "")
    return (
        f'<li class="wait{" old" if waited >= 2 else ""}{" extra" if extra else ""}" '
        + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items())
        + f'{" hidden" if extra else ""}><span class="wage">{age}</span><div class="wb">'
        f'<div class="wh"><b class="wn" title="{esc(th["from"])}">{esc(name)}</b>'
        f'<span class="wo">{esc(sub)}</span></div>'
        f'<p class="ws"{preview}>{esc(text)}</p>'
        f'<div class="wx"><span class="wt num">{esc(fmt_dt(u["asked"]))} 받음</span>'
        f'<button type="button" class="wgo"{"" if tasks else " hidden"}>할 일 <b class="num">{tasks}</b>건 →'
        f'</button><span class="st">{esc(_mark_text(mark, "thread"))}</span>'
        '<button type="button" class="btn ok wok" data-wmark="answered">답변완료</button>'
        '<button type="button" class="btn undo" data-wmark="open">되돌리기</button></div>'
        f'{_kebab(WAIT_MENU, "wmark", "메일 메뉴")}</div></li>')


def _wait_tasks(m: dict) -> dict[str, int]:
    """대화별 남은 내 할 일 줄 수 (오늘 탭 할 일 보기에 그려지는 줄과 같게 센다)."""
    if not m["stats"]["extracted"]:
        return {}
    return {c["thread"]["key"]: len(c["rows"]) for c in _conversations(_merge_same(m["my_actions"]))}


def _waiting(m: dict) -> str:
    items, handled = m["unanswered"], m.get("handled", [])
    head = ('<div class="sec-h"><h2>답을 기다리는 메일</h2>'
            '<span class="sec-m">받는사람(TO) 기준 · '
            f'<b class="c-warn num" id="wait-cnt">{len(items)}</b></span></div>'
            f'<p class="note">받는사람(To)으로 받은 최근 {m["window_days"]}일 메일 중 그 뒤로 답하지 않은 대화 · '
            '오래 기다린 순. 요약에 마우스를 올리면 받은 메일 원문이 보입니다.</p>')
    tasks = _wait_tasks(m)
    rows = "".join(_wait(u, i >= WAIT_PREVIEW, tasks.get(u["thread"]["key"], 0))
                   for i, u in enumerate(items))
    more = (f'<button type="button" class="more" data-more="waits" aria-expanded="false"'
            f'{"" if len(items) > WAIT_PREVIEW else " hidden"}>'
            f'나머지 {max(len(items) - WAIT_PREVIEW, 0)}건 보기</button>')
    return (
        head + f'<p class="empty-state" id="wait-empty"{" hidden" if items else ""}>'
        '받는사람으로 온 메일에 모두 답했습니다.</p>'
        f'<ul class="waits" id="waits">{rows}</ul>{more}'
        f'<details class="handled" id="handled"{"" if handled else " hidden"}>'
        f'<summary>답변완료·삭제한 메일 <b class="num" id="handled-cnt">{len(handled)}</b>건</summary>'
        f'<ul class="waits" id="handled-list">{"".join(_wait(u) for u in handled)}</ul></details>'
    )


def _recent_decisions(m: dict) -> str:
    ds = m["recent_decisions"]
    if not ds:
        return ""
    rows = "".join(
        f'<li class="rd"><span class="av" aria-hidden="true">{esc(person_key(d["by"])[:2] or "?")}'
        f'</span><div><p class="t">{esc(d["text"])}</p><p class="rm">'
        f'{esc(d["by"] + " 결정" if d["by"] else "결정자 미기재")} · '
        f'{esc(d["date"][5:].replace("-", "/") if d["date"] else "일자 미확인")} · '
        f'{esc(d["project"])}</p></div></li>'
        for d in ds)
    return ('<section class="recent-decs" aria-labelledby="rd-h"><div class="sec-h">'
            '<h2 id="rd-h">최근 결정사항</h2><span class="sec-m">프로젝트별 전체는 아래 각 업무 묶음에</span></div>'
            f'<ul class="rds">{rows}</ul></section>')


def _today_tab(m: dict) -> str:
    """오늘 탭은 할 일 · 프로젝트별 진행만 폭 전체로. 답을 기다리는 메일은 답장 대기 탭, 결정사항은 프로젝트 탭."""
    return f'<div class="today">{_acts(m)}{_progress(m)}</div>'


# ── 일정 탭 (마감 달력 · 기한 직접 정하기) ─────────────────────────────────────
def _sit(a: dict, today: date, order: int, shown: bool) -> str:
    """일정 탭 할 일 한 줄: 긴급도(프로젝트 할 일은 역할 · 담당자) · 기한 · 프로젝트 / 할 일 / 기한 정하기 / 버튼.

    달력 칸의 짧은 표시는 페이지 JS 가 이 줄의 data-* 로 그린다. 기한 · 표시가 바뀌면 줄은 그대로 두고
    data-* 만 고친 뒤 달력과 목록을 다시 그린다.
    """
    d, th = a["deadline"], a["thread"]
    if a.get("kind") == "task":
        tags = [_role_tag(a)] + ([f'<span class="tag">{esc(" · ".join(_names(a["assignee"])))}</span>']
                                 if a.get("assignee") else [])
    else:
        glyph, label = URG_LABEL[a["urgency"]]
        tags = [f'<span class="tag u-{a["urgency"]}"><i aria-hidden="true">{glyph}</i>{label}</span>']
    tags += [f'<span class="due lv-{d["level"]}">{esc(_due_text(d))}</span>{MANUAL_TAG}', _badges(th),
             f'<span class="pj"><i class="sw s{a["slot"]}" aria-hidden="true"></i>{esc(a["project"])}</span>']
    mail = d.get("mail", "")
    src = (f"메일에 적힌 기한 {_day_label(mail)}" if iso_day(mail)
           else f"메일에 적힌 기한: {mail}" if mail else "메일에 적힌 기한 없음")
    edit = (
        f'<div class="sdue"><label class="due-l">기한<input type="date" class="due-in" '
        f'value="{iso_day(d["raw"])}" min="{today.year - DUE_YEARS}-01-01" '
        f'max="{today.year + DUE_YEARS}-12-31"></label>'
        '<button type="button" class="btn due-rev" data-due-rev>메일 기한으로</button>'
        f'<span class="due-src">{esc(src)}</span></div>')
    attrs = {**_state_attrs(a), "data-slot": str(a["slot"]), "data-sorder": str(order)}
    return (
        f'<li class="sit{" done" if state_label(a) == "done" else ""}" '
        + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items()) + ("" if shown else " hidden")
        + f'><div class="tg">{"".join(tags)}</div><p class="t">{esc(a["text"])}</p>{edit}'
        f'<div class="tf">{_mail_line(a, {}, "stip")}{_controls(a)}</div></li>'
    )


def _schedule_tab(m: dict) -> str:
    """일정 탭: 마감 요약 · 기한 지남 줄 · 월간 달력 · 고른 날의 할 일 · 기한 없는 내 할 일.

    달력 격자와 칸 안의 짧은 표시는 페이지 JS 가 할 일 줄(.sit)로 그린다 (달을 넘기거나 기한을
    바꾸면 다시 그린다). 처음 보기는 이번 달 · 오늘 · 내 할 일이라, 그에 맞는 줄만 보이게 둔다.
    """
    s, today = m["stats"], m["today"]
    today_iso = today.isoformat()
    rows = m.get("schedule", [])
    dated = [r for r in rows if iso_day(r["deadline"]["raw"])]
    undated = [r for r in rows if not iso_day(r["deadline"]["raw"])]
    not_mine = {p["name"] for p in m["projects"] if p["pstate"] == "not_mine"}
    mine_today = [r.get("kind") != "task" and r["project"] not in not_mine
                  and iso_day(r["deadline"]["raw"]) == today_iso for r in dated]
    day_rows = "".join(_sit(r, today, i, shown) for i, (r, shown) in enumerate(zip(dated, mine_today)))
    none_rows = "".join(_sit(r, today, len(dated) + i, r.get("kind") != "task" and open_task(r)
                           and r["project"] not in not_mine)
                        for i, r in enumerate(undated))
    n_today = sum(mine_today)
    summary = (("overdue", "기한 지남"), ("today", "오늘"), ("week", f"{SCHEDULE_WEEK}일 안"),
               ("none", "기한 없음"))
    sums = "".join(
        f'<button type="button" class="ss lv-{k}" data-spanel="{k}"'
        + ("" if k == "none" else f' aria-pressed="{"true" if k == "today" else "false"}"')
        + f'><span>{label}</span><b class="num" id="ss-{k}">{s[f"sched_{k}"]}</b></button>'
        for k, label in summary)
    note = ("날짜를 누르면 그날 마감인 할 일을 보여 줍니다. 기한은 할 일마다 직접 정할 수 있고, "
            "직접 정한 기한이 메일에서 뽑은 기한보다 우선합니다." if s["extracted"]
            else "업무 정리 전이라 달력에 올릴 할 일이 아직 없습니다.")
    return (
        '<div class="pad sch" id="sch"><div class="sch-top">'
        f'<div class="sch-sum" role="group" aria-label="마감 요약">{sums}</div>'
        '<div class="seg" role="group" aria-label="일정에 올릴 할 일">'
        '<button type="button" data-sview="me" aria-pressed="true">내 할 일</button>'
        '<button type="button" data-sview="all" aria-pressed="false">프로젝트 할 일 포함</button>'
        f'</div></div><p class="note">{esc(note)}</p>'
        '<div class="sch-over" id="sch-over" hidden><span class="so-l">기한 지남</span>'
        '<span class="so-chips" id="sch-over-chips"></span></div>'
        '<div class="sch-body"><section class="cal" aria-label="월간 달력"><div class="cal-h">'
        '<button type="button" class="cal-nav" data-cal="-1" aria-label="이전 달">‹</button>'
        f'<h2 id="cal-title" aria-live="polite">{today.year}년 {today.month}월</h2>'
        '<button type="button" class="cal-nav" data-cal="1" aria-label="다음 달">›</button>'
        '<button type="button" class="btn" data-cal="0">오늘</button></div>'
        f'<div class="cal-wk" aria-hidden="true">{"".join(f"<span>{w}</span>" for w in WEEKDAYS)}</div>'
        '<div class="cal-grid" id="cal-grid"></div></section>'
        '<aside class="sch-side" aria-label="고른 날의 할 일"><div class="sec-h">'
        f'<h2 id="sp-title">{today.month}월 {today.day}일 ({WEEKDAYS[today.weekday()]}) · 오늘</h2>'
        f'<span class="sec-m" id="sp-cnt">{f"{n_today}건" if n_today else ""}</span></div>'
        f'<ul class="sits" id="sch-day">{day_rows}</ul>'
        f'<p class="empty-state" id="sp-empty"{" hidden" if n_today else ""}>이 날 마감인 할 일이 없습니다.</p>'
        '<div class="sec-h gap" id="sch-none-h"><h2>기한 없음</h2><span class="sec-m">내 할 일 '
        f'<b class="num" id="none-cnt">{s["sched_none"]}</b>건 · 날짜를 정하면 달력에 올라갑니다</span></div>'
        f'<ul class="sits" id="sch-none">{none_rows}</ul>'
        f'<p class="empty-state" id="none-empty"{" hidden" if s["sched_none"] else ""}>'
        '기한 없는 내 할 일이 없습니다.</p></aside></div></div>'
    )


def _blocked_tab(m: dict) -> str:
    """차단됨 탭: 차단된 메일(구분별) · 차단 규칙 · 차단 후보 · 보호 도메인.

    규칙 · 보호 도메인을 바꾸는 버튼은 서버 주소로 열었을 때만 동작한다 (/api/filters).
    이 탭의 CSS · JS 는 이 함수 안에 따로 둔다 (다른 탭 코드와 섞이지 않게).
    """
    info = m.get("blocking") or {"protected": [], "counts": {}, "candidates": [],
                                 "categories": [], "kinds": {}}
    cats, kinds = info["categories"], info["kinds"]

    def cat_tag(c: str | None) -> str:
        return f'<span class="cat">{esc(c or "기타")}</span>'

    def cat_select(selected: str) -> str:
        return ('<select name="category" aria-label="구분">' + "".join(
            f'<option{" selected" if c == selected else ""}>{esc(c)}</option>' for c in cats)
            + "</select>")

    # 1) 차단된 메일 — 구분별로 걸러 보기
    blocked = m["blocked"]
    by_cat: dict[str, int] = {}
    for b in blocked:
        c = b.get("filter_category") or "기타"
        by_cat[c] = by_cat.get(c, 0) + 1
    chips = [f'<button type="button" data-cat-filter="" aria-pressed="true">전체 {len(blocked)}</button>']
    chips += [f'<button type="button" data-cat-filter="{esc(c)}" aria-pressed="false">'
              f'{esc(c)} {by_cat[c]}</button>' for c in cats if by_cat.get(c)]
    rows = []
    for b in blocked:
        dom = sender_domain(b.get("from_email"))
        acts = []
        if b.get("blocked_by") and b.get("filter_enabled"):
            acts.append(f'<button type="button" class="btn" data-fop="toggle_rule" '
                        f'data-id="{int(b["blocked_by"])}" data-enabled="0">규칙 끄기</button>')
        if dom:
            acts.append(f'<button type="button" class="btn" data-fop="protect" '
                        f'data-domain="{esc(dom)}">{esc(dom)} 보호</button>')
        rows.append(
            f'<tr data-cat="{esc(b.get("filter_category") or "기타")}">'
            f'<td class="num">{esc(fmt_dt(b.get("sent_at")))}</td>'
            f'<td>{esc(b.get("from_name") or b.get("from_email"))}'
            f'<div class="off">{esc(b.get("from_email") or "")}</div></td>'
            f'<td>{esc(b.get("subject"))}</td><td>{cat_tag(b.get("filter_category"))}</td>'
            f'<td><code>{esc(b.get("filter_pattern"))}</code></td>'
            f'<td><span class="fx">{"".join(acts)}</span></td></tr>')
    empty_mails = '<tr><td colspan="6" class="empty">차단된 메일 없음</td></tr>'

    # 2) 차단 규칙 — 켜기 · 끄기 · 삭제 + 추가
    counts = info["counts"]
    rule_rows = "".join(
        f'<tr><td>{cat_tag(f.category)}</td><td>{esc(kinds.get(f.kind, f.kind))}</td>'
        f'<td><code>{esc(f.pattern)}</code>'
        + (f'<div class="off">{esc(f.note)}</div>' if f.note else "")
        + f'</td><td class="num">{counts.get(f.id, 0)}</td>'
        f'<td>{"사용" if f.enabled else "<span class=off>해제</span>"}</td>'
        f'<td><span class="fx"><button type="button" class="btn{"" if f.enabled else " ok"}" '
        f'data-fop="toggle_rule" data-id="{f.id}" data-enabled="{0 if f.enabled else 1}">'
        f'{"끄기" if f.enabled else "켜기"}</button>'
        f'<button type="button" class="btn" data-fop="remove_rule" data-id="{f.id}">삭제</button>'
        '</span></td></tr>'
        for f in m["filters"]) or '<tr><td colspan="6" class="empty">등록된 규칙 없음</td></tr>'
    add_form = (
        '<form class="fadd" data-fop="add_rule"><select name="kind" aria-label="규칙 종류">'
        + "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in kinds.items())
        + '</select><input name="pattern" required aria-label="문구" '
        'placeholder="문구 — 예: (광고) · example.com · news@example.com">'
        + cat_select("기타")
        + '<input name="note" aria-label="메모" placeholder="메모 (선택)">'
        '<button type="submit" class="btn ok">규칙 추가</button></form>'
        '<p class="hint">보호 도메인을 겨냥한 규칙은 저장되지 않고, 제목 규칙에 걸려도 보호 도메인 '
        '메일은 차단되지 않습니다.</p>')

    # 3) 차단 후보 — 제안만, 자동 차단 안 함
    cands = info["candidates"]
    cand_rows = "".join(
        f'<li class="cand" data-cand><div class="cb"><div class="fx">{cat_tag(c["category"])}'
        f'<b>{esc(kinds.get(c["kind"], c["kind"]))}: {esc(c["pattern"])}</b>'
        f'<span class="off">{c["count"]}통</span></div>'
        f'<div class="rs">{esc(" · ".join(c["reasons"]))}</div>'
        f'<div class="ex" title="{esc(c["sample"])}">예: {esc(c["sample"])}</div></div>'
        f'<div class="fx">{cat_select(c["category"])}'
        f'<button type="button" class="btn ok" data-fop="add_rule" data-kind="{esc(c["kind"])}" '
        f'data-pattern="{esc(c["pattern"])}" data-note="{esc(", ".join(c["reasons"]))}">'
        '규칙으로 추가</button>'
        f'<button type="button" class="btn" data-fop="dismiss" data-kind="{esc(c["kind"])}" '
        f'data-pattern="{esc(c["pattern"])}">후보에서 빼기</button></div></li>'
        for c in cands)

    # 4) 보호 도메인
    pds = "".join(
        f'<span class="pd">{esc(p["domain"])}'
        + ('<span class="fix">고정</span>' if p["fixed"] else
           f'<button type="button" class="btn" data-fop="unprotect" '
           f'data-domain="{esc(p["domain"])}">빼기</button>')
        + "</span>" for p in info["protected"])
    protect_form = (
        '<form class="fadd" data-fop="protect"><input name="domain" required '
        'aria-label="보호할 도메인" placeholder="보호할 도메인 — 예: bluead.example">'
        '<input name="note" aria-label="메모" placeholder="메모 (선택)">'
        '<button type="submit" class="btn ok">보호 추가</button></form>')

    return (
        f'<style>{BLOCK_CSS}</style><div class="blk" id="blk">'
        '<div class="banner">차단은 삭제가 아닙니다 — 규칙을 끄거나 지우면 메일이 되살아납니다. '
        '보호 도메인에서 온 메일은 어떤 규칙에도 차단되지 않습니다.</div>'
        '<p class="msg" id="blk-msg" role="status" aria-live="polite" hidden></p>'
        f'<h4 class="sec">차단된 메일 {len(blocked)}통 '
        f'<span class="off">최근 {m.get("window_days", 7)}일</span></h4>'
        f'<div class="chips" role="group" aria-label="구분별 보기">{"".join(chips)}</div>'
        '<div class="tbl-wrap"><table class="tbl" id="blk-mails"><thead><tr><th>수신</th>'
        '<th>보낸사람</th><th>제목</th><th>구분</th><th>걸린 규칙</th><th></th></tr></thead>'
        f'<tbody>{"".join(rows) or empty_mails}</tbody></table></div>'
        f'<h4 class="sec">차단 규칙 {len(m["filters"])}개</h4>'
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>구분</th><th>종류</th>'
        '<th>문구</th><th>차단</th><th>상태</th><th></th></tr></thead>'
        f'<tbody>{rule_rows}</tbody></table></div>{add_form}'
        f'<h4 class="sec">차단 후보 {len(cands)}건 <span class="off">자동으로 차단하지 않습니다</span></h4>'
        + (f'<ul class="cands">{cand_rows}</ul>' if cands
           else '<p class="empty-state">지금은 차단 후보가 없습니다.</p>')
        + '<h4 class="sec">보호 도메인 <span class="off">어떤 규칙에도 차단하지 않음</span></h4>'
        f'<div class="pds">{pds}</div>{protect_form}'
        f'</div><script>{BLOCK_JS}</script>'
    )


BLOCK_CSS = """
.blk .off{color:var(--muted);font-weight:400;font-size:12px}
.blk h4.sec .off{margin-left:6px}
.blk .msg{margin:12px 0;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:var(--side);font-size:13px}
.blk .msg.bad{border-color:var(--warn);color:var(--ink)}
.blk .chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.blk .chips button{appearance:none;padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--surface);
 color:var(--ink-2);font:inherit;font-size:12px;cursor:pointer}
.blk .chips button[aria-pressed="true"]{border-color:var(--acc);color:var(--acc);font-weight:600}
.blk .cat{display:inline-block;padding:0 7px;border:1px solid var(--line);border-radius:999px;background:var(--side);
 color:var(--ink-2);font-size:11px;line-height:18px;white-space:nowrap}
.blk .fx{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.blk .fadd{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:10px 0 0}
.blk .fadd input,.blk .fadd select,.blk .cand select{padding:5px 8px;border:1px solid var(--line);border-radius:8px;
 background:var(--surface);color:var(--ink);font:inherit;font-size:13px}
.blk .fadd input[name=pattern],.blk .fadd input[name=domain]{flex:1 1 220px;min-width:0}
.blk .hint{margin:6px 0 0;color:var(--muted);font-size:12px}
.blk .cands{margin:0;padding:0;list-style:none;border:1px solid var(--line-2);border-radius:12px;background:var(--surface)}
.blk .cand{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px 16px;padding:12px 14px;
 border-bottom:1px solid var(--line)}
.blk .cand:last-child{border-bottom:0}
.blk .cb{min-width:0}
.blk .cand .rs{margin-top:4px;color:var(--muted);font-size:12px}
.blk .cand .ex{margin-top:2px;overflow:hidden;color:var(--ink-2);font-size:12px;text-overflow:ellipsis;white-space:nowrap}
.blk .pds{display:flex;flex-wrap:wrap;gap:6px}
.blk .pd{display:inline-flex;align-items:center;gap:6px;padding:3px 5px 3px 11px;border:1px solid var(--line);
 border-radius:999px;background:var(--surface);font-size:12.5px}
.blk .pd .fix{padding:0 6px;color:var(--muted);font-size:11px}
@media (max-width:760px){.blk .cand{grid-template-columns:1fr}}
"""

BLOCK_JS = r"""
(function(){
  var root=document.getElementById('blk');if(!root)return;
  var msg=document.getElementById('blk-msg'),KEY='nwmail.blkmsg';
  function conf(){try{return JSON.parse(document.getElementById('nw-conf').textContent)}catch(e){return {}}}
  function served(c){return !!c.port&&/^https?:$/.test(location.protocol)&&location.port===String(c.port);}
  function say(text,bad){msg.textContent=text;msg.hidden=false;msg.classList.toggle('bad',!!bad);}
  function lock(on,title){
    root.querySelectorAll('[data-fop] button,button[data-fop],.fadd input,.fadd select,.cand select')
      .forEach(function(el){el.disabled=on;if(title)el.title=title;});
  }
  try{var carry=sessionStorage.getItem(KEY);if(carry){sessionStorage.removeItem(KEY);say(carry);}}catch(e){}
  document.addEventListener('DOMContentLoaded',function(){
    var c=conf();
    if(!served(c)){
      lock(true,'대시보드 서버 주소에서 열면 바꿀 수 있습니다');
      say('규칙 · 보호 도메인 변경은 대시보드 서버 주소(http://127.0.0.1:'+(c.port||8787)+'/)로 열었을 때 저장됩니다.');
    }
  });
  function send(p){
    var c=conf();if(!served(c))return;
    lock(true);say('적용하는 중 — 저장된 메일에 다시 적용하고 화면을 새로 만듭니다…');
    fetch('/api/filters',{method:'POST',
      headers:{'Content-Type':'application/json','X-Nwmail-Token':c.token},body:JSON.stringify(p)})
    .then(function(r){return r.json().catch(function(){return {};}).then(function(j){
      if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;});})
    .then(function(j){
      var extra=[];
      if(j.blocked)extra.push('새로 차단 '+j.blocked+'통');
      if(j.unblocked)extra.push('되살아난 메일 '+j.unblocked+'통');
      if(j.rebuilt===false)extra.push('화면은 다음 자동 갱신 때 반영됩니다');
      say(j.message+(extra.length?' · '+extra.join(' · '):''));
      try{sessionStorage.setItem(KEY,msg.textContent);}catch(e){}
      setTimeout(function(){location.hash='#blocked';location.reload();},400);
    })
    .catch(function(err){lock(false);say('바꾸지 못했어요 — '+err.message,true);});
  }
  root.addEventListener('click',function(e){
    var chip=e.target.closest('[data-cat-filter]');
    if(chip){
      var cat=chip.dataset.catFilter;
      root.querySelectorAll('[data-cat-filter]').forEach(function(b){
        b.setAttribute('aria-pressed',b===chip?'true':'false');});
      root.querySelectorAll('#blk-mails tr[data-cat]').forEach(function(tr){
        tr.hidden=!!cat&&tr.dataset.cat!==cat;});
      return;
    }
    var b=e.target.closest('button[data-fop]');if(!b||b.disabled)return;
    var op=b.dataset.fop,p={op:op};
    if(op==='toggle_rule'){p.id=+b.dataset.id;p.enabled=b.dataset.enabled==='1';}
    else if(op==='remove_rule'){
      if(!confirm('규칙을 삭제할까요? 이 규칙으로 차단된 메일은 되살아납니다.'))return;
      p.id=+b.dataset.id;
    }else if(op==='protect'){
      if(!confirm(b.dataset.domain+' 를 보호 도메인에 추가할까요? 이 도메인 메일은 앞으로 차단되지 않습니다.'))return;
      p.domain=b.dataset.domain;
    }else if(op==='unprotect'){p.domain=b.dataset.domain;}
    else if(op==='dismiss'){p.kind=b.dataset.kind;p.pattern=b.dataset.pattern;}
    else if(op==='add_rule'){
      var row=b.closest('[data-cand]');
      p.kind=b.dataset.kind;p.pattern=b.dataset.pattern;p.note=b.dataset.note||'';
      p.category=row?row.querySelector('select').value:'기타';
    }else return;
    send(p);
  });
  root.addEventListener('submit',function(e){
    var f=e.target.closest('form[data-fop]');if(!f)return;
    e.preventDefault();
    var p={op:f.dataset.fop};
    Array.prototype.forEach.call(f.elements,function(el){if(el.name)p[el.name]=el.value.trim();});
    send(p);
  });
})();
"""


# ── 맨 아래 2depth: 메일 분류 규칙 · 토큰 사용 현황 ─────────────────────────────
# 1depth 두 줄은 접혀 있어도 요약 한 줄이 보이고, 누르면 2depth 보기(오늘 탭의 할 일 · 완료 · 제외 전환과 같은 모양)가
# 펼쳐진다. 펼친 줄과 고른 보기는 브라우저에 기억한다 (페이지 JS). 규칙은 지금 설정(projects.json · 차단 규칙 ·
# 코드의 기준값)을 그대로 읽어 보여 주고, 토큰은 usage.py 가 mail.db 에 남긴 호출 기록으로 센다.
BASIS = (("keyword", "제목 키워드"), ("model", "업무 정리(LLM)가 고른 것"), ("tag", "제목 태그 · 회사"),
         ("domain", "발신 도메인"), ("unknown", "미분류"))
ROLE_ORDER = ("수정", "개발", "디자인", "기타")          # 업무 정리 프롬프트가 역할을 판정하는 순서
ROLE_NOTES = {"수정": "이미 만든 페이지 · 시안 · 배너 · 운영 데이터를 요청대로 고침 (피드백 반영, 문구 변경, 기한 연장, 회차 삭제)",
              "개발": "새 페이지 · 기능 구현, API 연동, 개발계 · 운영계 반영",
              "디자인": "새 시안 · 이미지 · 배너 제작",
              "기타": "검토 · 확인, 콘텐츠 수급, 일정 조율, 회신, 테스트 (예전에 기획 · 마케팅으로 뽑힌 일 포함)"}
TOKEN_CELLS = ("내 할 일", "프로젝트 할 일", "결정사항", "대화 요약", "메일에서 뽑은 기한", "LLM 이 고른 세부 프로젝트")
FREE_CELLS = ("메일 받기", "차단", "대화 묶기", "키워드 · 태그 · 도메인 분류", "답을 기다리는 메일", "일정 달력",
              "진행 막대")
HEAVY_TOP = 5


def _tok(n) -> str:
    """토큰 수를 짧게: 950 · 12.3K · 1.24M."""
    n = int(n or 0)
    if n < 1000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"


def _usd(x) -> str:
    """API 환산 금액 (참고용, 실제 청구 없음)."""
    x = float(x or 0)
    return "$0" if not x else "<$0.01" if x < 0.01 else f"${x:,.2f}"


def _pct(x) -> str:
    """늘고 줌을 퍼센트로: +11% · -4% · +1,100%."""
    v = round(float(x or 0) * 100)
    return f"{v:+,}%"


def _arrow(d: dict) -> str:
    return "▲ " if d["dir"] == "up" else "▼ " if d["dir"] == "down" else ""


def _delta_text(d: dict, prev_label: str) -> str:
    """증가량 한 줄: '▲ 12.3K · +11% 어제보다'. prev_label 은 견준 기간 이름.

    이전 기간에 쓴 게 없으면 몇 % 늘었다고 말할 수 없으므로 '이번이 처음'이라고만 쓴다.
    """
    if d["dir"] == "new":
        return f'<span class="it-d new">{esc(prev_label)} 기록 없음</span>'
    if d["dir"] == "flat":
        return (f'<span class="it-d flat" title="{esc(prev_label)} {_tok(d["prev"])} 토큰">'
                f'{esc(prev_label)}와 같음</span>')
    pct = f' · {_pct(d["pct"])}' if d["pct"] is not None else ""
    return (f'<span class="it-d {d["dir"]}" title="{esc(prev_label)} {_tok(d["prev"])} 토큰">'
            f'{_arrow(d)}{_tok(abs(d["tokens"]))}{pct}'
            f'<span class="it-w"> {esc(prev_label)}보다</span></span>')


def _chars(n) -> str:
    """보내는 글자 수: 4.1만 자 · 870자."""
    n = int(n or 0)
    return (f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "만 자") if n >= 10000 else f"{n:,}자"


def _bar_row(label: str, value: float, top: float, text: str, mute: bool = False,
             tag: str = "div", title: str = "") -> str:
    """가로 막대 한 줄: 이름 · 막대(가장 큰 값 기준) · 값. 막대는 꾸밈이라 값은 늘 글자로 쓴다.

    label · text 는 이미 이스케이프한 HTML 이다.
    """
    pct = max(0, min(100, round(value * 100 / top))) if top else 0
    tip = f' title="{esc(title)}"' if title else ""
    return (f'<{tag} class="ib"{tip}><span class="ib-l">{label}</span>'
            f'<span class="track" aria-hidden="true"><span class="fill{" mute" if mute else ""}" '
            f'style="width:{pct}%"></span></span><span class="ib-n num">{text}</span></{tag}>')


def _window_threads(m: dict) -> list[dict]:
    return [th for p in m["projects"] for th in p["threads"]]


def _il(rows) -> str:
    """규칙 목록: (이름, 설명, 숫자) 줄. 설명 · 숫자는 이미 이스케이프한 HTML."""
    return '<ul class="il">' + "".join(
        f'<li><b>{esc(name)}</b><span class="d">{desc}</span><span class="v num">{val}</span></li>'
        for name, desc, val in rows) + "</ul>"


def _rules_line(m: dict) -> str:
    """1depth 요약 한 줄 (접혀 있을 때도 보인다)."""
    prot = (m.get("blocking") or {}).get("protected", [])
    fs = m["filters"]
    on = sum(f.enabled for f in fs)
    state = "" if not fs or on == len(fs) else " (꺼짐)" if not on else f" (사용 {on})"
    unknown = sum(th.get("basis") == "unknown" for th in _window_threads(m))
    return (f'보호 도메인 {len(prot)} · 차단 규칙 {len(fs)}{state} · 세부 프로젝트 {len(m["rules"].client_of)}'
            f' · 최근 {m["window_days"]}일 미분류 {unknown}건')


def _rules_flow(m: dict) -> str:
    """한눈에 보기: 메일 한 통이 대시보드에 오기까지. 단계마다 규칙/LLM, 이번 주 숫자, 고치는 곳."""
    s, w = m["stats"], m["window_days"]
    unknown = sum(th.get("basis") == "unknown" for th in _window_threads(m))
    steps = (
        ("메일 받기", "규칙",
         "10분마다 받은메일함을 읽습니다 (직전 평일부터, 월요일은 금요일부터). 같은 대화의 과거 메일과 내 답장은 "
         "메일함 전체에서 찾습니다.",
         f"최근 {w}일 받은 메일 {s['window_mails']}통", ".env 의 WINDOW_DAYS · auto.py"),
        ("차단", "규칙",
         "보낸 주소 · 보낸 도메인 · 제목 규칙에 걸리면 숨깁니다 (지우지 않음). 보호 도메인 메일은 어떤 규칙에도 "
         "걸리지 않습니다.",
         f"차단 {s['blocked']}통 · 규칙 {s['rules']}개", "차단됨 탭"),
        ("대화 묶기", "규칙",
         "답장 헤더(References · In-Reply-To)로 같은 대화를 잇습니다. 헤더 없는 답장(RE:)은 90일 안에 회신 표식을 "
         "뗀 제목이 완전히 같은 메일만 붙입니다.",
         f"대화 {s['threads']}건 · 과거 메일 {s['history']}통 포함", "store.py · context.py"),
        ("프로젝트 분류", "규칙 + LLM",
         "제목 키워드 → 업무 정리(LLM)가 고른 세부 프로젝트 → 제목 태그 · 회사 → 발신 도메인 순으로, 먼저 맞는 것으로 "
         "정합니다. 대시보드를 만들 때마다 다시 정해서 규칙을 바꿔도 다시 추출하지 않습니다.",
         f"미분류 {unknown}건", "projects.json"),
        ("업무 정리", "LLM",
         "09 · 13 · 17시에 내용이 바뀐 대화만 Claude 에 보내 요약 · 결정사항 · 할 일 · 내 할 일 · 기한을 뽑습니다. "
         "정기 메일은 보내지 않습니다.",
         f"정리 {s['extracted']}/{s['threads'] - s['excluded']} · 정기 메일 {s['excluded']}건 제외",
         f"extract.py 프롬프트 (버전 {PROMPT_VERSION})"),
        ("화면 분류", "규칙",
         "답을 기다리는 메일, 마감 묶음, 역할 태그, 같은 메일명 묶기를 대시보드를 만들 때 정합니다.",
         f"답 대기 {s['unanswered']}건 · 기한 지남 {s['overdue']}건", "dashboard.py"),
    )
    return '<ol class="iflow">' + "".join(
        f'<li class="if"><div class="if-h"><span class="if-n num">{i}</span><b>{esc(name)}</b>'
        f'<span class="itag{" llm" if "LLM" in how else ""}">{esc(how)}</span>'
        f'<span class="if-c num">{esc(count)}</span></div>'
        f'<p class="if-d">{esc(desc)}</p><p class="if-w">고치는 곳 · {esc(where)}</p></li>'
        for i, (name, how, desc, count, where) in enumerate(steps, 1)) + "</ol>"


def _rules_block(m: dict) -> str:
    """차단: 보호 도메인 · 규칙 · 차단 후보 근거. 고치는 건 차단됨 탭에서 (여기는 보기만)."""
    info = m.get("blocking") or {}
    kinds, counts = info.get("kinds") or {}, info.get("counts") or {}
    pds = "".join(f'<span class="chip">{esc(p["domain"])}{" · 고정" if p["fixed"] else ""}</span>'
                  for p in info.get("protected", []))
    rows = "".join(
        f'<tr><td>{esc(f.category)}</td><td>{esc(kinds.get(f.kind, f.kind))}</td>'
        f'<td><code>{esc(f.pattern)}</code></td><td class="num">{counts.get(f.id, 0)}</td>'
        f'<td>{"사용" if f.enabled else "꺼짐"}</td></tr>'
        for f in m["filters"]) or '<tr><td colspan="5" class="empty">등록된 규칙 없음</td></tr>'
    by_cat: dict[str, int] = {}
    for b in m["blocked"]:
        cat = b.get("filter_category") or "기타"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    cats = " · ".join(f"{c} {n}" for c, n in by_cat.items())
    reasons = (
        ("광고 표기", "제목의 <code>(광고)</code> · <code>[광고]</code> (정보통신망법상 광고 메일 표기) → 제목 규칙", "광고·홍보"),
        ("뉴스레터", "수신거부 헤더, 뉴스레터성 제목, news · marketing 발송 주소", "뉴스레터·구독"),
        ("행사 안내", "설명회 · 세미나 · 웨비나 · 참석 안내 제목", "행사·안내"),
        ("자동 발송", "noreply · notification 주소에서 2통 이상", "자동 알림"),
        ("올리지 않음", "보호 도메인, 우리 회사와 메일을 주고받은 상대, 이미 규칙이 있거나 후보에서 뺀 곳", ""),
    )
    return (
        '<h4 class="ih">보호 도메인<span class="ih-m">어떤 규칙에도 차단하지 않음 · 고정은 코드(filters.py)에 있어 뺄 수 없음</span></h4>'
        f'<div class="ichips">{pds}</div>'
        f'<h4 class="ih">차단 규칙 {len(m["filters"])}개<span class="ih-m">최근 {m["window_days"]}일 차단 '
        f'{len(m["blocked"])}통{f" ({esc(cats)})" if cats else ""} · 보낸 주소(포함) · 보낸 도메인(하위 포함) · 제목(포함)</span></h4>'
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>구분</th><th>종류</th><th>문구</th>'
        f'<th class="num">차단</th><th>상태</th></tr></thead><tbody>{rows}</tbody></table></div>'
        '<h4 class="ih">차단 후보를 올리는 근거<span class="ih-m">제안만 하고 자동으로 차단하지 않음</span></h4>'
        + _il((name, desc, esc(cat)) for name, desc, cat in reasons)
        + f'<p class="ifoot"><span>지금 차단 후보 <b class="num">{len(info.get("candidates", []))}</b>건</span>'
        '<a class="btn" href="#blocked">차단됨 탭에서 고치기</a></p>'
    )


def _rules_proj(m: dict) -> str:
    """프로젝트: 판정 순서와 근거별 대화 수(누르면 제목), projects.json 의 세부 프로젝트 표."""
    rules, w = m["rules"], m["window_days"]
    threads = _window_threads(m)
    groups = {key: [th for th in threads if th.get("basis") == key] for key, _ in BASIS}
    top = max((len(v) for v in groups.values()), default=0)
    rows = []
    for i, (key, label) in enumerate(BASIS, 1):
        ths = groups[key]
        num = "" if key == "unknown" else f'<span class="ib-o num">{i}</span>'
        items = "".join(f'<li><span class="ib-s">{esc(th["subject"])}</span>'
                        f'<span class="ib-p">{esc(th["project"])}</span></li>' for th in ths)
        rows.append(f'<details class="ib-d ib-{key}">'
                    + _bar_row(num + esc(label), len(ths), top, f"{len(ths)}건", mute=key == "unknown",
                               tag="summary", title="눌러서 대화 제목 보기")
                    + f'<ul class="ib-list">{items or "<li>없음</li>"}</ul></details>')

    raw = rules.raw or {}
    alias = raw.get("aliases") or {}

    def names(n: str) -> list[str]:
        return [n] + [v for v in alias.get(n, []) if v != n]
    tags: dict[str, list[str]] = {}
    for org, proj in rules.org_projects.items():
        tags.setdefault(rules.resolve(proj) or proj, []).extend(f"[{v}]" for v in names(org))
    for client in rules.clients:
        tags.setdefault(rules.defaults[client], []).extend(f"[{v}]" for v in names(client))
    doms: dict[str, list[str]] = {}
    for dom, name in rules.domain_hints.items():
        if target := rules.resolve(name):
            doms.setdefault(target, []).append(dom)
    count = {p["name"]: len(p["threads"]) for p in m["projects"]}
    body = []
    for client in rules.clients:
        default = rules.defaults[client]
        for p in [x for x in rules.keywords if rules.client_of.get(x) == client and x != default] + [default]:
            how = []
            if kws := rules.keywords.get(p):
                how.append("키워드 " + " · ".join(kws))
            if p in tags:
                how.append("태그 " + " ".join(dict.fromkeys(tags[p])))
            if p in doms:
                how.append("도메인 " + " · ".join(doms[p]))
            badge = ' <span class="ih-m">기본</span>' if p == default else ""
            if p == default:
                how.append("키워드로 못 정한 이 고객사 대화")
            body.append(f'<tr><td class="nw">{esc(client)}</td><td class="nw">{esc(p)}{badge}</td>'
                        f'<td>{esc(" / ".join(how))}</td><td class="num">{count.get(p, 0)}</td></tr>')
    outside = [p for p in m["projects"] if p["name"] not in rules.client_of and p["name"] != UNCLASSIFIED]
    extra = ("".join(f'<span class="chip">{esc(p["name"])} · {len(p["threads"])}</span>' for p in outside)
             or '<span class="ih-m">없음</span>')

    def chips(xs) -> str:
        return "".join(f'<span class="chip">{esc(x)}</span>' for x in xs)
    return (
        f'<h4 class="ih">판정 순서<span class="ih-m">위에서 먼저 맞는 것으로 정함 · 최근 {w}일 대화 {len(threads)}건 · '
        '줄을 누르면 대화 제목</span></h4>'
        f'<div class="ib-set">{"".join(rows)}</div>'
        '<h4 class="ih">세부 프로젝트<span class="ih-m">projects.json · 코드 수정 없이 고침 · 띄어쓰기 · 대소문자 무시, '
        '위에 적힌 프로젝트가 먼저</span></h4>'
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>고객사</th><th>세부 프로젝트</th>'
        f'<th>이렇게 정함</th><th class="num">최근 {w}일</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        '<h4 class="ih">설정 밖 이름<span class="ih-m">설정에 없는 태그나 LLM 이 지은 이름 → 기타 묶음 (대화 수)</span></h4>'
        f'<div class="ichips">{extra}</div>'
        '<h4 class="ih">말머리 · 추출 제외<span class="ih-m">말머리는 프로젝트로 보지 않음 · 추출 제외 제목은 Claude 에 '
        '보내지 않음 (대시보드에는 보임)</span></h4>'
        f'<div class="ichips">{chips(raw.get("non_project_tags") or [])}</div>'
        f'<div class="ichips gap">{chips(f"추출 제외: {x}" for x in rules.extract_skip)}</div>'
    )


def _rules_task(m: dict) -> str:
    """할 일 · 역할: 업무 정리(LLM)가 뽑는 것과 역할 판정 순서, 담당자 · 소속 · 업무명 묶기."""
    rules = m["rules"]
    counts = {r: 0 for r in rules.roles}
    for p in m["projects"]:
        for it in p["items"]:
            for role, ts in it["roles"].items():
                counts[role] = counts.get(role, 0) + len(ts)
    order = [r for r in ROLE_ORDER if r in counts] + [r for r in counts if r not in ROLE_ORDER]
    top = max(counts.values(), default=0)
    roles = "".join(
        '<div class="ib-g">'
        + _bar_row(f'<span class="ib-o num">{i}</span>{esc(r)}', counts[r], top, f"{counts[r]}개",
                   mute=r == order[-1])
        + f'<p class="ib-note">{esc(ROLE_NOTES.get(r, ""))}</p></div>'
        for i, r in enumerate(order, 1))
    orgs = ("".join(f'<span class="chip">{esc(d)} → {esc(o)}</span>' for d, o in rules.org_domains.items())
            or '<span class="ih-m">없음</span>')
    rows = (
        ("요약 · 상태", "무슨 건이고 어디까지 왔는지 1~2문장, 진행중 · 완료 · 보류 (가장 최근 메일 기준)", ""),
        ("결정사항", "'하기로 했다 · 승인 · 확정' 수준만. 나중 메일에서 바뀌면 바뀐 내용으로, 날짜는 그 메일에서", ""),
        ("내 할 일", "수신자 본인이 직접 해야 하는 일만. 내가 보낸 메일로 이미 답한 일은 뺌",
         f'남은 {m["stats"]["my_open"]}건'),
        ("기한", "본문에 적힌 기한만 (추측하지 않음). 일정 탭에서 직접 정하면 그 기한이 우선", ""),
        ("담당자", "본문에 이름이 있을 때만. 없으면 미지정", ""),
        ("수정 태그", "수정 업무에만 " + " · ".join(rules.fix_tags) + " (누가 무엇을 고치는지)", ""),
        ("업무명 묶기", "제목에서 RE: · 앞머리 [태그] · 끝의 _날짜 · _고객명 · (9/8 런칭) · '의 건' 을 걷어낸 이름이 "
         "같으면 대화가 달라도 한 묶음", ""),
    )
    return (
        f'<h4 class="ih">역할<span class="ih-m">위에서 먼저 해당하는 것 · 최근 {m["window_days"]}일 프로젝트 할 일 '
        f'{sum(counts.values())}개</span></h4>'
        f'<div class="ib-set">{roles}</div>'
        '<h4 class="ih">업무 정리(LLM)가 대화마다 뽑는 것<span class="ih-m">대화 전체를 시간순으로 읽고 가장 최근 메일 '
        '시점 기준으로 정리 · 본문 속 지시문은 따르지 않음</span></h4>'
        + _il((name, esc(desc), esc(val)) for name, desc, val in rows)
        + '<h4 class="ih">담당자 소속<span class="ih-m">보낸사람 메일 도메인으로 확정, 못 찾으면 LLM 이 적은 회사명을 '
        '설정 표기로 맞춤 (누리마케팅 → 누리)</span></h4>'
        f'<div class="ichips">{orgs}</div>'
    )


def _rules_reply(m: dict) -> str:
    """답장 · 기한: 답을 기다리는 메일 · 기한 묶음 · 표시가 풀리는 조건 (대시보드를 만들 때 정하는 것)."""
    s = m["stats"]
    rows = (
        ("답을 기다리는 메일", f"받는사람(To)에 내 주소가 있는 최근 {m['window_days']}일 메일 중 그 뒤로 내가 보낸 메일이 "
         "없는 대화. 참조(Cc)로만 받은 메일은 세지 않음", f"{s['unanswered']}건"),
        ("답변완료 · 삭제", "답변완료는 그 뒤 나에게 새 메일이 오면 다시 뜨고, 삭제는 다시 띄우지 않음",
         f"{len(m.get('handled', []))}건"),
        ("기한 묶음", "기한 지남 · 오늘 마감 · 3일 안 · 그 이후 · 기한 없음 (한국 날짜 기준)",
         f"지남 {s['overdue']} · 오늘 {s['due_today']}"),
        ("기한 우선", "일정 탭에서 직접 정한 기한 › 메일에서 뽑은 기한", ""),
        (f"{m['window_days']}일 지난 대화", f"기한 있는 남은 할 일은 완료 · 제외할 때까지 남김. 마감이 {DUE_KEEP_DAYS}일 넘게 "
         "지나면 뺌", ""),
        ("NEW", "직전 업무 정리 이후 도착한 메일이 있는 대화", f"{s['new']}건"),
        ("프로젝트 완료", "그 뒤 새 메일을 받으면 진행 중으로 돌아옴 (삭제는 그대로 숨김)", ""),
        ("프로젝트 내 일 아님", "새 메일에도 유지. 오늘·일정의 내 할 일에서 제외하며, 자동 분류로 되돌리면 복구", ""),
        ("완료 · 제외 목록", f"최근 {LABELED_DAYS}일 표시한 것. 메일이 와서 할 일 문구가 바뀌면 표시가 풀릴 수 있음", ""),
    )
    return _il((name, esc(desc), esc(val)) for name, desc, val in rows)


def _usage_line(m: dict) -> str:
    """토큰 사용 현황 페이지 머리에 쓰는 요약 한 줄 (사용량 · 증가량). HTML 이 아닌 글자."""
    u = m["usage"]
    if not u["has_data"]:
        return f'아직 기록 없음 · 다음 자동 정리({_next_label(m)})부터 호출마다 쌓입니다'
    t, w, d = u["today"], u["week"], u["delta"]["week"]
    grew = (f'지난 {u["days"]}일 기록 없음' if d["dir"] == "new" else f'지난 {u["days"]}일과 같음'
            if d["dir"] == "flat" else
            f'지난 {u["days"]}일보다 {_arrow(d)}{_tok(abs(d["tokens"]))}'
            + (f' ({_pct(d["pct"])})' if d["pct"] is not None else ""))
    return (f'오늘 호출 {t["calls"]}회 · {_tok(t["tokens"])} 토큰 — 최근 {u["days"]}일 {_tok(w["tokens"])} 토큰 · '
            f'{grew} · API 환산 {_usd(w["cost"])} · 한도 초과 {w["limit"]}회')


def _usage_sum(m: dict) -> str:
    """요약: 오늘 · 최근 7일 · 이번 달, 일별 · 회차별 막대, 한도 초과, 다시 묻지 않은 대화."""
    u = m["usage"]

    def tile(label: str, b: dict, key: str, prev_label: str) -> str:
        """칸 하나: 사용량(큰 숫자) 아래에 증가량(직전 같은 길이의 기간과 견준 차이)."""
        fails = f' · 실패 {b["fails"]}회' if b["fails"] else ""
        return (f'<div class="itile"><p class="it-l">{label}</p><p class="it-v num">{_tok(b["tokens"])}'
                f'<span class="it-u">토큰</span></p>'
                f'<p class="it-g">{_delta_text(u["delta"][key], prev_label)}</p>'
                f'<p class="it-c">호출 {b["calls"]}회 · API 환산 {_usd(b["cost"])}{fails}</p></div>')
    t = u["total"]
    avg = (f'하루 평균 {_tok(t["per_day"])} · {t["span"]}일째' if t["span"] else "아직 없음")
    tiles = ('<div class="itiles">'
             + tile("오늘", u["today"], "today", "어제")
             + tile(f'최근 {u["days"]}일', u["week"], "week", f'지난 {u["days"]}일')
             + tile(f'이번 달 ({m["today"].month}월)', u["month"], "month", "지난달")
             + f'<div class="itile"><p class="it-l">누적</p><p class="it-v num">{_tok(t["tokens"])}'
             f'<span class="it-u">토큰</span></p><p class="it-g"><span class="it-d flat">{esc(avg)}</span></p>'
             f'<p class="it-c">호출 {t["calls"]:,}회 · API 환산 {_usd(t["cost"])}</p></div>'
             + "</div>")
    if not u["has_data"]:
        return (tiles + '<p class="inote">아직 기록이 없습니다. 업무 정리가 돌 때마다 호출별로 쌓입니다 '
                f'(다음 자동 정리 {_next_label(m)}). 금액은 구독 사용량을 API 요금으로 바꿔 본 참고값이고 '
                '실제 청구는 없습니다.</p>')
    # 일별은 사용량 막대 옆에 하루 전보다 얼마나 늘었는지(증가량)를 같이 적는다
    top = max((d["tokens"] for d in u["day_list"]), default=0)
    day_rows, before = [], u["prev"]["day0"]["tokens"]
    for i, d in enumerate(u["day_list"]):
        gap = d["tokens"] - (u["day_list"][i - 1]["tokens"] if i else before)
        chip = (f'<span class="it-d {"up" if gap > 0 else "down"}">'
                f'{"▲ " if gap > 0 else "▼ "}{_tok(abs(gap))}</span>' if gap and d["tokens"] else "")
        day_rows.append(_bar_row(
            f'{d["day"]:%m/%d} {WEEKDAYS[d["day"].weekday()]}'
            + (' <span class="itag bad">한도 초과</span>' if d["limit"] else ""),
            d["tokens"], top, f'{_tok(d["tokens"])} · {d["calls"]}회 {chip}'))
    days = "".join(day_rows)
    top = max((x["tokens"] for x in u["slots"]), default=0)
    slots = "".join(_bar_row(esc(x["label"]), x["tokens"], top, f'{_tok(x["tokens"])} · {x["calls"]}회',
                             mute=x["label"] == "직접 실행") for x in u["slots"])
    w, r, lim = u["week"], u["runs"], u["limit"]
    last = r["last"]
    rows = [
        ("한도 초과로 실패", (f'최근 {fmt_dt(lim["at"])}' + (f' · {lim["reset"]}에 풀림' if lim["reset"] else "")
                         + ' · 실패한 대화는 다음 정리 때 다시 보냄') if lim["count"] else "없음",
         f'{lim["count"]}회'),
        ("다시 묻지 않은 대화", f'내용이 그대로라 호출 없이 이전 결과를 씀 · 새로 정리 {r["extracted"]}개 · '
         f'실패 {r["failed"]}개 · 정리 {r["count"]}번에 호출 {r["calls"]}번', f'{r["cached"]}개'),
        ("토큰 구성", f'입력 {_tok(w["input_tokens"])} · 캐시 쓰기 {_tok(w["cache_write_tokens"])} · 캐시 읽기 '
         f'{_tok(w["cache_read_tokens"])} · 출력 {_tok(w["output_tokens"])}', ""),
        ("모델", " · ".join(u["models"]) or "—", ""),
    ]
    if last:
        rows.insert(2, ("마지막 정리", f'{fmt_dt(last["at"])} · 대화 {last["threads"]}개 중 재사용 {last["cached"]} · '
                         f'새로 {last["extracted"]} · 실패 {last["failed"]}', f'호출 {last["calls"]}번'))
    return (
        tiles
        + f'<h4 class="ih">일별<span class="ih-m">최근 {u["days"]}일 · 토큰 · 호출 수</span></h4>'
        + f'<div class="ib-set">{days}</div>'
        + f'<h4 class="ih">회차별<span class="ih-m">최근 {u["days"]}일 합계 · 자동 정리 09 · 13 · 17시와 직접 실행</span></h4>'
        + f'<div class="ib-set">{slots}</div>'
        + f'<h4 class="ih">최근 {u["days"]}일</h4>'
        + _il((name, esc(desc), esc(val)) for name, desc, val in rows)
        + f'<p class="inote">기록 시작 {esc(fmt_dt(u["first_at"]))} · 금액은 구독 사용량을 API 요금으로 바꿔 본 참고값이고 '
        '실제 청구는 없습니다. 출력 · 캐시 쓰기는 비싸고 캐시 읽기는 쌉니다.</p>'
    )


def _usage_where(m: dict) -> str:
    """어디에 쓰나: 용도별, 호출마다 붙는 기본 지시문, 대시보드 칸별, 프로젝트별 · 무거운 대화 (추정)."""
    u, sizes, w = m["usage"], m.get("sizes") or {}, m["usage"]["days"]
    top = max((x["tokens"] for x in u["uses"]), default=0)
    uses = "".join(
        _bar_row(esc(x["label"]), x["tokens"], top,
                 f'{_tok(x["tokens"])} · {x["calls"]}회 · {round(x["share"] * 100)}%')
        for x in u["uses"])
    probe = u.get("probe")
    base = (f'약 {_tok(probe["tokens"])} 토큰 — {fmt_dt(probe["at"])} 짧은 확인 호출("OK")로 잰 값. 메일 내용과 별개로 '
            'Claude Code 가 호출할 때마다 넣는 지시문이라, 대화를 묶어 호출 수를 줄일수록 덜 씁니다'
            if probe else "짧은 확인 호출(set_token.py)을 하면 여기에 크기가 나옵니다")
    cells = (("토큰으로 채우는 칸", " · ".join(TOKEN_CELLS), ""),
             ("토큰 없이 만드는 칸", " · ".join(FREE_CELLS), ""),
             ("호출마다 붙는 기본 지시문", base, f'{_tok(probe["tokens"])}' if probe else ""))

    # 대화 · 프로젝트별은 추정: 호출 토큰을 그 호출에 담은 대화들의 글자 수 비율로 나눈 값
    rows: dict[str, dict] = {k: {**v, "tokens": 0, "sent": 0} for k, v in sizes.items()}
    for key, t in u["threads"].items():
        row = rows.setdefault(key, {"subject": t["subject"], "project": t["project"], "mails": t["mails"],
                                    "chars": t["chars"], "tokens": 0, "sent": 0})
        row["tokens"], row["sent"] = t["tokens"], t["sent"]
    by_proj: dict[str, int] = {}
    for row in rows.values():
        if row["tokens"]:
            name = row["project"] or UNCLASSIFIED
            by_proj[name] = by_proj.get(name, 0) + row["tokens"]
    top = max(by_proj.values(), default=0)
    projs = "".join(_bar_row(esc(n), v, top, _tok(v), mute=n == UNCLASSIFIED)
                    for n, v in sorted(by_proj.items(), key=lambda kv: -kv[1]))
    heavy = sorted(rows.values(), key=lambda r: (-r["tokens"], -r["chars"]))[:HEAVY_TOP]
    trs = "".join(
        f'<tr><td class="subj-c" title="{esc(r["subject"])}">{esc(r["subject"])}</td><td>{esc(r["project"])}</td>'
        f'<td class="num">{r["mails"]}통</td><td class="num">{_chars(r["chars"])}</td>'
        f'<td class="num">{r["sent"]}번</td><td class="num">{_tok(r["tokens"]) if r["tokens"] else "—"}</td></tr>'
        for r in heavy) or '<tr><td colspan="6" class="empty">대화 없음</td></tr>'
    order = "추정 토큰 순" if any(r["tokens"] for r in heavy) else "아직 기록 전이라 지금 크기 순"
    return (
        f'<h4 class="ih">용도별<span class="ih-m">최근 {w}일 · 토큰을 쓰는 곳은 이 넷뿐</span></h4>'
        f'<div class="ib-set">{uses}</div>'
        '<h4 class="ih">대시보드 칸별</h4>'
        + _il((name, esc(desc), esc(val)) for name, desc, val in cells)
        + f'<h4 class="ih">프로젝트별<span class="ih-m">최근 {w}일 · 추정 (한 호출에 대화 여러 개를 묶어 보내서 글자 수 '
        '비율로 나눔)</span></h4>'
        + (f'<div class="ib-set">{projs}</div>' if projs else '<p class="inote">아직 기록이 없습니다.</p>')
        + f'<h4 class="ih">무거운 대화 {HEAVY_TOP}개<span class="ih-m">{order} · 새 메일이 한 통만 와도 대화 전체를 다시 '
        '보냄 · 정기 메일이면 projects.json 의 extract_skip 에 넣으면 빠짐</span></h4>'
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>대화</th><th>프로젝트</th><th class="num">메일</th>'
        f'<th class="num">지금 크기</th><th class="num">{w}일 동안 보낸 횟수</th><th class="num">추정 토큰</th>'
        f'</tr></thead><tbody>{trs}</tbody></table></div>'
    )


def _usage_log(m: dict) -> str:
    """호출 기록: 최근 호출부터. 입력은 캐시로 읽고 쓴 부분까지 (자세한 구성은 말풍선)."""
    rows = []
    for c in m["usage"]["log"]:
        run = f'자동 {c["slot"]}' if c["trigger"] == "auto" else "직접"
        size = (f'{c["threads"]} · {c["mails"]}통' if c["threads"] else f'{c["mails"]}통' if c["mails"] else "—")
        read = c["input_tokens"] + c["cache_read_tokens"] + c["cache_write_tokens"]
        parts = (f'입력 {c["input_tokens"]:,} · 캐시 쓰기 {c["cache_write_tokens"]:,} · '
                 f'캐시 읽기 {c["cache_read_tokens"]:,}')
        if c["ok"]:
            result = '<span class="ih-m">성공</span>'
        else:
            label = ERROR_KINDS.get(c["error_kind"], "오류") + (f' · {c["reset"]} 풀림' if c["reset"] else "")
            result = f'<span class="itag bad" title="{esc(c["error"])}">{esc(label)}</span>'
        rows.append(
            f'<tr><td class="nw"><span class="num">{esc(c["when"])}</span></td><td class="nw">{esc(run)}</td>'
            f'<td title="{esc(c["model"] or "")}">{esc(c["purpose_label"])}</td><td class="num">{esc(size)}</td>'
            f'<td class="num" title="{esc(parts)}">{_tok(read) if c["ok"] or read else "—"}</td>'
            f'<td class="num">{_tok(c["output_tokens"]) if c["ok"] or c["output_tokens"] else "—"}</td>'
            f'<td class="num">{round(c["seconds"] or 0)}초</td><td class="num">{_usd(c["cost"])}</td>'
            f'<td>{result}</td></tr>')
    if not rows:
        return '<p class="inote">아직 기록이 없습니다. 업무 정리 · 메일 요약 · 토큰 확인을 할 때마다 한 줄씩 쌓입니다.</p>'
    return ('<div class="tbl-wrap"><table class="tbl"><thead><tr><th>시각</th><th>실행</th><th>용도</th>'
            '<th class="num">대화 · 메일</th><th class="num">입력</th><th class="num">출력</th>'
            '<th class="num">걸린 시간</th><th class="num">API 환산</th><th>결과</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            f'<p class="inote">최근 {len(rows)}번 · 입력에 마우스를 올리면 캐시 구성이, 실패에 올리면 메시지가 보입니다.</p>')


def _usage_reduction(m: dict) -> str:
    from .usage_report import render_report_html
    return render_report_html(m["usage"]["reduction_report"])


USAGE_VIEWS = (("sum", "요약", _usage_sum), ("where", "어디에 쓰나", _usage_where),
               ("reduction", "절감 보고서", _usage_reduction), ("log", "호출 기록", _usage_log))
RULES_VIEWS = (("flow", "한눈에 보기", _rules_flow), ("block", "차단", _rules_block),
               ("proj", "프로젝트", _rules_proj), ("task", "할 일·역할", _rules_task),
               ("reply", "답장·기한", _rules_reply))


def render_rules_html(m: dict) -> str:
    """상단 메일 분류 규칙 버튼에서 여는 페이지."""
    seg = "".join(f'<button type="button" data-sub="{k}" aria-pressed="{str(i == 0).lower()}">'
                  f'{esc(label)}</button>' for i, (k, label, _) in enumerate(RULES_VIEWS))
    panes = "".join(f'<div class="info-p" data-pane="{k}"{"" if i == 0 else " hidden"}>'
                    f'{fn(m).replace(chr(34) + "#blocked" + chr(34), chr(34) + "./#blocked" + chr(34))}</div>'
                    for i, (k, _, fn) in enumerate(RULES_VIEWS))
    local = m["generated_at"].astimezone(KST)
    return (_head("메일 분류 규칙 · Mailwork")
            + '<body class="usage-page"><main class="wrap"><div class="card">'
            '<header class="hero"><div class="hero-t">'
            '<a class="back" href="./"><span class="back-a" aria-hidden="true"></span>Mailwork</a>'
            f'<h1>메일 분류 규칙</h1><p class="sub">{esc(_rules_line(m))}</p></div>'
            f'<div class="hero-acts">{_usage_link(m)}'
            '<button type="button" id="theme" class="pill" aria-label="테마 전환">테마: 시스템</button>'
            '</div></header><div class="usage-body" data-usage data-view-key="nwmail.rulesView">'
            f'<div class="seg" role="group" aria-label="메일 분류 규칙 보기">{seg}</div>{panes}</div></div>'
            f'<p class="page-foot">{local:%Y-%m-%d %H:%M} 기준 · 현재 설정과 분류 근거</p>'
            f'</main><script>{THEME_JS}{USAGE_JS}</script></body></html>')


def _info_section(m: dict) -> str:
    """맨 아래 2depth. 1depth 줄(접혀 있어도 요약 한 줄) → 2depth 보기 버튼 → 내용.

    토큰 사용 현황은 따로 만든 페이지(usage.html)로 옮겼다 — 머리말의 '토큰 사용 현황' 버튼.
    """
    rows = (
        ("rules", "메일 분류 규칙", _rules_line(m),
         (("flow", "한눈에 보기", _rules_flow), ("block", "차단", _rules_block), ("proj", "프로젝트", _rules_proj),
          ("task", "할 일·역할", _rules_task), ("reply", "답장·기한", _rules_reply))),
    )
    out = []
    for key, title, line, subs in rows:
        seg = "".join(f'<button type="button" data-sub="{k}" aria-pressed="{"true" if i == 0 else "false"}">'
                      f'{esc(label)}</button>' for i, (k, label, _) in enumerate(subs))
        panes = "".join(f'<div class="info-p" data-pane="{k}"{"" if i == 0 else " hidden"}>{fn(m)}</div>'
                        for i, (k, _, fn) in enumerate(subs))
        out.append(
            f'<div class="info-row" data-info="{key}">'
            f'<button type="button" class="info-h" id="info-h-{key}" aria-expanded="false" '
            f'aria-controls="info-b-{key}"><span class="info-t">{esc(title)}</span>'
            f'<span class="info-s">{esc(line)}</span></button>'
            f'<div class="info-b" id="info-b-{key}" role="region" aria-labelledby="info-h-{key}" hidden>'
            f'<div class="seg" role="group" aria-label="{esc(title)} 보기">{seg}</div>{panes}</div></div>')
    return f'<section class="info" aria-label="메일 분류 규칙">{"".join(out)}</section>'


def render_usage_html(m: dict) -> str:
    """토큰 사용 현황 페이지 (usage.html). 대시보드 머리말의 버튼에서 넘어온다.

    대시보드와 같은 CSS·테마를 쓰되, 버튼 기록을 보내지 않으므로 서버 설정도 큰 JS 도 필요 없다.
    """
    seg = "".join(f'<button type="button" data-sub="{k}" aria-pressed="{"true" if i == 0 else "false"}">'
                  f'{esc(label)}</button>' for i, (k, label, _) in enumerate(USAGE_VIEWS))
    panes = "".join(f'<div class="info-p" data-pane="{k}"{"" if i == 0 else " hidden"}>{fn(m)}</div>'
                    for i, (k, _, fn) in enumerate(USAGE_VIEWS))
    local = m["generated_at"].astimezone(KST)
    return (
        _head("토큰 사용 현황 · Mailwork")
        + '<body class="usage-page"><main class="wrap"><div class="card">'
        '<header class="hero"><div class="hero-t">'
        f'<a class="back" href="./"><span class="back-a" aria-hidden="true"></span>Mailwork</a>'
        '<h1>토큰 사용 현황</h1>'
        f'<p class="sub">{esc(_usage_line(m))}</p></div>'
        f'<div class="hero-acts">{_rules_link()}'
        '<button type="button" id="theme" class="pill" aria-label="테마 전환">테마: 시스템</button>'
        '</div></header>'
        f'<div class="usage-body" data-usage><div class="seg" role="group" aria-label="토큰 사용 현황 보기">'
        f'{seg}</div>{panes}</div></div>'
        f'<p class="page-foot">{esc(f"{local:%Y-%m-%d %H:%M}")} 기준 · 자동 정리가 돌 때마다 다시 만들어집니다</p>'
        f'</main><script>{THEME_JS}{USAGE_JS}</script></body></html>'
    )


def _custom_css(custom: dict[int, str]) -> str:
    """직접 고른 프로젝트 색: 10번 칸부터 라이트 · 다크 각각 밝기를 맞춘 값(colors.tones)으로 정의한다."""
    if not custom:
        return ""
    t = {n: tones(h) for n, h in custom.items()}
    light = ";".join(f"--s{n}:{v['light']}" for n, v in t.items())
    dark = ";".join(f"--s{n}:{v['dark']}" for n, v in t.items())
    return (f'<style id="pc-css">:root{{{light}}}'
            f'@media (prefers-color-scheme:dark){{:root:where(:not([data-theme="light"])){{{dark}}}}}'
            f':root[data-theme="dark"]{{{dark}}}'
            + "".join(f".s{n}{{background:var(--s{n})}}" for n in t) + "</style>")


def render_html(m: dict, notice: str = "", server: dict | None = None) -> str:
    """server = {"port": 8787, "token": "..."} 이면 버튼 기록을 그 로컬 서버로 보낸다."""
    s = m["stats"]
    conf = {"port": (server or {}).get("port"), "token": (server or {}).get("token", ""),
            "labels": LABELS, "reasons": {k: list(v) for k, v in REASONS.items()},
            "analysisExclusions": EXTRACT_EXCLUSION_REASONS,
            "taskAliases": m.get("task_aliases", {}),
            "projectTasks": {p["name"]: p["progress_items"] for p in m["projects"]},
            "projectActions": m.get("project_actions", {}),
            "marks": MARKS, "waitPreview": WAIT_PREVIEW, "unclassified": UNCLASSIFIED,
            # 일정 탭: 기한 등급은 서버의 오늘(KST) 기준으로 센다
            "today": m["today"].isoformat(), "week": SCHEDULE_WEEK, "cap": SCHEDULE_CAP,
            "glyphs": GLYPH, "holidays": HOLIDAYS, "colorNames": list(COLOR_NAMES), "maxSlots": MAX_SLOTS,
            "colorBands": BANDS}
    conf_json = json.dumps(conf, ensure_ascii=False).replace("<", "\\u003c")
    banner = ""
    if not s["extracted"] and s["threads"] - s.get("excluded", 0):
        banner = ('<div class="banner">업무·결정사항 정리가 아직 없습니다. 프로젝트는 제목 키워드·태그·'
                  '발신 도메인으로 먼저 묶어 보여줍니다. 정리: <code>python extract.py</code> 후 '
                  '<code>python dashboard.py</code></div>')
    if notice:
        banner = f'<div class="banner warn" role="alert">{esc(notice)}</div>' + banner
    if alerts := m.get("recipient_alerts"):
        rows = "".join(
            f'<li><strong>{esc(a["subject"])}</strong>'
            f'<span>{esc(a["sender"])} · {esc(parse_dt(a["at"]).astimezone(KST).strftime("%m/%d %H:%M"))}</span></li>'
            for a in alerts)
        banner = (f'<section class="banner warn recipient-alerts" role="status" aria-label="받는사람 재지정 알림">'
                  f'<strong>받는사람으로 재지정되었습니다.</strong>'
                  f'<p>분석에서 제외한 대화 {len(alerts)}건의 새 메일에 받는사람으로 포함되어 있습니다. '
                  'AI 분석 제외는 유지됩니다. 다시 분석하려면 해당 할 일의 제외·삭제 표시를 되돌리세요.</p>'
                  f'<ul>{rows}</ul></section>') + banner

    tabs = [("tab-today", "오늘", None),
            ("tab-wait", "답장 대기", s["unanswered"]),
            ("tab-projects", "프로젝트", s["projects_open"] + s["projects_delayed"]),
            ("tab-schedule", "일정", s["sched_badge"]),
            ("tab-blocked", "차단됨", s["blocked"])]
    # 답장 대기 숫자는 기다리는 메일이 있으면 주황 (오늘 탭에서도 탭 줄에서 바로 보이게)
    tablist = "".join(
        f'<button class="tab" role="tab" id="b-{tid}" aria-controls="{tid}" '
        f'data-target="{tid}" aria-selected="false" tabindex="-1">{esc(label)}'
        + (f'<span class="cnt{" warn" if tid == "tab-wait" and n else ""}" data-tabcnt="{tid}">{n}</span>'
           if n is not None else "")
        + "</button>"
        for tid, label, n in tabs)

    panels = {
        "tab-today": _today_tab(m),
        "tab-wait": f'<div class="pad">{_waiting(m)}</div>',
        "tab-projects": _projects_tab(m),
        "tab-schedule": _schedule_tab(m),
        "tab-blocked": f'<div class="pad">{_blocked_tab(m)}</div>',
    }
    panel_html = "".join(
        f'<section class="panel" role="tabpanel" id="{tid}" aria-labelledby="b-{tid}" hidden>'
        f'{panels[tid]}</section>' for tid, _, _ in tabs)

    return (
        _head("Mailwork") + "<body>" + _custom_css(m.get("custom_colors") or {}) +
        f'<main class="wrap"><div class="card">{banner}{_hero(m)}{_kpis(m)}'
        '<div class="view-toolbar">'
        f'<nav class="tabs" role="tablist" aria-label="대시보드 보기">{tablist}</nav>'
        '<label class="pill switch">완료 숨기기<input type="checkbox" id="hide-done" role="switch"></label></div>'
        f'{panel_html}</div></main>{_act_templates(m)}'
        '<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>'
        f'<script type="application/json" id="nw-conf">{conf_json}</script>'
        f'<script>{THEME_JS}{PREVIEW_JS}{JS}</script></body></html>'
    )


# 색은 1a(라이트) · 1b(다크) 디자인 기준. 글꼴은 PC 에 설치된 Pretendard, 없으면 맑은 고딕.
LIGHT = """
 --page:#f2f1ec;--card:#fdfcf9;--side:#faf9f5;--surface:#fff;
 --ink:#1c1b18;--ink-2:#57534a;--muted:#6f6a60;
 --line:rgba(0,0,0,.08);--line-2:rgba(0,0,0,.07);--box:#c9c4b8;--switch:#ddd9cf;
 --acc:#1f6f6a;--acc-bg:#e6efee;--acc-ink:#fff;
 --warn:#b06a1e;--warn-bg:#f3e6d4;--warn-soft:#d9c59a;--crit:#b3261e;--caution:#c28a00;--caution-ink:#7f5a00;
 --tag:#eeeae0;--tag-ink:#6b6252;--track:#eeeae0;--new-bg:#e6efee;--new-ink:#14514d;
 --shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px -18px rgba(0,0,0,.25);--focus:#1f6f6a;
 --s0:#898781;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
 --s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948;--s9:#a64ba0"""

DARK = """color-scheme:dark;
 --page:#0c1413;--card:#15201f;--side:#121c1b;--surface:#1b2927;
 --ink:#f2f1ec;--ink-2:#c3d0cd;--muted:#98a8a4;
 --line:rgba(255,255,255,.09);--line-2:rgba(255,255,255,.07);--box:#5d6e6a;
 --switch:rgba(255,255,255,.2);
 --acc:#5fd2c0;--acc-bg:rgba(95,210,192,.12);--acc-ink:#0f1a19;
 --warn:#e8b476;--warn-bg:rgba(224,160,74,.18);--warn-soft:#6b5a3e;--crit:#f08a7e;
 --caution:#e8c15a;--caution-ink:#e8c15a;--tag:rgba(255,255,255,.08);--tag-ink:#c3d0cd;
 --track:rgba(255,255,255,.08);--new-bg:rgba(95,210,192,.18);--new-ink:#9fe7da;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -18px rgba(0,0,0,.6);--focus:#5fd2c0;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;
 --s7:#9085e9;--s8:#e66767;--s9:#961dae"""

CSS = (":root{color-scheme:light;" + LIGHT + "}"
       '@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){' + DARK + "}}"
       ':root[data-theme="dark"]{' + DARK + "}" + """
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--page);color:var(--ink);-webkit-font-smoothing:antialiased;
 font:14px/1.5 Pretendard,"Pretendard Variable",-apple-system,BlinkMacSystemFont,"Segoe UI",
 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
.num{font-variant-numeric:tabular-nums}
.c-acc{color:var(--acc)}.c-warn{color:var(--warn)}
ul{list-style:none;margin:0;padding:0}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:var(--side);
 border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--ink)}

.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:16px 24px;flex-wrap:wrap;padding:34px 40px 26px}
.eyebrow{margin:0;font-size:11px;font-weight:500;letter-spacing:.14em;color:var(--muted)}
h1{margin:12px 0 8px;font-size:30px;line-height:1.2;font-weight:600;letter-spacing:-.01em}
h1 em{font-style:normal}
.sub{margin:0;font-size:13px;color:var(--muted)}
.hero-side{display:flex;flex-direction:column;align-items:flex-end;gap:10px;padding-top:6px}
.tools{display:flex;flex-wrap:wrap;gap:8px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border:1px solid var(--line);
 border-radius:999px;background:var(--surface);font:inherit;font-size:12.5px;color:var(--ink-2);cursor:pointer}
/* 머리말 오른쪽: 토큰 사용 현황(따로 만든 페이지로 감) · 테마 */
.hero-acts{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
a.pill{text-decoration:none}
a.pill:hover{border-color:var(--box);background:var(--side)}
.pill-n{padding:1px 7px;border-radius:5px;background:var(--tag);color:var(--tag-ink);font-size:11.5px;font-weight:600}
.pill-d{font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.pill-d.up{color:var(--warn)}.pill-d.down{color:var(--acc)}
.pill-go{width:6px;height:6px;margin:0 1px 0 -2px;border-top:1.5px solid var(--muted);
 border-right:1.5px solid var(--muted);transform:rotate(45deg)}
/* 토큰 사용 현황 페이지 */
.usage-page .hero{align-items:flex-start;padding:30px 40px 24px}
.usage-page .hero-t{display:block}
.usage-page h1{margin:8px 0 8px}
.back{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);text-decoration:none}
.back:hover{color:var(--ink-2)}
.back-a{width:6px;height:6px;border-bottom:1.5px solid currentColor;border-left:1.5px solid currentColor;
 transform:rotate(45deg)}
.usage-body{padding:4px 40px 36px}
.usage-body>.seg{margin:0 0 20px}
.page-foot{margin:14px 2px 0;font-size:12px;color:var(--muted)}
.switch input{appearance:none;position:relative;flex:none;width:26px;height:15px;margin:0;border-radius:999px;
 background:var(--switch);cursor:pointer;transition:background .15s}
.switch input::after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;
 background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2);transition:transform .15s}
.switch input:checked{background:var(--acc)}.switch input:checked::after{transform:translateX(11px)}
.runs{margin:0;font-size:12px;color:var(--muted)}.runs b{font-weight:500;color:var(--ink)}
.banner{background:var(--side);border:1px solid var(--line);border-radius:10px;
 padding:10px 14px;color:var(--ink-2);margin:0 0 16px;font-size:13px}
.card>.banner{margin:0 40px 20px}
.banner.warn{border-color:var(--warn);color:var(--ink)}
.recipient-alerts p{margin:6px 0;color:var(--ink-2)}
.recipient-alerts li{padding:8px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}
.recipient-alerts li span{display:block;font-size:12px;color:var(--muted)}

.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.hero{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:24px 40px}
.hero-t{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;min-width:0}
.hero .sub{overflow-wrap:anywhere}
.brand-identity{display:flex;align-items:center;flex-wrap:wrap;gap:12px;min-width:0}
.brand-mark{display:block;flex:none;width:auto;height:30px;color:var(--ink)}
.brand-account{display:flex;flex-direction:column;gap:1px;min-width:0}
.hero .brand-email{font-size:13px;line-height:1.5;color:var(--ink-2)}
.brand-date{margin:0;font-size:12px;line-height:1.5;color:var(--muted);font-variant-numeric:tabular-nums}
.digest-overview{display:grid;grid-template-columns:minmax(0,1.62fr) minmax(0,1fr);gap:48px;padding:0 40px 28px}
.work-overview{min-width:0}
.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:40px;padding:0}
.kpi{min-width:0}
.kpi p,.kpi h2{margin:0}
.kpi .l,.system-summary h2{font-size:12px;font-weight:600;color:var(--muted)}
.kpi .v{display:flex;align-items:baseline;gap:10px;margin-top:10px}
.kpi .v b{font-size:48px;line-height:1.2;font-weight:650;letter-spacing:-1.5px}
.kpi .u{font-size:13px;color:var(--muted)}
.kpi .c{margin-top:8px;font-size:12px;color:var(--ink);line-height:1.7;word-break:keep-all;overflow-wrap:anywhere}
.deadline-counts{display:flex;flex-wrap:wrap;gap:4px 16px}
.deadline-counts>span{white-space:nowrap}
.deadline-counts>span>span{font-weight:650}
.project-summary{margin:20px 0 0;padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.project-summary b{color:var(--ink);font-weight:650}
.system-summary{min-width:0;padding:16px 20px;background:var(--side);border:1px solid var(--line);border-radius:12px}
.system-summary h2{margin:0 0 10px}
.system-summary dl{margin:0;display:grid;gap:8px;font-size:12px;line-height:1.65}
.system-summary dl>div{display:grid;grid-template-columns:56px minmax(0,1fr);gap:12px}
.system-summary dt{color:var(--muted)}
.system-summary dd{margin:0;overflow-wrap:anywhere;word-break:keep-all}
.system-summary b{font-weight:400}
.view-toolbar{display:flex;align-items:center;gap:16px;padding-right:40px;border-top:1px solid var(--line);box-shadow:inset 0 -1px 0 var(--line)}
.view-toolbar .tabs{flex:1;min-width:0;box-shadow:none}
.view-toolbar .switch{flex:none;border:0;padding:0;background:transparent;gap:10px;color:var(--muted);white-space:nowrap}
.view-toolbar .switch input{width:32px;height:18px}
.view-toolbar .switch input::after{width:14px;height:14px}
.view-toolbar .switch input:checked::after{transform:translateX(14px)}
.card>.banner:first-child{margin:0;padding:8px 40px;border:0;border-bottom:1px solid var(--line);border-radius:0;font-size:12px}
.card>.banner.warn:first-child{border-bottom-color:var(--warn)}

.tabs{display:flex;gap:4px;padding:0 30px;box-shadow:inset 0 -1px 0 var(--line);overflow:auto hidden}
.tab{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
 padding:14px 10px 12px;font:inherit;font-size:13.5px;color:var(--muted);cursor:pointer;white-space:nowrap}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
.cnt{display:inline-block;min-width:18px;margin-left:6px;padding:0 6px;border-radius:999px;
 background:var(--tag);color:var(--tag-ink);font-size:11px;font-weight:500;text-align:center}
.cnt.warn{background:var(--warn-bg);color:var(--warn);font-weight:600}
.pad{padding:28px 36px 40px}

/* 오늘 탭은 한 칸 (머리 요약과 같은 40px 들여쓰기) */
.today{padding:34px 40px 44px;min-width:0}
.sec-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:0 0 18px}
.sec-h.gap{margin-top:38px}
.sec-h h2{margin:0;font-size:16px;line-height:1.2;font-weight:600}
.sec-m{flex:none;font-size:11.5px;color:var(--muted)}
.note{margin:-6px 0 14px;font-size:12.5px;color:var(--muted)}

.task.done .t{text-decoration:line-through;color:var(--muted)}
.task:not([data-state="open"])>.rtag{opacity:.55}
.hide-done .task.done{display:none}

.seg{display:inline-flex;flex-wrap:wrap;gap:2px;padding:3px;border-radius:999px;background:var(--tag)}
.seg button{appearance:none;border:0;border-radius:999px;background:none;padding:6px 12px;font:inherit;
 font-size:12.5px;color:var(--muted);cursor:pointer;white-space:nowrap}
.seg button b{font-weight:600;margin-left:2px}
.seg button[aria-pressed="true"]{background:var(--surface);color:var(--ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
#tab-today .note{margin:-8px 0 16px}
#srv-note{margin:0 0 16px}
.acts{display:flex;flex-direction:column;gap:12px}
/* 할 일 보기: 구역 머리(마감 있는 일 · 기한 없는 일) > 프로젝트 머리줄 > 패널(대화 줄 · 왼쪽 날짜 칸) */
.acts-sum{margin:-6px 0 18px;font-size:12.5px;color:var(--muted)}
.acts-sum b{color:var(--ink-2);font-weight:600}
.acts-sum .over,.acts-sum .over b{color:var(--crit)}
.az+.az{margin-top:32px}
.az-h{display:flex;align-items:center;gap:8px;margin:0 0 12px;font-size:14px;font-weight:650;color:var(--ink)}
.az-h::after{content:"";flex:1;order:1;height:1px;background:var(--line)}
.az-n{min-width:22px;padding:1px 7px;border-radius:999px;background:var(--tag);color:var(--tag-ink);
 font-size:11.5px;font-weight:600;line-height:18px;text-align:center}
.az-fold{order:2;appearance:none;border:0;background:none;padding:0;font:inherit;font-size:12px;font-weight:500;
 color:var(--muted);cursor:pointer}
.az-fold:hover{color:var(--ink)}
.apgs{display:flex;flex-direction:column;gap:28px}
/* 프로젝트 머리줄: 색 네모 · 이름 · 개수 · 가는 선 · 접기 화살표(맨 끝) */
.apg>summary{display:flex;align-items:center;gap:8px;margin:0 0 12px;padding:0;border-top:0;list-style:none;cursor:pointer;
 user-select:none;color:var(--ink)}
.apg:not([open])>summary{margin-bottom:0}
.apg>summary::after{content:"";flex:1;order:3;height:1px;background:var(--line-2)}
.apg>summary .chv{order:4;font-size:12px}
.apg>summary:hover .chv{color:var(--ink)}
.apg>summary .sw{flex:none;width:11px;height:11px;border-radius:3px}
.apg>summary::-webkit-details-marker,.cv-oth>summary::-webkit-details-marker{display:none}
.chv{display:inline-block;width:14px;flex:none;color:var(--muted);font-size:10px;text-align:center;transition:transform .15s}
.chv::before{content:"▶"}
/* 카드 안 접기(결정사항 · 메일 대화 · 제외한 할 일 · 답변완료한 메일 …)도 '다른 담당자' 와 같은 작은 ▶ */
:is(.decs,.gone,.pend,.excl,.rel,.handled)>summary{list-style:none}
:is(.decs,.gone,.pend,.excl,.rel,.handled)>summary::-webkit-details-marker{display:none}
:is(.decs,.gone,.pend,.excl,.rel,.handled)>summary::before{content:"▶";display:inline-block;width:14px;margin-right:6px;
 color:var(--muted);font-size:10px;text-align:center;vertical-align:1px;transition:transform .15s}
:is(.decs,.gone,.pend,.excl,.rel,.handled)[open]>summary::before{transform:rotate(90deg)}
.apg[open]>summary .chv,.cv-oth[open]>summary .chv{transform:rotate(90deg)}
.apg-n{font-size:17px;font-weight:650;letter-spacing:-.01em}
.apg-c{font-size:13px;color:var(--muted)}
/* 패널: 대화는 줄로. 줄 사이 선은 1px 틈으로 그려 숨긴 줄이 있어도 선이 겹치지 않는다 */
.cvs{display:flex;flex-direction:column;gap:1px;overflow:hidden;background:var(--line);border:1px solid var(--line-2);
 border-radius:14px}
.cv{padding:16px 18px 6px 20px;background:var(--surface)}
.cv-b{min-width:0}
/* 제목 위 날짜 칩: 마감 있는 일은 가장 급한 마감일 · D-day (기한 등급 색: 지남 빨강 · 오늘 주황 · 3일 안 노랑).
   기한 없는 대화는 빈 줄 (높이 0) — 최근 메일 시각은 메일 줄에 */
.cv-due{margin:0 0 7px}
.cv-due:empty{margin:0}
.cv-dc{display:inline-block;padding:3px 9px;border-radius:7px;background:var(--tag);color:var(--tag-ink);
 font-size:12px;font-weight:600;line-height:1.35;white-space:nowrap}
.cv-dc b{font-weight:700}
.cv-dc.lv-overdue{background:color-mix(in srgb,var(--crit) 16%,transparent);color:var(--crit)}
.cv-dc.lv-today{background:var(--warn-bg);color:var(--warn)}
.cv-dc.lv-soon{background:color-mix(in srgb,var(--caution) 16%,transparent);color:var(--caution-ink)}
/* 프로젝트 묶음 안에서는 프로젝트 이름을 뺀다 (머리줄에 있음) */
.apg .cv-m .pj{display:none}
@media (max-width:560px){.apg-n{font-size:16px}}
.cv-h h3{margin:0;font-size:14.5px;line-height:1.4;font-weight:650;color:var(--ink);overflow-wrap:anywhere;
 word-break:keep-all}
.cv-h h3 :is(.new,.chip,.stt){vertical-align:2px}
.cv-h h3 .stt{margin:0 0 0 6px}
.cv-m{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin:4px 0 0;font-size:12px;color:var(--muted)}
.cv-m .pj{color:var(--ink-2)}
.cv-mail i{margin-right:5px;font-style:normal}
.cv-m .rep{font-weight:600;color:var(--warn)}
.cv-s{margin:8px 0 0;font-size:13px;line-height:1.6;color:var(--ink-2)}
.cv-s .tx{margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:keep-all}
.cv-s.open .tx{display:block}
.cv-l{margin-right:6px;color:var(--muted);font-weight:500}
.cv-more{display:none;appearance:none;border:0;background:none;padding:0;margin-top:2px;font:inherit;font-size:12px;
 color:var(--muted);cursor:pointer}
.cv-s.clip .cv-more,.cv-s.open .cv-more{display:inline-block}
.cv-more:hover{color:var(--ink)}
.tks{margin-top:10px;border-top:1px solid var(--line)}
.tk{display:flex;flex-wrap:wrap;align-items:flex-start;gap:4px 10px;padding:9px 0 8px;border-bottom:1px dashed var(--line)}
.tk:last-child{border-bottom:0}
.tk-b{flex:1;min-width:0}
.tk-t{margin:0;font-size:14px;line-height:1.5;font-weight:500;color:var(--ink);overflow-wrap:anywhere;word-break:keep-all}
.tk-b .dup{display:block;margin-top:2px;font-size:11.5px;color:var(--muted)}
.tk-b .sim{margin-top:4px}
.tk-r{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:6px 8px;flex:none;margin-top:1px}
.tk .due{padding:2px 8px;border-radius:999px;background:var(--tag);font-size:12px;font-weight:600;white-space:nowrap}
.tk .due.lv-overdue{background:color-mix(in srgb,var(--crit) 15%,transparent)}
.tk .due.lv-today{background:var(--warn-bg)}
.tk .due.lv-soon{color:var(--caution)}
.tk .due.lv-later{font-weight:500;color:var(--ink-2)}
/* 버튼: 프로젝트 탭 할 일과 똑같이 — 같은 테두리 단추(.btn)를 그 줄 오른쪽 끝에 늘 띄운다 */
.tk .ctl{gap:4px;margin-left:auto}
.tk .btn{padding:3px 9px;font-size:11.5px}
.tk .undo,.tk:not([data-state="open"]) .btn:not(.undo){display:none}
.tk:not([data-state="open"]) .undo{display:inline-block}
.tk[data-state="done"] .tk-t{color:var(--muted);text-decoration:line-through;text-decoration-color:var(--box)}
.tk[data-state="excluded"] .tk-t,.tk[data-state="deleted"] .tk-t{color:var(--muted)}
.tk:not([data-state="open"]) .due{opacity:.45}
.tk[data-state="done"] .st{background:var(--acc-bg);color:var(--acc)}
.tk .why{flex-basis:100%;margin:4px 0 2px;padding-top:10px}
.cv-oth{margin:2px 0}
.cv-oth>summary{display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 0;list-style:none;font-size:12px;
 color:var(--muted);cursor:pointer}
.cv-oth>summary:hover{color:var(--ink)}
.cv-oth .late{font-weight:600;color:var(--crit)}
.cv-oth>div{display:grid;grid-template-columns:max-content minmax(0,1fr) max-content;gap:5px 14px;align-items:baseline;
 margin:2px 0 6px 20px;font-size:12.5px;line-height:1.6;color:var(--ink-2);word-break:keep-all}
.cv-fin{display:none;margin:6px 0 4px;font-size:12px;color:var(--acc)}
.cv.allset>*{opacity:.6}      /* 줄 바탕은 그대로 둬야 패널 선 색이 비치지 않는다 */
.cv.allset:hover>*,.cv.allset:focus-within>*{opacity:1}
.cv.allset .cv-fin{display:block}
/* 넓은 화면: 왼쪽 메일(제목 · 메일 줄 · 현황 · 다른 담당자) | 오른쪽 내 할 일. 요약 한 줄 길이는 좁던 때와 비슷하게
   두고 카드 높이를 줄인다. 할 일 칸이 좁으니 기한 · 버튼은 글 아래 줄로 내리고, 버튼은 그 줄 오른쪽 끝.
   날짜 칩 줄(1행)은 두 칸에 걸친 머리라 할 일 칸은 제목 높이(2행)부터. 기한 없는 대화는 이 줄 높이가 0 */
@media (min-width:901px){
 .cv{padding-bottom:12px}
 .cv-b{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(0,1fr);grid-template-rows:auto auto auto auto auto 1fr;
  column-gap:28px;align-items:start}
 .cv-b>*{grid-column:1}
 .cv-b>.cv-due{grid-column:1/-1;grid-row:1}
 .cv-b>.tks{grid-column:2;grid-row:2/-1;align-self:stretch;margin:-6px 0;padding-left:22px;border-top:0;
  border-left:1px solid var(--line)}
 .cv-oth>summary{border-top:0}
 .tk-r{flex-basis:100%;justify-content:flex-start}
 .tk-r .due:empty{display:none}
}
.hide-done .tk[data-state="done"],.hide-done .cv.alldone{display:none}
.act{padding:18px 22px 16px 20px;background:var(--surface);border:1px solid var(--line-2);border-left-width:3px;border-radius:12px}
.act[data-state="open"][data-level="overdue"]{border-left-color:var(--crit)}
.act[data-state="open"][data-level="today"]{border-left-color:var(--warn)}
.act[data-state="open"][data-level="soon"]{border-left-color:var(--caution)}
.act[data-state="done"],.act[data-state="excluded"],.act[data-state="deleted"]{background:var(--side)}
.act p{margin:0}
.tg{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin-bottom:9px;font-size:11.5px;color:var(--muted)}
.tag{display:inline-flex;align-items:center;gap:4px;padding:5px 8px;border-radius:5px;background:var(--tag);
 color:var(--tag-ink);font-size:10.5px;font-weight:500;line-height:1;letter-spacing:.04em}
.tag i{font-style:normal;font-size:9px}
.tag.u-high{background:var(--warn-bg);color:var(--warn)}
.tag.u-medium{background:var(--acc-bg);color:var(--acc)}
.pj{display:inline-flex;align-items:center;gap:5px}
.due{font-weight:500;color:var(--ink-2)}.due.lv-today{color:var(--warn)}.due.lv-overdue{color:var(--crit)}
.due.lv-later{font-weight:400;color:var(--muted)}.due.lv-none{display:none}
.man{display:none;padding:3px 6px;border-radius:4px;background:var(--acc-bg);color:var(--acc);font-size:10.5px;
 font-weight:600;line-height:1}
[data-manual="1"] .man{display:inline-block}
.act .t{font-size:15px;line-height:1.45;font-weight:600;color:var(--ink)}
.tg .pj{margin-left:auto;color:var(--ink-2)}
.stt{display:inline-block;margin-right:7px;padding:0 6px;border-radius:4px;background:var(--tag);color:var(--tag-ink);
 font-size:11px;font-weight:600;line-height:18px;vertical-align:1px}
.stt.s-hold{background:var(--warn-bg);color:var(--warn)}.stt.s-done{background:var(--acc-bg);color:var(--acc)}
/* 다른 담당자 한 줄 = 바깥 칸(이름 | 할 일 | 기한)을 그대로 쓰는 subgrid. 이름 길이와 상관없이 할 일이 한 선에서 시작 */
.oth{display:grid;grid-column:1/-1;grid-template-columns:subgrid;align-items:baseline;margin:0}
.oth>.who{grid-column:1}.oth>.rx{grid-column:2}.oth>.dl{grid-column:3;font-size:12px}
.oth .nm{color:var(--ink);font-weight:500}.oth.more{display:block;font-size:12px;color:var(--muted)}
.tf{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:14px;font-size:12px;color:var(--muted)}
.mail{position:relative;display:inline-flex;align-items:center;gap:6px;min-width:0;max-width:100%;margin:0 auto 0 -7px;
 padding:4px 8px 4px 7px;border-radius:6px;color:var(--ink-2);cursor:help}
.mail:empty{padding:0;cursor:auto}
.mail:hover,.mail:focus-visible{background:var(--tag)}
.mail .ic{font-style:normal;color:var(--muted)}
.mail .mi{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 text-decoration:underline dotted var(--box);text-underline-offset:3px}
.mail .rep{flex:none;font-weight:600;color:var(--warn)}
.tip{position:absolute;left:0;bottom:calc(100% + 8px);z-index:6;width:max-content;max-width:min(480px,calc(100vw - 56px));
 padding:9px 12px 10px;border-radius:10px;background:var(--ink);color:var(--card);box-shadow:0 8px 24px rgba(0,0,0,.22);
 font-size:13px;font-weight:500;line-height:1.5;white-space:normal;overflow-wrap:anywhere;cursor:text;
 opacity:0;visibility:hidden;transform:translateY(4px);transition:opacity .12s,transform .12s,visibility 0s linear .12s}
.tip::after{content:"";position:absolute;left:0;right:0;top:100%;height:10px}
.tip-l{display:block;margin-bottom:2px;font-size:10.5px;font-weight:600;letter-spacing:.04em;opacity:.65}
.mail:hover .tip,.mail:focus .tip{opacity:1;visibility:visible;transform:none;
 transition:opacity .12s linear .15s,transform .12s ease .15s,visibility 0s linear .15s}
.mail.tip-off .tip{opacity:0;visibility:hidden;transition:none}
/* 제목 옆 (!) 도움말: 말풍선은 제목 줄(.sec-h) 폭 안에서 아래로 연다 — 좁은 화면에서도 밖으로 넘치지 않게 */
.sec-h.has-hint{position:relative}
.sec-t{display:flex;align-items:center;gap:6px;min-width:0}
.infotip{display:inline-flex;flex:none;border-radius:50%;color:var(--muted);cursor:help}
.infotip .hi{display:inline-grid;place-items:center;width:16px;height:16px;border:1.5px solid currentColor;
 border-radius:50%;font-size:10.5px;font-weight:700;line-height:1;font-style:normal}
.infotip:hover,.infotip:focus-visible{color:var(--ink)}
.infotip .tip{left:0;top:calc(100% + 8px);bottom:auto;max-width:min(560px,100%);white-space:pre-line;
 transform:translateY(-4px);cursor:auto}
.infotip .tip::after{top:auto;bottom:100%;height:22px}
.infotip:hover .tip,.infotip:focus .tip{opacity:1;visibility:visible;transform:none;
 transition:opacity .12s linear .15s,transform .12s ease .15s,visibility 0s linear .15s}
.infotip.tip-off .tip{opacity:0;visibility:hidden;transition:none}
.st:empty{display:none}
.st{flex:none;padding:3px 9px;border-radius:999px;background:var(--tag);color:var(--tag-ink);font-size:11.5px;font-weight:500}
.act[data-state="done"] .st,.sit[data-state="done"] .st{background:var(--acc-bg);color:var(--acc)}
.ctl{display:inline-flex;flex:none;flex-wrap:wrap;gap:6px}
.btn{appearance:none;padding:5px 11px;border:1px solid var(--line);border-radius:999px;background:var(--surface);
 font:inherit;font-size:12px;color:var(--ink-2);cursor:pointer;white-space:nowrap}
.btn:hover:not(:disabled){border-color:var(--box);color:var(--ink)}
.btn:disabled{cursor:not-allowed;opacity:.5}
.btn.ok{border-color:color-mix(in srgb,var(--acc) 45%,transparent);color:var(--acc);font-weight:600}
.btn.ok:hover:not(:disabled){background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.act .undo,.task .undo,.sit .undo,
.act:not([data-state="open"]) .ctl .btn:not(.undo),
.task:not([data-state="open"]) .ctl .btn:not(.undo),
.sit:not([data-state="open"]) .ctl .btn:not(.undo){display:none}
.act:not([data-state="open"]) .undo,.task:not([data-state="open"]) .undo,
.sit:not([data-state="open"]) .undo{display:inline-block}
/* 제외 · 업무삭제 사유: 줄 아래 점선 밑, 고르는 칸(.why-p)은 오른쪽 — 누른 버튼 밑 */
.why{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;margin-top:12px;padding-top:12px;
 border-top:1px dashed var(--line)}
.why-p{display:flex;flex:0 1 auto;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:6px;max-width:100%;
 margin-left:auto}
.why-l{margin-right:4px;font-size:12px;font-weight:600;color:var(--ink-2)}
.why-note{flex:1 1 240px;min-width:0;font-size:12px;line-height:1.55;color:var(--muted);word-break:keep-all}
.why .btn.skip{color:var(--muted)}
.toast{position:fixed;left:50%;bottom:24px;z-index:10;display:flex;align-items:center;gap:12px;max-width:calc(100% - 32px);
 transform:translateX(-50%);padding:10px 12px 10px 16px;border-radius:12px;background:var(--ink);color:var(--card);
 font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.2)}
.toast button{appearance:none;border:0;border-radius:8px;padding:5px 10px;background:rgba(127,127,127,.25);
 font:inherit;font-size:12.5px;font-weight:600;color:inherit;cursor:pointer}

.progs{display:flex;flex-direction:column;gap:1px;background:var(--line);border:1px solid var(--line-2);border-radius:12px;overflow:hidden}
.progress-tabs{margin-bottom:12px}
.prow{display:flex;align-items:center;background:var(--surface)}
.prow[data-pstate="delayed"] .pv{color:var(--crit)}
.prow:hover{background:var(--side)}
.pgo{appearance:none;flex:1;min-width:0;display:grid;grid-template-columns:190px minmax(0,1fr) 92px;gap:20px;align-items:center;
 margin:0;padding:18px 8px 18px 22px;border:0;background:none;font:inherit;color:inherit;text-align:left;cursor:pointer}
.prow>.kb{padding-right:14px}
#progs-empty{margin-top:4px}

.kb{flex:none;display:inline-flex}
.kb-btn{appearance:none;display:grid;place-items:center;width:28px;height:28px;margin:-4px 0;padding:0;border:0;
 border-radius:7px;background:none;color:var(--muted);cursor:pointer}
.kb-btn svg{fill:currentColor}
.kb-btn:hover:not(:disabled),.kb-btn[aria-expanded="true"]{background:var(--tag);color:var(--ink)}
.kb-btn:disabled{cursor:not-allowed;opacity:.4}
.kb-menu{position:fixed;z-index:20;display:flex;flex-direction:column;min-width:156px;padding:4px;
 background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.18)}
.kb-menu button{appearance:none;padding:8px 10px;border:0;border-radius:6px;background:none;font:inherit;
 font-size:13px;color:var(--ink);text-align:left;cursor:pointer;white-space:nowrap}
.kb-menu button:hover,.kb-menu button:focus-visible{background:var(--tag);outline:none}
.kb-menu [data-wmark="deleted"],.kb-menu [data-pmark="deleted"]{color:var(--crit)}
[data-pstate][data-manual-state=""] .kb-menu [data-pmark="open"],
[data-pstate="done"] .kb-menu [data-pmark="done"],
[data-pstate="not_mine"] .kb-menu [data-pmark="not_mine"],
[data-pstate="deleted"] .kb-menu [data-pmark="deleted"]{display:none}
.pn{display:block;min-width:0}
.pname{display:block;font-size:13.5px;font-weight:500;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pc{display:block;margin-top:4px;font-size:11px;color:var(--muted)}
.track{display:block;height:7px;border-radius:999px;background:var(--track);overflow:hidden}
.fill{display:block;width:0;height:100%;border-radius:inherit;background:var(--acc);transition:width .2s}
.pv{text-align:right;font-size:12px;font-weight:500;color:var(--ink-2)}

/* 답장 대기 탭: 경과일 배지 | 보낸 사람 · 소속 | 요약 두 줄 | 받은 시각 · 할 일 → | 답변완료 | ⋮ (좁으면 위아래로).
   줄은 오늘 탭 대화 카드처럼 한 패널 안에 1px 틈으로 나눈다 */
.waits{display:flex;flex-direction:column;gap:1px;overflow:hidden;background:var(--line);border:1px solid var(--line-2);
 border-radius:14px}
.waits:empty{display:none}
.wait{display:grid;grid-template-columns:44px minmax(0,1fr);align-items:center;gap:18px;padding:14px 14px 14px 16px;
 background:var(--surface)}
.wait p{margin:0}
.wage{display:flex;flex-direction:column;align-items:center;justify-content:center;height:42px;border-radius:10px;
 background:var(--tag);color:var(--tag-ink);line-height:1}
.wage b{font-size:17px;font-weight:700}.wage small{margin-top:3px;font-size:10.5px;font-weight:600}
.wait[data-lv="0"] .wage b{font-size:12px;font-weight:600}
.wait[data-lv="1"] .wage{background:color-mix(in srgb,var(--caution) 16%,transparent);color:var(--caution-ink)}
.wait[data-lv="2"] .wage{background:var(--warn-bg);color:var(--warn)}
.wb{display:grid;grid-template-columns:minmax(0,170px) minmax(0,1fr) auto 28px;align-items:center;column-gap:20px;
 min-width:0}
.wh{display:flex;flex-direction:column;gap:2px;min-width:0}
.wn{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wo{font-size:11.5px;line-height:1.45;color:var(--muted);word-break:keep-all;overflow-wrap:anywhere}
.wo:empty{display:none}
.wb>.kb{justify-self:end;margin-right:-6px}
.wait .ws{font-size:13px;line-height:1.55;color:var(--ink-2);display:-webkit-box;-webkit-line-clamp:2;
 -webkit-box-orient:vertical;overflow:hidden;word-break:keep-all}
/* 받은 시각 위 · 할 일 → 아래, 오른쪽에 답변완료(표시한 줄은 상태 · 되돌리기) */
.wx{display:grid;grid-template-columns:104px 76px;align-items:center;gap:2px 16px;font-size:11.5px;color:var(--muted);
 white-space:nowrap}
.wt{grid-column:1;grid-row:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.wgo{grid-column:1;grid-row:2;justify-self:start}
.wx .st{grid-column:1;grid-row:1/span 2;justify-self:start}
.wgo{appearance:none;padding:0;border:0;background:none;font:inherit;font-weight:600;color:var(--acc);cursor:pointer}
.wgo:hover{text-decoration:underline;text-underline-offset:3px}
.wgo:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.wx .btn{grid-column:2;grid-row:1/span 2;justify-self:end;padding:3px 10px;font-size:11.5px}
.wx .undo{display:none}
.wait .wok{opacity:0;transition:opacity .12s}
.wait:hover .wok,.wait:focus-within .wok{opacity:1}
@media (hover:none){.wait .wok{opacity:1}.wait .wok:disabled{opacity:.5}}
.wait:hover .wok:disabled,.wait:focus-within .wok:disabled{opacity:.5}
.wait:not([data-wstate="open"]){background:var(--side)}
.wait:not([data-wstate="open"]) .wage{background:var(--tag);color:var(--muted)}
.wait:not([data-wstate="open"]) .ws{color:var(--muted)}
.wait:not([data-wstate="open"]) :is(.kb,.wt,.wgo,.wok){display:none}
.wait:not([data-wstate="open"]) .undo{display:inline-block}
.cv.flash{animation:cv-flash 1.6s ease-out}   /* 패널이 바깥을 잘라 안쪽 테두리로 */
@keyframes cv-flash{from{box-shadow:inset 0 0 0 2px var(--acc)}to{box-shadow:inset 0 0 0 2px transparent}}
@media (prefers-reduced-motion:reduce){.wok{transition:none}.cv.flash{animation:none;box-shadow:inset 0 0 0 2px var(--acc)}}
.handled summary{padding:14px 0 0;border-top:0;font-size:12.5px;color:var(--muted)}
.handled .waits{margin-top:12px}
.more{display:block;margin:12px auto 0;padding:4px 10px;border:0;border-radius:6px;background:none;
 font:inherit;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer}
.more:hover{color:var(--ink)}

/* 최근 결정사항: 프로젝트 탭 맨 위, 넓으면 두 줄씩 */
.recent-decs{margin:0 0 28px;padding:0 0 26px;border-bottom:1px solid var(--line)}
.recent-decs .sec-h{margin-bottom:14px}
.rds{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 40px}
.rd{display:flex;gap:13px}.rd p{margin:0}
.av{flex:none;display:grid;place-items:center;width:30px;height:30px;border-radius:50%;
 background:var(--tag);color:var(--tag-ink);font-size:11px;font-weight:600}
.rd:first-child .av{background:var(--acc-bg);color:var(--acc)}
.rd .t{font-size:13px;line-height:1.5}
.rd .rm{margin-top:5px;font-size:11.5px;color:var(--muted)}

.ptools{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;margin:0 0 26px}
.ptools .note{margin:0}
.pgroups{display:grid;gap:30px}
.client-h{display:flex;align-items:center;gap:8px;margin:0 0 12px}
.client-h h2{margin:0;font-size:15px;line-height:1.2;font-weight:600}
.client-h .sw{width:10px;height:10px;border-radius:3px}
.client-h .cnt{margin-left:2px}
/* 프로젝트는 상자 없이 머리줄(펼침 화살표 · 색 막대 · 이름 · 가는 선 · 숫자 · ⋮). 펼치면 색 막대가 아래로 이어지는
   세로줄(.proj-b 왼쪽 선, 프로젝트 색 --pc)로 그 프로젝트의 업무 상자들을 묶는다 — 상자 안에 상자를 만들지 않게 */
.projs{display:grid;gap:28px}
details.proj>summary.proj-h{display:flex;align-items:center;gap:8px;padding:2px 0;border-top:0;
 list-style:none;font-size:14px;color:var(--ink);cursor:pointer}
.proj-h::-webkit-details-marker{display:none}
.proj-h::before{content:"";flex:none;width:7px;height:7px;margin:0 3px 0 1px;border-right:1.5px solid var(--muted);
 border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
.proj-h::after{content:"";flex:1;order:1;min-width:24px;height:1px;background:var(--line)}
.proj[open]>.proj-h::before{transform:rotate(45deg)}
.proj[open]>.proj-h{margin-bottom:6px}
.proj-h:hover h3{color:var(--acc)}
.proj-h h3{margin:0;font-size:16px;font-weight:600}
.proj-h .pm{order:2;color:var(--muted);font-size:12px}
.proj-h .st{order:3;margin-left:4px}
.proj-h .kb{order:4}
/* 프로젝트 색: 머리줄 색 막대는 버튼 — 누르면 색 고르는 창(.cp). 올리면 막대가 커지고 테두리 */
.proj-h>.bar{appearance:none;flex:none;padding:0;border:0;cursor:pointer;transition:transform .12s,box-shadow .12s}
.proj-h>.bar:hover:not(:disabled),.proj-h>.bar.on{transform:scaleX(1.5);
 box-shadow:0 0 0 3px color-mix(in srgb,var(--pc,var(--line)) 35%,transparent)}
.proj-h>.bar:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
.proj-h>.bar:disabled{cursor:default}
.cp{position:fixed;z-index:20;width:304px;padding:14px 14px 12px;background:var(--card);color:var(--ink);
 border:1px solid var(--line-2);border-radius:14px;box-shadow:0 18px 40px -16px rgba(0,0,0,.55),0 2px 6px rgba(0,0,0,.18)}
.cp-h{display:flex;align-items:center;gap:8px;margin:0 0 12px;font-size:13px}
.cp-h b{font-weight:650}
.cp-h span{margin-left:auto;font-size:12px;color:var(--muted)}
.cp-dot{flex:none;width:10px;height:10px;border-radius:3px}
.cp-sws{display:grid;grid-template-columns:repeat(9,24px);justify-content:space-between;row-gap:6px}
.cp-sw{position:relative;appearance:none;width:24px;height:24px;padding:0;border:0;border-radius:50%;cursor:pointer;
 box-shadow:inset 0 0 0 1px rgba(0,0,0,.12);transition:transform .1s}
.cp-sw:hover{transform:scale(1.12)}
.cp-sw:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
.cp-sw[aria-checked="true"]{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--ink)}
.cp-sw[aria-checked="true"]::after{content:"";position:absolute;left:50%;top:45%;width:5px;height:10px;margin:-6px 0 0 -3px;
 border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.cp-used{position:absolute;left:50%;bottom:-8px;width:4px;height:4px;margin-left:-2px;border-radius:50%;background:var(--muted)}
.cp-note{display:flex;align-items:center;gap:6px;margin:14px 0 0;font-size:11.5px;line-height:1.5;color:var(--muted)}
.cp-note .cp-used{position:static;flex:none;margin:0}
.cp-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:12px;padding-top:11px;
 border-top:1px solid var(--line)}
.cp-auto{appearance:none;padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:none;font:inherit;
 font-size:12px;color:var(--ink-2);cursor:pointer}
.cp-auto:disabled{opacity:.45;cursor:default}
.cp-pick{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2);cursor:pointer}
.cp-pick input{width:28px;height:22px;padding:0;border:1px solid var(--line);border-radius:6px;background:none;cursor:pointer}
.bar{display:block;width:4px;height:20px;border-radius:2px}
.s0{background:var(--s0)}.s1{background:var(--s1)}.s2{background:var(--s2)}.s3{background:var(--s3)}
.s4{background:var(--s4)}.s5{background:var(--s5)}.s6{background:var(--s6)}.s7{background:var(--s7)}
.s8{background:var(--s8)}.s9{background:var(--s9)}
.sw{display:inline-block;width:8px;height:8px;border-radius:2px}
/* 펼친 프로젝트 안: 세로줄(머리줄 색 막대 아래로 맞춤) 오른쪽에 업무 묶음 카드 한 장씩 */
.proj-b{display:grid;gap:10px;margin-left:19px;padding:6px 0 2px 18px;
 border-left:4px solid color-mix(in srgb,var(--pc,var(--line)) 55%,transparent)}
.work{background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.work-h{display:flex;align-items:center;flex-wrap:wrap;gap:6px 8px;padding:11px 16px;border-bottom:1px solid var(--line);
 border-radius:9px 9px 0 0;background:color-mix(in srgb,var(--tag) 60%,transparent)}
.work-h .new{margin-left:0}.work-h .stt{margin-right:0}
.work-h .wm{margin-left:auto;white-space:nowrap}.wm b{font-weight:600;color:var(--ink-2)}
.wchip{display:inline-block;padding:0 7px;border:1px dashed var(--box);border-radius:4px;color:var(--muted);
 font-size:11px;font-weight:600;line-height:18px}
h4{margin:0 0 6px;font-size:12px;font-weight:600;color:var(--ink-2)}
h4.wt{margin:0;font-size:14.5px;color:var(--ink)}
h4.sec{margin:22px 0 10px;font-size:13px;color:var(--ink)}
.wm{color:var(--muted);font-size:12px}
.no-task{margin:0;padding:12px 16px;color:var(--muted);font-size:12px}
.tasks{padding:2px 16px 4px}
.tasks .task,.dec{padding:8px 0;border-bottom:1px solid var(--line)}
.tasks .task:last-child,.dec:last-child{border-bottom:0}
.tasks .task .t{margin:0;font-size:13.5px;line-height:1.5;color:var(--ink)}
/* 업무 줄: 역할 태그는 같은 폭 왼쪽 칸(가장 긴 '수정·디자인' 기준, 더 긴 태그는 그 줄만 넓힘) |
   문구 · 넘친 둘째 줄 · 담당자 줄은 오른쪽 칸의 같은 선에서 시작 */
.tasks .task{display:grid;grid-template-columns:minmax(66px,max-content) minmax(0,1fr);column-gap:10px;align-items:start}
/* 제외 · 업무삭제 사유 줄은 태그 · 글 두 칸에 걸친다 — 태그 칸에 들어가면 그 너비만큼 태그 칸이 넓어져 글이 밀린다 */
.tasks .task>.why{grid-column:1/-1;min-width:0}
.tasks .task>.rtag{margin:0;text-align:center}
.tb{min-width:0}
.tasks .task .th{display:flex;flex-wrap:wrap;align-items:flex-start;gap:2px 6px}
.tasks .task .th>.new{margin-top:2px}.tasks .task .th>.chip{margin-top:1px}
/* 담당자 · 소속 · 기한 · 표시 상태 · 버튼은 세로 가운데. 소속 칩과 표시 상태 칩은 같은 20px 높이 */
.tasks .task .meta{align-items:center;margin:5px 0 0}
.tasks .task .who{align-items:center}.tasks .task .org{line-height:18px}
.task .ctl{margin-left:auto}
.task .btn{padding:3px 9px;font-size:11.5px}
.task .st:empty{display:none}
.task .st{padding:0 8px;font-size:11px;line-height:20px}
.task[data-state="done"] .st{background:var(--acc-bg);color:var(--acc)}
.task:not([data-state="open"]) .meta .dl{display:none}     /* 완료 · 제외한 일에 'N일 지남' 경고는 남기지 않음 */
.gone summary{padding:8px 18px;color:var(--muted);font-size:12px;cursor:pointer}
.gone .tasks .task .t{color:var(--muted)}
.meta{display:flex;flex-wrap:wrap;gap:4px 10px;margin:3px 0 0 27px;color:var(--muted);font-size:12px;min-width:0}
.dec .meta,.thr .meta{margin-left:0}
.src{min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dl{display:inline-flex;gap:4px;align-items:center;color:var(--ink-2)}
.dl i{font-style:normal}
i.crit{color:var(--crit)}i.serious{color:var(--warn)}i.warn{color:var(--caution)}i.muted{color:var(--muted)}
.rtag{display:inline-block;margin-right:6px;padding:0 6px;border-radius:4px;font-size:11px;
 font-weight:600;line-height:18px;vertical-align:1px;white-space:nowrap;color:var(--ink);
 background:var(--tag);border:1px solid var(--line)}
.rtag.rt1{background:color-mix(in srgb,var(--s1) 16%,transparent);border-color:color-mix(in srgb,var(--s1) 45%,transparent)}
.rtag.rt2{background:color-mix(in srgb,var(--s7) 16%,transparent);border-color:color-mix(in srgb,var(--s7) 45%,transparent)}
.rtag.rt3{background:color-mix(in srgb,var(--s2) 16%,transparent);border-color:color-mix(in srgb,var(--s2) 45%,transparent)}
.sim{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;
 line-height:18px;white-space:nowrap;cursor:help;color:var(--warn);
 background:var(--warn-bg);border:1px solid var(--warn-soft)}
.who{display:inline-flex;gap:4px;align-items:baseline;color:var(--ink-2)}
.who .none{color:var(--muted)}
.org{font-size:11px;padding:0 5px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.decs .ds{margin-left:4px;color:var(--muted);font-size:12px}
.dec-g{padding:2px 18px 8px}
.dec-by{display:flex;align-items:baseline;gap:6px;margin:6px 0 0;font-size:13px;font-weight:600}
.dec-by .none,.dd .none{color:var(--muted);font-weight:400}
.dec{display:flex;gap:12px;align-items:baseline}
.dd{flex:none;min-width:44px;color:var(--ink-2);font-size:12px}.dt{min-width:0}
.new{display:inline-block;margin-left:6px;font-size:10px;font-weight:700;letter-spacing:.02em;
 padding:1px 6px;border-radius:999px;background:var(--new-bg);color:var(--new-ink);vertical-align:middle}
.tg .new,.tg .chip{margin-left:0}
.chip{display:inline-block;margin-left:6px;font-size:11px;padding:0 6px;border-radius:999px;
 border:1px solid var(--line);color:var(--ink-2);vertical-align:middle}
.thr-list{display:grid}
.thr{padding:12px 18px;border-bottom:1px solid var(--line)}.thr:last-child{border-bottom:0}
.thr-list.box{background:var(--surface);border:1px solid var(--line-2);border-radius:12px;overflow:hidden}
.thr-h{display:flex;align-items:center;flex-wrap:wrap}.subj{font-weight:500}
.snip{margin:4px 0 0;color:var(--ink-2);font-size:13px}
[data-preview].has-preview{cursor:help;text-decoration:underline dotted var(--box);text-underline-offset:4px}
[data-preview].has-preview:focus-visible{outline:2px solid var(--acc);outline-offset:3px;border-radius:4px}
.mail-preview{position:fixed;z-index:1000;box-sizing:border-box;width:480px;max-width:calc(100vw - 24px);
 padding:14px 16px;background:var(--ink);color:var(--card);border:1px solid var(--ink);
 border-top:3px solid var(--acc);border-radius:12px;box-shadow:0 10px 32px rgba(0,0,0,.18);font-size:13px;line-height:1.65}
.mail-preview[hidden]{display:none}
.mail-preview::after{content:"";position:absolute;left:var(--preview-arrow,24px);top:-7px;width:10px;height:10px;
 background:var(--ink);border-top:1px solid var(--acc);border-left:1px solid var(--acc);transform:rotate(45deg)}
.mail-preview[data-placement="above"]::after{top:auto;bottom:-6px;border-color:var(--ink);transform:rotate(225deg)}
.mail-preview-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:8px;
 color:color-mix(in srgb,var(--card) 75%,var(--ink));font-size:11px;font-weight:600}
.mail-preview-head small{font-size:10px;font-weight:400}
.mail-preview-body{max-height:min(320px,50vh);overflow:auto;overscroll-behavior:contain;white-space:pre-wrap;
 overflow-wrap:anywhere;user-select:text}
.pend-only .pend-note{margin:0;padding:10px 16px 8px;color:var(--muted);font-size:12px}
/* 업무 카드 안의 원본 대화: 안쪽 상자 + 한 단계 낮춘 제목 (따로 된 업무로 보이지 않게) */
.work .thr-list{margin:0 16px 12px;background:var(--side);border:1px solid var(--line);border-radius:8px}
.work .thr{padding:10px 14px}
.work .thr .subj{font-size:13px;color:var(--ink-2)}
.work details summary{padding:9px 16px}
.work .dec-g{padding:2px 16px 8px}
details summary{padding:10px 18px;color:var(--ink-2);font-size:13px;cursor:pointer;border-top:1px solid var(--line)}
.tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line-2);border-radius:12px}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.empty,.empty-state{color:var(--muted);font-size:13px}.empty{padding:6px 0}.empty-state{margin:0}

/* 일정 탭: 마감 요약 · 기한 지남 줄 · 월간 달력(페이지 JS 가 그림) · 고른 날의 할 일 · 기한 없음 */
.sch-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 16px;margin:0 0 12px}
.sch-sum{display:flex;flex-wrap:wrap;gap:8px}
.ss{appearance:none;display:inline-flex;align-items:baseline;gap:8px;padding:8px 14px;border:1px solid var(--line);
 border-radius:10px;background:var(--surface);font:inherit;font-size:12.5px;color:var(--ink-2);cursor:pointer}
.ss b{font-size:17px;font-weight:600;line-height:1;color:var(--ink)}
.ss.lv-overdue b{color:var(--crit)}.ss.lv-today b{color:var(--warn)}
.ss:hover{border-color:var(--box)}
.ss[aria-pressed="true"]{border-color:var(--acc);box-shadow:inset 0 0 0 1px var(--acc);color:var(--ink)}
#tab-schedule .note{margin:0 0 16px}
.sch-over{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:0 0 16px;padding:8px 10px;border-radius:10px;
 border:1px solid color-mix(in srgb,var(--crit) 30%,transparent);background:color-mix(in srgb,var(--crit) 6%,var(--surface))}
.so-l{margin:0 4px 0 2px;font-size:12px;font-weight:600;color:var(--crit)}
.so-chips{display:contents}
.sch-body{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(290px,1fr);gap:24px;align-items:start}
.cal{min-width:0}
.cal-h{display:flex;align-items:center;gap:6px;margin:0 0 10px}
.cal-h h2{min-width:112px;margin:0 4px;font-size:16px;font-weight:600;text-align:center}
.cal-nav{appearance:none;display:grid;place-items:center;width:30px;height:30px;padding:0;border:1px solid var(--line);
 border-radius:8px;background:var(--surface);font:inherit;font-size:17px;line-height:1;color:var(--ink-2);cursor:pointer}
.cal-nav:hover{border-color:var(--box);color:var(--ink)}
.cal-h .btn{margin-left:auto}
.cal-wk,.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
.cal-wk{margin:0 0 6px;font-size:11.5px;font-weight:500;color:var(--muted);text-align:center}
.cal-wk span:last-child{color:var(--crit)}
.cal-grid{gap:1px;overflow:hidden;background:var(--line);border:1px solid var(--line-2);border-radius:12px}
.cd{display:flex;flex-direction:column;gap:3px;min-width:0;min-height:98px;padding:4px 4px 6px;background:var(--surface);cursor:pointer}
.cd:hover{background:color-mix(in srgb,var(--tag) 45%,var(--surface))}
.cd.we,.cd.hol{background:var(--side)}
.cd.out>*{opacity:.45}
.cd.sel{box-shadow:inset 0 0 0 2px var(--acc)}
.cd-top{display:flex;align-items:center;gap:4px;min-width:0}
.cd-d{appearance:none;flex:none;display:grid;place-items:center;min-width:24px;height:24px;padding:0 6px;border:0;
 border-radius:999px;background:none;font:inherit;font-size:12px;font-weight:500;color:var(--ink-2);cursor:pointer}
.cd.sun .cd-d,.cd.hol .cd-d{color:var(--crit)}
.cd.now .cd-d{background:var(--acc);color:var(--acc-ink);font-weight:700}
.cd-h{min-width:0;overflow:hidden;font-size:10.5px;color:var(--crit);text-overflow:ellipsis;white-space:nowrap}
.cc{display:flex;align-items:center;gap:4px;min-width:0;padding:2px 5px 2px 4px;border:0;border-left:3px solid var(--box);
 border-radius:4px;background:var(--tag);font:inherit;font-size:11px;line-height:1.35;color:var(--ink);text-align:left}
.cc .cx{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cc .sw{flex:none;width:6px;height:6px}
.cc.lv-overdue{border-left-color:var(--crit)}.cc.lv-today{border-left-color:var(--warn)}
.cc.lv-soon{border-left-color:var(--caution)}
.cc.pt{background:none;box-shadow:inset 0 0 0 1px var(--line);color:var(--ink-2)}
.cc.lv-done{border-left-color:var(--line);color:var(--muted)}.cc.lv-done .cx{text-decoration:line-through}
button.cc{max-width:260px;cursor:pointer}
button.cc:hover{background:color-mix(in srgb,var(--crit) 12%,var(--surface))}
.cd-more{appearance:none;align-self:flex-start;padding:0 4px;border:0;border-radius:4px;background:none;font:inherit;
 font-size:11px;font-weight:500;color:var(--muted);cursor:pointer}
.cd-more:hover{color:var(--ink)}
.cd-dots{display:none}
.sch-side{min-width:0;padding:18px 18px 20px;background:var(--side);border:1px solid var(--line-2);border-radius:12px}
.sch-side .sec-h{margin:0 0 12px}.sch-side .sec-h.gap{margin-top:28px}
.sch-side .sec-h h2{font-size:15px}
.sits{display:flex;flex-direction:column;gap:10px}
.sit{padding:12px 14px;background:var(--surface);border:1px solid var(--line-2);border-left:3px solid var(--line);border-radius:10px}
.sit[data-state="open"][data-level="overdue"]{border-left-color:var(--crit)}
.sit[data-state="open"][data-level="today"]{border-left-color:var(--warn)}
.sit[data-state="open"][data-level="soon"]{border-left-color:var(--caution)}
.sit[data-kind="task"]{border-top-style:dashed;border-right-style:dashed;border-bottom-style:dashed}
.sit p{margin:0}
.sit .tg{margin-bottom:6px}
.sit .t{font-size:13.5px;font-weight:600;line-height:1.45}
.sit.done .t{text-decoration:line-through;color:var(--muted)}
.sit.flash{animation:sit-flash 1.6s ease-out}
@keyframes sit-flash{from{box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 55%,transparent)}to{box-shadow:0 0 0 3px transparent}}
.sdue{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin-top:10px;font-size:12px;color:var(--muted)}
.due-l{display:inline-flex;align-items:center;gap:6px}
.due-in{padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink);
 font:inherit;font-size:12px}
.due-in:disabled{opacity:.5}
.sit:not([data-manual="1"]) .due-rev,.sit:not([data-manual="1"]) .due-src,.sit:not([data-state="open"]) .sdue{display:none}
.sit .tf{margin-top:10px}
.sit .tip{max-width:min(260px,calc(100vw - 56px))}   /* 좁은 목록 칸에서 카드 밖으로 잘리지 않게 */
@media (prefers-reduced-motion:reduce){.sit.flash{animation:none;outline:2px solid var(--acc)}}

/* 맨 아래 2depth: 1depth 줄(메일 분류 규칙 · 토큰 사용 현황)을 누르면 2depth 보기가 펼쳐진다 */
.info{margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
.info-row+.info-row{border-top:1px solid var(--line)}
.info-h{appearance:none;display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 14px;width:100%;margin:0;
 padding:18px 28px 18px 22px;border:0;background:none;font:inherit;color:var(--ink);text-align:left;cursor:pointer}
.info-h:hover{background:var(--side)}
.info-h::before{content:"";flex:none;align-self:center;width:7px;height:7px;margin:0 2px 0 1px;border-right:1.5px solid var(--muted);
 border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
.info-h[aria-expanded="true"]::before{transform:rotate(45deg)}
.info-t{font-size:15px;font-weight:600}
.info-s{flex:1 1 320px;min-width:0;font-size:12.5px;color:var(--muted)}
.info-b{padding:4px 28px 28px 42px}
.info-b>.seg{margin:0 0 20px}
.info-p{min-width:0}
.ih{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:24px 0 10px;font-size:13px;font-weight:600;color:var(--ink)}
.info-p>.ih:first-child{margin-top:0}
.ih-m{font-size:12px;font-weight:400;color:var(--muted)}
.ib{display:grid;grid-template-columns:minmax(0,210px) minmax(60px,1fr) 150px;gap:14px;align-items:center;padding:6px 0;font-size:13px}
.ib-l{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.ib-l .itag{margin-left:6px;vertical-align:1px}
.ib-o{display:inline-grid;place-items:center;min-width:18px;height:18px;margin-right:8px;border-radius:50%;
 background:var(--tag);color:var(--tag-ink);font-size:11px;font-weight:600;vertical-align:1px}
.ib-n{min-width:72px;text-align:right;font-size:12.5px;font-weight:500;color:var(--ink-2);white-space:nowrap}
.info .fill,.usage-body .fill{transition:none}.fill.mute{background:var(--box)}
details.ib-d>summary.ib{padding:6px 0;border-top:0;list-style:none;font-size:13px;color:var(--ink);cursor:pointer}
details.ib-d>summary::-webkit-details-marker{display:none}
details.ib-d>summary:hover .ib-l{text-decoration:underline dotted var(--box);text-underline-offset:3px}
.ib-list{margin:2px 0 10px 26px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--side);font-size:12.5px}
.ib-list li{display:flex;justify-content:space-between;gap:12px;padding:3px 0;color:var(--muted)}
.ib-list .ib-s{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2)}
.ib-list .ib-p{flex:none}
.ib-g+.ib-g{border-top:1px dashed var(--line)}
.ib-note{margin:-2px 0 8px 26px;font-size:12px;color:var(--muted)}
.iflow{display:grid;gap:10px;margin:0;padding:0;list-style:none}
.if{padding:12px 16px 13px;background:var(--surface);border:1px solid var(--line-2);border-radius:10px}
.if-h{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-size:13.5px}
.if-n{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--acc-bg);
 color:var(--acc);font-size:11px;font-weight:700}
.if-c{margin-left:auto;font-size:12px;color:var(--ink-2)}
.if-d{margin:6px 0 0 30px;font-size:12.5px;line-height:1.55;color:var(--ink-2)}
.if-w{margin:4px 0 0 30px;font-size:11.5px;color:var(--muted)}
.itag{display:inline-block;padding:0 7px;border-radius:4px;background:var(--tag);color:var(--tag-ink);font-size:11px;
 font-weight:600;line-height:18px;white-space:nowrap}
.itag.llm{background:var(--acc-bg);color:var(--acc)}
.itag.bad{background:color-mix(in srgb,var(--crit) 12%,transparent);color:var(--crit)}
.ichips{display:flex;flex-wrap:wrap;gap:6px}.ichips.gap{margin-top:8px}
.ichips .chip{margin-left:0;padding:1px 9px;font-size:12px}
.il{margin:0;padding:0;list-style:none;border:1px solid var(--line-2);border-radius:10px;background:var(--surface)}
.il>li{display:grid;grid-template-columns:minmax(0,170px) minmax(0,1fr) auto;gap:4px 16px;align-items:baseline;
 padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px}
.il>li:last-child{border-bottom:0}
.il b{font-weight:600;color:var(--ink)}
.il .d{color:var(--ink-2);line-height:1.55}
.il .v{font-size:12px;color:var(--muted);white-space:nowrap}
.itiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line-2);
 border-radius:10px;overflow:hidden}
.itile{padding:14px 18px;background:var(--surface)}
.itile p{margin:0}
.it-l{font-size:12px;color:var(--muted)}
.it-v{margin-top:6px;font-size:24px;line-height:1.1;font-weight:600}
.it-u{margin-left:5px;font-size:12px;font-weight:500;color:var(--muted)}
.it-g{margin-top:7px}
.it-c{margin-top:6px;font-size:11.5px;color:var(--muted)}
/* 증가량: 늘면 주의색(주황), 줄면 강조색(초록) — 토큰은 적게 쓸수록 좋다 */
.it-d{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px;font-weight:600;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.it-d.up{background:var(--warn-bg);color:var(--warn)}
.it-d.down{background:var(--acc-bg);color:var(--acc)}
.it-d.flat,.it-d.new{background:var(--tag);color:var(--tag-ink);font-weight:500}
.it-w{font-weight:400;opacity:.8}
.ib-n .it-d{margin-left:6px;padding:0 5px;font-size:11px}
.inote{margin:12px 0 0;font-size:12.5px;line-height:1.55;color:var(--muted)}
.ifoot{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:14px 0 0;font-size:12.5px;color:var(--muted)}
a.btn{display:inline-block;text-decoration:none}
.info .tbl th.num,.info .tbl td.num,.usage-body .tbl th.num,.usage-body .tbl td.num{text-align:right;white-space:nowrap}
.info .tbl td,.usage-body .tbl td{vertical-align:middle}
.info .tbl .subj-c,.usage-body .tbl .subj-c{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.info .tbl .nw,.usage-body .tbl .nw{white-space:nowrap}
/* 칸이 많은 표(호출 기록 · 무거운 대화)는 좁은 화면에서 글자를 세로로 쪼개지 말고 옆으로 밀어서 본다 */
.usage-body .tbl{min-width:660px}

@media (max-width:900px){
 .digest-overview{grid-template-columns:minmax(0,1fr);gap:24px}
 .kpis{gap:24px}
 .sch-body{grid-template-columns:minmax(0,1fr)}
 .rds{grid-template-columns:minmax(0,1fr)}
 /* 답장 대기 줄: 이름 · 소속 + ⋮ / 요약 / 받은 시각 · 할 일 → · 답변완료 */
 .wait{gap:12px;padding:12px 12px 10px}
 .wb{grid-template-columns:minmax(0,1fr) 28px;row-gap:3px}
 .wh{flex-direction:row;align-items:baseline;gap:6px}
 .wn{flex:none;max-width:62%}
 .wo{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .wb>.kb{grid-column:2;grid-row:1}
 .wait .ws,.wx{grid-column:1/-1}
 .wx{display:flex;gap:8px;min-height:26px}
 .wx .btn{margin-left:auto}
 .hero-side{align-items:flex-start}
 .itiles{grid-template-columns:repeat(2,minmax(0,1fr))}
 .usage-body{padding:4px 24px 30px}
}
@media (max-width:600px){
 .wrap{padding:16px 16px 48px}
 .hero{padding:24px 20px 18px}
 h1{font-size:23px}
 .card>.banner{margin:0 20px 16px}
 .digest-overview{padding:0 20px 24px}
 .kpi .v b{font-size:40px}
 .deadline-counts{gap:2px 10px}
 .view-toolbar{flex-wrap:wrap;gap:0;padding-right:0}
 .view-toolbar .tabs{flex-basis:100%}
 .view-toolbar .switch{margin:8px 20px 12px 0;margin-left:auto}
 .card>.banner:first-child{padding:8px 20px}
 .tabs{padding:0 12px}
 .today,.pad{padding:24px 18px 30px}
 .act{padding:16px}
 .apg>.cvs{padding-left:0}
 .cv{padding:12px 14px 6px}
 .tk-r{width:100%;justify-content:flex-start}
 .proj-b{gap:8px;margin-left:0;padding:6px 0 2px 12px;border-left-width:3px}
 .work-h .wm{margin-left:0;white-space:normal}
 #tab-today .sec-h:first-child{flex-wrap:wrap}
 .pgo{grid-template-columns:minmax(0,1fr) auto;gap:8px 12px;padding:14px 4px 14px 16px}
 .pgo .track{grid-column:1/-1;grid-row:2}
 .prow>.kb{padding-right:10px}
 details.proj>summary.proj-h{flex-wrap:wrap;row-gap:4px}
 .proj-h::after{display:none}
 .proj-h h3{flex:1 1 0;min-width:0}
 .proj-h .pm{order:5;flex-basis:100%;padding-left:31px}
 .ss{flex:1 1 40%;justify-content:space-between}
 .cd{min-height:52px;padding:3px 2px 5px;align-items:center}
 .cd .cc,.cd .cd-more,.cd-h{display:none}
 .cd-dots{display:flex;gap:2px}
 .cd-dots i{width:5px;height:5px;border-radius:50%;background:var(--box)}
 .cd-dots i.lv-overdue{background:var(--crit)}.cd-dots i.lv-today{background:var(--warn)}
 .cd-dots i.lv-soon{background:var(--caution)}
 .sch-side{padding:14px 12px 16px}
 .info-h{padding:16px 16px 16px 14px}
 .info-b{padding:4px 16px 22px}
 .usage-body{padding:4px 16px 26px}
 .usage-page .hero{padding:20px 20px 16px}
 .ib{grid-template-columns:minmax(0,1fr) auto;gap:4px 10px}
 .ib .track{grid-column:1/-1;grid-row:2}
 .il>li{grid-template-columns:minmax(0,1fr) auto}
 .il .d{grid-column:1/-1;grid-row:2}
 .itiles{grid-template-columns:repeat(2,minmax(0,1fr))}
 .it-w{display:none}
 .if-d,.if-w,.ib-note,.ib-list{margin-left:0}
}
""")

# 두 페이지가 같이 쓰는 테마 전환 (고른 테마는 브라우저에 기억해 대시보드 · 사용 현황이 같이 따라간다)
THEME_JS = """
(function(){
  var modes=['system','light','dark'],names={system:'시스템',light:'라이트',dark:'다크'};
  var btn=document.getElementById('theme'),mode='system';
  if(!btn)return;
  try{mode=localStorage.getItem('nwmail.theme')||'system'}catch(e){}
  if(modes.indexOf(mode)<0)mode='system';
  function applyTheme(){
    if(mode==='system')document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme',mode);
    btn.textContent='테마: '+names[mode];
  }
  btn.addEventListener('click',function(){mode=modes[(modes.indexOf(mode)+1)%3];
    try{localStorage.setItem('nwmail.theme',mode)}catch(e){}applyTheme();});
  applyTheme();
})();
"""

# 토큰 사용 현황 페이지의 2depth 보기 전환. 고른 보기는 주소(#log)와 브라우저에 남겨,
# 자동 새로고침으로 파일이 바뀌어도 보던 자리로 돌아온다.
PREVIEW_JS = r"""
(function(){
  var tip=document.createElement('div'),head=document.createElement('div'),label=document.createElement('span'),hint=document.createElement('small'),body=document.createElement('div');
  tip.id='mail-preview';tip.className='mail-preview';tip.hidden=true;tip.setAttribute('role','tooltip');
  head.className='mail-preview-head';hint.textContent='Esc 닫기';body.className='mail-preview-body';
  head.appendChild(label);head.appendChild(hint);tip.appendChild(head);tip.appendChild(body);document.body.appendChild(tip);
  var current=null,timer=null,blocked=null;
  function available(el){return el.classList.contains('has-preview');}
  function measure(el){
    var clipped=el.clientWidth>0&&(el.scrollWidth>el.clientWidth+1||el.scrollHeight>el.clientHeight+1||el.dataset.preview.trim()!==el.textContent.trim());
    el.classList.toggle('has-preview',clipped);
    if(clipped)el.setAttribute('tabindex','0');else el.removeAttribute('tabindex');
  }
  var observer=window.ResizeObserver?new ResizeObserver(function(entries){entries.forEach(function(e){measure(e.target);});}):null;
  document.querySelectorAll('[data-preview],.ws,.src,.ib-s,.subj-c').forEach(function(el){
    if(!el.dataset.preview)el.dataset.preview=el.getAttribute('title')||el.textContent;
    if(!el.dataset.previewLabel)el.dataset.previewLabel=el.classList.contains('ws')?'메일 요약':'메일 제목';
    el.removeAttribute('title');measure(el);if(observer)observer.observe(el);
  });
  function hide(){
    clearTimeout(timer);
    if(current){
      var ids=(current.getAttribute('aria-describedby')||'').split(/\s+/).filter(function(id){return id&&id!==tip.id;});
      if(ids.length)current.setAttribute('aria-describedby',ids.join(' '));else current.removeAttribute('aria-describedby');
    }
    tip.hidden=true;current=null;
  }
  function show(el){
    clearTimeout(timer);if(!available(el)||blocked===el)return;
    if(current!==el)hide();current=el;
    label.textContent=el.dataset.previewLabel+' 전체';body.textContent=el.dataset.preview;
    var ids=(el.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean);
    if(ids.indexOf(tip.id)<0)ids.push(tip.id);el.setAttribute('aria-describedby',ids.join(' '));
    tip.hidden=false;body.style.maxHeight='';
    var r=el.getBoundingClientRect(),below=window.innerHeight-r.bottom-20,above=r.top-20;
    var up=below<Math.min(tip.offsetHeight,220)&&above>below;
    body.style.maxHeight=Math.max(48,Math.min(320,window.innerHeight/2,(up?above:below)-60))+'px';
    var top=up?r.top-tip.offsetHeight-8:r.bottom+8;
    tip.style.left=Math.max(12,Math.min(r.left,window.innerWidth-tip.offsetWidth-12))+'px';
    tip.style.top=Math.max(12,Math.min(top,window.innerHeight-tip.offsetHeight-12))+'px';
    tip.dataset.placement=up?'above':'below';
    tip.style.setProperty('--preview-arrow',Math.max(18,Math.min(r.left+r.width/2-parseFloat(tip.style.left),tip.offsetWidth-28))+'px');
  }
  function leave(){
    clearTimeout(timer);timer=setTimeout(function(){
      if(current&&(current.matches(':hover')||tip.matches(':hover')||document.activeElement===current))return;
      hide();
    },160);
  }
  document.addEventListener('pointerover',function(e){
    var el=e.target.closest('[data-preview]');if(el&&el!==current)show(el);
    if(tip.contains(e.target))clearTimeout(timer);
  });
  document.addEventListener('pointerout',function(e){
    var el=e.target.closest('[data-preview]');
    if(el&&!el.contains(e.relatedTarget)){if(blocked===el)blocked=null;leave();}
    if(tip.contains(e.target)&&!tip.contains(e.relatedTarget))leave();
  });
  document.addEventListener('focusin',function(e){var el=e.target.closest('[data-preview]');if(el){blocked=null;show(el);}});
  document.addEventListener('focusout',function(e){if(e.target===blocked)blocked=null;leave();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'){blocked=current;hide();}});
  document.addEventListener('pointerdown',function(e){
    var el=e.target.closest('[data-preview]');
    if(el&&e.pointerType==='touch'){blocked=null;show(el);}
    else if(!el&&!tip.contains(e.target))hide();
  });
  document.addEventListener('scroll',function(e){
    if(tip.contains(e.target)||!current)return;
    var r=current.getBoundingClientRect();
    if(r.bottom>0&&r.top<window.innerHeight&&(current.matches(':hover')||document.activeElement===current))show(current);
    else hide();
  },true);
  window.addEventListener('resize',function(){hide();document.querySelectorAll('[data-preview]').forEach(measure);});
})();
"""

USAGE_JS = """
(function(){
  var box=document.querySelector('[data-usage]');if(!box)return;
  var KEY=box.dataset.viewKey||'nwmail.usageView';
  var btns=Array.prototype.slice.call(box.querySelectorAll('[data-sub]'));
  function pick(key,push){
    if(!btns.some(function(b){return b.dataset.sub===key;}))key=btns.length?btns[0].dataset.sub:'';
    btns.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.sub===key?'true':'false');});
    box.querySelectorAll('[data-pane]').forEach(function(p){p.hidden=p.dataset.pane!==key;});
    try{localStorage.setItem(KEY,key)}catch(e){}
    if(push&&history.replaceState)history.replaceState(null,'','#'+key);
    return key;
  }
  var start=location.hash.slice(1);
  if(!start){try{start=localStorage.getItem(KEY)||''}catch(e){}}
  pick(start,false);
  btns.forEach(function(b){b.addEventListener('click',function(){pick(b.dataset.sub,true);});});
  window.addEventListener('hashchange',function(){pick(location.hash.slice(1),false);});
})();
"""

JS = """
(function(){
  var conf={};
  try{conf=JSON.parse(document.getElementById('nw-conf').textContent)}catch(e){}
  var SERVER=conf.port?'http://127.0.0.1:'+conf.port+'/':'';
  var served=!!conf.port&&/^https?:$/.test(location.protocol)&&location.port===String(conf.port);
  // 파일로 열었던 대시보드가 서버 주소로 옮겨 올 때, 그 브라우저에 있던 체크 기록을 #import= 로 받는다
  var imported=null;
  if(location.hash.indexOf('#import=')===0){
    try{imported=JSON.parse(decodeURIComponent(location.hash.slice(8)))}catch(e){}
    if(history.replaceState)history.replaceState(null,'',location.pathname+location.search);
  }
  function put(id,v){var el=document.getElementById(id);if(el)el.textContent=v;}
  function pad(n){return ('0'+n).slice(-2);}
  function mmdd(iso){var d=iso?new Date(iso):new Date();return isNaN(d)?'':pad(d.getMonth()+1)+'/'+pad(d.getDate());}
  function sameDay(iso){var d=new Date(iso);return !!iso&&!isNaN(d)&&d.toDateString()===new Date().toDateString();}

  // 파일로 열었을 때 이 브라우저에 남아 있던 예전 체크 (서버로 옮긴다)
  var KEY='nwmail.done.v1', done;
  try{done=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'))}catch(e){done=new Set()}
  if(imported){
    (imported.done||[]).forEach(function(id){done.add(id);});
    try{
      localStorage.setItem(KEY,JSON.stringify(Array.from(done)));
      if(imported.theme&&!localStorage.getItem('nwmail.theme'))localStorage.setItem('nwmail.theme',imported.theme);
      if(imported.hide&&localStorage.getItem('nwmail.hide')===null)localStorage.setItem('nwmail.hide',imported.hide);
    }catch(e){}
  }
  Object.keys(conf.taskAliases||{}).forEach(function(alias){
    var id=conf.taskAliases[alias];
    if(done.has(alias))done.add(id);
    if(imported&&imported.at&&imported.at[alias]&&!imported.at[id])imported.at[id]=imported.at[alias];
  });
  // 완료 수 · 프로젝트별 진행 막대를 표시 상태로 다시 센다 (프로젝트 카드 · 업무 카드 · 오늘 탭 모두 완료 / 전체).
  // 서버와 같은 셈: 전체 = 남은 일 + 완료 (제외 · 업무삭제한 일은 전체에서 뺀다)
  function tally(el){
    var c={open:0,total:0};
    el.querySelectorAll('.task').forEach(function(li){
      var s=li.dataset.state;if(s==='open')c.open++;if(s==='open'||s==='done')c.total++;});
    return c;
  }
  function progress(){
    var by={};
    document.querySelectorAll('.proj[data-project]').forEach(function(art){
      var c=by[art.dataset.project]=projectTally(art.dataset.project);
      var b=art.querySelector('[data-done]');if(b)b.textContent=c.total-c.open;
      var t=art.querySelector('[data-ptotal]');if(t)t.textContent=c.total;
    });
    document.querySelectorAll('.work [data-wdone]').forEach(function(b){
      var w=b.closest('.work'),c=tally(w),t=w.querySelector('[data-wtotal]');
      b.textContent=c.total-c.open;if(t)t.textContent=c.total;
    });
    document.querySelectorAll('.prow').forEach(function(r){
      var c=by[r.dataset.goto];if(!c)return;
      var n=c.total-c.open;
      r.querySelector('.pv').textContent=c.total?n+'/'+c.total:'할 일 없음';
      r.querySelector('.fill').style.width=(c.total?Math.round(n*100/c.total):0)+'%';
    });
    layoutProjects();
  }

  // ── 내 할 일: 완료 · 제외 · 업무삭제 (로컬 서버의 feedback.db 에 기록) ──────
  var lists={},view='open';
  document.querySelectorAll('[data-list]').forEach(function(ul){lists[ul.dataset.list]=ul;});
  function listKey(state){return state==='open'?'open':state==='done'?'done':'dropped';}
  function cards(ul){return Array.prototype.filter.call(ul.children,function(k){return k.classList.contains('act');});}
  function all(root,sel){return root?Array.prototype.slice.call(root.querySelectorAll(sel)):[];}
  function openState(li){return li.dataset.state==='open';}
  function projectIsMine(name){
    var els=projEls(name);return !els.length||els[0].dataset.manualState!=='not_mine';
  }
  // ── 할 일 보기: 대화 카드(.cv) 안의 줄(.tk). 표시한 줄은 그 자리에 두고(취소선 · 되돌리기) 숫자만 다시 센다 ──
  // 묶음 · 칸의 건수, 다 정리한 대화는 흐리게, 빈 묶음 · 칸은 숨긴다 (서버가 그린 것과 같은 규칙)
  function layoutOpen(){
    var box=lists.open,c={open:0,today:0,over:0,cv:0};if(!box)return c;
    var hideDone=document.body.classList.contains('hide-done');
    function shown(cv){return !cv.hidden&&!(hideDone&&cv.classList.contains('alldone'));}
    all(box,'.cv').forEach(function(cv){
      if(!projectIsMine(cv.dataset.project||cv.dataset.pname||cv.dataset.pg)){
        cv.hidden=true;return;
      }
      var rows=all(cv,'.tk'),live=rows.filter(openState);
      live.forEach(function(li){c.open++;if(li.dataset.level==='today')c.today++;if(li.dataset.level==='overdue')c.over++;});
      if(live.length)c.cv++;
      cv.hidden=!rows.length;
      cv.classList.toggle('allset',!!rows.length&&!live.length);
      cv.classList.toggle('alldone',!!rows.length&&rows.every(function(li){return li.dataset.state==='done';}));
      paintDate(cv,live,rows);
    });
    all(box,'.apg').forEach(function(g){
      var cvs=all(g,'.cv').filter(shown),live=cvs.filter(function(cv){return !cv.classList.contains('allset');});
      g.hidden=!cvs.length;
      g.querySelector('.apg-c').textContent='대화 '+live.length+' · 할 일 '+live.reduce(function(n,cv){return n+all(cv,'.tk[data-state="open"]').length;},0);
    });
    all(box,'[data-zone]').forEach(function(z){
      z.hidden=!all(z,'.cv').filter(shown).length;
      z.querySelector('.az-n').textContent=all(z,'.cv').filter(shown).reduce(function(n,cv){return n+all(cv,'.tk[data-state="open"]').length;},0);
    });
    put('sum-cv',c.cv);put('sum-n',c.open);put('sum-over-n',c.over);
    var so=document.getElementById('sum-over');if(so)so.hidden=!c.over;
    return c;
  }
  // 대화 줄 제목 위 날짜 칩 (서버 _cv_date 와 같은 모양). '마감 있는 일' 칸이면 가장 급한 마감(남은 줄 우선)을
  // '10/16 금 · D-10' 으로, 프로젝트 묶음이면 비운다 (최근 메일 시각은 메일 줄에). 칸은 arrange() 가 정하므로 지금 놓인 칸을 따른다
  function hasDue(li){return dueInfo(li.dataset.deadline).level!=='none';}
  function paintDate(cv,live,rows){
    var box=cv.querySelector('.cv-due');if(!box)return;
    var pick=null;
    if(cv.closest('[data-zone="due"]')){
      var src=live.filter(hasDue);if(!src.length)src=rows.filter(hasDue);
      src.forEach(function(li){var i=dueInfo(li.dataset.deadline);if(!pick||i.sort<pick.sort)pick=i;});
    }
    box.textContent='';
    if(!pick)return;
    var chip=document.createElement('span'),b=document.createElement('b');
    chip.className='cv-dc lv-'+pick.level;b.className='num';
    b.textContent=pick.day?(+pick.day.slice(5,7))+'/'+(+pick.day.slice(8,10))+' '+WD.charAt(wd(pick.day)):'기한';
    chip.appendChild(b);chip.appendChild(document.createTextNode(' · '+pick.label));box.appendChild(chip);
  }
  function statusText(li){
    var s=li.dataset.state;
    if(s==='open')return '';
    if(s==='done')return mmdd(li.dataset.at)+' 완료';
    return [conf.labels[s],li.dataset.reason,mmdd(li.dataset.at)].filter(Boolean).join(' · ');
  }
  // 완료 · 제외 카드는 최근 표시가 위. 남은 일이 된 카드는 할 일 보기의 줄(.tk)이 맡으므로
  // 빼 두었다가 다시 표시할 때 쓴다 (ensureCard)
  var parked={};
  function place(li){
    if(li.dataset.state==='open'){
      if(li.parentElement){parked[li.dataset.task]=li;li.remove();}
      return;
    }
    delete parked[li.dataset.task];
    var ul=lists[listKey(li.dataset.state)];if(!ul)return;
    var before=null;
    for(var i=0;i<ul.children.length;i++){
      var k=ul.children[i];if(k===li||!k.classList.contains('act'))continue;
      if((k.dataset.at||'')<(li.dataset.at||'')){before=k;break;}
    }
    ul.insertBefore(li,before);
    li.querySelector('.st').textContent=statusText(li);
  }
  // 어느 탭에서 표시하든(할 일 보기 줄 · 프로젝트 · 일정) 오늘 탭 완료 / 제외·삭제 목록에 카드를 바로 올린다.
  // 카드는 빼 두었던 것, 없으면 서버가 <template> 에 그려 둔 틀을 복사해 쓴다 (다음 갱신 때 서버가 그린 카드로 바뀐다)
  function ensureCard(src){
    var id=src.dataset.task;
    if(src.dataset.state==='open'||document.querySelector('.act[data-task="'+id+'"]'))return;
    var li=parked[id],tpl=document.getElementById('act-'+id);
    if(!li&&tpl&&tpl.content.firstElementChild)li=tpl.content.firstElementChild.cloneNode(true);
    if(!li)return;
    li.dataset.state=src.dataset.state;li.dataset.reason=src.dataset.reason||'';li.dataset.at=src.dataset.at||'';
    li.dataset.deadline=src.dataset.deadline||'';li.dataset.manual=src.dataset.manual||'';
    paintDue(li);place(li);
  }
  // 할 일 보기에 줄이 없는 내 할 일을 되돌리면 서버가 그려 둔 대화 카드 틀(cv-{id})에서 꺼내 넣는다.
  // 같은 대화 카드가 있으면 줄만 그 안에, 없으면 카드째 넣고 arrange() 가 칸 · 묶음을 정한다
  function cvOf(thread){return all(lists.open,'.cv').filter(function(cv){return cv.dataset.thread===thread;})[0];}
  // 답장 대기 탭의 '할 일 N건 →': 할 일 보기에 같은 대화의 남은 줄이 있을 때만. 누르면 오늘 탭의 그 대화 카드로
  function waitTasks(){
    document.querySelectorAll('.wait[data-thread]').forEach(function(li){
      var b=li.querySelector('.wgo'),cv=cvOf(li.dataset.thread);
      var n=cv&&!cv.hidden?all(cv,'.tk[data-state="open"]').length:0;
      if(!b)return;b.hidden=!n;b.querySelector('b').textContent=n;
    });
  }
  document.addEventListener('click',function(e){
    var b=e.target.closest('.wgo'),cv=b&&cvOf(b.closest('.wait').dataset.thread);
    if(!cv)return;
    var tt=document.getElementById('b-tab-today');if(tt&&tt.getAttribute('aria-selected')!=='true')tt.click();
    if(view!=='open')setView('open');
    var g=cv.closest('details');if(g)g.open=true;
    cv.classList.remove('flash');void cv.offsetWidth;cv.classList.add('flash');
    cv.scrollIntoView({block:'center',behavior:'smooth'});
  });
  function groupOf(name,slot){
    return all(lists.open,'.apg').filter(function(g){return g.dataset.pg===name;})[0]||makeGroup(name,slot);
  }
  function makeGroup(name,slot){      // 서버 _act_group 과 같은 모양
    var box=lists.open&&lists.open.querySelector('[data-zone="rest"] .apgs');if(!box)return null;
    var g=document.createElement('details'),s=document.createElement('summary'),ch=document.createElement('span');
    var sw=document.createElement('i'),n=document.createElement('span'),c=document.createElement('span');
    var cvs=document.createElement('div');
    g.className='apg';g.dataset.pg=name;g.open=true;ch.className='chv';ch.setAttribute('aria-hidden','true');
    sw.className='sw s'+(slot||0);sw.setAttribute('aria-hidden','true');n.className='apg-n';n.textContent=name;
    c.className='apg-c num';cvs.className='cvs';
    [ch,sw,n,c].forEach(function(x){s.appendChild(x);});g.appendChild(s);g.appendChild(cvs);
    var last=all(box,'.apg').filter(function(x){return x.dataset.pg===conf.unclassified;})[0];
    box.insertBefore(g,name===conf.unclassified?null:last||null);    // 미분류는 맨 아래
    watchGroup(g);return g;
  }
  function ensureRow(src){
    var id=src.dataset.task;
    if(!lists.open||src.dataset.kind==='task'||src.dataset.state!=='open'
      ||lists.open.querySelector('.tk[data-task="'+id+'"]'))return;
    var tpl=document.getElementById('cv-'+id),cv=tpl&&tpl.content.firstElementChild;
    if(!cv)return;
    cv=cv.cloneNode(true);
    var row=cv.querySelector('.tk'),home=cvOf(cv.dataset.thread);
    row.dataset.state='open';row.dataset.reason='';row.dataset.at='';
    row.dataset.deadline=src.dataset.deadline||'';row.dataset.manual=src.dataset.manual||'';
    if(home)home.querySelector('.tks').appendChild(row);
    else{lists.open.querySelector('[data-zone="due"] .cvs').appendChild(cv);watchCv(cv);}
    paintDue(row);placeRow(row);arrange();
  }
  function refresh(){
    var c=layoutOpen(),doneToday=0;
    Object.keys(lists).forEach(function(key){
      var n=key==='open'?c.open:cards(lists[key]).length;put('cnt-'+key,n);
      var empty=document.querySelector('[data-empty="'+key+'"]');
      if(empty)empty.hidden=!(key===view&&n===0);
    });
    // 오늘 완료: 내 할 일 + 프로젝트 할 일. 같은 할 일이 여러 곳(할 일 줄 · 카드 · 프로젝트 탭 줄)에 있어 할 일마다 한 번만 센다
    var doneIds={};
    document.querySelectorAll('.act[data-state="done"],.task[data-state="done"],.tk[data-state="done"]').forEach(function(li){
      if(sameDay(li.dataset.at))doneIds[li.dataset.task]=1;});
    doneToday=Object.keys(doneIds).length;
    put('kpi-me',c.open);put('hl-me',c.open);
    put('kpi-today',c.today);put('kpi-over',c.over);put('kpi-done',doneToday);
    progress();drawSchedule();waitTasks();
  }
  function setView(v){
    if(!lists[v])return;
    view=v;
    document.querySelectorAll('.seg [data-view]').forEach(function(b){
      var on=b.dataset.view===v;b.setAttribute('aria-pressed',on?'true':'false');
      if(on)put('acts-note',b.dataset.note);});
    Object.keys(lists).forEach(function(key){lists[key].hidden=key!==v;});
    closeWhy();refresh();
  }
  document.querySelectorAll('.seg [data-view]').forEach(function(b){
    b.addEventListener('click',function(){setView(b.dataset.view);});});

  var toastEl=document.getElementById('toast'),toastTimer;
  function toast(msg,undo){
    toastEl.textContent='';
    var span=document.createElement('span');span.textContent=msg;toastEl.appendChild(span);
    if(undo){
      var b=document.createElement('button');b.type='button';b.textContent='되돌리기';
      b.addEventListener('click',function(){toastEl.hidden=true;undo();});toastEl.appendChild(b);
    }
    toastEl.hidden=false;clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){toastEl.hidden=true;},6000);
  }
  function send(url,body){
    return fetch(url,{method:'POST',
      headers:{'Content-Type':'application/json','X-Nwmail-Token':conf.token},
      body:JSON.stringify(body)}).then(function(r){
        return r.json().catch(function(){return {};}).then(function(j){
          if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;});
      });
  }
  function post(items){return send('/api/feedback',{items:items});}
  function snapshot(li){
    var t=li.querySelector('.t');
    return {text:li.dataset.text||(t?t.textContent:''),thread_key:li.dataset.thread,
      subject:li.dataset.subject,project:li.dataset.pname,
      deadline:li.dataset.deadline,urgency:li.dataset.urgency,
      kind:li.dataset.kind||'me',role:li.dataset.role||'',assignee:li.dataset.assignee||''};
  }
  // 프로젝트 줄은 자리를 옮기지 않고 그 자리에서 완료 표시만 한다 (묶음 안에서 아래로)
  // 남은 할 일 목록 · '제외·삭제한 할 일' 접힘을 필요할 때 만든다 (서버가 그리는 차례와 같게)
  function taskList(work){
    var ul=work.querySelector(':scope>.tasks');
    if(!ul){
      ul=document.createElement('ul');ul.className='tasks';
      var head=work.querySelector('.work-h');
      work.insertBefore(ul,head?head.nextSibling:work.firstChild);
    }
    return ul;
  }
  function goneBox(work){
    var box=work.querySelector(':scope>.gone');
    if(!box){
      box=document.createElement('details');box.className='gone';
      var sum=document.createElement('summary'),ul=document.createElement('ul');ul.className='tasks';
      box.appendChild(sum);box.appendChild(ul);
      var main=work.querySelector(':scope>.tasks');
      work.insertBefore(box,main?main.nextSibling:null);
    }
    return box;
  }
  // 접힘 건수 · '남은 할 일 없음' 줄을 지금 목록에 맞춘다
  function syncWork(work){
    var main=work.querySelector(':scope>.tasks'),box=work.querySelector(':scope>.gone');
    var live=main?main.children.length:0,gone=box?box.querySelector('.tasks').children.length:0;
    if(box){box.hidden=!gone;box.querySelector('summary').textContent='제외·삭제한 할 일 '+gone+'개';}
    if(main)main.hidden=!live;
    var note=work.querySelector(':scope>.no-task');
    if(!note&&!live){
      note=document.createElement('p');note.className='no-task';note.textContent='남은 할 일 없음';
      work.insertBefore(note,main||box);
    }
    if(note)note.hidden=!!live;
  }
  // 프로젝트 탭 줄: 완료는 묶음 맨 아래로, 제외 · 업무삭제는 '제외·삭제한 할 일' 접힘으로 옮긴다.
  // 되돌리면 남은 할 일 자리(완료한 줄 앞)로 돌아온다 — 서버가 다시 그릴 때와 같은 모양
  function placeTask(li){
    var work=li.closest('.work'),s=li.dataset.state,drop=s==='excluded'||s==='deleted';
    li.classList.toggle('done',s==='done');
    li.querySelector('.st').textContent=statusText(li);
    if(!work)return;
    var ul=drop?goneBox(work).querySelector('.tasks'):taskList(work),moved=li.parentElement!==ul;
    if(drop||s==='done'){if(moved||li.nextElementSibling)ul.appendChild(li);}
    else if(moved){
      var at=null,ks=ul.children;
      for(var i=0;i<ks.length;i++){if(ks[i].dataset.state!=='open'){at=ks[i];break;}}
      ul.insertBefore(li,at);
    }
    syncWork(work);
  }
  // 할 일 보기 줄: 표시해도 자리는 그대로 (취소선 · 상태 + 되돌리기)
  function placeRow(li){
    li.querySelector('.st').textContent=statusText(li);
  }
  function relocate(li){
    if(li.classList.contains('task'))placeTask(li);else if(li.classList.contains('sit'))placeSit(li);
    else if(li.classList.contains('tk'))placeRow(li);else place(li);
  }
  function apply(li,state){
    Object.keys(conf.projectTasks||{}).forEach(function(name){
      var it=conf.projectTasks[name][li.dataset.task];if(it)it.state=state?state.label:'open';
    });
    li.dataset.state=state?state.label:'open';
    li.dataset.reason=state&&state.reason||'';
    li.dataset.at=state?state.labeled_at:'';
    relocate(li);
    // 같은 할 일이 여러 탭(오늘 · 프로젝트 · 일정)에 있으면 함께 맞춘다
    document.querySelectorAll('[data-task="'+li.dataset.task+'"]').forEach(function(o){
      if(o===li)return;
      o.dataset.state=li.dataset.state;o.dataset.reason=li.dataset.reason;o.dataset.at=li.dataset.at;
      relocate(o);
    });
    ensureCard(li);ensureRow(li);   // 완료 · 제외 목록에 카드가, 할 일 보기에 줄이 없으면 만든다
  }
  var pendingTasks={};
  function taskBusy(id,busy){
    pendingTasks[id]=busy;
    document.querySelectorAll('[data-task="'+id+'"] .ctl button').forEach(function(b){b.disabled=busy;});
  }
  function mark(li,label,reason){
    if(pendingTasks[li.dataset.task])return;
    closeWhy();
    var prev=li.dataset.state==='open'?null:
      {label:li.dataset.state,reason:li.dataset.reason,labeled_at:li.dataset.at};
    apply(li,label==='open'?null:{label:label,reason:reason||'',labeled_at:new Date().toISOString()});
    taskBusy(li.dataset.task,true);
    refresh();
    // 다른 대화에 똑같이 잡혀 한 줄로 합친 할 일(data-dups)도 같은 표시로 함께 기록한다
    var dups=(li.dataset.dups||'').split(' ').filter(Boolean),items=[
      {task_id:li.dataset.task,label:label,reason:reason||'',snapshot:snapshot(li)}];
    dups.forEach(function(id){
      var o=document.querySelector('.sit[data-task="'+id+'"]');
      items.push({task_id:id,label:label,reason:reason||'',snapshot:snapshot(o||li)});
    });
    function each(fn){dups.forEach(function(id){var o=document.querySelector('[data-task="'+id+'"]');if(o)fn(o,id);});}
    post(items).then(function(j){
      apply(li,j.states&&j.states[li.dataset.task]||null);
      each(function(o,id){apply(o,j.states&&j.states[id]||null);});
      refresh();
      var excluded=li.dataset.thread&&conf.analysisExclusions[label]===reason;
      toast(label==='open'?'남은 할 일로 되돌렸어요':excluded?
        '이 대화는 새 답장이 와도 AI 분석에서 제외해요':conf.labels[label]+'로 표시했어요',function(){
        mark(li,prev?prev.label:'open',prev?prev.reason:'');});
    }).catch(function(err){
      apply(li,prev);each(function(o){apply(o,prev);});refresh();toast('저장하지 못했어요 — '+err.message);
    }).finally(function(){
      taskBusy(li.dataset.task,false);
    });
  }
  var whyBox=null;
  function closeWhy(){if(whyBox){whyBox.remove();whyBox=null;}}
  // 제외 · 업무삭제는 사유를 한 번 더 고른다 (건너뛸 수 있음). 줄 아래 점선 한 줄(.why)을 차지하고,
  // 왼쪽에 안내 문구, 오른쪽(누른 버튼 밑)에 사유 · 사유 없이 · 취소를 한 줄로. 좁으면 버튼이 아래로 내려간다
  function askWhy(li,label){
    closeWhy();
    var wrap=document.createElement('div'),box=document.createElement('div');
    wrap.className='why';box.className='why-p';wrap.appendChild(box);
    var title=document.createElement('span');title.className='why-l';
    title.textContent=conf.labels[label]+' 사유';box.appendChild(title);
    function add(text,cls,fn){
      var b=document.createElement('button');b.type='button';b.className='btn'+(cls?' '+cls:'');
      b.textContent=text;b.addEventListener('click',fn);box.appendChild(b);
    }
    (conf.reasons[label]||[]).forEach(function(r){add(r,'',function(){mark(li,label,r);});});
    if(conf.analysisExclusions[label]){
      var note=document.createElement('span');note.className='why-note';
      note.textContent='“'+conf.analysisExclusions[label]+'”을 선택하면 이 대화는 새 답장이 와도 AI 분석에서 제외합니다. 되돌리면 다시 분석합니다.';
      wrap.insertBefore(note,box);
    }
    add('사유 없이','skip',function(){mark(li,label,'');});
    add('취소','skip',closeWhy);
    li.appendChild(wrap);whyBox=wrap;box.querySelector('button').focus();
  }
  document.addEventListener('click',function(e){
    var b=e.target.closest('[data-act]');if(!b||b.disabled)return;
    var li=b.closest('.act,.tk,.task,.sit'),act=b.dataset.act;if(!li)return;
    if(act==='excluded'||act==='deleted')askWhy(li,act);else mark(li,act,'');
  });
  // Esc: 사유 고르기를 닫고, 떠 있는 말풍선(메일 제목 · 도움말)도 숨긴다 (포인터가 나가거나 포커스가 빠지면 다시 뜬다)
  document.addEventListener('keydown',function(e){
    if(e.key!=='Escape')return;closeWhy();
    document.querySelectorAll('.mail:hover,.mail:focus,.infotip:hover,.infotip:focus')
      .forEach(function(m){m.classList.add('tip-off');});
  });
  document.addEventListener('mouseout',function(e){
    var m=e.target.closest&&e.target.closest('.mail,.infotip');if(m&&!m.contains(e.relatedTarget))m.classList.remove('tip-off');});
  document.addEventListener('focusout',function(e){
    var m=e.target.closest&&e.target.closest('.mail,.infotip');if(m)m.classList.remove('tip-off');});

  // ── ⋮ 메뉴: 답을 기다리는 메일(답변완료 · 삭제) · 프로젝트(완료 · 삭제 · 되돌리기) ──────
  // 삭제는 대시보드에서 숨기기만 한다. 답변완료 · 프로젝트 완료는 그 뒤 새 메일이 오면 풀린다.
  var openMenu=null;
  function menuItems(menu){
    return Array.prototype.filter.call(menu.querySelectorAll('button'),function(b){return b.offsetParent!==null;});
  }
  function closeMenu(focus){
    if(!openMenu)return;
    var m=openMenu;openMenu=null;m.menu.hidden=true;m.btn.setAttribute('aria-expanded','false');
    if(focus)m.btn.focus();
  }
  function toggleMenu(btn){
    var was=openMenu&&openMenu.btn===btn;closeMenu();
    if(was||btn.disabled)return;
    var menu=btn.nextElementSibling;menu.hidden=false;btn.setAttribute('aria-expanded','true');
    openMenu={btn:btn,menu:menu};
    // 스크롤 영역에 잘리지 않도록 화면 기준으로 버튼 아래(모자라면 위)에 띄운다
    var r=btn.getBoundingClientRect(),w=menu.offsetWidth,h=menu.offsetHeight;
    var top=r.bottom+4;if(top+h>window.innerHeight-8)top=Math.max(8,r.top-h-4);
    menu.style.top=top+'px';menu.style.left=Math.max(8,Math.min(r.right-w,window.innerWidth-w-8))+'px';
    var items=menuItems(menu);if(items[0])items[0].focus();
  }
  function markText(target,label,at){
    if(!label||label==='open')return '';
    var name=conf.marks[target][label];
    return label==='deleted'?name+' · '+mmdd(at):mmdd(at)+' '+name;
  }
  function active(st,latest){   // 서버와 같은 규칙 (feedback.mark_active)
    if(!st||st.label==='deleted'||st.label==='not_mine'||!latest||!st.ref_at)return st;
    return Date.parse(latest)>Date.parse(st.ref_at)?null:st;
  }

  var waitsUl=document.getElementById('waits'),handledUl=document.getElementById('handled-list');
  function layoutWaits(){
    if(!waitsUl)return;
    var more=document.querySelector('[data-more="waits"]'),n=waitsUl.children.length,oldest=0;
    var expanded=!!more&&more.getAttribute('aria-expanded')==='true',cut=conf.waitPreview||3;
    Array.prototype.forEach.call(waitsUl.children,function(li,i){
      li.classList.toggle('extra',i>=cut);li.hidden=i>=cut&&!expanded;
      oldest=Math.max(oldest,+li.dataset.waited||0);});
    if(more){
      if(n<=cut){expanded=false;more.setAttribute('aria-expanded','false');}
      more.hidden=n<=cut;more.textContent=expanded?'접기':'나머지 '+(n-cut)+'건 보기';
    }
    put('wait-cnt',n);put('hl-wait',n);put('kpi-wait',n);
    var tc=document.querySelector('[data-tabcnt="tab-wait"]');if(tc){tc.textContent=n;tc.classList.toggle('warn',n>0);}
    put('kpi-wait-c',n?(oldest?'가장 오래된 건 '+oldest+'일 경과':'모두 오늘 온 메일'):'모두 답했습니다');
    var k=document.getElementById('kpi-wait');if(k)k.classList.toggle('c-warn',n>0);
    var empty=document.getElementById('wait-empty');if(empty)empty.hidden=n>0;
    var h=handledUl?handledUl.children.length:0;put('handled-cnt',h);
    var box=document.getElementById('handled');if(box)box.hidden=!h;
  }
  // 표시한 메일은 아래 '답변완료·삭제한 메일' 로(최근 표시가 위), 되돌리면 오래 기다린 순서 자리로
  function applyWait(li,st){
    li.dataset.wstate=st?st.label:'open';li.dataset.at=st?st.labeled_at:'';
    var ul=st?handledUl:waitsUl,before=null;
    if(!ul)return;
    for(var i=0;i<ul.children.length;i++){
      var k=ul.children[i];if(k===li)continue;
      if(st?((k.dataset.at||'')<li.dataset.at):(+k.dataset.waited<+li.dataset.waited)){before=k;break;}
    }
    ul.insertBefore(li,before);
    if(st){li.classList.remove('extra');li.hidden=false;}
    li.querySelector('.wx .st').textContent=markText('thread',st&&st.label,st&&st.labeled_at);
    layoutWaits();
  }
  function markThread(li,label){
    var prev=li.dataset.wstate==='open'?null:{label:li.dataset.wstate,labeled_at:li.dataset.at};
    applyWait(li,label==='open'?null:{label:label,labeled_at:new Date().toISOString()});
    send('/api/mark',{target:'thread',key:li.dataset.thread,label:label,
      ref_at:li.dataset.asked,title:li.dataset.title}).then(function(j){
      applyWait(li,j.state||null);
      toast(label==='open'?'답을 기다리는 메일로 되돌렸어요':
        label==='deleted'?'목록에서 삭제했어요 (메일은 그대로예요)':'답변완료로 표시했어요',
        function(){markThread(li,prev?prev.label:'open');});
    }).catch(function(err){applyWait(li,prev);toast('저장하지 못했어요 — '+err.message);});
  }

  var pview='open',progressView='open';
  function projectTally(name){
    var c={open:0,total:0,overdue:0},items=(conf.projectTasks||{})[name]||{};
    Object.keys(items).forEach(function(id){
      var it=items[id];if(it.state==='open'){
        c.open++;var day=isoOf(it.deadline);if(day&&day<conf.today)c.overdue++;
      }
      if(it.state==='open'||it.state==='done')c.total++;
    });return c;
  }
  function projectState(el){
    var manual=el.dataset.manualState;if(manual)return manual;
    var c=projectTally(el.dataset.project||el.dataset.goto);
    return c.overdue?'delayed':!c.open&&el.dataset.ready==='1'?'done':'open';
  }
  function projEls(name){
    return Array.prototype.filter.call(document.querySelectorAll('.proj[data-project],.prow[data-goto]'),
      function(el){return (el.dataset.project||el.dataset.goto)===name;});
  }
  function layoutProjects(){
    var counts={open:0,done:0,delayed:0,not_mine:0,deleted:0},rowCounts={open:0,done:0,delayed:0,not_mine:0},named=0,rows=0;
    document.querySelectorAll('.proj[data-project]').forEach(function(a){
      var s=a.dataset.pstate=projectState(a);counts[s]=(counts[s]||0)+1;a.hidden=s!==pview;
      if((s==='open'||s==='delayed')&&a.dataset.project!==conf.unclassified)named++;});
    function priority(el){
      return projectTally(el.dataset.project||el.dataset.goto).open&&
        (el.dataset.pstate==='open'||el.dataset.pstate==='delayed')?0:1;
    }
    document.querySelectorAll('.proj[data-project]').forEach(function(a){
      var current=a.closest('.client'),band=priority(a),target=null;
      document.querySelectorAll('.client').forEach(function(sec){
        if(sec.dataset.client===current.dataset.client&&+sec.dataset.band===band)target=sec;
      });
      if(target&&target!==current)target.querySelector('.projs').appendChild(a);
    });
    document.querySelectorAll('.client').forEach(function(sec){
      var n=sec.querySelectorAll('.proj:not([hidden])').length;sec.hidden=!n;
      var c=sec.querySelector('[data-ccnt]');if(c)c.textContent=n;});
    document.querySelectorAll('.prow[data-goto]').forEach(function(r){
      r.dataset.pstate=projectState(r);r.hidden=r.dataset.pstate!==progressView;
      if(r.dataset.pstate!=='deleted')rowCounts[r.dataset.pstate]++;
      if(!r.hidden)rows++;});
    document.querySelectorAll('.projs,.progs').forEach(function(parent){
      Array.prototype.slice.call(parent.children).sort(function(a,b){
        return priority(a)-priority(b)||(+a.dataset.order)-(+b.dataset.order);
      }).forEach(function(el){parent.appendChild(el);});
    });
    Object.keys(rowCounts).forEach(function(k){put('progress-cnt-'+k,rowCounts[k]);});
    Object.keys(counts).forEach(function(k){put('pcnt-'+k,counts[k]);});
    put('kpi-proj',named);
    var tc=document.querySelector('[data-tabcnt="tab-projects"]');if(tc)tc.textContent=counts.open+counts.delayed;
    var pe=document.getElementById('pempty');if(pe)pe.hidden=!!counts[pview];
    var ge=document.getElementById('progs-empty');if(ge)ge.hidden=rows>0;
  }
  function setProgressView(v){
    if(['open','done','delayed','not_mine'].indexOf(v)<0)return;
    progressView=v;
    document.querySelectorAll('[data-progress-view]').forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.progressView===v));});
    put('progs-empty',v==='done'?'완료한 프로젝트가 없습니다.':v==='delayed'?'지연된 프로젝트가 없습니다.':v==='not_mine'?'내 일 아님으로 표시한 프로젝트가 없습니다.':'진행 중인 프로젝트가 없습니다.');
    closeMenu();layoutProjects();
  }
  document.querySelectorAll('[data-progress-view]').forEach(function(b){
    b.addEventListener('click',function(){setProgressView(b.dataset.progressView);});});
  function setPView(v){
    if(['open','done','delayed','not_mine','deleted'].indexOf(v)<0)return;
    pview=v;
    document.querySelectorAll('[data-pview]').forEach(function(b){
      var on=b.dataset.pview===v;b.setAttribute('aria-pressed',on?'true':'false');
      if(on){put('pview-note',b.dataset.note);put('pempty',b.dataset.empty);}});
    closeMenu();layoutProjects();
  }
  document.querySelectorAll('[data-pview]').forEach(function(b){
    b.addEventListener('click',function(){setPView(b.dataset.pview);});});
  function applyProject(name,st){
    projEls(name).forEach(function(el){
      el.dataset.manualState=st?st.label:'';el.dataset.at=st?st.labeled_at:'';
      var t=el.querySelector('.proj-h>.st');if(t)t.textContent=markText('project',st&&st.label,st&&st.labeled_at);
    });
    if(!st||st.label!=='not_mine'){
      ((conf.projectActions||{})[name]||[]).forEach(function(id){
        var tpl=document.getElementById('cv-'+id),row=tpl&&tpl.content.querySelector('.tk');
        var item=((conf.projectTasks||{})[name]||{})[id];
        if(row&&item&&item.state==='open')ensureRow(row);
      });
    }
    refresh();
  }
  function markProject(el,label){
    var name=el.dataset.project||el.dataset.goto;
    var prev=el.dataset.manualState?{label:el.dataset.manualState,labeled_at:el.dataset.at}:null;
    applyProject(name,label==='open'?null:{label:label,labeled_at:new Date().toISOString()});
    send('/api/mark',{target:'project',key:name,label:label,ref_at:el.dataset.latest,title:name}).then(function(j){
      applyProject(name,j.state||null);
      toast(label==='open'?'「'+name+'」 업무 상태에 따라 다시 분류했어요':
        label==='done'?'「'+name+'」 완료 프로젝트로 옮겼어요':label==='not_mine'?'「'+name+'」 내 일 아님으로 표시했어요':'「'+name+'」 삭제했어요 (메일은 그대로예요)',
        function(){markProject(el,prev?prev.label:'open');});
    }).catch(function(err){applyProject(name,prev);toast('저장하지 못했어요 — '+err.message);});
  }
  // ── 프로젝트 색: 머리줄 색 막대(또는 ⋮ '색 바꾸기…')를 누르면 고르는 창 ──────────────────────
  // 팔레트 9색 · 자동 색으로 · 직접 고르기. 고르면 그 프로젝트 색이 쓰인 곳(머리줄 막대 · 세로줄, 오늘 탭 묶음 ·
  // 카드, 일정 탭)을 바로 바꾸고 서버(feedback.db)에 저장한다. 직접 고른 색은 테마별로 밝기를 맞춘다
  // (고르는 동안은 페이지가, 저장하면 서버가 같은 계산으로)
  var colorPop=null,colorBar=null,liveCss=null,liveSlots={},customSlot={},nextCustom=100;
  function isSlot(k){return k.length>1&&k.charAt(0)==='s'&&String(+k.slice(1))===k.slice(1);}
  function slotOf(el){var n=0;Array.prototype.forEach.call(el.classList,function(k){if(isSlot(k))n=+k.slice(1);});return n;}
  function setSlot(el,n){if(!el)return;
    Array.prototype.slice.call(el.classList).forEach(function(k){if(isSlot(k))el.classList.remove(k);});el.classList.add('s'+n);}
  function barOf(name){var d=projEls(name).filter(function(x){return x.classList.contains('proj');})[0];
    return d?d.querySelector('.proj-h>.bar'):null;}
  function liveColor(n,light,dark){     // 직접 고른 색 칸: 테마별 값을 페이지 맨 끝 style 로 (서버가 그린 칸보다 뒤)
    liveSlots[n]={light:light,dark:dark||light};
    if(!liveCss){liveCss=document.createElement('style');liveCss.id='pc-live';document.body.appendChild(liveCss);}
    var l=[],d=[],c=[];
    Object.keys(liveSlots).forEach(function(k){l.push('--s'+k+':'+liveSlots[k].light);d.push('--s'+k+':'+liveSlots[k].dark);
      c.push('.s'+k+'{background:var(--s'+k+')}');});
    liveCss.textContent=':root{'+l.join(';')+'}@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){'
      +d.join(';')+'}}:root[data-theme="dark"]{'+d.join(';')+'}'+c.join('');
  }
  // 직접 고른 색을 테마 밝기 범위(conf.colorBands) 안으로 — nwmail/colors.py tone 과 같은 계산 (OKLCH 밝기만 옮김)
  function okLab(hex){
    var c=[1,3,5].map(function(i){var v=parseInt(hex.slice(i,i+2),16)/255;
      return v<=0.04045?v/12.92:Math.pow((v+0.055)/1.055,2.4);});
    var l=Math.cbrt(0.4122214708*c[0]+0.5363325363*c[1]+0.0514459929*c[2]),
        m=Math.cbrt(0.2119034982*c[0]+0.6806995451*c[1]+0.1073969566*c[2]),
        s=Math.cbrt(0.0883024619*c[0]+0.2817188376*c[1]+0.6299787005*c[2]);
    return [0.2104542553*l+0.7936177850*m-0.0040720468*s,1.9779984951*l-2.4285922050*m+0.4505937099*s,
      0.0259040371*l+0.7827717662*m-0.8086757660*s];
  }
  function toneOf(hex,band){
    var lab=okLab(hex),L=lab[0],C=Math.hypot(lab[1],lab[2]),hue=Math.atan2(lab[2],lab[1]),rgb;
    if(L>=band[0]&&L<=band[1])return hex;
    L=Math.min(band[1],Math.max(band[0],L));
    for(var k=0;k<60;k++){                   // 색역 밖이면 채도만 줄인다
      var a=C*Math.cos(hue),b=C*Math.sin(hue),l=Math.pow(L+0.3963377774*a+0.2158037573*b,3),
          m=Math.pow(L-0.1055613458*a-0.0638541728*b,3),s=Math.pow(L-0.0894841775*a-1.2914855480*b,3);
      rgb=[4.0767416621*l-3.3077115913*m+0.2309699292*s,-1.2684380046*l+2.6097574011*m-0.3413193965*s,
        -0.0041960863*l-0.7034186147*m+1.7076147010*s];
      if(rgb.every(function(v){return v>=-1e-4&&v<=1+1e-4;}))break;
      C*=0.9;
    }
    return '#'+rgb.map(function(v){v=Math.min(1,Math.max(0,v));v=v<=0.0031308?12.92*v:1.055*Math.pow(v,1/2.4)-0.055;
      return ('0'+Math.round(v*255).toString(16)).slice(-2);}).join('');
  }
  function tonesOf(hex){var B=conf.colorBands||{light:[0.45,0.75],dark:[0.55,0.75]};
    return {light:toneOf(hex,B.light),dark:toneOf(hex,B.dark)};}
  function paintColor(name,color,tones){   // color: '' (자동) | 'sN' | '#rrggbb'
    var bar=barOf(name);if(!bar)return;
    var n;
    if(!color)n=+bar.dataset.auto||0;
    else if(color.charAt(0)==='s')n=+color.slice(1);
    else{
      var cur=slotOf(bar);
      n=customSlot[name]||(cur>(conf.maxSlots||9)?cur:nextCustom++);customSlot[name]=n;
      var t=tones&&tones.light?tones:tonesOf(color);liveColor(n,t.light,t.dark);
    }
    bar.dataset.color=color;
    projEls(name).forEach(function(el){if(el.classList.contains('proj'))el.style.setProperty('--pc','var(--s'+n+')');});
    setSlot(bar,n);
    function squares(root){
      root.querySelectorAll('.apg').forEach(function(g){if(g.dataset.pg===name)setSlot(g.querySelector(':scope>summary .sw'),n);});
      root.querySelectorAll('.cv').forEach(function(cv){if(cv.dataset.pg!==name)return;cv.dataset.slot=n;
        cv.querySelectorAll('.cv-m .pj .sw').forEach(function(s){setSlot(s,n);});});
      root.querySelectorAll('.act,.sit').forEach(function(li){if(li.dataset.pname!==name)return;
        if(li.dataset.slot)li.dataset.slot=n;li.querySelectorAll('.pj .sw').forEach(function(s){setSlot(s,n);});});
    }
    squares(document);
    document.querySelectorAll('template').forEach(function(t){squares(t.content);});
  }
  function saveColor(name,color,prev){
    var bar=barOf(name);if(prev===undefined)prev=bar?bar.dataset.color||'':'';
    paintColor(name,color);
    send('/api/color',{project:name,color:color}).then(function(j){
      var c=j.color||{};paintColor(name,c.color||'',c);
      toast(c.color?'「'+name+'」 색을 바꿨어요':'「'+name+'」 자동 색으로 되돌렸어요',
        function(){saveColor(name,prev,c.color||'');});
    }).catch(function(err){paintColor(name,prev);toast('저장하지 못했어요 — '+err.message);});
  }
  function closeColor(focus){
    if(!colorPop)return;
    colorPop.remove();colorPop=null;
    if(colorBar){colorBar.classList.remove('on');colorBar.setAttribute('aria-expanded','false');if(focus)colorBar.focus();}
    colorBar=null;
  }
  function hexOf(n){var v=getComputedStyle(document.documentElement).getPropertyValue('--s'+n).trim();
    return /^#[0-9a-f]{6}$/i.test(v)?v.toLowerCase():'#888888';}
  function openColor(bar){
    if(colorBar===bar){closeColor(true);return;}
    closeColor();closeMenu();
    var name=bar.closest('.proj').dataset.project,cur=bar.dataset.color||'',n=slotOf(bar),names=conf.colorNames||[];
    var committed=cur;                          // 저장된 색 ('직접 고르기' 미리 보기와 구분)
    function choose(color){closeColor(true);if(color!==committed)saveColor(name,color,committed);else paintColor(name,color);}
    var sec=bar.closest('.client'),used={};       // 같은 고객사의 다른 프로젝트가 쓰는 색
    if(sec)sec.querySelectorAll('.proj').forEach(function(o){
      if(o.dataset.project===name)return;var b=o.querySelector('.proj-h>.bar');
      if(b){var s=slotOf(b);(used[s]=used[s]||[]).push(o.dataset.project);}});
    var pop=document.createElement('div');pop.className='cp';pop.setAttribute('role','dialog');
    pop.setAttribute('aria-label',name+' 색 바꾸기');
    var h=document.createElement('div');h.className='cp-h';
    var dot=document.createElement('i');dot.className='cp-dot s'+n;
    var t=document.createElement('b');t.textContent=name;
    var sub=document.createElement('span');sub.textContent='색 바꾸기';
    h.appendChild(dot);h.appendChild(t);h.appendChild(sub);pop.appendChild(h);
    var sws=document.createElement('div');sws.className='cp-sws';sws.setAttribute('role','radiogroup');
    sws.setAttribute('aria-label','팔레트');
    for(var i=1;i<=(conf.maxSlots||9);i++)(function(i){
      var b=document.createElement('button');b.type='button';b.className='cp-sw s'+i;b.setAttribute('role','radio');
      b.setAttribute('aria-checked',String(cur.charAt(0)!=='#'&&n===i));
      b.title=(names[i]||'')+(used[i]?' · '+used[i].join(', ')+'에서 쓰는 중':'');b.setAttribute('aria-label',b.title);
      if(used[i]){var u=document.createElement('i');u.className='cp-used';b.appendChild(u);}
      b.addEventListener('click',function(){choose('s'+i);});
      sws.appendChild(b);})(i);
    pop.appendChild(sws);
    var note=document.createElement('p');note.className='cp-note';
    note.innerHTML='<i class="cp-used"></i>같은 고객사의 다른 프로젝트가 쓰는 색';pop.appendChild(note);
    var row=document.createElement('div');row.className='cp-row';
    var auto=document.createElement('button');auto.type='button';auto.className='cp-auto';
    auto.textContent='자동 색('+(names[+bar.dataset.auto||0]||'')+')으로';auto.disabled=!cur;
    auto.addEventListener('click',function(){choose('');});
    var lab=document.createElement('label');lab.className='cp-pick';lab.appendChild(document.createTextNode('직접 고르기'));
    var inp=document.createElement('input');inp.type='color';inp.value=cur.charAt(0)==='#'?cur:hexOf(n);
    inp.addEventListener('input',function(){paintColor(name,inp.value.toLowerCase());dot.className='cp-dot s'+slotOf(bar);
      sws.querySelectorAll('.cp-sw').forEach(function(x){x.setAttribute('aria-checked','false');});auto.disabled=false;});
    inp.addEventListener('change',function(){var v=inp.value.toLowerCase();
      if(v!==committed){saveColor(name,v,committed);committed=v;}});
    lab.appendChild(inp);row.appendChild(auto);row.appendChild(lab);pop.appendChild(row);
    var tip=document.createElement('p');tip.className='cp-note';
    tip.textContent='직접 고른 색은 다크 · 라이트 테마에 맞게 밝기를 맞춰 씁니다.';pop.appendChild(tip);
    document.body.appendChild(pop);colorPop=pop;colorBar=bar;
    bar.classList.add('on');bar.setAttribute('aria-expanded','true');
    var r=bar.getBoundingClientRect(),w=pop.offsetWidth,ph=pop.offsetHeight,top=r.bottom+8;
    if(top+ph>window.innerHeight-8)top=Math.max(8,r.top-ph-8);
    pop.style.top=top+'px';pop.style.left=Math.max(8,Math.min(r.left-14,window.innerWidth-w-8))+'px';
    var first=pop.querySelector('.cp-sw[aria-checked="true"]')||pop.querySelector('.cp-sw');if(first)first.focus();
  }
  document.addEventListener('keydown',function(e){if(colorPop&&e.key==='Escape'){e.preventDefault();closeColor(true);}});
  window.addEventListener('resize',function(){closeColor();});
  window.addEventListener('scroll',function(){closeColor();},{passive:true,capture:true});
  document.addEventListener('click',function(e){
    if(colorPop&&!e.target.closest('.cp')&&e.target.closest('.proj-h>.bar')!==colorBar)closeColor();
    var cb=e.target.closest('.proj-h>.bar');
    if(cb){e.preventDefault();if(!cb.disabled)openColor(cb);return;}    // 머리줄 안이라도 접고 펴지 않는다
    var co=e.target.closest('[data-color-open]');
    if(co){e.preventDefault();closeMenu();var cbar=co.closest('.proj-h').querySelector('.bar');
      if(cbar&&!cbar.disabled)openColor(cbar);return;}
    var kb=e.target.closest('.kb-btn');
    if(kb){e.preventDefault();toggleMenu(kb);return;}     // 프로젝트 머리말 안이라도 접고 펴지 않는다
    var w=e.target.closest('[data-wmark]'),p=e.target.closest('[data-pmark]');
    if(w||p){
      e.preventDefault();closeMenu();
      if((w||p).disabled)return;
      if(w){var li=w.closest('.wait');if(li)markThread(li,w.dataset.wmark);}
      else{var el=p.closest('[data-pstate]');if(el)markProject(el,p.dataset.pmark);}
      return;
    }
    if(e.target.closest('.kb-menu')){e.preventDefault();return;}
    if(openMenu)closeMenu();
  });
  document.addEventListener('keydown',function(e){
    if(!openMenu)return;
    if(e.key==='Escape'){e.preventDefault();closeMenu(true);return;}
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){
      e.preventDefault();
      var items=menuItems(openMenu.menu),i=items.indexOf(document.activeElement);
      var n=items[(i+(e.key==='ArrowDown'?1:-1)+items.length)%items.length];if(n)n.focus();
    }
  });
  window.addEventListener('resize',function(){closeMenu();});
  window.addEventListener('scroll',function(){closeMenu();},{passive:true,capture:true});

  // ── 일정 탭: 월간 달력 · 기한 직접 정하기 (직접 정한 기한은 feedback.db 의 task_due) ──────────
  // 달력은 할 일 줄(.sit)의 data-* 로 그린다. 기한 · 표시가 바뀌면 refresh() 가 drawSchedule() 로 다시 그린다.
  var sch=document.getElementById('sch'),TODAY=conf.today||'',WD='월화수목금토일',URG={high:0,medium:1,low:2};
  var cal={month:TODAY.slice(0,7),day:TODAY,panel:'day',scope:'me'},pinned=null,dueTimer;
  try{if(localStorage.getItem('nwmail.sview')==='all')cal.scope='all';}catch(e){}
  function urg(el){var u=URG[el.dataset.urgency];return u===undefined?1:u;}
  function ymd(d){return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());}
  function parts(iso){var p=iso.split('-');return new Date(+p[0],+p[1]-1,+p[2]);}
  function isoOf(raw){     // 서버 iso_day 와 같은 뜻 ('YYYY-MM-DD' 로 읽히는 날짜만)
    var s=String(raw||'').trim().slice(0,10);
    return s.length===10&&s.charAt(4)==='-'&&s.charAt(7)==='-'&&ymd(parts(s))===s?s:'';
  }
  function addDays(iso,n){var d=parts(iso);d.setDate(d.getDate()+n);return ymd(d);}
  function wd(iso){return (parts(iso).getDay()+6)%7;}      // 월=0 … 일=6
  function dayLabel(iso){var d=parts(iso);return (d.getMonth()+1)+'/'+d.getDate()+'('+WD.charAt(wd(iso))+')';}
  function dueInfo(raw){   // 서버 deadline_info · _due_text 와 같은 규칙
    raw=String(raw||'').trim();
    var day=isoOf(raw);
    if(!raw)return {level:'none',label:'',text:'',day:'',sort:99999};
    if(!day)return {level:'later',label:raw,text:raw,day:'',sort:99998};
    var n=Math.round((parts(day)-parts(TODAY))/864e5),label=n<0?(-n)+'일 지남':n===0?'오늘':'D-'+n;
    return {level:n<0?'overdue':n===0?'today':n<=3?'soon':'later',label:label,day:day,sort:n,
      text:label+(n===0?' 마감':'')+' · '+dayLabel(day)};
  }
  function sits(){return Array.prototype.filter.call(document.querySelectorAll('.sit'),function(li){return projectIsMine(li.dataset.pname);});}
  function inScope(li){return cal.scope==='all'||li.dataset.kind!=='task';}
  // 달력 · 날짜 목록에 올리는 줄: 남은 일 + 완료한 일(완료 숨기기가 꺼져 있을 때). 제외 · 삭제한 일은 뺀다
  function onCal(li){
    var s=li.dataset.state;
    return inScope(li)&&(s==='open'||(s==='done'&&!document.body.classList.contains('hide-done')));
  }
  function byDue(a,b){     // 서버 _schedule_items 와 같은 순서
    var x=isoOf(a.dataset.deadline)||'9999',y=isoOf(b.dataset.deadline)||'9999';
    return x<y?-1:x>y?1:(a.dataset.state!=='open')-(b.dataset.state!=='open')
      ||(a.dataset.kind==='task')-(b.dataset.kind==='task')||urg(a)-urg(b)||(+a.dataset.sorder||0)-(+b.dataset.sorder||0);
  }
  function chip(li,tag){
    var c=document.createElement(tag||'span'),sw=document.createElement('i'),x=document.createElement('span');
    var done=li.dataset.state==='done';
    c.className='cc lv-'+(done?'done':li.dataset.level)+(li.dataset.kind==='task'?' pt':'');
    c.dataset.cid=li.dataset.task;if(tag==='button')c.type='button';
    sw.className='sw s'+(li.dataset.slot||0);sw.setAttribute('aria-hidden','true');
    x.className='cx';x.textContent=li.querySelector('.t').textContent;
    c.title=[x.textContent,li.dataset.pname,done?'완료':''].filter(Boolean).join(' · ');
    c.appendChild(sw);c.appendChild(x);return c;
  }
  function drawCal(){
    var grid=document.getElementById('cal-grid');if(!grid)return;
    var y=+cal.month.slice(0,4),m=+cal.month.slice(5,7),first=new Date(y,m-1,1);
    var lead=(first.getDay()+6)%7,cells=Math.ceil((lead+new Date(y,m,0).getDate())/7)*7;
    var by={},hol=conf.holidays||{},cap=conf.cap||3,tab=cal.day.slice(0,7)===cal.month?cal.day:cal.month+'-01';
    sits().forEach(function(li){var d=isoOf(li.dataset.deadline);if(d&&onCal(li))(by[d]=by[d]||[]).push(li);});
    put('cal-title',y+'년 '+m+'월');
    grid.textContent='';
    for(var i=0;i<cells;i++){
      var iso=ymd(new Date(y,m-1,1-lead+i)),w=wd(iso),items=(by[iso]||[]).sort(byDue),sel=cal.panel==='day'&&iso===cal.day;
      var cell=document.createElement('div'),top=document.createElement('div'),num=document.createElement('button');
      cell.className='cd'+(iso.slice(0,7)!==cal.month?' out':'')+(w>=5?' we':'')+(w===6?' sun':'')
        +(hol[iso]?' hol':'')+(iso===TODAY?' now':'')+(sel?' sel':'');
      cell.dataset.day=iso;top.className='cd-top';
      num.type='button';num.className='cd-d';num.dataset.day=iso;num.textContent=+iso.slice(8);
      num.tabIndex=iso===tab?0:-1;num.setAttribute('aria-pressed',sel?'true':'false');
      num.setAttribute('aria-label',(+iso.slice(5,7))+'월 '+(+iso.slice(8))+'일 '+WD.charAt(w)+'요일'
        +(iso===TODAY?', 오늘':'')+(hol[iso]?', '+hol[iso]:'')+(items.length?', 할 일 '+items.length+'건':''));
      top.appendChild(num);
      if(hol[iso]){var h=document.createElement('span');h.className='cd-h';h.textContent=hol[iso];top.appendChild(h);}
      cell.appendChild(top);
      items.slice(0,cap).forEach(function(li){cell.appendChild(chip(li));});
      if(items.length>cap){
        var more=document.createElement('button');more.type='button';more.className='cd-more';more.tabIndex=-1;
        more.textContent='+'+(items.length-cap)+'건';cell.appendChild(more);
      }
      if(items.length){      // 좁은 화면에서는 문구 대신 점
        var dots=document.createElement('span');dots.className='cd-dots';dots.setAttribute('aria-hidden','true');
        items.slice(0,3).forEach(function(li){var dot=document.createElement('i');
          dot.className='lv-'+(li.dataset.state==='done'?'done':li.dataset.level);dots.appendChild(dot);});
        cell.appendChild(dots);
      }
      grid.appendChild(cell);
    }
  }
  // 기한 지난 남은 일은 지난달 일이라도 달력 위 한 줄에 모아 둔다
  function drawOver(){
    var box=document.getElementById('sch-over'),wrap=document.getElementById('sch-over-chips');if(!box)return;
    var list=sits().filter(function(li){var d=isoOf(li.dataset.deadline);
      return li.dataset.state==='open'&&inScope(li)&&!!d&&d<TODAY;}).sort(byDue);
    wrap.textContent='';
    list.slice(0,6).forEach(function(li){var c=chip(li,'button');c.dataset.spanel='overdue';wrap.appendChild(c);});
    if(list.length>6){
      var b=document.createElement('button');b.type='button';b.className='cd-more';b.dataset.spanel='overdue';
      b.textContent='외 '+(list.length-6)+'건';wrap.appendChild(b);
    }
    box.hidden=!list.length;
  }
  // 오른쪽 목록: 고른 날(기본) · 기한 지남 · 7일 안. 기한을 고치는 중인 줄(pinned)은 그 자리에 둔다
  function drawPanel(){
    var day=document.getElementById('sch-day'),none=document.getElementById('sch-none');if(!day||!none)return;
    var n=0,k=0,end=addDays(TODAY,conf.week||7),hol=conf.holidays||{},p=cal.panel,d=parts(cal.day);
    Array.prototype.forEach.call(day.children,function(li){
      var due=isoOf(li.dataset.deadline),ok=projectIsMine(li.dataset.pname)&&(li===pinned||onCal(li)&&!!due&&(p==='overdue'?
        li.dataset.state==='open'&&due<TODAY:p==='week'?due>TODAY&&due<=end:due===cal.day));
      li.hidden=!ok;if(ok)n++;
    });
    Array.prototype.forEach.call(none.children,function(li){
      var owned=projectIsMine(li.dataset.pname),mine=owned&&li.dataset.kind!=='task'&&li.dataset.state==='open';
      li.hidden=!(mine||owned&&li===pinned);if(mine)k++;});
    put('sp-title',p==='overdue'?'기한 지남':p==='week'?(conf.week||7)+'일 안에 마감':
      (d.getMonth()+1)+'월 '+d.getDate()+'일 ('+WD.charAt(wd(cal.day))+')'+(cal.day===TODAY?' · 오늘':'')
      +(hol[cal.day]?' · '+hol[cal.day]:''));
    put('sp-cnt',n?n+'건':'');
    var empty=document.getElementById('sp-empty');
    if(empty){empty.hidden=n>0;empty.textContent=p==='overdue'?'기한 지난 할 일이 없습니다.':
      p==='week'?(conf.week||7)+'일 안에 마감인 할 일이 없습니다.':'이 날 마감인 할 일이 없습니다.';}
    put('none-cnt',k);
    var ne=document.getElementById('none-empty');if(ne)ne.hidden=k>0;
    sch.querySelectorAll('.ss[aria-pressed]').forEach(function(b){b.setAttribute('aria-pressed',
      String(b.dataset.spanel===p||(b.dataset.spanel==='today'&&p==='day'&&cal.day===TODAY)));});
  }
  // 요약 숫자는 지금 보기(내 할 일 / 프로젝트 할 일 포함) 기준, 탭 숫자는 늘 내 할 일 기준 (서버와 같음)
  function schCounts(){
    var c={overdue:0,today:0,week:0,none:0},badge=0,end=addDays(TODAY,conf.week||7);
    sits().forEach(function(li){
      if(li.dataset.state!=='open')return;
      var d=isoOf(li.dataset.deadline),mine=li.dataset.kind!=='task';
      if(mine&&d&&d<=end)badge++;
      if(!inScope(li))return;
      if(!d){if(mine)c.none++;}
      else if(d<TODAY)c.overdue++;
      else if(d===TODAY)c.today++;
      else if(d<=end)c.week++;
    });
    Object.keys(c).forEach(function(k){put('ss-'+k,c[k]);});
    var tc=document.querySelector('[data-tabcnt="tab-schedule"]');if(tc)tc.textContent=badge;
  }
  function drawSchedule(){if(!sch||!TODAY)return;
    document.querySelectorAll('.sit').forEach(function(li){if(!projectIsMine(li.dataset.pname))li.hidden=true;});
    schCounts();drawOver();drawCal();drawPanel();}
  // 기한이 바뀐 할 일의 표시를 고친다 (오늘 탭 카드 · 프로젝트 탭 줄 · 일정 탭 줄)
  function paintDue(el){
    var info=dueInfo(el.dataset.deadline);
    el.dataset.level=info.level;
    if(el.classList.contains('task')){
      var meta=el.querySelector('.meta');if(!meta)return;
      var dl=meta.querySelector('.dl');
      if(info.level==='none'){if(dl)dl.remove();return;}
      if(!dl){dl=document.createElement('span');dl.className='dl';
        var who=meta.querySelector('.who');meta.insertBefore(dl,who?who.nextSibling:meta.firstChild);}
      var g=(conf.glyphs||{})[info.level]||['·','muted'],ic=document.createElement('i');
      ic.className=g[1];ic.setAttribute('aria-hidden','true');ic.textContent=g[0];
      dl.title=el.dataset.deadline;dl.textContent='';dl.appendChild(ic);dl.appendChild(document.createTextNode(info.label));
      return;
    }
    var tag=el.querySelector('.due');if(tag){tag.className='due lv-'+info.level;tag.textContent=info.text;}
    var inp=el.querySelector('.due-in');if(inp&&document.activeElement!==inp)inp.value=info.day;
  }
  function applyDue(id,st,quiet){
    document.querySelectorAll('[data-task="'+id+'"]').forEach(function(el){
      el.dataset.manual=st?'1':'';el.dataset.deadline=st?st.due:(el.dataset.mdl||'');paintDue(el);});
    var row=document.querySelector('[data-task="'+id+'"]');
    if(row)Object.keys(conf.projectTasks||{}).forEach(function(name){
      var it=conf.projectTasks[name][id];if(it)it.deadline=row.dataset.deadline;
    });
    if(!quiet){arrange();sortSits();refresh();}
  }
  // 기한이 바뀌면 줄 · 대화 카드를 서버와 같은 자리로: 대화 안에서는 마감 → 중요도 → 원래 순서.
  // 기한 있는 남은 일이 있는 대화는 '마감 있는 일' 칸(가장 급한 마감순), 없으면 프로젝트 묶음(원래 순서)
  function arrange(){
    var box=lists.open;if(!box)return;
    var due=box.querySelector('[data-zone="due"] .cvs');
    function key(li){return dueInfo(li.dataset.deadline).sort;}
    function byOrder(a,b){return (+a.dataset.order||0)-(+b.dataset.order||0);}
    all(box,'.cv').forEach(function(cv){
      var ul=cv.querySelector('.tks'),rows=all(ul,'.tk');
      rows.sort(function(a,b){return key(a)-key(b)||urg(a)-urg(b)||byOrder(a,b);}).forEach(function(li){ul.appendChild(li);});
      var live=rows.filter(openState),dated=(live.length?live:rows).filter(function(li){
        return dueInfo(li.dataset.deadline).level!=='none';});
      cv.dataset.due=dated.length?Math.min.apply(null,dated.map(key)):'';
      var g=dated.length?null:groupOf(cv.dataset.pg,cv.dataset.slot),to=g?g.querySelector('.cvs'):due;
      if(cv.parentElement!==to)to.appendChild(cv);
    });
    all(due,'.cv').sort(function(a,b){return (+a.dataset.due)-(+b.dataset.due)||byOrder(a,b);})
      .forEach(function(cv){due.appendChild(cv);});
    all(box,'.apg>.cvs').forEach(function(ul){all(ul,'.cv').sort(byOrder).forEach(function(cv){ul.appendChild(cv);});});
  }
  function sortSits(){
    var day=document.getElementById('sch-day'),none=document.getElementById('sch-none');
    if(!day||!none||pinned)return;       // 기한을 고치는 중인 줄은 칸에서 벗어날 때 옮긴다
    sits().sort(byDue).forEach(function(li,i){li.dataset.sorder=i;(isoOf(li.dataset.deadline)?day:none).appendChild(li);});
  }
  function placeSit(li){
    li.classList.toggle('done',li.dataset.state==='done');
    li.querySelector('.st').textContent=statusText(li);
  }
  function setDue(li,value){
    var id=li.dataset.task,prev=li.dataset.manual==='1'?li.dataset.deadline:'',snap=snapshot(li);
    snap.deadline=li.dataset.mdl||'';        // 기록에는 그때 메일에서 뽑혀 있던 기한을 남긴다
    applyDue(id,value?{due:value}:null);
    send('/api/due',{task_id:id,due:value,snapshot:snap}).then(function(j){
      applyDue(id,j.state||null);
      toast(value?'기한을 '+dayLabel(value)+'로 정했어요':'메일에 적힌 기한으로 되돌렸어요',function(){setDue(li,prev);});
    }).catch(function(err){applyDue(id,prev?{due:prev}:null);toast('기한을 저장하지 못했어요 — '+err.message);});
  }
  function flashRow(id){
    var li=sch.querySelector('.sit[data-task="'+id+'"]');if(!li||li.hidden)return;
    li.classList.remove('flash');void li.offsetWidth;li.classList.add('flash');
    li.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function pick(iso,scroll){
    cal.panel='day';cal.day=iso;cal.month=iso.slice(0,7);drawSchedule();
    // 좁은 화면에서는 목록이 달력 아래라 눌렀을 때 그쪽으로 내려 준다
    if(scroll&&window.matchMedia&&window.matchMedia('(max-width:900px)').matches){
      var h=document.getElementById('sp-title');if(h)h.scrollIntoView({block:'start',behavior:'smooth'});}
  }
  if(sch){
    sch.querySelectorAll('[data-sview]').forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.sview===cal.scope));});
    sch.addEventListener('click',function(e){
      var t=e.target,b=t.closest('[data-sview]');
      if(b){
        cal.scope=b.dataset.sview;try{localStorage.setItem('nwmail.sview',cal.scope)}catch(err){}
        sch.querySelectorAll('[data-sview]').forEach(function(x){x.setAttribute('aria-pressed',String(x===b));});
        return drawSchedule();
      }
      if((b=t.closest('[data-cal]'))){
        if(!+b.dataset.cal)return pick(TODAY);
        var d=parts(cal.month+'-01');d.setMonth(d.getMonth()+ +b.dataset.cal);cal.month=ymd(d).slice(0,7);
        return drawSchedule();
      }
      if((b=t.closest('[data-spanel]'))){
        var k=b.dataset.spanel;
        if(k==='none'){var nh=document.getElementById('sch-none-h');if(nh)nh.scrollIntoView({block:'start',behavior:'smooth'});return;}
        if(k==='today')return pick(TODAY,true);
        cal.panel=k;drawSchedule();if(b.dataset.cid)flashRow(b.dataset.cid);return;
      }
      if((b=t.closest('.cd'))){var c=t.closest('.cc');pick(b.dataset.day,true);if(c)flashRow(c.dataset.cid);return;}
      if((b=t.closest('[data-due-rev]'))&&!b.disabled){var li=b.closest('.sit');if(li)setDue(li,'');}
    });
    // 방향키로 날짜를 옮긴다 (좌우 하루, 위아래 한 주)
    sch.addEventListener('keydown',function(e){
      var b=e.target.closest&&e.target.closest('.cd-d'),step={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[e.key];
      if(!b||!step)return;
      e.preventDefault();pick(addDays(b.dataset.day,step));
      var nb=sch.querySelector('.cd-d[data-day="'+cal.day+'"]');if(nb)nb.focus();
    });
    // 날짜를 고르면 저장한다. 키보드로 칸마다 고치는 동안 여러 번 저장하지 않게 잠깐 기다리고,
    // 고치는 줄은 포커스가 줄을 벗어날 때까지 목록에서 옮기지 않는다
    sch.addEventListener('change',function(e){
      var inp=e.target.closest&&e.target.closest('.due-in');if(!inp||inp.disabled)return;
      var li=inp.closest('.sit'),v=inp.value;
      clearTimeout(dueTimer);
      if(!v||!inp.checkValidity()||v===isoOf(li.dataset.deadline))return;
      pinned=li;
      dueTimer=setTimeout(function(){if(inp.value===v)setDue(li,v);},700);
    });
    sch.addEventListener('focusout',function(e){
      if(!pinned||!pinned.contains(e.target))return;
      setTimeout(function(){
        if(!pinned||pinned.contains(document.activeElement))return;
        pinned=null;sortSits();drawSchedule();
      },800);
    });
  }

  function notice(html){var n=document.getElementById('srv-note');if(n){n.innerHTML=html;n.hidden=false;}}
  function disable(title){
    document.querySelectorAll('.act [data-act],.tk [data-act],.task [data-act],.kb-btn,.proj-h>.bar,.wx [data-wmark],.sit [data-act],.due-in,[data-due-rev]').forEach(function(b){
      b.disabled=true;b.title=title;});
  }
  function isoDay(md){   // 'MM/DD' → 가장 가까운 지난 날짜 'YYYY-MM-DD'
    var p=/^(\\d{2})\\/(\\d{2})$/.exec(md||'');if(!p)return '';
    var now=new Date(),y=now.getFullYear();
    if(new Date(y,+p[1]-1,+p[2])>now)y--;
    return y+'-'+p[1]+'-'+p[2];
  }
  if(!served){
    disable('대시보드 서버 주소에서 열면 기록됩니다');
    // 서버 주소로 옮겨 가면서 이 브라우저의 체크 기록을 가져간다 (링크를 눌러도, 자동으로 옮겨도)
    var target=SERVER;
    if(SERVER&&location.protocol==='file:'){
      var payload={done:Array.from(done)};
      try{
        payload.at=JSON.parse(localStorage.getItem('nwmail.doneAt.v1')||'{}');
        payload.theme=localStorage.getItem('nwmail.theme');payload.hide=localStorage.getItem('nwmail.hide');
      }catch(e){}
      target=SERVER+'#import='+encodeURIComponent(JSON.stringify(payload));
      fetch(SERVER+'api/ping',{mode:'no-cors'}).then(function(){location.href=target;}).catch(function(){});
    }
    notice(SERVER
      ?'완료·제외·업무삭제·답변완료 기록은 대시보드 서버 주소에서 열었을 때 저장됩니다. <a id="srv-link">'+SERVER+' 열기</a>'
      :'완료·제외·업무삭제·답변완료 기록은 대시보드 서버(<code>python serve.py</code>)로 열었을 때 저장됩니다.');
    var link=document.getElementById('srv-link');if(link)link.href=target;
  }else{
    // 서버의 최신 표시로 맞춘다 (대시보드가 다시 만들어지기 전에 누른 기록도 반영)
    fetch('/api/feedback',{headers:{'X-Nwmail-Token':conf.token}}).then(function(r){
      if(!r.ok)throw new Error('HTTP '+r.status);return r.json();
    }).then(function(j){
      var states=j.states||{},moved=[],seen={};
      document.querySelectorAll('.act,.tk,.task,.sit').forEach(function(li){
        var st=states[li.dataset.task]||null;
        if((st?st.label:'open')!==li.dataset.state||(st&&st.labeled_at!==li.dataset.at))apply(li,st);
        // 파일로 열었을 때 체크해 둔 할 일(내 할 일 · 프로젝트)은 완료로 옮긴다
        if(!st&&!seen[li.dataset.task]&&done.has(li.dataset.task)){
          seen[li.dataset.task]=1;
          moved.push({task_id:li.dataset.task,label:'done',reason:'',snapshot:snapshot(li),
            on:isoDay((imported&&imported.at||{})[li.dataset.task])});
        }
      });
      // 일정 탭에서 직접 정한 기한도 서버 기록으로 맞춘다
      var dues=j.dues||{},fixed=0;
      document.querySelectorAll('.act,.tk,.task,.sit').forEach(function(el){
        var st=dues[el.dataset.task]||null;
        if((st?'1':'')!==(el.dataset.manual||'')||(st&&st.due!==el.dataset.deadline)){applyDue(el.dataset.task,st,true);fixed++;}
      });
      if(fixed){arrange();sortSits();}
      // 답을 기다리는 메일 · 프로젝트 표시도 서버 기록으로 맞춘다
      var tm=j.threads||{},pm=j.projects||{};
      document.querySelectorAll('.wait[data-thread]').forEach(function(li){
        var st=active(tm[li.dataset.thread]||null,li.dataset.asked);
        if((st?st.label:'open')!==li.dataset.wstate||(st&&st.labeled_at!==li.dataset.at))applyWait(li,st);
      });
      document.querySelectorAll('.proj[data-project]').forEach(function(a){
        var st=active(pm[a.dataset.project]||null,a.dataset.latest);
        if((st?st.label:'')!==(a.dataset.manualState||'')||(st&&st.labeled_at!==a.dataset.at))applyProject(a.dataset.project,st);
      });
      // 프로젝트 색도 서버 기록으로 맞춘다. 예전 서버(색 기록이 없음)면 색 막대만 잠근다 — 서버를 다시 켜면 풀린다
      if(!j.colors)document.querySelectorAll('.proj-h>.bar,[data-color-open]').forEach(function(b){
        b.disabled=true;b.title='대시보드 서버를 다시 켜면 색을 바꿀 수 있어요';});
      else document.querySelectorAll('.proj[data-project]').forEach(function(a){
        var b=a.querySelector('.proj-h>.bar'),c=j.colors[a.dataset.project]||{color:''};
        if(b&&(c.color||'')!==(b.dataset.color||''))paintColor(a.dataset.project,c.color||'',c);
      });
      refresh();
      if(!moved.length)return;
      return post(moved).then(function(res){
        Object.keys(res.states||{}).forEach(function(id){
          var li=document.querySelector('[data-task="'+id+'"]');if(li)apply(li,res.states[id]);});
        try{localStorage.removeItem(KEY)}catch(e){}      // 옮겼으니 브라우저 기록은 지운다
        refresh();toast('예전에 체크한 할 일 '+moved.length+'건을 완료로 옮겼어요');
      });
    }).catch(function(){
      disable('대시보드 서버에 연결하지 못했습니다');
      notice('대시보드 서버에 연결하지 못해 표시를 저장할 수 없습니다. 서버가 켜져 있는지 확인하세요 (<code>python serve.py</code>).');
    });
  }
  refresh();

  var hide=document.getElementById('hide-done');
  try{hide.checked=localStorage.getItem('nwmail.hide')==='1'}catch(e){}
  function applyHide(){document.body.classList.toggle('hide-done',hide.checked);refresh();
    try{localStorage.setItem('nwmail.hide',hide.checked?'1':'0')}catch(e){}}
  hide.addEventListener('change',applyHide);applyHide();

  var tabs=Array.prototype.slice.call(document.querySelectorAll('.tabs [role=tab]'));
  function show(id){
    tabs.forEach(function(t){var on=t.dataset.target===id;
      t.setAttribute('aria-selected',on?'true':'false');t.tabIndex=on?0:-1;
      document.getElementById(t.dataset.target).hidden=!on;});
    try{localStorage.setItem('nwmail.tab',id)}catch(e){}
    if(history.replaceState)history.replaceState(null,'','#'+id.slice(4));
  }
  tabs.forEach(function(t,i){
    t.addEventListener('click',function(){show(t.dataset.target)});
    t.addEventListener('keydown',function(e){
      var d=e.key==='ArrowRight'?1:e.key==='ArrowLeft'?-1:0;if(!d)return;
      var n=tabs[(i+d+tabs.length)%tabs.length];show(n.dataset.target);n.focus();});
  });
  var start=location.hash?'tab-'+location.hash.slice(1):null;
  if(!start||!document.getElementById(start)){try{start=localStorage.getItem('nwmail.tab')}catch(e){}}
  if(!start||!document.getElementById(start))start=tabs[0].dataset.target;
  show(start);

  // 프로젝트 진행 줄을 누르면 프로젝트 탭에서 그 프로젝트를 펼쳐 보여 준다
  document.querySelectorAll('.prow .pgo').forEach(function(b){
    b.addEventListener('click',function(){
      var name=b.closest('.prow').dataset.goto;
      show('tab-projects');
      projEls(name).forEach(function(a){
        if(!a.classList.contains('proj'))return;
        if(a.dataset.pstate!==pview)setPView(a.dataset.pstate);
        a.open=true;a.scrollIntoView({block:'start'});
      });
    });
  });
  document.querySelectorAll('[data-more="waits"]').forEach(function(b){
    b.addEventListener('click',function(){
      b.setAttribute('aria-expanded',b.getAttribute('aria-expanded')==='true'?'false':'true');layoutWaits();
    });
  });
  // 펼친 프로젝트는 새로고침해도 펼쳐 둔다 (처음에는 모두 접힘)
  var PK='nwmail.projOpen',opened={};
  try{JSON.parse(localStorage.getItem(PK)||'[]').forEach(function(n){opened[n]=1;});}catch(e){}
  document.querySelectorAll('.proj[data-project]').forEach(function(a){
    if(opened[a.dataset.project])a.open=true;
    a.addEventListener('toggle',function(){
      if(a.open)opened[a.dataset.project]=1;else delete opened[a.dataset.project];
      try{localStorage.setItem(PK,JSON.stringify(Object.keys(opened)))}catch(e){}
    });
  });
  layoutWaits();layoutProjects();

  // 대화 카드 현황은 두 줄까지만. 넘칠 때만 '더보기' (탭이 가려져 있다 보일 때도 ResizeObserver 로 다시 잰다)
  var clampObs=window.ResizeObserver?new ResizeObserver(function(es){es.forEach(function(e){clamp(e.target);});}):null;
  function clamp(tx){
    var s=tx.parentElement;if(!s.classList.contains('open'))s.classList.toggle('clip',tx.scrollHeight>tx.clientHeight+1);
  }
  function watchCv(cv){all(cv,'.cv-s .tx').forEach(function(tx){clamp(tx);if(clampObs)clampObs.observe(tx);});}
  all(lists.open,'.cv').forEach(watchCv);
  document.addEventListener('click',function(e){
    var b=e.target.closest('.cv-more');if(!b)return;
    var s=b.parentElement,on=!s.classList.contains('open');
    s.classList.toggle('open',on);b.textContent=on?'접기':'더보기';b.setAttribute('aria-expanded',String(on));
    if(!on)clamp(s.querySelector('.tx'));
  });
  // 기한 없는 일의 프로젝트 묶음: 접은 묶음은 새로고침 · 다시 만들어져도 접어 둔다 (처음에는 모두 펼침)
  var FK='nwmail.actFold',folded={},foldBtn=document.querySelector('.az-fold');
  try{JSON.parse(localStorage.getItem(FK)||'[]').forEach(function(n){folded[n]=1;});}catch(e){}
  function syncFold(){
    if(!foldBtn)return;
    var gs=all(lists.open,'.apg').filter(function(g){return !g.hidden;});
    foldBtn.textContent=gs.length&&gs.every(function(g){return !g.open;})?'모두 펼치기':'모두 접기';
  }
  function watchGroup(g){
    if(folded[g.dataset.pg])g.open=false;
    g.addEventListener('toggle',function(){
      if(g.open)delete folded[g.dataset.pg];else folded[g.dataset.pg]=1;
      try{localStorage.setItem(FK,JSON.stringify(Object.keys(folded)))}catch(e){}
      syncFold();
    });
  }
  all(lists.open,'.apg').forEach(watchGroup);
  if(foldBtn)foldBtn.addEventListener('click',function(){
    var close=foldBtn.textContent==='모두 접기';
    all(lists.open,'.apg').forEach(function(g){g.open=!close;});
  });
  syncFold();

  // ── 맨 아래 2depth: 메일 분류 규칙 ────────────────────────────────────────────
  // 1depth 줄을 누르면 펼치고 접는다. 펼친 줄과 고른 2depth 보기는 브라우저에 기억한다 (처음에는 모두 접힘).
  // 자동 새로고침의 스크롤 되돌리기보다 먼저 펼쳐 둬야 보던 자리로 돌아간다.
  var IK='nwmail.info',info={open:{},sub:{}};
  try{var iv=JSON.parse(localStorage.getItem(IK)||'{}');info.open=iv.open||{};info.sub=iv.sub||{};}catch(e){}
  function saveInfo(){try{localStorage.setItem(IK,JSON.stringify(info))}catch(e){}}
  function openInfo(row,on){
    row.querySelector('.info-h').setAttribute('aria-expanded',on?'true':'false');
    row.querySelector('.info-b').hidden=!on;
  }
  function pickInfo(row,key){
    var btns=Array.prototype.slice.call(row.querySelectorAll('[data-sub]'));
    if(!btns.some(function(b){return b.dataset.sub===key;}))key=btns.length?btns[0].dataset.sub:'';
    btns.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.sub===key?'true':'false');});
    row.querySelectorAll('[data-pane]').forEach(function(p){p.hidden=p.dataset.pane!==key;});
    return key;
  }
  document.querySelectorAll('.info-row[data-info]').forEach(function(row){
    var k=row.dataset.info;
    openInfo(row,!!info.open[k]);pickInfo(row,info.sub[k]);
    row.querySelector('.info-h').addEventListener('click',function(){
      info.open[k]=!info.open[k];openInfo(row,info.open[k]);saveInfo();});
    row.querySelectorAll('[data-sub]').forEach(function(b){
      b.addEventListener('click',function(){info.sub[k]=pickInfo(row,b.dataset.sub);saveInfo();});});
  });
  // 다른 탭으로 가는 링크(#blocked 등): 탭을 바꾸고 탭 줄로 올라간다
  window.addEventListener('hashchange',function(){
    var id='tab-'+location.hash.slice(1);if(!document.getElementById(id))return;
    show(id);var nav=document.querySelector('.tabs');if(nav)nav.scrollIntoView({block:'start',behavior:'smooth'});
  });

  // 자동 실행이 10분마다 파일을 새로 만들므로, 보고 있지 않을 때 다시 읽는다.
  // 스크롤 위치 · 펼친 항목 · 내 할 일 보기 · 일정 탭에서 보던 달과 날짜는 유지한다 (sessionStorage).
  var SK='nwmail.view', idle=Date.now();
  function keyOf(d){
    var p=d.closest('[data-project]'),w=d.closest('[data-work]'),g=d.closest('[data-pg]'),cv=d.closest('.cv');
    return (p?p.dataset.project:'')+'|'+(w?w.dataset.work:'')+'|'+(g?g.dataset.pg:'')+'|'
      +(cv?cv.dataset.thread:'')+'|'+d.className;
  }
  try{var v=JSON.parse(sessionStorage.getItem(SK)||'null');
    if(v){sessionStorage.removeItem(SK);
      document.querySelectorAll('details').forEach(function(d){if(v.open.indexOf(keyOf(d))>=0)d.open=true;});
      if(v.acts)setView(v.acts);
      if(v.pview)setPView(v.pview);
      if(v.progressView)setProgressView(v.progressView);
      if(v.sch&&isoOf(v.sch.day)&&isoOf(v.sch.month+'-01')){
        cal.day=v.sch.day;cal.month=v.sch.month;cal.panel=v.sch.panel||'day';drawSchedule();}
      window.scrollTo(0,v.y);}}catch(e){}
  ['mousemove','keydown','scroll','click','touchstart'].forEach(function(ev){
    window.addEventListener(ev,function(){idle=Date.now();},{passive:true});});
  setInterval(function(){
    if(whyBox||openMenu||(!document.hidden&&Date.now()-idle<120000))return;   // 조작 중이면 미룬다
    if(pinned)return;                    // 기한을 고치는 중이면 미룬다
    try{sessionStorage.setItem(SK,JSON.stringify({y:window.scrollY,acts:view,pview:pview,progressView:progressView,
      sch:{month:cal.month,day:cal.day,panel:cal.panel},
      open:Array.prototype.map.call(document.querySelectorAll('details[open]'),keyOf)}));}catch(e){}
    location.reload();
  },600000);
})();
"""
