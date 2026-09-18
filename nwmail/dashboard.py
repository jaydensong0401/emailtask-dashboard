"""대시보드 HTML 생성 (4단계).

정적 HTML 한 장으로 끝나도록 CSS·JS 를 모두 인라인한다. 외부 리소스를 부르지
않으므로 사내망/오프라인에서도 열리고, 메일 내용이 PC 밖으로 나가지 않는다.

완료 체크는 브라우저 localStorage 에 저장한다. 업무 ID 가 (스레드, 종류, 내용)
기반으로 고정돼 있어서 대시보드를 다시 생성해도 체크가 유지된다.
다른 브라우저에서 열거나 사이트 데이터를 지우면 초기화된다.
"""
from __future__ import annotations

import hashlib
import html
import json
from datetime import date, datetime, timedelta, timezone

from .extract import load_all
from .feedback import DUE_YEARS, LABELS, MARKS, REASONS, FeedbackStore, mark_active
from .filters import block_overview, load_filters, sender_domain
from .mailtext import snippet  # noqa: F401 (테스트에서 사용)
from .projects import (
    canonical_org, classify, load_rules, normalize_role, people_orgs, person_key,
    skip_extract, title_key, work_title,
)
from .store import Store, window_start

KST = timezone(timedelta(hours=9))     # 한국은 서머타임이 없어 고정 오프셋으로 충분
UNCLASSIFIED = "미분류"
OTHER_CLIENT = "기타"                   # 설정에 없는 고객사(태그로만 알려진 프로젝트 · 미분류)의 묶음 이름
MAX_SLOTS = 8                           # 범주형 색은 8개까지, 넘치면 중립 회색
FULL_RUN_TIMES = ("09:00", "13:00", "17:00")   # setup_schedule.ps1 의 nwmail-full 시각과 맞춘다
WAIT_PREVIEW = 3                        # 답을 기다리는 메일은 이만큼만 펼쳐 둔다
RECENT_DECISIONS = 5
LABELED_DAYS = 30                       # 완료 · 제외 목록은 최근 이만큼만
WEEKDAYS = "월화수목금토일"
DUE_KEEP_DAYS = 30                      # 최근 N일 창 밖 대화의 기한 있는 할 일은 마감이 이만큼 지나면 뺀다
SCHEDULE_WEEK = 7                       # 일정 탭 '7일 안' 과 탭 숫자의 기준
SCHEDULE_CAP = 3                        # 달력 한 칸에 펼쳐 둘 할 일 수 (넘치면 +N건)
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


def task_id(thread_key: str, kind: str, text: str) -> str:
    """내용 기반 고정 ID. 재생성해도 같은 업무는 같은 ID → 완료 체크 유지."""
    raw = f"{thread_key}\x1f{kind}\x1f{(text or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


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
    """다음 자동 정리(nwmail-full) 시각 (KST)."""
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

    프로젝트 할 일은 진행 중 프로젝트 것만 넣고, 같은 대화의 내 할 일과 사실상 같은 일이면 뺀다.
    기한순(없으면 맨 뒤) → 남은 일 → 내 할 일 → 긴급도 순. 페이지 JS 의 byDue 와 같은 순서다.
    """
    mine: dict[str, list[str]] = {}
    for a in open_actions:
        mine.setdefault(a["thread"]["key"], []).append(a["text"])
    tasks = [t for p in projects if p["pstate"] == "open" for i in p["items"]
             for ts in i["roles"].values() for t in ts if open_task(t)] + stale_tasks
    rows = list(open_actions) + [
        t for t in tasks if iso_day(t["deadline"]["raw"])
        and not any(_same_work(t["text"], x) for x in mine.get(t["thread"]["key"], ()))]
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


def client_slots(rules) -> dict[str, int]:
    """색은 고객사를 따라간다 (세부 프로젝트는 고객사 색). projects.json 순서로 고정 배정한다."""
    return {c: i + 1 for i, c in enumerate(rules.clients[:MAX_SLOTS])}


def _received_at(mails: list[dict]) -> datetime | None:
    """마지막으로 받은 메일 시각 (내가 보낸 메일은 빼고). 완료 표시가 풀리는 기준."""
    return max((d for m in mails if not m.get("is_mine") and (d := parse_dt(m.get("sent_at")))),
               default=None)


# ── 데이터 모델 ─────────────────────────────────────────────────────────────
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
    slots = client_slots(rules)
    extractions = load_all(store)
    states = feedback.states() if feedback else {}
    dues = feedback.dues() if feedback else {}
    thread_marks = feedback.marks("thread") if feedback else {}
    project_marks = feedback.marks("project") if feedback else {}
    prev_run = parse_dt(new_since(store))

    projects: dict[str, dict] = {}
    my_actions: list[dict] = []
    unanswered: list[dict] = []
    handled: list[dict] = []
    direct_threads: list[dict] = []
    extracted = new_threads = excluded = 0

    def slot_of(project: str) -> int:
        """예전 표시 기록의 프로젝트 이름('루미' 등)도 지금 규칙의 고객사 색으로."""
        return slots.get(rules.client_of.get(rules.resolve(project) or project, ""), 0)

    def project(name: str, client: str) -> dict:
        if name not in projects:
            projects[name] = {"name": name, "client": client, "slot": slots.get(client, 0),
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
            tid = task_id(t["thread_key"], "task", text)
            out.append({
                "id": tid, "text": text, "kind": "task",
                "role": role, "fix": fix, "rslot": rules.roles.index(role) + 1,
                "assignee": assignee, "org": org,
                "deadline": due_info(task.get("deadline", ""), dues.get(tid), today),
                "project": name, "slot": slot,
                "state": states.get(tid), "thread": th,
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
                "deadline": due_info(act.get("deadline", ""), dues.get(aid), today),
                "project": name, "slot": slot, "thread": th,
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

        client, name, _ = classify(t["subject"], received[0].get("from_email", ""),
                                   (data or {}).get("project", ""), rules)
        name = name or UNCLASSIFIED
        th = thread_view(t, data, name, is_new)

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
            (handled if mark else unanswered).append({
                "thread": th, "asked": asked, "mark": mark,
                "waited": max((now - parse_dt(asked)).days, 0)})

    # 창 밖으로 나간 대화라도 기한이 있는 남은 할 일은 완료 · 제외할 때까지 남긴다. 마감이
    # DUE_KEEP_DAYS 일 넘게 지난 것은 뺀다. 먼저 추출 결과만으로 거르고, 남은 대화만 불러온다
    keep_from = (today - timedelta(days=DUE_KEEP_DAYS)).isoformat()

    def still_due(key: str, kind: str, text: str, deadline) -> bool:
        tid = task_id(key, kind, text)
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
        client, name, _ = classify(t["subject"], received[0].get("from_email", ""),
                                   data.get("project", ""), rules)
        name = name or UNCLASSIFIED
        th = dict(thread_view(t, data, name, False), stale=True)
        kept = [x for x in my_items(t, data, th, name, slots.get(client, 0))
                + project_tasks(t, data, th, name, slots.get(client, 0),
                                people_orgs(t["mails"], rules))
                if still_due(t["thread_key"], x["kind"], x["text"], x["deadline"]["raw"])]
        my_actions += [x for x in kept if x["kind"] == "me"]
        stale_tasks += [x for x in kept if x["kind"] == "task"]

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
        p["pstate"] = p["mark"]["label"] if p["mark"] else "open"
        p["received_at"] = received
    # 고객사(설정 순서) → 새 메일 → 최근 순. 고객사 안에서 기본 프로젝트(기타)와 미분류는 뒤로
    rank = {c: i for i, c in enumerate(rules.clients)}
    ordered = sorted(
        projects.values(),
        key=lambda p: (rank.get(p["client"], len(rank)), p["name"] == UNCLASSIFIED,
                       rules.is_default(p["name"]), not p["new"],
                       -(p["latest"].timestamp() if p["latest"] else 0)),
    )
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
        t["project"], "done" if t["project"] in project_marks else "open") == "open"]
    live = {a["id"]: a for a in my_actions}
    live.update({t["id"]: t for p in ordered for i in p["items"]
                 for ts in i["roles"].values() for t in ts})
    live.update({t["id"]: t for t in stale_tasks})
    open_actions = [a for a in my_actions if a["id"] not in states]
    done: list[dict] = []
    dropped: list[dict] = []
    for row in feedback.recent(LABELED_DAYS, now) if feedback else []:
        item = dict(live.get(row["task_id"])
                    or _snapshot_action(row, slot_of, today, rules.roles, dues), state=row)
        (done if row["label"] == "done" else dropped).append(item)

    # 일정 탭 요약 · 탭 숫자는 남은 내 할 일 기준 (프로젝트 할 일은 페이지에서 보기를 바꿀 때만 센다)
    schedule = _schedule_items(open_actions, ordered, stale_tasks, done)
    today_iso = today.isoformat()
    week_end = (today + timedelta(days=SCHEDULE_WEEK)).isoformat()
    days = [iso_day(a["deadline"]["raw"]) for a in open_actions]
    sched = {"overdue": sum(bool(d) and d < today_iso for d in days),
             "today": days.count(today_iso),
             "week": sum(today_iso < d <= week_end for d in days),
             "none": days.count("")}

    filters = load_filters(store, enabled_only=False)
    blocked = store.blocked_mails(window_days=window_days, now=now)
    levels = [a["deadline"]["level"] for a in open_actions]
    return {
        "generated_at": now, "today": today, "me": me, "window_days": window_days,
        "me_names": _my_names(threads),
        "last_sync": store.last_run_at("sync", offset=0),
        "last_extract": latest(store.last_run_at("extract", offset=0),
                               store.last_extracted_at()),
        "next_extract": next_full_run(now),
        "projects": ordered, "my_actions": open_actions, "done_actions": done,
        "dropped_actions": dropped, "schedule": schedule,
        "unanswered": unanswered, "handled": handled,
        "direct_threads": direct_threads, "blocked": blocked,
        "recent_decisions": recent[:RECENT_DECISIONS],
        "filters": filters, "blocking": block_overview(store, me), "roles": list(rules.roles),
        "stats": {"threads": len(threads), "blocked": len(blocked), "rules": len(filters),
                  "history": sum(t["mail_count"] - t["recent_count"] for t in threads),
                  "unanswered": len(unanswered), "new": new_threads,
                  "extracted": extracted, "excluded": excluded,
                  "my_open": len(open_actions), "due_today": levels.count("today"),
                  "overdue": levels.count("overdue"), "done": len(done),
                  "done_today": sum(_kst_day(d["state"]["labeled_at"]) == today for d in done),
                  "dropped": len(dropped),
                  **{f"sched_{k}": v for k, v in sched.items()},
                  "sched_badge": sched["overdue"] + sched["today"] + sched["week"],
                  "oldest_wait": max((u["waited"] for u in unanswered), default=0),
                  "named_projects": sum(p["name"] != UNCLASSIFIED and p["pstate"] == "open"
                                        for p in ordered),
                  **{f"projects_{k}": sum(p["pstate"] == k for p in ordered)
                     for k in ("open", *MARKS["project"])}},
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


def _badges(th: dict) -> str:
    out = []
    if th["new"]:
        out.append('<span class="new">NEW</span>')
    if th["direct"]:
        out.append('<span class="chip">직접</span>')
    if th.get("stale"):
        out.append('<span class="chip" title="최근 메일이 없는 대화 — 기한이 있어 완료 · 제외할 때까지 남겨 둡니다">'
                   '지난 대화</span>')
    return "".join(out)


def _who(item: dict) -> str:
    """담당자 이름 + 소속. 이름이 없으면 '미지정' 으로 비어 있음을 드러낸다."""
    name = (f'<span class="nm">{esc(item["assignee"])}</span>' if item.get("assignee")
            else '<span class="nm none">미지정</span>')
    org = f'<span class="org">{esc(item["org"])}</span>' if item.get("org") else ""
    return f'<span class="who">{name}{org}</span>'


def _role_tag(item: dict) -> str:
    label = item["role"] + (f'·{item["fix"]}' if item.get("fix") else "")
    return f'<b class="rtag rt{item.get("rslot", 0)}">{esc(label)}</b>'


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
        # 일정 탭에서 기한을 바꾸면 페이지가 세 탭의 같은 할 일을 함께 고친다 (메일 기한은 되돌릴 값)
        "data-mdl": d.get("mail", d["raw"]), "data-manual": "1" if d.get("manual") else "",
    }


def _task(item: dict, show_src: bool = True) -> str:
    """프로젝트 업무 한 줄: 역할 태그 + 내용 / 담당자 · 기한 · 출처 + 표시 버튼."""
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
        f'<div class="th"><p class="t">{_role_tag(item)}{esc(item["text"])}</p>'
        f'{_badges(item["thread"]) if item["thread"]["new"] else ""}</div>'
        f'<div class="meta">{"".join(meta)}{_controls(item)}</div></li>'
    )


def _thread_row(th: dict, extra: str = "") -> str:
    body = th["summary"] or th["snippet"]
    return (
        f'<li class="thr"><div class="thr-h"><span class="subj">{esc(th["subject"])}</span>'
        f'{_badges(th)}</div><div class="meta"><span>{esc(th["from"])}</span>'
        f'<span class="num">{esc(fmt_dt(th["last_at"]))}</span>'
        f'<span class="num">{_count_label(th)}</span>{extra}</div>'
        + (f'<p class="snip">{esc(body)}</p>' if body else "") + "</li>"
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

    counts = [f"{name or '결정자 미기재'} {len(ds)}" for name, ds in ordered]
    summary = " · ".join(counts[:3]) + (f" 외 {len(counts) - 3}명" if len(counts) > 3 else "")
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
            f' <span class="ds">{esc(summary)}</span></summary>{"".join(blocks)}</details>')


def _work_item(item: dict, roles: list[str]) -> str:
    """업무 묶음 하나 = 프로젝트 안의 카드 한 장.

    머리 띠(업무명 · 진행 상태 · 남은 할 일 · 메일 수) 아래에 할 일 목록(역할 태그, 역할 순) ·
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
    if extracted and shown:     # 남은 할 일 / 전체는 표시할 때마다 페이지가 다시 센다 (data-wopen · data-wtotal)
        info.append(f'남은 할 일 <b class="num" data-wopen>{sum(open_task(t) for t in shown)}</b>'
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
WAIT_MENU = (("answered", "답변완료"), ("deleted", "삭제"))
# 상태에 맞는 것만 보인다 (진행 중: 완료 · 삭제 / 완료 · 삭제: 되돌리기)
PROJECT_MENU = (("done", "프로젝트 완료"), ("deleted", "삭제"), ("open", "진행 중으로 되돌리기"))


def _kebab(items, attr: str, label: str) -> str:
    """세로 ⋮ 메뉴. 메뉴 칸은 스크롤 영역에 잘리지 않도록 페이지가 버튼 옆에 띄운다."""
    buttons = "".join(f'<button type="button" role="menuitem" data-{attr}="{k}">{esc(t)}</button>'
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
            f'data-at="{esc((p["mark"] or {}).get("labeled_at", ""))}"')


def _project_card(p: dict, roles: list[str]) -> str:
    """접고 펴는 프로젝트 한 칸. 접힌 머리말에 이름 · 남은 할 일 · 최근 메일 날짜."""
    meta = (f'업무 {len(p["items"])} · 남은 할 일 '
            f'<b class="num" data-open="{esc(p["name"])}">{p["open_count"]}</b>/'
            f'<span data-ptotal>{p["task_count"]}</span> · '
            f'결정 {p["decision_count"]} · 대화 {len(p["threads"])}')
    if p["latest"]:
        meta += f' · 최근 <span class="num">{p["latest"].astimezone(KST):%m/%d}</span>'
    head = (
        f'<summary class="proj-h"><i class="bar s{p["slot"]}" aria-hidden="true"></i>'
        f'<h3>{esc(p["name"])}</h3>{"<span class=new>NEW</span>" if p["new"] else ""}'
        f'<span class="pm">{meta}</span>'
        f'<span class="st">{esc(_mark_text(p["mark"], "project"))}</span>'
        f'{_kebab(PROJECT_MENU, "pmark", p["name"] + " 메뉴")}</summary>'
    )
    body = "".join(_work_item(i, roles) for i in p["items"])
    hidden = "" if p["pstate"] == "open" else " hidden"
    return (f'<details class="proj" data-project="{esc(p["name"])}" {_project_attrs(p)}{hidden}>'
            f'{head}<div class="proj-b">{body}</div></details>')


def _projects_tab(m: dict) -> str:
    """프로젝트 탭: 진행 중 · 완료 · 삭제 보기 + 고객사별 묶음 + 접고 펴는 프로젝트."""
    projects, s = m["projects"], m["stats"]
    if not projects:
        return '<div class="pad"><p class="empty-state">스레드가 없습니다.</p></div>'
    views = [
        ("open", "진행 중", "새 메일이 온 프로젝트부터 · 오른쪽 메뉴에서 완료하면 완료 보기로 옮겨집니다.",
         "진행 중인 프로젝트가 없습니다."),
        ("done", "완료", "완료한 뒤 새 메일을 받으면 진행 중으로 돌아옵니다.",
         "완료한 프로젝트가 없습니다."),
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
    for client in clients:
        ps = [p for p in projects if p["client"] == client]
        shown = sum(p["pstate"] == "open" for p in ps)
        cards = "".join(_project_card(p, m["roles"]) for p in ps)
        groups.append(
            f'<section class="client" data-client="{esc(client)}"{"" if shown else " hidden"}>'
            f'<div class="client-h"><i class="sw s{ps[0]["slot"]}" aria-hidden="true"></i>'
            f'<h2>{esc(client or OTHER_CLIENT)}</h2><span class="cnt num" data-ccnt>{shown}</span>'
            f'</div><div class="projs">{cards}</div></section>')
    return (
        '<div class="pad"><div class="ptools">'
        f'<div class="seg" role="group" aria-label="프로젝트 보기">{seg}</div>'
        f'<p class="note" id="pview-note">{esc(views[0][2])}</p></div>'
        f'<div class="pgroups">{"".join(groups)}</div>'
        f'<p class="empty-state" id="pempty"{" hidden" if s["projects_open"] else ""}>'
        f'{esc(views[0][3])}</p></div>'
    )


# ── 오늘 탭 (1a: 할 일 · 진행 / 답을 기다리는 메일 · 결정사항) ──────────────────
def _hero(m: dict) -> str:
    s = m["stats"]
    wait = f'답 기다리는 메일 <em class="c-warn"><span id="hl-wait">{s["unanswered"]}</span>건</em>'
    title = (f'처리할 일이 <em class="c-acc"><span id="hl-me">{s["my_open"]}</span>건</em>, {wait}'
             if s["extracted"] else f'업무 정리 전 · {wait}')
    local = m["generated_at"].astimezone(KST)
    nxt = m["next_extract"]
    nxt_label = f"{nxt:%H:%M}" if nxt.date() == local.date() else f"내일 {nxt:%H:%M}"
    sync = fmt_dt(m["last_sync"]) if m["last_sync"] else "없음"
    ext = fmt_dt(m["last_extract"]) if m.get("last_extract") else "없음"
    d = m["today"]
    return (
        '<header class="hero"><div class="hero-t">'
        f'<p class="eyebrow">MAIL DIGEST · {d:%m/%d} {WEEKDAYS[d.weekday()]}</p>'
        f'<h1>{title}</h1>'
        f'<p class="sub" title="생성 {local:%Y-%m-%d %H:%M}">{esc(m["me"])} · 최근 '
        f'{m["window_days"]}일 대화 {s["threads"]}건 · 마지막 동기화 {esc(sync)}</p></div>'
        '<div class="hero-side"><div class="tools">'
        '<label class="pill switch">완료 숨기기<input type="checkbox" id="hide-done" role="switch">'
        '</label><button type="button" id="theme" class="pill" aria-label="테마 전환">테마: 시스템'
        '</button></div>'
        f'<p class="runs">마지막 정리 <b class="num">{esc(ext)}</b> · '
        f'다음 자동 정리 <b class="num">{esc(nxt_label)}</b></p></div></header>'
    )


def _kpis(m: dict) -> str:
    s = m["stats"]
    todo = s["threads"] - s["excluded"]
    left = todo - s["extracted"]

    def tile(label: str, value: str, unit: str, caption: str) -> str:
        unit_html = f'<span class="u">{unit}</span>' if unit else ""
        return (f'<div class="kpi"><p class="l">{label}</p><p class="v">{value}{unit_html}</p>'
                f'<p class="c">{caption}</p></div>')

    if s["extracted"]:
        me = tile("내 할 일", f'<b class="num" id="kpi-me">{s["my_open"]}</b>', "건 남음",
                  f'오늘 마감 <span id="kpi-today">{s["due_today"]}</span> · '
                  f'지난 마감 <span id="kpi-over">{s["overdue"]}</span> · '
                  f'오늘 완료 <span id="kpi-done">{s["done_today"]}</span>')
    else:
        me = tile("내 할 일", '<b class="num">—</b>', "", "업무 정리 대기")
    if s["oldest_wait"]:
        wait_caption = f'가장 오래된 건 {s["oldest_wait"]}일 경과'
    else:
        wait_caption = "모두 오늘 온 메일" if s["unanswered"] else "모두 답했습니다"
    summary_caption = f"남은 {left}건은 다음 정리 때" if left > 0 else "모두 정리됨"
    if s["excluded"]:
        summary_caption += f' · 정기 메일 {s["excluded"]}건 제외'
    warn = " c-warn" if s["unanswered"] else ""
    return '<div class="kpis">' + "".join([
        me,
        tile("미응답", f'<b class="num{warn}" id="kpi-wait">{s["unanswered"]}</b>', "건 대기",
             f'<span id="kpi-wait-c">{wait_caption}</span>'),
        tile("진행 프로젝트", f'<b class="num" id="kpi-proj">{s["named_projects"]}</b>', "개",
             f'새로 온 대화 {s["new"]}건'),
        tile("요약 완료", f'<b class="num">{s["extracted"]}<span class="den">/{todo}</span></b>',
             "", summary_caption),
    ]) + "</div>"


def _status_text(state: dict | None) -> str:
    if not state:
        return ""
    day = _kst_day(state["labeled_at"])
    when = f"{day:%m/%d}" if day else ""
    if state["label"] == "done":
        return f"{when} 완료"
    return " · ".join(x for x in (LABELS[state["label"]], state.get("reason"), when) if x)


# 남은 내 할 일은 마감 기준으로 묶는다 (build_model 의 마감일순 정렬과 같은 순서). 페이지 JS 도 이 이름을 쓴다
ACT_GROUPS = {"overdue": "기한 지남", "today": "오늘 마감", "soon": "3일 안에 마감",
              "later": "그 이후", "none": "기한 없음"}
RELATED_PREVIEW = 2                     # 할 일 카드에 펼쳐 둘 다른 담당자 업무 수
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


def _same_work(a: str, b: str) -> bool:
    """사실상 같은 일인가: 띄어쓰기를 뺀 두 글자 조각이 짧은 쪽 기준 60% 이상 겹친다.

    업무 정리가 내 할 일을 남의 업무로 한 번 더 적는 경우가 있어, 카드의 다른 담당자 줄에서 뺀다.
    """
    def grams(s: str) -> set[str]:
        s = "".join(s.split()).lower()
        return {s[i:i + 2] for i in range(len(s) - 1)}
    x, y = grams(a), grams(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.6


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


def _act_details(a: dict, ctx: dict) -> str:
    """카드 가운데 칸: 현황(대화 요약 + 진행 상태) · 다른 담당자(같은 대화에서 남이 맡은 남은 일)."""
    th, rows = a["thread"], []
    if body := th.get("summary") or th.get("snippet"):
        status = th.get("status") or ""
        chip = (f'<span class="stt {THREAD_STATUS.get(status, "s-ing")}">{esc(status)}</span>'
                if status else "")
        rows.append(f'<div><dt>{"현황" if th.get("summary") else "최근 메일"}</dt>'
                    f'<dd>{chip}{esc(body)}</dd></div>')
    others = sorted((t for t in ctx.get("others", ()) if not _same_work(t["text"], a["text"])),
                    key=lambda t: t["deadline"]["sort"])
    if others:
        lines = "".join(f'<p class="oth">{_who(t)}<span class="rx">{esc(t["text"])}</span>'
                        f'{_deadline(t["deadline"])}</p>' for t in others[:RELATED_PREVIEW])
        if len(others) > RELATED_PREVIEW:
            lines += f'<p class="oth more">외 {len(others) - RELATED_PREVIEW}건은 프로젝트 탭에서</p>'
        rows.append(f'<div><dt>다른 담당자</dt><dd>{lines}</dd></div>')
    return f'<dl class="ctx">{"".join(rows)}</dl>' if rows else ""


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


def _act(a: dict, order: int | None = None, ctx: dict | None = None, details: bool = True) -> str:
    """내 할 일 카드. 위에서부터 한눈에:
    긴급도 · 기한(날짜) · NEW/직접 · 프로젝트 / 할 일 / 현황 · 다른 담당자 / 출처 메일(제목은 말풍선) + 버튼.

    상태는 data-state 하나로 두고, 버튼을 누르면 페이지가 카드를 해당 목록으로 옮긴다.
    details=False 는 현황 · 다른 담당자 칸을 뺀다 (완료 · 제외 목록에서는 어차피 가려지는 칸).
    """
    th, d = a["thread"], a["deadline"]
    here = (ctx or {}).get(th["key"], {})
    if a.get("kind") == "task":
        tags = [_role_tag(a)]
        if a.get("assignee"):
            tags.append(f'<span class="tag">{esc(a["assignee"])}</span>')
    else:
        glyph, label = URG_LABEL[a["urgency"]]
        tags = [f'<span class="tag u-{a["urgency"]}"><i aria-hidden="true">{glyph}</i>'
                f'{label}</span>']
    # 기한이 없어도 자리는 둔다 (일정 탭에서 기한을 정하면 페이지가 채운다)
    tags.append(f'<span class="due lv-{d["level"]}">{esc(_due_text(d))}</span>{MANUAL_TAG}')
    tags.append(_badges(th))
    tags.append(f'<span class="pj"><i class="sw s{a["slot"]}" aria-hidden="true"></i>'
                f'{esc(a["project"])}</span>')
    attrs = {**_state_attrs(a), "data-order": "" if order is None else str(order),
             "data-new": "1" if th.get("new") else ""}
    return (
        '<li class="act" ' + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items()) + ">"
        f'<div class="tg">{"".join(tags)}</div><p class="t">{esc(a["text"])}</p>'
        f'{_act_details(a, here) if details else ""}'
        f'<div class="tf">{_mail_line(a, here)}{_controls(a)}</div></li>'
    )


def _act_templates(m: dict) -> str:
    """프로젝트 · 일정 탭에서 표시한 할 일을 오늘 탭 완료 / 제외·삭제 목록에 바로 올릴 카드 틀.

    오늘 탭에 카드가 없는 프로젝트 할 일만 <template> 에 미리 그려 둔다 (화면에 안 보이고, 문서 검색에도
    걸리지 않는다). 누르면 페이지 JS 가 복사해 목록에 넣고, 다음 갱신 때 서버가 그린 카드로 바뀐다.
    """
    shown = {a["id"] for a in (*m["my_actions"], *m.get("done_actions", ()), *m.get("dropped_actions", ()))}
    tasks = {t["id"]: t for p in m["projects"] for i in p["items"] for ts in i["roles"].values() for t in ts}
    tasks.update({r["id"]: r for r in m.get("schedule", ()) if r.get("kind") == "task"})
    return "".join(f'<template id="act-{tid}">{_act(t, details=False)}</template>'
                   for tid, t in tasks.items() if tid not in shown)


def _act_group(level: str, n: int) -> str:
    """남은 할 일 목록 안의 마감 묶음 머리줄. 카드가 오가면 페이지 JS(regroup)가 같은 모양으로 다시 단다."""
    return (f'<li class="grp lv-{level}" data-grp="{level}"><span class="gl">{ACT_GROUPS[level]}'
            f'</span><b class="gn num">{n}</b></li>')


def _open_acts(acts: list[dict], ctx: dict) -> str:
    out = []
    for i, a in enumerate(acts):
        level = a["deadline"]["level"]
        if not i or acts[i - 1]["deadline"]["level"] != level:
            out.append(_act_group(level, sum(x["deadline"]["level"] == level for x in acts)))
        out.append(_act(a, i, ctx))
    return "".join(out)


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
        ("open", "할 일", acts, "남은 내 할 일이 없습니다.",
         "마감이 급한 순 · ✉ 메일 줄에 마우스를 올리면 제목이 보입니다.\n"
         "완료·제외·업무삭제로 표시하면 목록에서 빠지고, 표시 기록은 업무 정리 기준을 다듬는 데 씁니다."),
        ("done", "완료", m.get("done_actions", []),
         f"최근 {LABELED_DAYS}일 동안 완료한 일이 없습니다.",
         f"최근 {LABELED_DAYS}일 · 최근에 완료한 순"),
        ("dropped", "제외·삭제", m.get("dropped_actions", []),
         f"최근 {LABELED_DAYS}일 동안 제외하거나 삭제한 일이 없습니다.",
         f"최근 {LABELED_DAYS}일 · 최근에 표시한 순"),
    ]
    seg = "".join(
        f'<button type="button" data-view="{key}" data-note="{esc(note)}" '
        f'aria-pressed="{"true" if key == "open" else "false"}">'
        f'{label} <b class="num" id="cnt-{key}">{len(items)}</b></button>'
        for key, label, items, _, note in views)
    lists = "".join(
        f'<ul class="acts" data-list="{key}"{"" if key == "open" else " hidden"}>'
        + (_open_acts(items, ctx) if key == "open"
           else "".join(_act(a, None, ctx) for a in items)) + "</ul>"
        f'<p class="empty-state" data-empty="{key}"'
        f'{" hidden" if items or key != "open" else ""}>{empty}</p>'
        for key, _, items, empty, _ in views)
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
        total = p["task_count"]
        if p["name"] == UNCLASSIFIED and not total:
            continue
        rows.append(
            f'<div class="prow" data-goto="{esc(p["name"])}" {_project_attrs(p)}'
            f'{"" if p["pstate"] == "open" else " hidden"}>'
            '<button type="button" class="pgo" title="프로젝트 탭에서 보기">'
            f'<span class="pn"><span class="pname">{esc(p["name"])}'
            f'</span><span class="pc num">대화 {len(p["threads"])} · 결정 {p["decision_count"]}'
            '</span></span><span class="track" aria-hidden="true"><span class="fill" '
            f'style="width:{round((total - p["open_count"]) * 100 / total) if total else 0}%">'
            '</span></span>'
            f'<span class="pv num" data-total="{total}">'
            f'{f"{total - p['open_count']}/{total}" if total else "할 일 없음"}'
            f'</span></button>{_kebab(PROJECT_MENU, "pmark", p["name"] + " 메뉴")}</div>')
    if not rows:
        return ""
    return ('<div class="sec-h gap"><h2>프로젝트별 진행</h2>'
            '<span class="sec-m">완료한 할 일 / 전체</span></div>'
            f'<div class="progs">{"".join(rows)}</div>'
            f'<p class="empty-state" id="progs-empty"{" hidden" if m["stats"]["projects_open"] else ""}>'
            '진행 중인 프로젝트가 없습니다.</p>')


def _wait(u: dict, extra: bool = False) -> str:
    """답을 기다리는 메일 한 칸. ⋮ 메뉴로 답변완료 · 삭제, 표시한 칸은 되돌리기 버튼."""
    th, waited, mark = u["thread"], u["waited"], u.get("mark")
    who = " · ".join(x for x in (th["from"], th["project"]) if x and x != UNCLASSIFIED)
    sub = (f'<p class="wsub" title="{esc(th["subject"])}">{esc(th["subject"])}</p>'
           if th["summary"] else "")
    attrs = {"data-thread": th["key"], "data-asked": u["asked"], "data-waited": str(waited),
             "data-title": th["subject"], "data-wstate": mark["label"] if mark else "open",
             "data-at": mark["labeled_at"] if mark else ""}
    return (
        f'<li class="wait{" old" if waited >= 2 else ""}{" extra" if extra else ""}" '
        + " ".join(f'{k}="{esc(v)}"' for k, v in attrs.items())
        + f'{" hidden" if extra else ""}><div class="wh"><span class="wf">{esc(who)}</span>'
        f'<span class="wd num">{f"{waited}일 경과" if waited else "오늘"}</span>'
        f'{_kebab(WAIT_MENU, "wmark", "메일 메뉴")}</div>'
        f'<p class="ws" title="{esc(th["summary"] or th["subject"])}">'
        f'{esc(th["summary"] or th["subject"])}</p>{sub}'
        f'<div class="wfoot"><span class="st">{esc(_mark_text(mark, "thread"))}</span>'
        '<button type="button" class="btn undo" data-wmark="open">되돌리기</button></div></li>')


def _waiting(m: dict) -> str:
    items, handled = m["unanswered"], m.get("handled", [])
    head = ('<div class="sec-h"><h2>답을 기다리는 메일</h2>'
            '<span class="sec-m">받는사람(TO) 기준 · '
            f'<b class="c-warn num" id="wait-cnt">{len(items)}</b></span></div>')
    rows = "".join(_wait(u, i >= WAIT_PREVIEW) for i, u in enumerate(items))
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
    return ('<div class="sec-h gap"><h2>최근 결정사항</h2>'
            '<span class="sec-m">전체는 프로젝트 탭</span></div>'
            f'<ul class="rds">{rows}</ul>')


def _today_tab(m: dict) -> str:
    return (f'<div class="today"><div class="col-main">{_acts(m)}{_progress(m)}</div>'
            f'<div class="col-side">{_waiting(m)}{_recent_decisions(m)}</div></div>')


# ── 일정 탭 (마감 달력 · 기한 직접 정하기) ─────────────────────────────────────
def _sit(a: dict, today: date, order: int, shown: bool) -> str:
    """일정 탭 할 일 한 줄: 긴급도(프로젝트 할 일은 역할 · 담당자) · 기한 · 프로젝트 / 할 일 / 기한 정하기 / 버튼.

    달력 칸의 짧은 표시는 페이지 JS 가 이 줄의 data-* 로 그린다. 기한 · 표시가 바뀌면 줄은 그대로 두고
    data-* 만 고친 뒤 달력과 목록을 다시 그린다.
    """
    d, th = a["deadline"], a["thread"]
    if a.get("kind") == "task":
        tags = [_role_tag(a)] + ([f'<span class="tag">{esc(a["assignee"])}</span>']
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
    mine_today = [r.get("kind") != "task" and iso_day(r["deadline"]["raw"]) == today_iso for r in dated]
    day_rows = "".join(_sit(r, today, i, shown) for i, (r, shown) in enumerate(zip(dated, mine_today)))
    none_rows = "".join(_sit(r, today, len(dated) + i, r.get("kind") != "task" and open_task(r))
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
  function served(c){return !!c.sameOrigin||(!!c.port&&/^https?:$/.test(location.protocol)&&location.port===String(c.port));}
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


def render_html(m: dict, notice: str = "", server: dict | None = None) -> str:
    """server = {"port": 8787, "token": "..."} 이면 버튼 기록을 그 로컬 서버로 보낸다."""
    s = m["stats"]
    conf = {"port": (server or {}).get("port"), "token": (server or {}).get("token", ""),
            # 클라우드 데모: 페이지를 연 주소의 서버로 기록한다 (127.0.0.1:포트 가 아니어도)
            "sameOrigin": bool((server or {}).get("same_origin")),
            "labels": LABELS, "reasons": {k: list(v) for k, v in REASONS.items()},
            "groups": ACT_GROUPS,
            "marks": MARKS, "waitPreview": WAIT_PREVIEW, "unclassified": UNCLASSIFIED,
            # 일정 탭: 기한 등급은 서버의 오늘(KST) 기준으로 센다
            "today": m["today"].isoformat(), "week": SCHEDULE_WEEK, "cap": SCHEDULE_CAP,
            "glyphs": GLYPH, "holidays": HOLIDAYS}
    conf_json = json.dumps(conf, ensure_ascii=False).replace("<", "\\u003c")
    banner = ""
    if not s["extracted"] and s["threads"] - s.get("excluded", 0):
        banner = ('<div class="banner">업무·결정사항 정리가 아직 없습니다. 프로젝트는 제목 키워드·태그·'
                  '발신 도메인으로 먼저 묶어 보여줍니다. 정리: <code>python extract.py</code> 후 '
                  '<code>python dashboard.py</code></div>')
    if notice:
        banner = f'<div class="banner warn" role="alert">{esc(notice)}</div>' + banner

    tabs = [("tab-today", "오늘", None),
            ("tab-projects", "프로젝트", s["projects_open"]),
            ("tab-schedule", "일정", s["sched_badge"]),
            ("tab-blocked", "차단됨", s["blocked"])]
    tablist = "".join(
        f'<button class="tab" role="tab" id="b-{tid}" aria-controls="{tid}" '
        f'data-target="{tid}" aria-selected="false" tabindex="-1">{esc(label)}'
        + (f'<span class="cnt" data-tabcnt="{tid}">{n}</span>' if n is not None else "")
        + "</button>"
        for tid, label, n in tabs)

    panels = {
        "tab-today": _today_tab(m),
        "tab-projects": _projects_tab(m),
        "tab-schedule": _schedule_tab(m),
        "tab-blocked": f'<div class="pad">{_blocked_tab(m)}</div>',
    }
    panel_html = "".join(
        f'<section class="panel" role="tabpanel" id="{tid}" aria-labelledby="b-{tid}" hidden>'
        f'{panels[tid]}</section>' for tid, _, _ in tabs)

    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>메일 업무 대시보드</title><style>{CSS}</style></head><body>'
        f'<main class="wrap"><div class="card">{_hero(m)}{banner}{_kpis(m)}'
        f'<nav class="tabs" role="tablist" aria-label="대시보드 보기">{tablist}</nav>'
        f'{panel_html}</div></main>{_act_templates(m)}'
        '<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>'
        f'<script type="application/json" id="nw-conf">{conf_json}</script>'
        f'<script>{JS}</script></body></html>'
    )


# 색은 1a(라이트) · 1b(다크) 디자인 기준. 글꼴은 PC 에 설치된 Pretendard, 없으면 맑은 고딕.
LIGHT = """
 --page:#f2f1ec;--card:#fdfcf9;--side:#faf9f5;--surface:#fff;
 --ink:#1c1b18;--ink-2:#57534a;--muted:#6f6a60;
 --line:rgba(0,0,0,.08);--line-2:rgba(0,0,0,.07);--box:#c9c4b8;--switch:#ddd9cf;
 --acc:#1f6f6a;--acc-bg:#e6efee;--acc-ink:#fff;
 --warn:#b06a1e;--warn-bg:#f3e6d4;--warn-soft:#d9c59a;--crit:#b3261e;--caution:#c28a00;
 --tag:#eeeae0;--tag-ink:#6b6252;--track:#eeeae0;--new-bg:#e6efee;--new-ink:#14514d;
 --shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px -18px rgba(0,0,0,.25);--focus:#1f6f6a;
 --s0:#898781;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
 --s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948"""

DARK = """color-scheme:dark;
 --page:#0c1413;--card:#15201f;--side:#121c1b;--surface:#1b2927;
 --ink:#f2f1ec;--ink-2:#c3d0cd;--muted:#98a8a4;
 --line:rgba(255,255,255,.09);--line-2:rgba(255,255,255,.07);--box:#5d6e6a;
 --switch:rgba(255,255,255,.2);
 --acc:#5fd2c0;--acc-bg:rgba(95,210,192,.12);--acc-ink:#0f1a19;
 --warn:#e8b476;--warn-bg:rgba(224,160,74,.18);--warn-soft:#6b5a3e;--crit:#f08a7e;
 --caution:#e8c15a;--tag:rgba(255,255,255,.08);--tag-ink:#c3d0cd;
 --track:rgba(255,255,255,.08);--new-bg:rgba(95,210,192,.18);--new-ink:#9fe7da;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -18px rgba(0,0,0,.6);--focus:#5fd2c0;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;
 --s7:#9085e9;--s8:#e66767"""

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

.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border-block:1px solid var(--line)}
.kpi{background:var(--card);padding:22px 28px 24px;min-width:0}
.kpi p{margin:0}
.kpi .l{font-size:12px;color:var(--muted)}
.kpi .v{display:flex;align-items:baseline;gap:8px;margin-top:10px}
.kpi .v b{font-size:34px;line-height:1;font-weight:600}
.kpi .den{font-size:17px;font-weight:500;color:var(--muted)}
.kpi .u,.kpi .c{font-size:11.5px;color:var(--muted)}
.kpi .c{margin-top:12px}

.tabs{display:flex;gap:4px;padding:0 30px;box-shadow:inset 0 -1px 0 var(--line);overflow:auto hidden}
.tab{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
 padding:14px 10px 12px;font:inherit;font-size:13.5px;color:var(--muted);cursor:pointer;white-space:nowrap}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
.cnt{display:inline-block;min-width:18px;margin-left:6px;padding:0 6px;border-radius:999px;
 background:var(--tag);color:var(--tag-ink);font-size:11px;font-weight:500;text-align:center}
.pad{padding:28px 36px 40px}

.today{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr)}
.col-main{padding:34px 36px 40px;border-right:1px solid var(--line);min-width:0}
.col-side{padding:34px 36px 40px;background:var(--side);min-width:0}
.sec-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:0 0 18px}
.sec-h.gap{margin-top:38px}
.sec-h h2{margin:0;font-size:16px;line-height:1.2;font-weight:600}
.sec-m{flex:none;font-size:11.5px;color:var(--muted)}
.note{margin:-6px 0 14px;font-size:12.5px;color:var(--muted)}

.task.done .t{text-decoration:line-through;color:var(--muted)}
.task:not([data-state="open"]) .t .rtag{opacity:.55}
.hide-done .task.done{display:none}

.seg{display:inline-flex;flex-wrap:wrap;gap:2px;padding:3px;border-radius:999px;background:var(--tag)}
.seg button{appearance:none;border:0;border-radius:999px;background:none;padding:6px 12px;font:inherit;
 font-size:12.5px;color:var(--muted);cursor:pointer;white-space:nowrap}
.seg button b{font-weight:600;margin-left:2px}
.seg button[aria-pressed="true"]{background:var(--surface);color:var(--ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
#tab-today .note{margin:-8px 0 16px}
#srv-note{margin:0 0 16px}
.acts{display:flex;flex-direction:column;gap:12px}
.acts .grp{display:flex;align-items:center;gap:8px;margin:14px 0 -2px;font-size:12.5px;font-weight:600;color:var(--ink-2)}
.acts .grp:first-child{margin-top:0}
.acts .grp::after{content:"";flex:1;height:1px;background:var(--line)}
.grp .gn{min-width:20px;padding:1px 7px;border-radius:999px;background:var(--tag);color:var(--tag-ink);
 font-size:11px;font-weight:600;line-height:16px;text-align:center}
.grp.lv-overdue{color:var(--crit)}.grp.lv-today{color:var(--warn)}
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
.ctx{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 16px;margin:12px 0 0;padding:12px 0 0;
 border-top:1px dashed var(--line);font-size:12.5px;line-height:1.6}
.ctx>div{display:contents}
.ctx dt{color:var(--muted);font-weight:500;white-space:nowrap}
.ctx dd{margin:0;min-width:0;color:var(--ink-2)}
.act:not([data-state="open"]) .ctx{display:none}
.stt{display:inline-block;margin-right:7px;padding:0 6px;border-radius:4px;background:var(--tag);color:var(--tag-ink);
 font-size:11px;font-weight:600;line-height:18px;vertical-align:1px}
.stt.s-hold{background:var(--warn-bg);color:var(--warn)}.stt.s-done{background:var(--acc-bg);color:var(--acc)}
.oth{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}.oth+.oth{margin-top:4px}
.oth .nm{color:var(--ink);font-weight:500}.oth.more{font-size:12px;color:var(--muted)}
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
.why{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
.why-l{margin-right:4px;font-size:12px;font-weight:600;color:var(--ink-2)}
.why .btn.skip{color:var(--muted)}
.toast{position:fixed;left:50%;bottom:24px;z-index:10;display:flex;align-items:center;gap:12px;max-width:calc(100% - 32px);
 transform:translateX(-50%);padding:10px 12px 10px 16px;border-radius:12px;background:var(--ink);color:var(--card);
 font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.2)}
.toast button{appearance:none;border:0;border-radius:8px;padding:5px 10px;background:rgba(127,127,127,.25);
 font:inherit;font-size:12.5px;font-weight:600;color:inherit;cursor:pointer}

.progs{display:flex;flex-direction:column;gap:1px;background:var(--line);border:1px solid var(--line-2);border-radius:12px;overflow:hidden}
.prow{display:flex;align-items:center;background:var(--surface)}
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
[data-pstate="open"] .kb-menu [data-pmark="open"],
[data-pstate]:not([data-pstate="open"]) .kb-menu [data-pmark="done"],
[data-pstate]:not([data-pstate="open"]) .kb-menu [data-pmark="deleted"]{display:none}
.pn{display:block;min-width:0}
.pname{display:block;font-size:13.5px;font-weight:500;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pc{display:block;margin-top:4px;font-size:11px;color:var(--muted)}
.track{display:block;height:7px;border-radius:999px;background:var(--track);overflow:hidden}
.fill{display:block;width:0;height:100%;border-radius:inherit;background:var(--acc);transition:width .2s}
.pv{text-align:right;font-size:12px;font-weight:500;color:var(--ink-2)}

.waits{display:flex;flex-direction:column;gap:14px}
.wait{padding:18px 20px;background:var(--surface);border:1px solid var(--line-2);border-left:3px solid var(--warn-soft);border-radius:12px}
.wait.old{border-left-color:var(--warn)}
.wait p{margin:0}
.wh{display:flex;align-items:center;justify-content:space-between;gap:10px}
.wf{flex:1 1 auto;min-width:0;font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wd{flex:none;font-size:11px;color:var(--muted)}.wait.old .wd{color:var(--warn);font-weight:500}
.wh .kb{margin-right:-8px}
.wfoot{display:none;align-items:center;justify-content:space-between;gap:8px;margin-top:12px}
.wait:not([data-wstate="open"]) .wfoot{display:flex}
.wait:not([data-wstate="open"]) .kb{display:none}
.wait:not([data-wstate="open"]){border-left-color:var(--line);background:var(--side)}
.wait:not([data-wstate="open"]) .ws{color:var(--muted)}
.wfoot .undo{display:inline-block}
.handled summary{padding:14px 0 0;border-top:0;font-size:12.5px;color:var(--muted)}
.handled .waits{margin-top:12px}
.wait .ws{margin-top:9px;font-size:13px;line-height:1.5;color:var(--ink-2);
 display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.wait .wsub{margin-top:6px;font-size:11.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.more{display:block;margin:12px auto 0;padding:4px 10px;border:0;border-radius:6px;background:none;
 font:inherit;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer}
.more:hover{color:var(--ink)}

.rds{display:flex;flex-direction:column;gap:16px}
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
.projs{display:grid;gap:10px}
.proj{background:var(--surface);border:1px solid var(--line-2);border-radius:12px;overflow:hidden}
details.proj>summary.proj-h{display:flex;align-items:center;gap:10px;padding:14px 14px 14px 16px;border-top:0;
 list-style:none;font-size:14px;color:var(--ink);cursor:pointer}
.proj-h::-webkit-details-marker{display:none}
.proj-h::before{content:"";flex:none;width:7px;height:7px;margin:0 3px 0 1px;border-right:1.5px solid var(--muted);
 border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
.proj[open]>.proj-h::before{transform:rotate(45deg)}
.proj[open]>.proj-h{border-bottom:1px solid var(--line)}
.proj-h:hover{background:var(--side)}
.proj-h h3{margin:0;font-size:15.5px;font-weight:600}.pm{margin-left:auto;color:var(--muted);font-size:12px}
.proj-h .st{margin-left:4px}
.bar{display:block;width:4px;height:20px;border-radius:2px}
.s0{background:var(--s0)}.s1{background:var(--s1)}.s2{background:var(--s2)}.s3{background:var(--s3)}
.s4{background:var(--s4)}.s5{background:var(--s5)}.s6{background:var(--s6)}.s7{background:var(--s7)}
.s8{background:var(--s8)}
.sw{display:inline-block;width:8px;height:8px;border-radius:2px}
/* 프로젝트 안쪽은 한 단계 가라앉힌 바탕, 업무 묶음은 그 위에 뜬 카드 한 장씩 */
.proj-b{display:grid;gap:10px;padding:12px;background:var(--side)}
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
.tasks .task .th{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 6px}
.tasks .task .meta{margin-left:0}
.task .ctl{margin-left:auto}
.task .btn{padding:3px 9px;font-size:11.5px}
.task .st:empty{display:none}
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
.who{display:inline-flex;gap:4px;align-items:baseline;color:var(--ink-2)}
.who .none{color:var(--muted)}
.org{font-size:11px;padding:0 5px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.decs .ds{margin-left:6px;color:var(--muted);font-size:12px}
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

@media (max-width:900px){
 .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
 .today{grid-template-columns:minmax(0,1fr)}
 .sch-body{grid-template-columns:minmax(0,1fr)}
 .col-main{border-right:0;border-bottom:1px solid var(--line)}
 .hero-side{align-items:flex-start}
}
@media (max-width:600px){
 .wrap{padding:16px 16px 48px}
 .hero{padding:24px 20px 18px}
 h1{font-size:23px}
 .card>.banner{margin:0 20px 16px}
 .kpi{padding:16px 18px 18px}.kpi .v b{font-size:28px}
 .tabs{padding:0 12px}
 .col-main,.col-side,.pad{padding:24px 18px 30px}
 .act{padding:16px}
 .ctx{grid-template-columns:minmax(0,1fr);gap:0}
 .ctx dd{margin-bottom:8px}.ctx>div:last-child dd{margin-bottom:0}
 .proj-b{gap:8px;padding:8px}
 .work-h .wm{margin-left:0;white-space:normal}
 #tab-today .sec-h:first-child{flex-wrap:wrap}
 .pgo{grid-template-columns:minmax(0,1fr) auto;gap:8px 12px;padding:14px 4px 14px 16px}
 .pgo .track{grid-column:1/-1;grid-row:2}
 .prow>.kb{padding-right:10px}
 details.proj>summary.proj-h{flex-wrap:wrap;row-gap:4px}
 .proj-h h3{flex:1 1 0;min-width:0}
 .pm{order:5;margin-left:22px;width:100%}
 .ss{flex:1 1 40%;justify-content:space-between}
 .cd{min-height:52px;padding:3px 2px 5px;align-items:center}
 .cd .cc,.cd .cd-more,.cd-h{display:none}
 .cd-dots{display:flex;gap:2px}
 .cd-dots i{width:5px;height:5px;border-radius:50%;background:var(--box)}
 .cd-dots i.lv-overdue{background:var(--crit)}.cd-dots i.lv-today{background:var(--warn)}
 .cd-dots i.lv-soon{background:var(--caution)}
 .sch-side{padding:14px 12px 16px}
}
""")

JS = """
(function(){
  var conf={};
  try{conf=JSON.parse(document.getElementById('nw-conf').textContent)}catch(e){}
  var SERVER=conf.port?'http://127.0.0.1:'+conf.port+'/':'';
  var served=!!conf.sameOrigin||(!!conf.port&&/^https?:$/.test(location.protocol)&&location.port===String(conf.port));
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
  // 남은 할 일 수 · 프로젝트별 진행 막대를 표시 상태로 다시 센다.
  // 서버와 같은 셈: 전체 = 남은 일 + 완료 (제외 · 업무삭제한 일은 전체에서 뺀다), 진행 = 완료 / 전체
  function tally(el){
    var c={open:0,total:0};
    el.querySelectorAll('.task').forEach(function(li){
      var s=li.dataset.state;if(s==='open')c.open++;if(s==='open'||s==='done')c.total++;});
    return c;
  }
  function progress(){
    var by={};
    document.querySelectorAll('.proj[data-project]').forEach(function(art){
      var c=by[art.dataset.project]=tally(art);
      var b=art.querySelector('[data-open]');if(b)b.textContent=c.open;
      var t=art.querySelector('[data-ptotal]');if(t)t.textContent=c.total;
    });
    document.querySelectorAll('.work [data-wopen]').forEach(function(b){
      var w=b.closest('.work'),c=tally(w),t=w.querySelector('[data-wtotal]');
      b.textContent=c.open;if(t)t.textContent=c.total;
    });
    document.querySelectorAll('.prow').forEach(function(r){
      var c=by[r.dataset.goto];if(!c)return;
      var n=c.total-c.open;
      r.querySelector('.pv').textContent=c.total?n+'/'+c.total:'할 일 없음';
      r.querySelector('.fill').style.width=(c.total?Math.round(n*100/c.total):0)+'%';
    });
  }

  // ── 내 할 일: 완료 · 제외 · 업무삭제 (로컬 서버의 feedback.db 에 기록) ──────
  var lists={},view='open';
  document.querySelectorAll('[data-list]').forEach(function(ul){lists[ul.dataset.list]=ul;});
  function listKey(state){return state==='open'?'open':state==='done'?'done':'dropped';}
  function cards(ul){return Array.prototype.filter.call(ul.children,function(k){return k.classList.contains('act');});}
  // 남은 일은 마감 묶음(기한 지남 · 오늘 마감 · …)마다 머리줄. 카드가 오가면 서버가 그린 것과 같은 모양으로 다시 단다
  function regroup(){
    var ul=lists.open;if(!ul)return;
    Array.prototype.slice.call(ul.querySelectorAll('.grp')).forEach(function(h){h.remove();});
    var list=cards(ul),n={};
    list.forEach(function(li){n[li.dataset.level]=(n[li.dataset.level]||0)+1;});
    list.forEach(function(li,i){
      var lv=li.dataset.level;if(i&&list[i-1].dataset.level===lv)return;
      var h=document.createElement('li'),l=document.createElement('span'),c=document.createElement('b');
      h.className='grp lv-'+lv;h.dataset.grp=lv;l.className='gl';l.textContent=(conf.groups||{})[lv]||'';
      c.className='gn num';c.textContent=n[lv];h.appendChild(l);h.appendChild(c);ul.insertBefore(h,li);
    });
  }
  function statusText(li){
    var s=li.dataset.state;
    if(s==='open')return '';
    if(s==='done')return mmdd(li.dataset.at)+' 완료';
    return [conf.labels[s],li.dataset.reason,mmdd(li.dataset.at)].filter(Boolean).join(' · ');
  }
  // 남은 일은 원래 순서(마감일순)로, 완료 · 제외는 최근 표시가 위로.
  // 프로젝트 할 일은 오늘 탭에 완료 · 제외·삭제로만 둔다: 되돌리면 카드를 빼 두었다가 다시 표시할 때 쓴다 (ensureCard)
  var parked={};
  function place(li){
    if(li.dataset.kind==='task'&&li.dataset.state==='open'){
      if(li.parentElement){parked[li.dataset.task]=li;li.remove();}
      return;
    }
    if(li.dataset.kind==='task')delete parked[li.dataset.task];
    var ul=lists[listKey(li.dataset.state)];if(!ul)return;
    var open=li.dataset.state==='open',before=null;
    for(var i=0;i<ul.children.length;i++){
      var k=ul.children[i];if(k===li||!k.classList.contains('act'))continue;
      if(open?(+(k.dataset.order||1e9)>+(li.dataset.order||1e9)):((k.dataset.at||'')<(li.dataset.at||''))){before=k;break;}
    }
    ul.insertBefore(li,before);
    li.querySelector('.st').textContent=statusText(li);
  }
  // 프로젝트 · 일정 탭에서 표시한 프로젝트 할 일도 오늘 탭 완료 / 제외·삭제 목록에 바로 올린다.
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
  function refresh(){
    regroup();
    var open=0,today=0,over=0,doneToday=0;
    Object.keys(lists).forEach(function(key){
      var n=cards(lists[key]).length;put('cnt-'+key,n);
      var empty=document.querySelector('[data-empty="'+key+'"]');
      if(empty)empty.hidden=!(key===view&&n===0);
    });
    if(lists.open)cards(lists.open).forEach(function(li){
      open++;if(li.dataset.level==='today')today++;if(li.dataset.level==='overdue')over++;});
    // 오늘 완료: 내 할 일 + 프로젝트 할 일. 프로젝트 할 일은 오늘 탭 카드와 프로젝트 탭 줄 두 곳에 있어 할 일마다 한 번만 센다
    var doneIds={};
    document.querySelectorAll('.act[data-state="done"],.task[data-state="done"]').forEach(function(li){
      if(sameDay(li.dataset.at))doneIds[li.dataset.task]=1;});
    doneToday=Object.keys(doneIds).length;
    put('kpi-me',open);put('hl-me',open);
    put('kpi-today',today);put('kpi-over',over);put('kpi-done',doneToday);
    progress();drawSchedule();
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
    return {text:li.querySelector('.t').textContent,thread_key:li.dataset.thread,
      subject:li.dataset.subject,project:li.dataset.pname,
      deadline:li.dataset.deadline,urgency:li.dataset.urgency,
      kind:li.dataset.kind||'me',role:li.dataset.role||'',assignee:li.dataset.assignee||''};
  }
  // 프로젝트 줄은 자리를 옮기지 않고 그 자리에서 완료 표시만 한다 (묶음 안에서 아래로)
  function placeTask(li){
    var ul=li.parentElement;
    if(ul&&li.dataset.state!=='open'&&li.nextElementSibling===null)return;
    if(ul&&li.dataset.state!=='open')ul.appendChild(li);
    li.classList.toggle('done',li.dataset.state==='done');
    li.querySelector('.st').textContent=statusText(li);
  }
  function relocate(li){
    if(li.classList.contains('task'))placeTask(li);else if(li.classList.contains('sit'))placeSit(li);else place(li);
  }
  function apply(li,state){
    li.dataset.state=state?state.label:'open';
    li.dataset.reason=state&&state.reason||'';
    li.dataset.at=state?state.labeled_at:'';
    relocate(li);
    // 같은 할 일이 여러 탭(오늘 · 프로젝트 · 일정)에 있으면 함께 맞춘다
    document.querySelectorAll('[data-task="'+li.dataset.task+'"]').forEach(function(o){
      if(o===li||o.dataset.state===li.dataset.state)return;
      o.dataset.state=li.dataset.state;o.dataset.reason=li.dataset.reason;o.dataset.at=li.dataset.at;
      relocate(o);
    });
    if(li.dataset.kind==='task')ensureCard(li);      // 오늘 탭에 아직 카드가 없으면 완료 · 제외 목록에 올린다
  }
  function mark(li,label,reason){
    closeWhy();
    var prev=li.dataset.state==='open'?null:
      {label:li.dataset.state,reason:li.dataset.reason,labeled_at:li.dataset.at};
    apply(li,label==='open'?null:{label:label,reason:reason||'',labeled_at:new Date().toISOString()});
    refresh();
    post([{task_id:li.dataset.task,label:label,reason:reason||'',snapshot:snapshot(li)}]).then(function(j){
      apply(li,j.states&&j.states[li.dataset.task]||null);refresh();
      toast(label==='open'?'남은 할 일로 되돌렸어요':conf.labels[label]+'로 표시했어요',function(){
        mark(li,prev?prev.label:'open',prev?prev.reason:'');});
    }).catch(function(err){
      apply(li,prev);refresh();toast('저장하지 못했어요 — '+err.message);
    });
  }
  var whyBox=null;
  function closeWhy(){if(whyBox){whyBox.remove();whyBox=null;}}
  // 제외 · 업무삭제는 사유를 한 번 더 고른다 (건너뛸 수 있음)
  function askWhy(li,label){
    closeWhy();
    var box=document.createElement('div');box.className='why';
    var title=document.createElement('span');title.className='why-l';
    title.textContent=conf.labels[label]+' 사유';box.appendChild(title);
    function add(text,cls,fn){
      var b=document.createElement('button');b.type='button';b.className='btn'+(cls?' '+cls:'');
      b.textContent=text;b.addEventListener('click',fn);box.appendChild(b);
    }
    (conf.reasons[label]||[]).forEach(function(r){add(r,'',function(){mark(li,label,r);});});
    add('사유 없이','skip',function(){mark(li,label,'');});
    add('취소','skip',closeWhy);
    li.appendChild(box);whyBox=box;box.querySelector('button').focus();
  }
  document.addEventListener('click',function(e){
    var b=e.target.closest('[data-act]');if(!b||b.disabled)return;
    var li=b.closest('.act,.task,.sit'),act=b.dataset.act;if(!li)return;
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
    if(!st||st.label==='deleted'||!latest||!st.ref_at)return st;
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
    li.querySelector('.wfoot .st').textContent=markText('thread',st&&st.label,st&&st.labeled_at);
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

  var pview='open';
  function projEls(name){
    return Array.prototype.filter.call(document.querySelectorAll('.proj[data-project],.prow[data-goto]'),
      function(el){return (el.dataset.project||el.dataset.goto)===name;});
  }
  function layoutProjects(){
    var counts={open:0,done:0,deleted:0},named=0,rows=0;
    document.querySelectorAll('.proj[data-project]').forEach(function(a){
      var s=a.dataset.pstate;counts[s]=(counts[s]||0)+1;a.hidden=s!==pview;
      if(s==='open'&&a.dataset.project!==conf.unclassified)named++;});
    document.querySelectorAll('.client').forEach(function(sec){
      var n=sec.querySelectorAll('.proj:not([hidden])').length;sec.hidden=!n;
      var c=sec.querySelector('[data-ccnt]');if(c)c.textContent=n;});
    document.querySelectorAll('.prow[data-goto]').forEach(function(r){
      r.hidden=r.dataset.pstate!=='open';if(!r.hidden)rows++;});
    Object.keys(counts).forEach(function(k){put('pcnt-'+k,counts[k]);});
    put('kpi-proj',named);
    var tc=document.querySelector('[data-tabcnt="tab-projects"]');if(tc)tc.textContent=counts.open;
    var pe=document.getElementById('pempty');if(pe)pe.hidden=!!counts[pview];
    var ge=document.getElementById('progs-empty');if(ge)ge.hidden=rows>0;
  }
  function setPView(v){
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
      el.dataset.pstate=st?st.label:'open';el.dataset.at=st?st.labeled_at:'';
      var t=el.querySelector('.proj-h>.st');if(t)t.textContent=markText('project',st&&st.label,st&&st.labeled_at);
    });
    layoutProjects();
  }
  function markProject(el,label){
    var name=el.dataset.project||el.dataset.goto;
    var prev=el.dataset.pstate==='open'?null:{label:el.dataset.pstate,labeled_at:el.dataset.at};
    applyProject(name,label==='open'?null:{label:label,labeled_at:new Date().toISOString()});
    send('/api/mark',{target:'project',key:name,label:label,ref_at:el.dataset.latest,title:name}).then(function(j){
      applyProject(name,j.state||null);
      toast(label==='open'?'「'+name+'」 진행 중으로 되돌렸어요':
        label==='done'?'「'+name+'」 완료 프로젝트로 옮겼어요':'「'+name+'」 삭제했어요 (메일은 그대로예요)',
        function(){markProject(el,prev?prev.label:'open');});
    }).catch(function(err){applyProject(name,prev);toast('저장하지 못했어요 — '+err.message);});
  }
  document.addEventListener('click',function(e){
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
  function sits(){return Array.prototype.slice.call(document.querySelectorAll('.sit'));}
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
      var due=isoOf(li.dataset.deadline),ok=li===pinned||onCal(li)&&!!due&&(p==='overdue'?
        li.dataset.state==='open'&&due<TODAY:p==='week'?due>TODAY&&due<=end:due===cal.day);
      li.hidden=!ok;if(ok)n++;
    });
    Array.prototype.forEach.call(none.children,function(li){
      var mine=li.dataset.kind!=='task'&&li.dataset.state==='open';li.hidden=!(mine||li===pinned);if(mine)k++;});
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
  function drawSchedule(){if(!sch||!TODAY)return;schCounts();drawOver();drawCal();drawPanel();}
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
    var tag=el.querySelector('.tg .due');if(tag){tag.className='due lv-'+info.level;tag.textContent=info.text;}
    var inp=el.querySelector('.due-in');if(inp&&document.activeElement!==inp)inp.value=info.day;
  }
  function applyDue(id,st,quiet){
    document.querySelectorAll('[data-task="'+id+'"]').forEach(function(el){
      el.dataset.manual=st?'1':'';el.dataset.deadline=st?st.due:(el.dataset.mdl||'');paintDue(el);});
    if(!quiet){reorderActs();sortSits();refresh();}
  }
  // 남은 내 할 일은 서버와 같은 순서(마감 → 긴급도 → 새 메일)로 다시 늘어놓는다
  function reorderActs(){
    var ul=lists.open;if(!ul)return;
    cards(ul).sort(function(a,b){
      return dueInfo(a.dataset.deadline).sort-dueInfo(b.dataset.deadline).sort||urg(a)-urg(b)
        ||(b.dataset.new==='1')-(a.dataset.new==='1')||(+a.dataset.order||0)-(+b.dataset.order||0);
    }).forEach(function(li,i){li.dataset.order=i;ul.appendChild(li);});
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
    document.querySelectorAll('.act [data-act],.task [data-act],.kb-btn,.wfoot [data-wmark],.sit [data-act],.due-in,[data-due-rev]').forEach(function(b){
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
      document.querySelectorAll('.act,.task,.sit').forEach(function(li){
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
      document.querySelectorAll('.act,.task,.sit').forEach(function(el){
        var st=dues[el.dataset.task]||null;
        if((st?'1':'')!==(el.dataset.manual||'')||(st&&st.due!==el.dataset.deadline)){applyDue(el.dataset.task,st,true);fixed++;}
      });
      if(fixed){reorderActs();sortSits();}
      // 답을 기다리는 메일 · 프로젝트 표시도 서버 기록으로 맞춘다
      var tm=j.threads||{},pm=j.projects||{};
      document.querySelectorAll('.wait[data-thread]').forEach(function(li){
        var st=active(tm[li.dataset.thread]||null,li.dataset.asked);
        if((st?st.label:'open')!==li.dataset.wstate||(st&&st.labeled_at!==li.dataset.at))applyWait(li,st);
      });
      document.querySelectorAll('.proj[data-project]').forEach(function(a){
        var st=active(pm[a.dataset.project]||null,a.dataset.latest);
        if((st?st.label:'open')!==a.dataset.pstate||(st&&st.labeled_at!==a.dataset.at))applyProject(a.dataset.project,st);
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
  function applyHide(){document.body.classList.toggle('hide-done',hide.checked);drawSchedule();
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

  var modes=['system','light','dark'],names={system:'시스템',light:'라이트',dark:'다크'};
  var btn=document.getElementById('theme'),mode='system';
  try{mode=localStorage.getItem('nwmail.theme')||'system'}catch(e){}
  function applyTheme(){
    if(mode==='system')document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme',mode);
    btn.textContent='테마: '+names[mode];
  }
  btn.addEventListener('click',function(){mode=modes[(modes.indexOf(mode)+1)%3];
    try{localStorage.setItem('nwmail.theme',mode)}catch(e){}applyTheme();});
  applyTheme();

  // 자동 실행이 10분마다 파일을 새로 만들므로, 보고 있지 않을 때 다시 읽는다.
  // 스크롤 위치 · 펼친 항목 · 내 할 일 보기 · 일정 탭에서 보던 달과 날짜는 유지한다 (sessionStorage).
  var SK='nwmail.view', idle=Date.now();
  function keyOf(d){
    var p=d.closest('[data-project]'),w=d.closest('[data-work]');
    return (p?p.dataset.project:'')+'|'+(w?w.dataset.work:'')+'|'+d.className;
  }
  try{var v=JSON.parse(sessionStorage.getItem(SK)||'null');
    if(v){sessionStorage.removeItem(SK);
      document.querySelectorAll('details').forEach(function(d){if(v.open.indexOf(keyOf(d))>=0)d.open=true;});
      if(v.acts)setView(v.acts);
      if(v.pview)setPView(v.pview);
      if(v.sch&&isoOf(v.sch.day)&&isoOf(v.sch.month+'-01')){
        cal.day=v.sch.day;cal.month=v.sch.month;cal.panel=v.sch.panel||'day';drawSchedule();}
      window.scrollTo(0,v.y);}}catch(e){}
  ['mousemove','keydown','scroll','click','touchstart'].forEach(function(ev){
    window.addEventListener(ev,function(){idle=Date.now();},{passive:true});});
  setInterval(function(){
    if(whyBox||openMenu||(!document.hidden&&Date.now()-idle<120000))return;   // 조작 중이면 미룬다
    if(pinned)return;                    // 기한을 고치는 중이면 미룬다
    try{sessionStorage.setItem(SK,JSON.stringify({y:window.scrollY,acts:view,pview:pview,
      sch:{month:cal.month,day:cal.day,panel:cal.panel},
      open:Array.prototype.map.call(document.querySelectorAll('details[open]'),keyOf)}));}catch(e){}
    location.reload();
  },600000);
})();
"""
