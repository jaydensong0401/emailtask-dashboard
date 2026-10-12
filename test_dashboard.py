"""4단계 검증: 대시보드 모델 · 렌더링 · 보안(이스케이프) · 상태 유지.

합성 데이터로 끝까지 검증한다. 실제 메일함·LLM 불필요.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nwmail.client import Mail
from nwmail.dashboard import (
    USAGE_PAGE, build_model, deadline_info, render_html, render_usage_html, task_id,
)
from nwmail.feedback import FeedbackStore
from nwmail.filters import add_filter, reapply
from nwmail.store import Store

ME = "gdhong@lumi.example"
TODAY = date(2026, 9, 11)
NOW = datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)
ok = total = 0


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def mail(uid, subject, sender, mid, to=(), refs=(), body="본문",
         when="2026-09-10T09:00:00+09:00", folder="", name="", cc=()) -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name=name, from_email=sender,
                received_time=when, status="Read", body=body, to=list(to), cc=list(cc),
                message_id=mid, references=list(refs), folder=folder)


def put_extraction(st: Store, key: str, payload: dict) -> None:
    st.conn.execute(
        "INSERT INTO extractions (thread_key, project, payload, source_hash, backend,"
        " extracted_at) VALUES (?,?,?,?,?,?)",
        (key, payload.get("project", ""), json.dumps(payload, ensure_ascii=False),
         "test", "test", "2026-09-11"))
    st.conn.commit()


def section(html: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    i = html.find(start_marker)
    if i < 0:
        return ""
    ends = [j for m in end_markers if (j := html.find(m, i + len(start_marker))) > 0]
    return html[i:min(ends)] if ends else html[i:]


def main() -> int:
    hdr("[1] 기한 표시")
    cases = [("2026-09-09", "2일 지남", "overdue"), ("2026-09-11", "오늘", "today"),
             ("2026-09-13", "D-2", "soon"), ("2026-09-30", "D-19", "later"),
             ("", "", "none"), ("다음 주", "다음 주", "later")]
    for raw, label, level in cases:
        d = deadline_info(raw, TODAY)
        check(f"{raw or '(빈값)'!r} -> {label or '(표시 없음)'}",
              d["label"] == label and d["level"] == level, f"{d['label']}/{d['level']}")

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "d.db") as st:
            st.save_mails([
                # 루미: 추출 완료, 직접 수신, 신규, XSS 시도 포함
                mail(1, "[루미] <script>alert(1)</script> 기획 검토", "pm@lumi.example",
                     "<a1@t>", to=[ME], body="<img src=x onerror=alert(2)> 9월 13일까지",
                     when="2026-09-10T10:00:00+09:00", name="김개발 매니저"),
                # 누리: 두 표기(NURI / 누리마케팅) → 한 카드로 합쳐져야 함
                mail(2, "[NURI] 시그니처 연장", "a@lumi.example", "<t1@t>",
                     when="2026-09-08T09:00:00+09:00"),
                mail(3, "[누리마케팅] 신청페이지 수정", "b@lumi.example", "<t2@t>", to=[ME],
                     when="2026-09-08T10:00:00+09:00"),
                # 누리: 고객명 꼬리만 다른 별개 대화 → 2번과 같은 업무 묶음
                mail(10, "RE: [NURI] 시그니처 연장의 건_홍○순 고객", "a@lumi.example",
                     "<t3@t>", when="2026-09-08T11:00:00+09:00"),
                # 오로라 갤러리(키워드): 추출 없음 → 대기 표시, 직접 수신 + 회신 완료
                mail(4, "[문의] 갤러리 티켓", "x@aurora.example", "<g1@t>", to=[ME],
                     when="2026-09-07T09:00:00+09:00"),
                # 키워드 · 태그 없이 오로라 도메인 → 오로라 운영(기타)
                mail(12, "신규 매장 오픈 고객 초청 모수 확인", "y@aurora.example", "<g2@t>",
                     when="2026-09-07T10:00:00+09:00"),
                # 세부 프로젝트 키워드 → 업무명에서 프로젝트 이름을 뗌
                mail(13, "[누리마케팅] AX9  멤버십 테니스 클럽 레슨_알림톡 문구 변경 요청의 건",
                     "jh@nurimktg.co.example", "<gv@t>", when="2026-09-08T13:00:00+09:00"),
                mail(14, "RE: 오로라 한결카드 (오로라 PLCC 3.0) 카카오톡 안내 알림톡 발송 요청 건 (9/8)",
                     "su@aurora.example", "<pl@t>", when="2026-09-08T14:00:00+09:00"),
                # 태그 없음 → 미분류
                mail(5, "주간 회의록", "c@lumi.example", "<u1@t>",
                     when="2026-09-06T09:00:00+09:00"),
                # 광고 → 차단
                mail(6, "[광고] 세미나", "noreply@ad.com", "<ad@t>"),
                # KX볼트링크: 지난달 메일 + 이번 주 답장 → 과거 이력이 붙은 대화
                mail(7, "[KX볼트링크] API 확인", "k@voltlink.example", "<sk0@t>",
                     when="2026-08-01T09:00:00+09:00"),
                mail(8, "RE: [KX볼트링크] API 확인", "k@voltlink.example", "<sk1@t>",
                     refs=["<sk0@t>"], when="2026-09-09T09:00:00+09:00"),
                # 지난달에만 오간 대화 → 대시보드에 나오면 안 됨
                mail(9, "[루미] 지난달 정산", "old@lumi.example", "<old@t>", to=[ME],
                     when="2026-08-01T09:00:00+09:00"),
                # 참조(CC)로만 받은 요청 → 직접 수신이지만 '답을 기다리는 메일'에는 없어야 함
                mail(11, "[루미] 참조로 받은 검토 요청", "pm@lumi.example", "<cc1@t>",
                     to=["other@lumi.example"], cc=[ME], when="2026-09-08T12:00:00+09:00"),
            ], me=ME)
            add_filter(st, "subject", "[광고]", "광고")
            reapply(st)
            # 내가 보낸 답장 (보낸메일함) → 오로라 대화는 회신 완료
            st.save_mails([mail(900, "RE: [문의] 갤러리 티켓", ME, "<mine@t>",
                                refs=["<g1@t>"], when="2026-09-07T10:00:00+09:00")],
                          me=ME, folder="Sent")
            st.conn.execute("INSERT INTO runs (started_at, new_mails) VALUES (?,0)",
                            ("2026-09-09T00:00:00+00:00",))
            st.conn.execute("INSERT INTO runs (started_at, new_mails) VALUES (?,0)",
                            ("2026-09-11T00:00:00+00:00",))
            st.conn.commit()

            put_extraction(st, "<a1@t>", {
                "project": "루미", "summary": "기획안 검토 요청", "status": "진행중",
                "decisions": [
                    {"text": "예산 300만원 승인", "by": "김개발", "date": "2026-09-08"},
                    {"text": "시안 B안 채택", "by": "박외부", "date": "2026-09-09"},
                    {"text": "런칭일 10월 1일 확정", "by": "김개발 매니저", "date": "2026-09-10"},
                    {"text": "옛 결정 (날짜 없이 저장된 추출)", "by": ""},
                ],
                "tasks": [
                    {"text": "API 연동 개발", "role": "개발", "assignee": "김개발",
                     "deadline": "2026-09-13"},
                    {"text": "배너 시안 제작", "role": "디자인", "assignee": "",
                     "deadline": "2026-09-30"},
                    {"text": "일정표 정리", "role": "기획", "assignee": "", "deadline": ""},
                    {"text": "문구 수정 반영", "role": "수정", "fix_type": "디자인",
                     "assignee": "박외부", "assignee_org": "누리마케팅", "deadline": ""},
                    {"text": "잔여 회차 삭제", "role": "수정", "fix_type": "퍼블",
                     "assignee": "김개발", "deadline": ""},
                ],
                "my_actions": [
                    {"text": "기획안 회신", "deadline": "2026-09-11", "urgency": "high"},
                    {"text": "예산 승인", "deadline": "2026-09-09", "urgency": "medium"},
                    {"text": "참고 자료 확인", "deadline": "", "urgency": "low"},
                ],
            })
            put_extraction(st, "<u1@t>", {"project": "", "summary": "회의록 공유",
                                          "status": "완료", "decisions": [], "tasks": [],
                                          "my_actions": []})
            put_extraction(st, "<t1@t>", {"project": "NURI", "summary": "연장 요청",
                                          "status": "진행중", "decisions": [], "tasks": [
                {"text": "신청 기한 연장 처리", "role": "개발", "deadline": ""}],
                "my_actions": []})

            model = build_model(st, me=ME, today=TODAY, now=NOW)
            out = render_html(model)
            out2 = render_html(build_model(st, me=ME, today=TODAY, now=NOW))

            # 내 할 일 표시 기록: 완료 1 · 제외 1 + 지금 목록에 없는 완료 1 + 30일 넘은 삭제 1
            fb = FeedbackStore(Path(tmp) / "fb.db")
            ids = {t: task_id("<a1@t>", "me", t) for t in ("기획안 회신", "예산 승인", "참고 자료 확인")}
            tids = {t: task_id("<a1@t>", "task", t)
                    for t in ("API 연동 개발", "배너 시안 제작", "일정표 정리")}
            fb.apply(ids["기획안 회신"], "done", now=NOW)
            fb.apply(tids["API 연동 개발"], "done",
                     snapshot={"kind": "task", "role": "개발", "assignee": "김개발",
                               "text": "API 연동 개발", "project": "루미",
                               "subject": "[루미] 기획 검토"}, now=NOW)
            fb.apply(tids["일정표 정리"], "deleted", "할 일 아님",
                     snapshot={"kind": "task", "role": "기타", "text": "일정표 정리",
                               "project": "루미"}, now=NOW)
            fb.apply(ids["참고 자료 확인"], "excluded", "남의 일", now=NOW)
            fb.apply("aaaaaaaaaaaa", "done", snapshot={"text": "지난주 끝낸 일", "project": "누리",
                                                       "subject": "[NURI] 지난 건"},
                     now=NOW - timedelta(days=2))
            fb.apply("bbbbbbbbbbbb", "deleted", "중복", snapshot={"text": "오래전 삭제한 일"},
                     now=NOW - timedelta(days=40))
            model_fb = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
            out_fb = render_html(model_fb, server={"port": 8787, "token": fb.token()})
            fb.apply(ids["참고 자료 확인"], "open", now=NOW)          # 되돌리기
            undone = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
            undo_log = [r["action"] for r in fb.conn.execute(
                "SELECT action FROM feedback_log WHERE task_id = ? ORDER BY id",
                (ids["참고 자료 확인"],))]

            # 답을 기다리는 메일 · 프로젝트 표시
            asked = {u["thread"]["key"]: u["asked"] for u in model["unanswered"]}
            received = {p["name"]: p["received_at"] for p in model["projects"]}
            before = "2026-09-01T00:00:00+00:00"            # 표시한 뒤 새 메일이 온 경우
            fb.mark("thread", "<t2@t>", "answered", asked["<t2@t>"], "[누리마케팅] 신청페이지 수정",
                    now=NOW)
            fb.mark("thread", "<a1@t>", "answered", before, now=NOW)
            fb.mark("project", "KX볼트링크", "done", received["KX볼트링크"], now=NOW)
            fb.mark("project", "오로라 갤러리", "done", before, now=NOW)
            fb.mark("project", "미분류", "deleted", before, now=NOW)
            model_mk = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
            out_mk = render_html(model_mk, server={"port": 8787, "token": fb.token()})
            fb.mark("thread", "<t2@t>", "deleted", asked["<t2@t>"], now=NOW)
            fb.mark("thread", "<t2@t>", "open", now=NOW)
            reopened = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
            mark_log = [r["action"] for r in fb.conn.execute(
                "SELECT action FROM mark_log WHERE key = ? ORDER BY id", ("<t2@t>",))]
            fb.close()

            # 일정 탭: 직접 정한 기한 (기한 없던 일 · 메일 기한이 있던 일 · 프로젝트 할 일)
            fb2 = FeedbackStore(Path(tmp) / "fb2.db")
            fb2.set_due(ids["참고 자료 확인"], "2026-09-14", {"text": "참고 자료 확인", "kind": "me"}, now=NOW)
            fb2.set_due(ids["기획안 회신"], "2026-09-20",
                        {"text": "기획안 회신", "deadline": "2026-09-11", "kind": "me"}, now=NOW)
            fb2.set_due(tids["배너 시안 제작"], "2026-09-12", {"kind": "task"}, now=NOW)
            model_due = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb2)
            out_due = render_html(model_due, server={"port": 8787, "token": fb2.token()})
            due_rows = fb2.dues()
            fb2.set_due(ids["기획안 회신"], "", now=NOW)                  # 메일 기한으로 되돌리기
            reverted = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb2)
            due_log = [r["due"] for r in fb2.conn.execute(
                "SELECT due FROM due_log WHERE task_id = ? ORDER BY id", (ids["기획안 회신"],))]
            # 프로젝트 색: 팔레트 칸 · 직접 고른 색 · 자동 색으로 되돌린 것
            fb3 = FeedbackStore(Path(tmp) / "fb3.db")
            fb3.set_color("오로라 스토어", "s9", now=NOW)
            fb3.set_color("KX볼트링크", "#123456", now=NOW)
            fb3.set_color("오로라 갤러리", "s2", now=NOW)
            fb3.set_color("오로라 갤러리", "", now=NOW)
            model_col = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb3)
            out_col = render_html(model_col, server={"port": 8787, "token": fb3.token()})
            col_log = [r["color"] for r in fb3.conn.execute(
                "SELECT color FROM color_log WHERE project = ? ORDER BY id", ("오로라 갤러리",))]
            fb3.close()
            # 지난달에만 오간 대화(최근 7일 창 밖)에 기한 있는 할 일이 남아 있는 경우
            put_extraction(st, "<old@t>", {
                "project": "루미", "summary": "지난달 정산 건", "status": "진행중", "decisions": [],
                "tasks": [{"text": "정산표 검수", "role": "기타", "assignee": "김개발",
                           "deadline": "2026-09-25"},
                          {"text": "기한 없는 옛 업무", "role": "기타", "deadline": ""}],
                "my_actions": [{"text": "정산 자료 회신", "deadline": "2026-09-18", "urgency": "high"},
                               {"text": "한참 지난 마감", "deadline": "2026-08-01", "urgency": "medium"},
                               {"text": "기한 없는 옛 일", "deadline": "", "urgency": "low"}],
            })
            model_old = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb2)
            out_old = render_html(model_old)
            fb2.apply(task_id("<old@t>", "me", "정산 자료 회신"), "done",
                      snapshot={"text": "정산 자료 회신", "kind": "me", "deadline": "2026-09-18"}, now=NOW)
            model_old_done = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb2)
            # 프로젝트 완료 표시는 창 안 대화의 새 메일로 풀리고, 삭제는 유지된다
            fb2.mark("project", "오로라 스토어", "done", "2026-09-01T00:00:00+00:00", now=NOW)
            sched_reopened = {r["text"] for r in build_model(
                st, me=ME, today=TODAY, now=NOW, feedback=fb2)["schedule"]}
            fb2.mark("project", "오로라 스토어", "deleted", now=NOW)
            sched_deleted = {r["text"] for r in build_model(
                st, me=ME, today=TODAY, now=NOW, feedback=fb2)["schedule"]}
            fb2.close()

        hdr("[2] 보안 — 메일 내용은 전부 이스케이프")
        check("제목의 <script> 가 실행 가능한 형태로 남지 않음", "<script>alert(1)" not in out)
        check("이스케이프된 형태로는 보임", "&lt;script&gt;alert(1)" in out)
        check("본문의 <img onerror> 가 태그로 남지 않음", "<img src=x" not in out)
        check("외부 리소스 로드 없음 (자체 완결)",
              not re.search(r'<(script|link|img)[^>]+(src|href)=["\']?https?:', out))

        hdr("[3] 프로젝트 카드 — 고객사 > 세부 프로젝트")
        names = re.findall(r'data-project="([^"]+)"', out)
        check("회사 태그(루미 · 누리)는 프로젝트 카드가 되지 않음",
              "루미" not in names and "누리" not in names, f"카드: {names}")
        check("[루미] 는 오로라 스토어, [NURI] · [누리마케팅] 은 오로라 운영(기타)",
              "오로라 스토어" in names and names.count("오로라 운영(기타)") == 1)
        check("태그 없는 스레드는 미분류", "미분류" in names)
        check("미분류 카드는 맨 뒤", names[-1] == "미분류", str(names))
        clients = re.findall(r'<section class="client" data-client="([^"]*)"', out)
        check("고객사는 설정 순서, 설정에 없는 건 '기타' 묶음으로 맨 뒤",
              clients == ["오로라", "KX볼트링크", ""], str(clients))
        aurora = section(out, '<section class="client" data-client="오로라"',
                          ('<section class="client"',))
        g_names = re.findall(r'data-project="([^"]+)"', aurora)
        check("세부 프로젝트가 고객사 아래로 (AX9 · PLCC · 갤러리 · 스토어)",
              {"AX9 멤버십 테니스클럽", "오로라 한결카드 PLCC", "오로라 갤러리",
               "오로라 스토어", "오로라 운영(기타)"} == set(g_names), str(g_names))
        check("기본 프로젝트(기타)는 고객사 안에서 맨 뒤", g_names[-1] == "오로라 운영(기타)")
        check("'기타' 묶음 머리말", '<h2>기타</h2>' in out and '<h2>오로라</h2>' in aurora)
        misc_ops = section(out, 'data-project="오로라 운영(기타)"', ('<details class="proj"', '<section class="client"'))
        check("키워드 · 태그 없는 오로라 도메인 메일은 오로라 운영(기타)", "신규 매장 오픈" in misc_ops)
        gv = section(out, 'data-project="AX9 멤버십 테니스클럽"', ('<details class="proj"', '<section class="client"'))
        plcc = section(out, 'data-project="오로라 한결카드 PLCC"', ('<details class="proj"', '<section class="client"'))
        check("업무명에서 프로젝트 이름을 뗌 (AX9 … 레슨_알림톡 문구 변경 요청)",
              'data-work="알림톡 문구 변경 요청"' in gv, gv[:300])
        check("업무명에서 앞머리 괄호 속 프로젝트 이름도 뗌 (PLCC)",
              'data-work="카카오톡 안내 알림톡 발송 요청"' in plcc)
        check("프로젝트는 접힌 상태로 시작 (아코디언)",
              '<details class="proj"' in out and not re.search(r'<details class="proj"[^>]* open', out))
        check("머리말에 최근 메일 날짜", '최근 <span class="num">09/08</span>' in gv)
        check("머리말에 ⋮ 메뉴 (프로젝트 완료 · 삭제 · 되돌리기)",
              'class="kb-btn"' in gv and 'data-pmark="done">직접 완료 처리' in gv
              and 'data-pmark="deleted">삭제' in gv and 'data-pmark="open">자동 분류로 되돌리기' in gv)
        check("진행 중 · 완료 · 지연 · 삭제 보기 (추출 완료된 빈 프로젝트는 완료)",
              'data-pview="open"' in out and 'aria-pressed="true">진행 중' in out
              and 'id="pcnt-done">1' in out and 'id="pcnt-delayed">1' in out and 'id="pcnt-deleted">0' in out)

        lumi = section(out, 'data-project="오로라 스토어"', ('<details class="proj"', '<section class="client"'))
        items = [(re.search(r'data-role="([^"]*)"', a).group(1), a + body)
                 for a, body in re.findall(r'<li class="task[^"]*"([^>]*)>(.*?)</li>', lumi, re.S)]
        li = {text: body for _, body in items
              for text in ("API 연동 개발", "배너 시안 제작", "문구 수정 반영", "잔여 회차 삭제",
                           "일정표 정리") if text in body}
        dec = section(lumi, 'data-col="결정사항"', ('</details>',))
        order = ["개발", "디자인", "수정", "기타"]
        roles_seen = [r for r, _ in items]
        check("역할별 열 없이 한 목록 (빈 칸 없음)",
              'class="cols"' not in out and 'data-col="개발"' not in out
              and 'class="tasks"' in lumi)
        check("할 일은 개발 → 디자인 → 수정 → 기타 순",
              roles_seen == sorted(roles_seen, key=order.index) and len(roles_seen) == 5,
              str(roles_seen))
        check("문구 앞 칸에 역할 태그 (개발) — 태그는 문구 밖, 문구 · 담당자 줄은 오른쪽 칸",
              '<b class="rtag rt1">개발</b><div class="tb"><div class="th"><p class="t">API 연동 개발' in lumi)
        check("문구 앞 칸에 역할 태그 (디자인)", '<b class="rtag rt2">디자인</b><div class="tb"><div class="th"><p class="t">배너 시안 제작' in lumi)
        check("수정은 세부 종류까지 태그", '<b class="rtag rt3">수정·디자인</b><div class="tb"><div class="th"><p class="t">문구 수정 반영' in lumi)
        check("설정에 없는 수정 종류는 태그에서 뺌",
              '<b class="rtag rt3">수정</b><div class="tb"><div class="th"><p class="t">잔여 회차 삭제' in lumi and "퍼블" not in lumi)
        check("기획 업무는 '기타' 태그로 (누락 없음)", '<b class="rtag rt4">기타</b><div class="tb"><div class="th"><p class="t">일정표 정리' in lumi)
        check("태그 칸은 같은 폭(가장 긴 태그 기준), 담당자 줄은 세로 가운데 · 완료한 일은 '기한 지남' 숨김",
              ".tasks .task{display:grid;grid-template-columns:minmax(66px,max-content) minmax(0,1fr)" in out
              and ".tasks .task .meta{align-items:center" in out
              and '.task:not([data-state="open"]) .meta .dl{display:none}' in out
              and '.task[data-state="done"] .st{background:var(--acc-bg)' in out)
        check("결정사항은 업무 묶음 아래 접힘", "런칭일 10월 1일 확정" in dec)

        hdr("[3-2] 결정사항 — 결정한 사람 기준 + 결정 일자")
        by_order = re.findall(r'<div class="dec-g" data-by="([^"]*)"', dec)
        check("결정자별로 묶임 (직함이 달라도 같은 사람)",
              by_order.count("김개발") == 1, str(by_order))
        check("최근에 결정한 사람 먼저, 결정자 미기재는 맨 뒤",
              by_order == ["김개발", "박외부", ""], str(by_order))
        kim = section(dec, 'data-by="김개발"', ('<div class="dec-g"',))
        check("같은 사람 안에서는 최근 결정부터",
              0 < kim.find("런칭일 10월 1일 확정") < kim.find("예산 300만원 승인"))
        check("결정 일자 표시 (MM/DD)",
              '<span class="dd num" title="2026-09-10">09/10</span><div class="dt"><span class="t">런칭일' in kim)
        check("결정자 소속 표시 (메일 주소 기준)", '<span class="org">루미</span>' in kim)
        check("날짜가 없는 예전 추출은 '일자 미확인'",
              "일자 미확인" in section(dec, 'data-by=""', ('</details>',)))
        check("요약 줄에 결정자 이름만 (사람별 건수는 펼치면 보임)",
              "결정사항 4개 <span class=\"ds\">· 김개발 · 박외부 · 결정자 미기재</span>" in dec, dec[:200])
        check("카드 안 접기 화살표는 '다른 담당자' 와 같은 작은 ▶ (기본 표시는 숨김)",
              ':is(.decs,.gone,.pend,.excl,.rel,.handled)>summary::before{content:"▶"' in out
              and ':is(.decs,.gone,.pend,.excl,.rel,.handled)>summary::-webkit-details-marker{display:none}' in out)
        from nwmail.dashboard import MAX_SLOTS, name_slot, project_slots
        from nwmail.projects import ProjectRules, load_rules as _rules
        ps = project_slots(_rules())
        by_client: dict[str, list[int]] = {}
        for pname, pclient in _rules().client_of.items():
            by_client.setdefault(pclient, []).append(ps[pname])
        check("세부 프로젝트마다 다른 색 (projects.json 순서로 1번부터, 같은 고객사 안에서는 겹치지 않음)",
              list(ps.values())[:MAX_SLOTS] == list(range(1, min(len(ps), MAX_SLOTS) + 1))
              and all(len(set(v)) == len(v) for v in by_client.values() if len(v) <= MAX_SLOTS)
              and f'class="bar s{ps["오로라 스토어"]}"' in lumi and f'class="bar s{ps["AX9 멤버십 테니스클럽"]}"' in gv
              and ps["오로라 스토어"] != ps["AX9 멤버십 테니스클럽"]
              and f'class="bar s{ps["KX볼트링크"]}"' in section(out, 'data-project="KX볼트링크"', ('</details>',)), str(ps))
        fake = project_slots(ProjectRules(client_of={**{f"a{i}": "A" for i in range(11)}, "b0": "B", "b1": "B"}))
        check("색이 모자라면 처음부터 다시, 다음 고객사는 이어지는 차례부터 (같은 고객사 안에서는 안 쓴 색 먼저)",
              [fake[f"a{i}"] for i in range(11)] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2]
              and (fake["b0"], fake["b1"]) == (3, 4), str(fake))
        from nwmail.dashboard import extra_slots
        ex = extra_slots({"": ["세이프원", "보안 공지", "재발송"]}, {}, {})
        check("설정에 없는 프로젝트끼리 같은 묶음에서 겹치면 이름순으로 뒤의 것이 다음 색",
              len(set(ex.values())) == 3 and ex["보안 공지"] == name_slot("보안 공지"), str(ex))
        check("설정에 없는 프로젝트는 이름으로 고정 색 · 고객사 머리 네모는 회색 · 9번째 색은 두 테마 모두",
              name_slot("빌노트") == name_slot("빌노트") and 1 <= name_slot("빌노트") <= MAX_SLOTS
              and '<div class="client-h"><i class="sw s0" aria-hidden="true"></i>' in out
              and "--s9:#a64ba0" in out and "--s9:#961dae" in out and ".s9{background:var(--s9)}" in out)
        from nwmail.colors import BANDS, lightness, tone, tones
        pc = {p["name"]: p for p in model_col["projects"]}
        lumi_c = section(out_col, 'data-project="오로라 스토어"', ('</summary>',))
        sk_c = section(out_col, 'data-project="KX볼트링크"', ('</summary>',))
        lounge_c = section(out_col, 'data-project="오로라 갤러리"', ('</summary>',))
        sk_t = tones("#123456")
        check("고른 색: 팔레트 칸은 그 칸 · 자동 색 칸은 따로 들고 있음 (오늘 탭 카드 네모도 같은 색)",
              pc["오로라 스토어"]["slot"] == 9 and pc["오로라 스토어"]["auto_slot"] == ps["오로라 스토어"]
              and f'class="bar s9" data-color="s9" data-auto="{ps["오로라 스토어"]}"' in lumi_c
              and 'style="--pc:var(--s9)"' in lumi_c
              and '<i class="sw s9" aria-hidden="true"></i>오로라 스토어</span>' in out_col)
        check("직접 고른 색은 10번 칸부터 · 테마별로 밝기를 맞춘 값으로 정의",
              pc["KX볼트링크"]["slot"] == 10 and 'class="bar s10" data-color="#123456"' in sk_c
              and f'--s10:{sk_t["light"]}' in out_col and f'--s10:{sk_t["dark"]}' in out_col
              and ".s10{background:var(--s10)}" in out_col, str(sk_t))
        check("자동 색으로 되돌리면 자동 색 · 바꾼 기록은 전부 남음",
              pc["오로라 갤러리"]["slot"] == ps["오로라 갤러리"] and 'data-color=""' in lounge_c
              and col_log == ["s2", ""], str(col_log))
        check("색 막대는 색 고르는 버튼 + ⋮ 맨 위 '색 바꾸기…' (서버 주소가 아니거나 예전 서버면 잠금)",
              'aria-label="오로라 스토어 색 바꾸기"' in lumi_c and "data-color-open>색 바꾸기…</button>" in lumi_c
              and "function openColor(bar)" in out_col and "send('/api/color'" in out_col
              and ".kb-btn,.proj-h>.bar," in out_col and "if(!j.colors)" in out_col)
        check("고르는 동안 미리 보기도 같은 밝기 범위로 (페이지 설정에 범위 · 같은 계산)",
              '"colorBands": {"light": [0.45, 0.75], "dark": [0.55, 0.75]}' in out_col
              and "function tonesOf(hex)" in out_col and "var committed=cur;" in out_col)
        check("직접 고른 색은 테마 밝기 범위 안으로 (범위 안이면 그대로)",
              tone("#2a78d6", "light") == "#2a78d6"
              and all(BANDS[mode][0] - 0.01 <= lightness(tone(h, mode)) <= BANDS[mode][1] + 0.01
                      for h in ("#000000", "#ffffff", "#123456", "#fdfcf9", "#ff0000") for mode in BANDS))

        hdr("[3-1] 업무 묶음 · 담당자 소속")
        check("업무명에서 프로젝트 태그를 뗌",
              '<h4 class="wt">&lt;script&gt;alert(1)&lt;/script&gt; 기획 검토</h4>' in lumi)
        check("담당자 소속은 메일 주소로 확정 (김개발 매니저 <pm@lumi.example>)",
              '<span class="nm">김개발</span><span class="org">루미</span>'
              in li.get("API 연동 개발", ""))
        check("주소로 못 찾으면 모델이 적은 회사명을 설정 표기로 맞춤 (누리마케팅 → 누리)",
              '<span class="nm">박외부</span><span class="org">누리</span>'
              in li.get("문구 수정 반영", ""))
        check("담당자 없으면 '미지정'", 'class="nm none">미지정' in li.get("배너 시안 제작", ""))
        check("한 대화뿐인 묶음은 업무마다 제목을 반복하지 않음",
              all('class="src"' not in body for _, body in items))
        nuri = section(out, 'data-project="오로라 운영(기타)"', ('<details class="proj"', '<section class="client"'))
        ext = section(nuri, 'data-work="시그니처 연장"', ('<section class="work"',))
        check("고객명 꼬리만 다른 대화는 한 묶음",
              nuri.count('data-work="시그니처 연장"') == 1 and "대화 2개" in ext,
              ext[:120])
        check("여러 대화가 묶이면 업무마다 출처 제목 표시",
              'class="src"' in section(ext, '<ul class="tasks">', ('</ul>',)))
        check("쓰지 않는 역할은 자리를 차지하지 않음",
              ext.count('<li class="task"') == 1 and 'class="empty"' not in ext)
        check("이전 역할(개발)로 저장된 추출도 태그와 함께 표시",
              '<b class="rtag rt1">개발</b><div class="tb"><div class="th"><p class="t">신청 기한 연장 처리' in ext)
        check("묶음이 다른 업무는 따로", 'data-work="신청페이지 수정"' in nuri)
        check("오늘 탭 내 할 일에는 역할 태그 없음",
              'class="rtag' not in section(out, 'id="tab-today"', ('id="tab-projects"',)))

        gen = section(out, 'data-project="오로라 갤러리"', ('<details class="proj"', '<section class="client"'))
        check("추출 전 프로젝트는 '추출 대기' + 스레드 표시",
              "업무 추출 대기" in gen and "갤러리 티켓" in gen)
        misc = section(out, 'data-project="미분류"', ('<details class="proj"', '<section class="client"'))
        check("미분류는 중립색 (회색)", 'class="bar s0"' in misc)
        check("할 일이 하나도 없는 업무는 빈 열 대신 한 줄",
              "남은 할 일 없음" in misc and 'class="tasks"' not in misc)
        a1_work = section(lumi, 'data-work="&lt;script&gt;alert(1)&lt;/script&gt; 기획 검토"',
                          ('<section class="work"',))
        a1_head = section(a1_work, '<div class="work-h">', ('</div>',))
        check("업무 묶음은 카드 한 장: 머리 띠에 진행 상태 · 완료한 할 일 · 메일 수",
              '<span class="stt s-ing">진행중</span>' in a1_head
              and '완료 <b class="num" data-wdone>0</b>/<span data-wtotal>5</span> · 메일 1통' in a1_head,
              a1_head)
        check("정리 전 업무는 머리 띠에 '정리 대기', 원본 대화는 카드 안쪽 상자",
              '<span class="wchip">정리 대기</span>' in gen
              and '<div class="pend-only"><p class="pend-note">업무 추출 대기 — 원본 대화 1개</p>'
                  '<ul class="thr-list">' in gen)
        check("할 일 카드 '다른 담당자' 줄은 업무 카드의 details.rel 과 클래스가 겹치지 않음",
              'class="oth"' in out and '<p class="rel' not in out and '.rel{' not in out)
        check("완료 · 되돌리기 때 업무 카드 완료 수도 고침",
              "querySelectorAll('.work [data-wdone]')" in out)

        hdr("[4] 오늘 — 내 할 일 (대화 카드)")
        today_tab = section(out, 'id="tab-today"', ('id="tab-wait"',))
        wait_tab = section(out, 'id="tab-wait"', ('id="tab-projects"',))
        me_tab = section(today_tab, '<div class="acts-open" data-list="open">', ('<p class="empty-state" data-empty="open"',))
        hint = section(today_tab, '<span class="infotip"', ('<div class="seg"',))
        check("보기 설명은 제목 옆 (!) 말풍선으로 (늘 보이는 설명 줄 없음)",
              '<h2>내가 해야 할 일</h2><span class="infotip" tabindex="0"' in today_tab
              and 'aria-describedby="acts-note"' in hint and 'role="tooltip" id="acts-note"' in hint
              and '<p class="note" id="acts-note">' not in today_tab)
        check("말풍선 문구는 두 줄 (묶는 방식 / 체크 · 제외 · 표시 기록 안내)",
              "메일 대화별로 묶었습니다 · 기한 있는 일이 위, 기한 없는 일은 프로젝트별.\n"
              "체크하면 완료, 줄에 마우스를 올리면 제외·업무삭제. 표시 기록은 업무 정리 기준을 다듬는 데 씁니다."
              in hint and ".infotip .tip{" in out and "white-space:pre-line" in out)
        order = [me_tab.find(t) for t in ("예산 승인", "기획안 회신", "참고 자료 확인")]
        check("같은 대화의 할 일 세 건은 대화 카드 한 장 안의 세 줄, 마감순 (기한 지남 → 오늘 → 기한 없음)",
              me_tab.count('<article class="cv"') == 1 and me_tab.count('<li class="tk"') == 3
              and -1 not in order and order == sorted(order), str(order))
        due_zone = section(me_tab, '<section class="az" data-zone="due">', ('<section class="az" data-zone="rest"',))
        check("기한 있는 할 일이 있는 대화는 '마감 있는 일' 칸 + 프로젝트 이름 · 색을 카드에 (기한 없는 칸은 숨김)",
              '<article class="cv"' in due_zone and '<span>마감 있는 일</span><b class="az-n num">3</b>' in due_zone
              and f'<span class="pj"><i class="sw s{ps["오로라 스토어"]}" aria-hidden="true"></i>오로라 스토어</span>'
              in due_zone
              and '<section class="az" data-zone="rest" hidden>' in me_tab)
        check("머리 요약: 대화 수 · 할 일 수 · 기한 지남",
              '대화 <b class="num" id="sum-cv">1</b>개 · 할 일 <b class="num" id="sum-n">3</b>건' in me_tab
              and '기한 지남 <b class="num" id="sum-over-n">1</b>' in me_tab)
        check("할 일 줄에 중요도 표시(▲) 없음",
              'class="imp"' not in out and "중요도 높음" not in me_tab and "낮음" not in me_tab)
        check("기한 라벨 + 날짜 · 요일 (오늘은 '오늘 마감')",
              "2일 지남 · 9/9(수)" in me_tab and "오늘 마감 · 9/11(금)" in me_tab)
        check("카드 제목은 메일 제목에서 앞머리 [태그] 를 걷어낸 것 (전체 제목은 title, 태그 글자는 이스케이프)",
              '<h3 title="[루미] &lt;script&gt;alert(1)&lt;/script&gt; 기획 검토">'
              '&lt;script&gt;alert(1)&lt;/script&gt; 기획 검토' in me_tab and "<script>alert" not in me_tab)
        check("모두 직접 받은 대화라 '직접' 칩은 달지 않음", '<span class="chip">직접</span>' not in me_tab)
        check("현황: 대화 요약 (두 줄까지, 넘치면 더보기) · 진행 상태 칩은 제목 옆",
              '<p class="tx">기획안 검토 요청</p>' in me_tab
              and re.search(r'<h3 [^>]*>[^<]*(?:<span class="(?:new|chip)">[^<]*</span>)*'
                            r'<span class="stt s-ing">진행중</span></h3>', me_tab)
              and '<button type="button" class="cv-more" aria-expanded="false">더보기</button>' in me_tab
              and "-webkit-line-clamp:2" in out)
        oth = section(me_tab, '<details class="cv-oth">', ("</details>",))
        check("다른 담당자: 접어 두고 머리에 건수 · 이름, 펼치면 이름 있는 남의 업무만 마감순",
              "다른 담당자 3건 · " in oth and -1 < oth.find("API 연동 개발") < oth.find("문구 수정 반영")
              and "배너 시안 제작" not in me_tab, oth[:200])
        check("메일 줄: 마지막 보낸 사람 · 시각 · 통수 · 답장 대기 (카드에 한 번, 시각은 묶음 안에서도 보임)",
              me_tab.count('김개발<span class="cv-at"> · 09/10 10:00</span> · 메일 1통') == 1
              and "답장 대기 1일" in me_tab)
        check("마감 있는 대화 줄은 제목 위 날짜 칩 = 가장 급한 마감일 · 요일 · D-day · 기한 등급 (지난 마감은 빨강)",
              '<div class="cv-b"><p class="cv-due"><span class="cv-dc lv-overdue"><b class="num">9/9 수</b> · 2일 지남'
              '</span></p><div class="cv-h">' in due_zone
              and 'data-last="2026-09-10"' in due_zone and "data-lv=" not in me_tab and 'class="cv-d ' not in out)
        check("구역 머리 14px · 프로젝트 머리 17px · 패널 줄 사이는 1px 틈 · 묶음 안에서는 프로젝트 이름만 숨김",
              ".az-h{display:flex;align-items:center;gap:8px;margin:0 0 12px;font-size:14px" in out
              and ".apg-n{font-size:17px" in out and ".cvs{display:flex;flex-direction:column;gap:1px" in out
              and ".apg .cv-m .pj{display:none}" in out and ".cv-at{display:none}" not in out)
        check("한국어는 단어 단위로 줄바꿈 (할 일 · 제목 · 현황)",
              out.count("word-break:keep-all") >= 4)
        check("날짜 칩은 페이지도 같은 규칙으로 다시 그림 (칸을 옮기거나 표시할 때)",
              "function paintDate(cv,live,rows)" in out and "paintDate(cv,live,rows);" in out
              and "cv.querySelector('.cv-due')" in out)
        from nwmail.dashboard import _cv_others
        multi = _cv_others({"rows": [{"act": {"text": "내 일"}}]},
                           {"others": [{"assignee": "문지호, 배건우", "org": "", "text": "피그마 기준 개발계 작업",
                                        "deadline": {"level": "none", "sort": 99, "raw": "", "label": ""}}]})
        check("다른 담당자: 여럿이 적힌 담당자는 사람마다 이름 ('문지호,' 처럼 쉼표가 남지 않음)",
              "다른 담당자 1건 · 문지호 · 배건우" in multi and '<span class="nm">문지호 · 배건우</span>' in multi
              and "문지호," not in multi, multi[:240])
        check("다른 담당자: 이름 · 할 일 · 기한 칸을 줄마다 같은 선에 (subgrid)",
              ".oth{display:grid;grid-column:1/-1;grid-template-columns:subgrid" in out)
        from nwmail.dashboard import _act_context, _same_work
        fake = {"me_names": ["홍길동"], "unanswered": [], "projects": [{"items": [{"roles": {"기타": [
            {"assignee": "홍길동 선임", "text": "내 일", "thread": {"key": "k"}},
            {"assignee": "이지우", "text": "남의 일", "thread": {"key": "k"}},
            {"assignee": "", "text": "이름 없는 일", "thread": {"key": "k"}}]}}]}]}
        check("다른 담당자에서 나 · 이름 없는 업무는 뺌",
              [t["text"] for t in _act_context(fake)["k"]["others"]] == ["남의 일"])
        check("표현만 다른 같은 일은 다른 담당자에서 뺌 (실제 문구)",
              _same_work("CRM(관리자) 권한설정 관련 세이프원 월별 점검 시 받은 정책 또는 가이드 내용이 "
                         "있는지 검토 후 메일 회신",
                         "CRM(관리자) 권한설정 관련 세이프원 월별 점검 시 받은 정책·가이드 내용이 있는지 "
                         "확인 후 메일로 공유")
              and not _same_work("계정 발급 회신 지연 상황 확인 및 필요 시 대체 조치 방안 검토",
                                 "사내 메신저 PC버전 계정 발급 진행 상황 확인 및 결과 회신"))
        check("기한 등급 속성 (오늘 마감 · 지난 마감 집계용)", 'data-level="overdue"' in me_tab
              and 'data-level="today"' in me_tab)
        check("줄마다 체크 상자(완료) + 제외 · 업무삭제 · 되돌리기 (완료 버튼 대신 체크)",
              me_tab.count('<input type="checkbox" class="ck"') == 3 and 'data-act="done"' not in me_tab
              and me_tab.count('data-act="excluded">제외</button>') == 3
              and 'data-act="deleted">업무삭제</button>' in me_tab
              and 'data-act="open">되돌리기</button>' in me_tab)
        check("체크 상자 이름은 그 줄 문구 (aria-labelledby 가 같은 id)",
              re.search(r'class="ck" aria-labelledby="(tkt-[0-9a-f]+)">.*?id="\1"', me_tab) is not None)
        check("표시 기록 없으면 모두 남은 일 (완료 · 제외·삭제 목록은 비어 있음)",
              me_tab.count('data-state="open"') == 3 and 'id="cnt-done">0' in today_tab
              and 'id="cnt-dropped">0' in today_tab)
        check("줄은 표시해도 그 자리에 두고, 묶음 건수 · 칸 이동은 페이지가 다시 맞춤",
              "function layoutOpen()" in out and "function placeRow(li)" in out and "function arrange()" in out
              and "function ensureRow(src)" in out and "function regroup()" not in out and '"groups":' not in out)
        check("기한 없는 묶음은 접은 것을 기억 · '모두 접기'",
              "nwmail.actFold" in out and '<button type="button" class="az-fold">모두 접기</button>' in me_tab)

        hdr("[4-2] 오늘 — 대화 카드 묶기 · 같은 할 일 합치기")
        from nwmail.dashboard import _act_title, _merge_same, _open_view

        def fake(n, text, key, project, raw="", urgency="medium"):
            d = deadline_info(raw, TODAY)
            return {"id": f"{n:012x}", "text": text, "urgency": urgency, "project": project, "slot": 1,
                    "kind": "me", "state": None, "deadline": {**d, "mail": raw, "manual": False},
                    "thread": {"key": key, "subject": f"RE: [누리] 대화 {key}_202601001", "new": False,
                               "direct": True, "summary": "", "snippet": "", "from": "", "count": 1,
                               "last_at": "2026-09-10T01:00:00+00:00"}}
        same = "스토어 PC·MO 홈 화면 오퍼링 배너 4개 카테고리 구성 기획안 전달"
        fakes = [fake(1, same, "k1", "오로라 운영", "2026-09-20", "high"),
                 fake(2, same.replace(" ", "  ", 2), "k2", "오로라 운영"),
                 fake(3, "확인 후 회신", "k2", "오로라 운영"), fake(4, "확인 후 회신", "k3", "오로라 운영"),
                 fake(5, same, "k4", "오로라 갤러리"), fake(6, "공고문 검토", "k5", "미분류"),
                 fake(7, "답변 확인", "k6", "빌노트")]
        merged = _merge_same(fakes)
        check("같은 프로젝트 · 띄어쓰기만 다른 같은 문구는 한 줄로 (앞 줄에 붙임)",
              len(merged) == 6 and [x["id"] for x in merged[0]["dups"]] == [fakes[1]["id"]])
        check("짧은 문구('확인 후 회신') · 다른 프로젝트의 같은 문구는 합치지 않음",
              sum(r["act"]["text"] == "확인 후 회신" for r in merged) == 2
              and any(r["act"]["project"] == "오로라 갤러리" for r in merged))
        view = _open_view(fakes, {})
        rest = section(view, 'data-zone="rest"', ("</section>",))
        groups = re.findall(r'<details class="apg" data-pg="([^"]+)" open>', view)
        check("기한 없는 대화는 프로젝트 묶음 (처음 나온 순, 미분류는 맨 아래)",
              groups == ["오로라 운영", "오로라 갤러리", "빌노트", "미분류"] and "k1" not in rest, str(groups))
        check("묶음 머리에 대화 수 · 할 일 수 (합친 줄은 한 건)",
              '오로라 운영</span><span class="apg-c num">대화 2 · 할 일 2</span>' in view)
        check("기한 없는 대화 줄은 날짜 칩 없이 (빈 줄 · 최근 메일 시각은 메일 줄에) · 마감 있는 줄은 마감일 칩",
              rest.count('<p class="cv-due"></p>') == rest.count('<article class="cv"') > 0
              and '<span class="cv-at">' in rest
              and '<b class="num">9/20 일</b> · ' in section(view, 'data-zone="due"', ("</section>",)))
        check("합친 줄은 붙은 할 일 id 를 싣고, 다른 대화에도 있다고 알림",
              f'data-dups="{fakes[1]["id"]}"' in view and "다른 대화 1곳의 같은 할 일도 함께 표시됩니다" in view)
        check("머리 요약 할 일 수도 합친 줄 기준 (7건 → 6줄)", '<b class="num" id="sum-n">6</b>' in view)
        check("카드 제목: 회신 표식 · 앞머리 태그 · 긴 관리번호 · '의 건' 을 뗌",
              _act_title({"subject": "RE: [누리마케팅] AX9 신청페이지 배너 구성 변경 관련의 건_202601001"})
              == "AX9 신청페이지 배너 구성 변경 관련")

        hdr("[4-3] 내 할 일 표시 기록 — 완료 · 제외 · 업무삭제")
        s_fb = model_fb["stats"]
        t_fb = section(out_fb, 'id="tab-today"', ('id="tab-projects"',))
        open_l = section(t_fb, 'data-list="open"', ('</ul>',))
        done_l = section(t_fb, 'data-list="done"', ('</ul>',))
        drop_l = section(t_fb, 'data-list="dropped"', ('</ul>',))
        check("완료 · 제외한 일은 남은 일에서 빠짐",
              "예산 승인" in open_l and "기획안 회신" not in open_l and "참고 자료 확인" not in open_l)
        check("남은 일 수 = 헤드라인 · 숫자 칸", s_fb["my_open"] == 1
              and '<span id="hl-me">1</span>' in out_fb and 'id="kpi-me">1</b>' in out_fb)
        check("완료 목록 (완료 날짜 표시)", "기획안 회신" in done_l
              and '<span class="st">09/11 완료</span>' in done_l, done_l[:200])
        check("완료 카드 메일 줄 말풍선은 키보드 포커스로도 열림 (tabindex + aria-describedby 가 같은 id)",
              re.search(r'class="mail" tabindex="0" aria-describedby="(tip-[0-9a-f]+)">.*?id="\1"',
                        done_l) is not None)
        check("7일 창 밖 할 일도 표시할 때 문구로 완료 목록에 남음",
              "지난주 끝낸 일" in done_l
              and '<span class="tip-l">메일 제목</span>[NURI] 지난 건</span>' in done_l)
        check("완료 목록은 최근 완료가 위", done_l.find("기획안 회신") < done_l.find("지난주 끝낸 일"))
        check("제외 · 사유 · 날짜 표시", "참고 자료 확인" in drop_l
              and '<span class="st">제외 · 남의 일 · 09/11</span>' in drop_l)
        check("30일 넘은 기록은 목록에서 뺌", "오래전 삭제한 일" not in out_fb)
        check("보기 전환 숫자 (할 일 1 · 완료 3 · 제외·삭제 2 — 프로젝트 할 일 포함)",
              'id="cnt-open">1' in t_fb and 'id="cnt-done">3' in t_fb and 'id="cnt-dropped">2' in t_fb)
        check("오늘 완료 수 (내 할 일 + 프로젝트 할 일)",
              s_fb["done_today"] == 2 and 'id="kpi-done">2' in out_fb)
        check("완료 · 제외 목록은 처음엔 접힘", '<ul class="acts" data-list="done" hidden>' in t_fb)
        check("되돌리면 다시 남은 일 + 기록은 로그에 남음",
              ids["참고 자료 확인"] in {a["id"] for a in undone["my_actions"]}
              and undo_log == ["excluded", "open"], str(undo_log))
        check("서버 주소 · 토큰을 페이지 설정에 심음",
              '"port": 8787' in out_fb and '"token": "' in out_fb and 'id="nw-conf"' in out_fb)
        check("사유 목록은 설정에서 (제외 · 업무삭제)", '"남의 일"' in out_fb and '"중복"' in out_fb)
        check("서버 설정 없이 만든 페이지는 포트 없음 (버튼 기록 안 함)", '"port": null' in out)

        hdr("[4-4] 프로젝트 탭 할 일 — 완료 · 제외 · 서버 기록")
        lumi_fb = section(out_fb, 'data-project="오로라 스토어"', ('<details class="proj"', '<section class="client"'))
        tasks_fb = section(lumi_fb, '<ul class="tasks">', ('</ul>',))
        gone_fb = section(lumi_fb, '<details class="gone">', ('</details>',))
        check("할 일마다 완료 · 제외 · 업무삭제 · 되돌리기 버튼 (오늘 탭과 같은 버튼)",
              tasks_fb.count('data-act="done"') == 4
              and all(f'data-act="{a}"' in tasks_fb
                      for a in ("excluded", "deleted", "open")))
        check("서버에 보낼 문구에 NEW·직접 배지 글자가 섞이지 않음",
              '<p class="t">' in tasks_fb and '</p><span class="new">' in lumi_fb)
        check("서버에 보낼 값(종류 · 역할 · 담당자)을 줄에 싣음",
              'data-kind="task"' in tasks_fb and 'data-role="개발"' in tasks_fb
              and 'data-assignee="김개발"' in tasks_fb)
        done_task = section(tasks_fb, '<li class="task done"', ('<li class="task',))
        check("완료한 할 일은 취소선 + 완료 날짜",
              tids["API 연동 개발"] in done_task
              and '<span class="st">09/11 완료</span>' in done_task, done_task[:160])
        check("완료한 할 일은 묶음 안에서 맨 아래로",
              tasks_fb.find("API 연동 개발") > tasks_fb.find("배너 시안 제작"))
        check("업무삭제한 할 일은 목록에서 빠지고 접힌 곳에 남음",
              "일정표 정리" not in tasks_fb and "일정표 정리" in gone_fb
              and "제외·삭제한 할 일 1개" in lumi_fb)
        check("프로젝트 카드 머리말 = 완료 / 전체 (오늘 탭 진행 막대와 같은 뜻)",
              '완료 <b class="num" data-done="오로라 스토어">2</b>/<span data-ptotal>6</span>' in lumi_fb,
              lumi_fb[:400])
        check("업무 카드 머리 띠도 완료 / 전체 (제외 · 삭제는 빼고 셈)",
              'data-wdone>1</b>/<span data-wtotal>4</span>' in lumi_fb)
        progs_fb = section(out_fb, '<div class="progs">', ('id="progs-empty"',))
        check("진행 막대는 표시 기록으로 채움 (페이지 열기 전부터)",
              'data-total="6">2/6' in progs_fb and 'style="width:33%"' in progs_fb)
        done_l_fb = section(section(out_fb, 'id="tab-today"', ('id="tab-projects"',)),
                            'data-list="done"', ('</ul>',))
        check("오늘 탭 완료 목록에 프로젝트 할 일도 (역할 태그로 구분)",
              "API 연동 개발" in done_l_fb and '<b class="rtag rt1">개발</b>' in done_l_fb
              and '<span class="tag">김개발</span>' in done_l_fb)
        check("오늘 완료 수에 프로젝트 할 일 포함",
              model_fb["stats"]["done_today"] == 2, str(model_fb["stats"]["done_today"]))
        check("7일 창을 벗어나도 문구로 남김 (프로젝트 할 일 스냅샷)",
              model_fb["stats"]["dropped"] == 2)
        templates = out_fb[out_fb.find("</main>"):out_fb.find('<div class="toast"')]
        tpl_ids = set(re.findall(r'<template id="act-([0-9a-f]{12})">', templates))
        check("완료 · 제외 목록에 카드가 없는 할 일(남은 내 할 일 · 프로젝트 할 일)만 카드 틀을 탭 밖 <template> 에",
              tids["배너 시안 제작"] in tpl_ids and tids["API 연동 개발"] not in tpl_ids
              and tids["일정표 정리"] not in tpl_ids and ids["예산 승인"] in tpl_ids
              and ids["기획안 회신"] not in tpl_ids and ids["참고 자료 확인"] not in tpl_ids
              and "<template" not in section(out_fb, 'id="tab-today"', ("</main>",)), str(len(tpl_ids)))
        cv_ids = set(re.findall(r'<template id="cv-([0-9a-f]{12})">', templates))
        check("완료 · 제외한 내 할 일은 되돌릴 때 쓸 대화 카드 틀 (7일 창 밖 기록 포함, 프로젝트 할 일은 없음)",
              cv_ids == {ids["기획안 회신"], ids["참고 자료 확인"], "aaaaaaaaaaaa"}, str(len(cv_ids)))
        banner_tpl = section(templates, f'<template id="act-{tids["배너 시안 제작"]}">', ("</template>",))
        check("카드 틀은 오늘 탭 카드와 같은 모양 (역할 태그 · 버튼, 가려지는 현황 칸은 뺌)",
              f'<li class="act" data-task="{tids["배너 시안 제작"]}"' in banner_tpl
              and '<b class="rtag rt2">디자인</b>' in banner_tpl and 'data-act="done">완료' in banner_tpl
              and 'class="ctx"' not in banner_tpl, banner_tpl[:160])
        check("프로젝트 · 일정 탭에서 표시하면 틀을 복사해 오늘 탭 완료 · 제외 목록에 올리는 JS",
              "function ensureCard(src)" in out_fb and "ensureCard(li);ensureRow(li);" in out_fb
              and "parked[li.dataset.task]=li;li.remove();" in out_fb)
        check("오늘 완료는 할 일마다 한 번만 셈 (프로젝트 할 일은 오늘 · 프로젝트 탭 두 곳에 있음)",
              "doneIds[li.dataset.task]=1" in out_fb)
        check("제외 · 업무삭제한 줄은 누른 즉시 '제외·삭제한 할 일' 접힘으로 옮기는 JS (되돌리면 돌아옴)",
              "function goneBox(work)" in out_fb and "function syncWork(work)" in out_fb
              and "'제외·삭제한 할 일 '+gone+'개'" in out_fb
              and "var ul=drop?goneBox(work).querySelector('.tasks'):taskList(work)" in out_fb)
        check("제외 · 업무삭제는 진행 막대 · 남은 할 일의 전체에서 뺌 (페이지가 다시 셀 때도 서버와 같게)",
              "if(s==='open'||s==='done')c.total++" in out_fb and "[data-ptotal]" in out_fb
              and "[data-wtotal]" in out_fb)

        hdr("[4-1] 오늘 — 머리글 · 숫자")
        s = model["stats"]
        check("헤드라인에 할 일 · 답 기다리는 메일 수",
              '처리할 일이 <em class="c-acc"><span id="hl-me">3</span>건</em>' in out
              and '답 기다리는 메일 <em class="c-warn"><span id="hl-wait">2</span>건</em>' in out)
        check("날짜 + 요일", '<time datetime="2026-09-11">09/11 금</time>' in out)
        check("오늘 마감 · 지난 마감 건수", s["due_today"] == 1 and s["overdue"] == 1
              and '오늘 마감 <span id="kpi-today">1</span>' in out
              and '지난 마감 <span id="kpi-over">1</span>' in out)
        check("미응답 가장 오래 기다린 일수", f'가장 오래된 건 {s["oldest_wait"]}일 경과' in out
              and s["oldest_wait"] >= 1, str(s["oldest_wait"]))
        todo = s["threads"] - s["excluded"]
        check("요약 완료 = 정리된 대화 / 정리 대상",
              f'{s["extracted"]}<span class="den">/{todo}</span>' in out)
        check("진행 프로젝트는 미분류 제외",
              s["named_projects"] == len([p for p in model["projects"] if p["name"] != "미분류"])
              and 'id="kpi-proj">' in out)
        check("프로젝트 탭 숫자 = 진행 중 + 지연 프로젝트",
              f'data-tabcnt="tab-projects">{s["projects_open"] + s["projects_delayed"]}<' in out
              and s["projects_open"] + s["projects_delayed"] + s["projects_done"] == len(model["projects"]))
        check("다시 요약 버튼 없음", "다시 요약" not in out)
        check("마지막 정리 = 추출 결과 저장 시각 (실행 기록이 없어도)",
              '마지막 <b class="num">09/11 09:00</b>' in out)
        check("다음 자동 정리 시각 (12:00 기준 → 13:00)",
              '다음 <b class="num">13:00</b>' in out)
        from nwmail.dashboard import next_full_run
        late = next_full_run(datetime(2026, 9, 11, 9, 30, tzinfo=timezone.utc))   # 18:30 KST
        check("17시 이후면 다음 날 09:00", (late.day, late.hour) == (12, 9), str(late))

        hdr("[4-2] 오늘 — 프로젝트 진행 / 프로젝트 탭 — 최근 결정사항")
        progs = section(today_tab, '<div class="progs">', ('id="progs-empty"',))
        check("프로젝트별 진행 줄 (전체 할 일 수)",
              'data-goto="오로라 스토어"' in progs and 'data-total="8">0/8' in progs)
        check("진행 줄마다 ⋮ 메뉴 (줄 이동 버튼과 따로)",
              '<button type="button" class="pgo"' in progs
              and progs.count('class="kb-btn"') == progs.count('class="prow"'))
        check("표시 기록이 없으면 진행 막대는 비어 있음", 'style="width:0%"' in progs)
        check("할 일 없는 프로젝트는 '할 일 없음'", 'data-total="0">할 일 없음' in progs)
        check("할 일 없는 미분류는 진행 목록에서 뺌", 'data-goto="미분류"' not in progs)
        p_tab = section(out, 'id="tab-projects"', ('id="tab-schedule"',))
        check("오늘 탭은 한 칸 — 답을 기다리는 메일 · 결정사항 없음",
              'id="waits"' not in today_tab and 'class="rds"' not in today_tab and "col-side" not in out)
        check("최근 결정사항은 프로젝트 탭 맨 위 (보기 버튼보다 위)",
              -1 < p_tab.find('<section class="recent-decs"') < p_tab.find('<div class="ptools">')
              and p_tab.count('<ul class="rds">') == 1 and "전체는 프로젝트 탭" not in out)
        rds = section(p_tab, '<ul class="rds">', ('</ul>',))
        rd_order = [rds.find(t) for t in ("런칭일 10월 1일 확정", "시안 B안 채택", "예산 300만원 승인")]
        check("최근 결정사항은 날짜 최신순", -1 not in rd_order and rd_order == sorted(rd_order),
              str(rd_order))
        check("결정자 이니셜 · 결정자 · 날짜 · 프로젝트",
              '<span class="av" aria-hidden="true">김개</span>' in rds
              and "김개발 매니저 결정 · 09/10 · 오로라 스토어" in rds)
        check("최근 결정사항은 날짜 없는 것까지 모두 (5개 이하)", rds.count('<li class="rd">') == 4)

        hdr("[5] 미응답 · 차단 · 신규")
        check("답장 대기는 오늘 다음 탭, 숫자 = 미응답 수 (있으면 주황)",
              out.find('id="b-tab-today"') < out.find('id="b-tab-wait"') < out.find('id="b-tab-projects"')
              and f'<span class="cnt warn" data-tabcnt="tab-wait">{s["unanswered"]}<' in out
              and s["unanswered"] == 2, str(s["unanswered"]))
        check("머리 요약 '답장 대기' 의 '목록 보기 →' 는 답장 대기 탭으로",
              '<a class="kpi-go" href="#wait">목록 보기 →</a>' in out)
        check("'할 일 N건 →' 은 오늘 탭으로 바꾼 뒤 카드로 · 답변완료 때 탭 숫자도 고침",
              "getElementById('b-tab-today');if(tt&&" in out and '[data-tabcnt="tab-wait"]' in out)
        un = section(wait_tab, 'id="waits"', ('</ul>',))
        check("받는사람(TO)으로 받고 미회신 스레드 포함", "신청페이지 수정" in un)
        check("회신한 스레드는 제외", "갤러리 티켓" not in un)
        check("참조(CC)로만 받은 메일은 제외 (직접 수신이어도)",
              "참조로 받은 검토 요청" not in un
              and any(th["subject"] == "[루미] 참조로 받은 검토 요청" for th in model["direct_threads"]))
        check("기준 표시", "받는사람(TO) 기준" in wait_tab and "최근 7일 메일 중" in wait_tab)
        from nwmail.dashboard import awaiting_to_me
        to_mail = {"is_mine": 0, "sent_at": "2026-09-10T01:00:00+00:00",
                   "to_addrs": json.dumps(["Other@x.com", ME.upper()])}
        cc_mail = dict(to_mail, to_addrs=json.dumps(["other@x.com"]))
        cut = "2026-09-04T15:00:00+00:00"
        check("To 에 내 주소(대소문자 무시) → 대기",
              awaiting_to_me({"mails": [to_mail], "my_last_at": None}, ME, cut) == to_mail["sent_at"])
        check("내가 그 뒤에 답했으면 대기 아님",
              awaiting_to_me({"mails": [to_mail], "my_last_at": "2026-09-10T02:00:00+00:00"},
                             ME, cut) is None)
        check("To 에 없으면 대기 아님", awaiting_to_me({"mails": [cc_mail], "my_last_at": None},
                                                ME, cut) is None)
        check("최근 7일 전 메일은 대기 아님",
              awaiting_to_me({"mails": [dict(to_mail, sent_at="2026-09-01T00:00:00+00:00")],
                              "my_last_at": None}, ME, cut) is None)
        check("대기 일수는 왼쪽 배지 (숫자 + 일)",
              '<span class="wage"><b class="num">3</b><small>일</small><span class="sr-only"> 경과</span>' in un,
              un[:300])
        check("배지 색 단계: 오늘 0 · 1일 1 · 2일 이상 2",
              'data-waited="3" data-lv="2"' in un and 'data-waited="1" data-lv="1"' in un, un[:300])
        check("2일 넘게 기다린 메일은 강조", '<li class="wait old" ' in un)
        check("보낸 사람 이름 + 소속(메일 주소 기준) · 프로젝트",
              '<b class="wn" title="김개발 매니저">김개발 매니저</b><span class="wo">루미 · 오로라 스토어</span>' in un)
        check("요약은 두 줄까지, 마우스를 올리면 받은 메일 본문 (이스케이프)",
              'data-preview-label="받은 메일">기획안 검토 요청</p>' in un
              and "onerror=alert(2)&gt;" in un and "<img" not in un)
        check("받은 시각", "09/10 10:00 받음" in un)
        check("같은 대화에 남은 내 할 일이 있으면 '할 일 N건 →' (없으면 숨김)",
              'class="wgo">할 일 <b class="num">3</b>건 →' in un
              and 'class="wgo" hidden>할 일 <b class="num">0</b>건 →' in un)
        check("답변완료는 줄에 바로 보이는 버튼, 삭제는 줄 맨 끝 ⋮ 메뉴",
              un.count('data-wmark="answered">답변완료') == 2
              and un.count('class="kb-btn"') == 2 and 'data-wmark="deleted">삭제' in un
              and 'role="menuitem" data-wmark="answered"' not in un
              and un.count('data-wmark="open">되돌리기</button></div><span class="kb">') == 2)
        check("6건 이하면 '나머지 보기' 숨김",
              'data-more="waits" aria-expanded="false" hidden>' in wait_tab)
        check("표시한 메일이 없으면 '답변완료·삭제한 메일' 숨김",
              '<details class="handled" id="handled" hidden>' in wait_tab)
        from nwmail.dashboard import _waiting, sender_parts
        many = dict(model, unanswered=model["unanswered"] * 4)
        w = _waiting(many)
        check("7건 이상이면 6건만 펼치고 나머지는 접음",
              len(re.findall(r'<li class="wait[^>]* hidden>', w)) == 2 and "나머지 2건 보기" in w)
        for raw, email, want in (
                ("서나윤 대리_누리마케팅", "cl@nurimktg.co.example", ("서나윤 대리", "누리")),
                ("루미 강민호 이사", "k@lumi.example", ("강민호 이사", "루미")),
                ("유나래 Narae Yoo 책임매니저 오로라고객경험팀", "s@aurora.example", ("유나래 책임매니저", "오로라")),
                (" 인증확인시스템 ", "v@certcheck.or.example", ("인증확인시스템", "")),
                ("", "b@lumi.example", ("b@lumi.example", "루미"))):
            check(f"보낸 사람 나누기: {raw or email!r}", sender_parts(raw, email) == want,
                  str(sender_parts(raw, email)))
        from nwmail.dashboard import _wait
        gen = dict(model["unanswered"][0], name="유나래 책임매니저", org="오로라",
                   thread=dict(model["unanswered"][0]["thread"], project="오로라 운영(기타)"))
        check("소속이 프로젝트 이름에 들어 있으면 소속은 뺌",
              '<span class="wo">오로라 운영(기타)</span>' in _wait(gen))
        bl = section(out, 'id="tab-blocked"', ("</main>",))
        check("차단 규칙 표에 패턴과 차단 수", "[광고]" in bl and "세미나" in bl)
        check("차단 메일은 프로젝트 탭에서 제외",
              "세미나" not in section(out, 'id="tab-projects"', ('id="tab-schedule"', 'id="tab-blocked"')))
        check("이전 동기화 이후 도착한 스레드에 NEW",
              "NEW" in lumi and model["stats"]["new"] == 1, f"new={model['stats']['new']}")

        hdr("[5-1] 최근 7일 기준 + 과거 이력")
        projects_tab = section(out, 'id="tab-projects"', ('id="tab-schedule"', 'id="tab-blocked"'))
        check("지난달에만 오간 대화는 표시 안 함", "지난달 정산" not in out)
        sk = section(out, 'data-project="KX볼트링크"', ('<article class="proj"',))
        check("과거 메일이 붙은 대화는 최근/전체 메일 수를 구분 표시",
              "최근 1통 · 전체 2통" in sk, sk[sk.find("통") - 30: sk.find("통") + 20] if sk else "")
        check("과거 메일 수 집계", model["stats"]["history"] == 1,
              f"history={model['stats']['history']}")
        gen2 = section(out, 'data-project="오로라 갤러리"', ('<details class="proj"', '<section class="client"'))
        check("마지막 메일이 내 답장이면 보낸사람을 '나'로 표시", "<span>나</span>" in gen2)
        check("상단 타일이 최근 7일 기준임을 표시", "최근 7일 대화" in out)
        check("내 메일은 차단 목록에 없음", "RE: [문의] 갤러리 티켓" not in bl)
        check("프로젝트 탭에 과거 이력 대화 포함", "API 확인" in projects_tab)

        hdr("[6] 완료 체크 상태 유지")
        ids1 = sorted(set(re.findall(r'data-task="([0-9a-f]+)"', out)))
        ids2 = sorted(set(re.findall(r'data-task="([0-9a-f]+)"', out2)))
        check("재생성해도 업무 ID 동일 → 체크 유지", ids1 == ids2 and len(ids1) >= 6,
              f"{len(ids1)}개")
        check("ID 는 내용 기반", task_id("<a1@t>", "me", "기획안 회신") in ids1)
        check("예전 브라우저 체크는 서버로 옮기는 코드만 남김",
              "nwmail.done.v1" in out and "input[data-task]" not in out)
        check("서버 주소가 아니면 프로젝트 할 일 버튼 · 체크 상자도 잠금",
              ".act [data-act],.tk [data-act],.tk .ck,.task [data-act]" in out)
        rows = re.findall(r'<li class="task[^>]*data-text="([^"]*)"[^>]*>(.*?)</li>', out, re.S)
        check("프로젝트 할 일 스냅샷 문구에 역할 태그가 섞이지 않음 (태그는 문구 밖 칸)",
              bool(rows) and all(f'<p class="t">{t}</p>' in li and "rtag" not in li[li.find('<p class="t">'):]
                                 for t, li in rows), f"{len(rows)}건")
        check("표시를 보낼 때 data-text 를 먼저 쓴다", "li.dataset.text||" in out)

        hdr("[6-1] ⋮ 메뉴 표시 — 답변완료 · 프로젝트 완료 · 삭제")
        s_mk = model_mk["stats"]
        t_mk = section(out_mk, 'id="tab-today"', ('id="tab-wait"',))
        w_mk = section(out_mk, 'id="tab-wait"', ('id="tab-projects"',))
        waits_mk = section(w_mk, 'id="waits"', ('</ul>',))
        handled_mk = section(w_mk, 'id="handled"', ('</details>',))
        check("답변완료한 메일은 기다리는 목록에서 빠지고 아래 목록으로",
              "신청페이지 수정" not in waits_mk and "신청페이지 수정" in handled_mk
              and 'data-wstate="answered"' in handled_mk
              and '<span class="st">09/11 답변완료</span>' in handled_mk, handled_mk[:300])
        check("표시한 뒤 새 메일이 오면 다시 기다리는 목록으로",
              any(u["thread"]["key"] == "<a1@t>" for u in model_mk["unanswered"]))
        check("미응답 숫자 · 머리글도 줄어듦",
              s_mk["unanswered"] == 1 and 'id="hl-wait">1<' in out_mk and 'id="kpi-wait">1<' in out_mk
              and 'id="wait-cnt">1<' in out_mk and 'id="handled-cnt">1<' in out_mk
              and 'data-tabcnt="tab-wait">1<' in out_mk)
        check("답변완료 목록은 되돌리기 버튼", 'data-wmark="open">되돌리기' in handled_mk
              and '<details class="handled" id="handled">' in w_mk)
        pj = {p["name"]: p for p in model_mk["projects"]}
        check("프로젝트 완료 → 완료 보기로", pj["KX볼트링크"]["pstate"] == "done"
              and 'data-project="KX볼트링크" data-pstate="done"' in out_mk)
        check("완료한 뒤 새 메일을 받으면 진행 중으로", pj["오로라 갤러리"]["pstate"] == "open")
        check("삭제는 새 메일이 와도 유지", pj["미분류"]["pstate"] == "deleted")
        sk_card = section(out_mk, 'data-project="KX볼트링크"', ('</summary>',))
        check("완료 카드는 처음 보기(진행 중)에서 숨김 + 완료 날짜",
              re.search(r'data-project="KX볼트링크"[^>]* hidden>', out_mk) is not None
              and '<span class="st">09/11 완료</span>' in sk_card)
        check("보기 숫자 (진행 중 · 완료 · 삭제)",
              s_mk["projects_done"] == 1 and s_mk["projects_deleted"] == 1
              and 'id="pcnt-done">1<' in out_mk and 'id="pcnt-deleted">1<' in out_mk
              and f'data-tabcnt="tab-projects">{s_mk["projects_open"] + s_mk["projects_delayed"]}<' in out_mk)
        check("진행 프로젝트 숫자에서 완료 · 삭제는 뺌",
              s_mk["named_projects"] == s["named_projects"] - 1)
        progs_mk = section(t_mk, '<div class="progs">', ('id="progs-empty"',))
        check("프로젝트별 진행에서 완료 프로젝트 줄은 숨김",
              re.search(r'class="prow" data-goto="KX볼트링크"[^>]* hidden>', progs_mk) is not None)
        client_sk = section(out_mk, '<section class="client" data-client="KX볼트링크"', ('>',))
        check("보이는 프로젝트가 없는 고객사 묶음은 숨김", client_sk.endswith(" hidden"), client_sk)
        check("되돌리면 다시 기다리는 목록 + 기록은 로그에 남음",
              any(u["thread"]["key"] == "<t2@t>" for u in reopened["unanswered"])
              and mark_log == ["answered", "deleted", "open"], str(mark_log))
        check("페이지 설정에 표시 이름 (답변완료 · 완료 · 삭제)",
              '"answered": "답변완료"' in out_mk and '"waitPreview": 6' in out_mk)
        check("서버 주소가 아니면 ⋮ 메뉴 · 색 막대 · 답변완료도 잠금", ".kb-btn,.proj-h>.bar,.wx [data-wmark]" in out)

        hdr("[7] 테마 · 접근성")
        check("라이트 기본 + 다크 두 경로(OS 설정 / 토글)",
              "prefers-color-scheme:dark" in out and ':root[data-theme="dark"]' in out)
        check("탭은 role=tab / tabpanel", 'role="tab"' in out and 'role="tabpanel"' in out)
        check("차단 표는 가로 스크롤 컨테이너 안", 'class="tbl-wrap"' in out)
        check("글꼴은 설치된 Pretendard 우선, 없으면 시스템 글꼴", "font:14px/1.5 Pretendard," in out
              and '"Malgun Gothic"' in out)
        check("파일로 연 브라우저의 예전 체크를 서버 주소로 옮겨 감",
              "#import=" in out and "nwmail.doneAt.v1" in out and "api/ping" in out)
        odd = render_html(model, server={"port": 1, "token": "</script><b>x"})
        check("설정 JSON 은 </script> 로 끊기지 않게 이스케이프",
              "</script><b>x" not in odd and "\\u003c/script>\\u003cb>x" in odd)

        def sit_rows(html: str) -> dict[str, str]:
            """일정 탭 할 일 줄: 문구 → 줄 HTML (여는 태그 포함)."""
            rows = {}
            for chunk in html.split('<li class="sit')[1:]:
                text = re.search(r'<p class="t">([^<]*)</p>', chunk)
                if text:
                    rows[text.group(1)] = chunk
            return rows

        def head(row: str) -> str:
            return row[:row.find(">")]

        hdr("[10] 일정 탭 — 마감 달력")
        tab_pos = [out.find(f'id="b-tab-{k}"') for k in ("today", "projects", "schedule", "blocked")]
        check("탭 순서: 오늘 → 프로젝트 → 일정 → 차단됨",
              -1 not in tab_pos and tab_pos == sorted(tab_pos), str(tab_pos))
        sch = section(out, 'id="tab-schedule"', ('id="tab-blocked"',))
        check("탭 숫자 = 기한 지남 + 오늘 + 7일 안 (남은 내 할 일)",
              s["sched_badge"] == 2 and 'data-tabcnt="tab-schedule">2<' in out)
        check("마감 요약 (기한 지남 1 · 오늘 1 · 7일 안 0 · 기한 없음 1)",
              'id="ss-overdue">1<' in sch and 'id="ss-today">1<' in sch
              and 'id="ss-week">0<' in sch and 'id="ss-none">1<' in sch)
        day_l = section(sch, 'id="sch-day"', ('id="sp-empty"',))
        none_l = section(sch, 'id="sch-none"', ('id="none-empty"',))
        rows = sit_rows(sch)
        order = [day_l.find(t) for t in ("예산 승인", "기획안 회신", "API 연동 개발", "배너 시안 제작")]
        check("기한 있는 할 일은 날짜순 (내 할 일 + 기한 있는 프로젝트 할 일)",
              -1 not in order and order == sorted(order), str(order))
        check("기한 없는 내 할 일은 '기한 없음' 목록", "참고 자료 확인" in none_l and "참고 자료 확인" not in day_l)
        check("기한 없는 프로젝트 할 일은 일정에 올리지 않음",
              "일정표 정리" not in sch and "문구 수정 반영" not in sch)
        check("달력은 줄의 날짜 · 종류 · 색으로 그림",
              'data-deadline="2026-09-13"' in head(rows["API 연동 개발"])
              and 'data-kind="task"' in head(rows["API 연동 개발"])
              and f'data-slot="{ps[re.search(r'data-pname="([^"]*)"', head(rows["기획안 회신"])).group(1)]}"'
              in head(rows["기획안 회신"]))
        check("처음엔 오늘 마감인 내 할 일만 보임 (프로젝트 할 일은 보기를 바꿔야)",
              not head(rows["기획안 회신"]).endswith(" hidden")
              and head(rows["예산 승인"]).endswith(" hidden")
              and head(rows["API 연동 개발"]).endswith(" hidden")
              and not head(rows["참고 자료 확인"]).endswith(" hidden"))
        check("줄마다 기한 칸(날짜 선택) + 완료 · 제외 · 업무삭제 버튼",
              'type="date" class="due-in" value="2026-09-11" min="2023-01-01" max="2029-12-31"'
              in rows["기획안 회신"] and 'type="date" class="due-in" value=""' in rows["참고 자료 확인"]
              and all('data-act="done">완료' in r for r in rows.values()))
        check("오늘 목록 머리말", "9월 11일 (금) · 오늘" in sch and 'id="sp-cnt">1건<' in sch)
        check("달력 자리 · 이번 달 제목 · 요일 머리줄",
              'id="cal-grid"' in sch and '<h2 id="cal-title" aria-live="polite">2026년 9월</h2>' in sch
              and "<span>월</span>" in sch and "<span>일</span></div>" in sch)
        check("페이지 설정에 서버 기준 오늘 · 공휴일",
              '"today": "2026-09-11"' in out and '"2026-09-25": "추석"' in out
              and '"2026-10-05": "대체공휴일"' in out)
        tips = re.findall(r' id="(s?tip-[0-9a-f]+)"', out)
        check("같은 할 일이 두 탭에 있어도 말풍선 id 는 겹치지 않음", len(tips) == len(set(tips)) and len(tips) > 3)
        check("서버 주소가 아니면 기한 칸 · 버튼도 잠금", ".sit [data-act],.due-in,[data-due-rev]" in out)
        check("달력 · 기한 저장 JS", "function drawCal()" in out and "send('/api/due'" in out
              and "applyDue(el.dataset.task,st,true)" in out)

        hdr("[10-1] 일정 — 기한 직접 정하기")
        sch_d = section(out_due, 'id="tab-schedule"', ('id="tab-blocked"',))
        rows_d = sit_rows(sch_d)
        plan = head(rows_d["기획안 회신"])
        check("직접 정한 기한이 메일 기한보다 우선 (메일 9/11 → 직접 9/20)",
              'data-deadline="2026-09-20"' in plan and 'data-mdl="2026-09-11"' in plan
              and 'data-manual="1"' in plan and 'value="2026-09-20"' in rows_d["기획안 회신"])
        check("정한 기한 옆에 메일 기한 + 되돌리기 버튼 + '직접 정함' 표시",
              "메일에 적힌 기한 9/11(금)" in rows_d["기획안 회신"] and "data-due-rev" in rows_d["기획안 회신"]
              and '<span class="man" title="일정 탭에서 직접 정한 기한">직접 정함</span>' in rows_d["기획안 회신"])
        check("메일에 기한이 없던 일은 '메일에 적힌 기한 없음'", "메일에 적힌 기한 없음" in rows_d["참고 자료 확인"])
        check("기한 없던 내 할 일에 기한을 정하면 달력으로 (기한 없음 목록에서 빠짐)",
              "참고 자료 확인" in section(sch_d, 'id="sch-day"', ('id="sp-empty"',))
              and "참고 자료 확인" not in section(sch_d, 'id="sch-none"', ('id="none-empty"',)))
        s_due = model_due["stats"]
        check("일정 요약 · 탭 숫자 (기한 지남 1 · 오늘 0 · 7일 안 1 · 기한 없음 0 → 탭 2)",
              (s_due["sched_overdue"], s_due["sched_today"], s_due["sched_week"], s_due["sched_none"],
               s_due["sched_badge"]) == (1, 0, 1, 0, 2) and 'data-tabcnt="tab-schedule">2<' in out_due,
              str((s_due["sched_overdue"], s_due["sched_today"], s_due["sched_week"], s_due["sched_none"])))
        open_due = section(section(out_due, 'id="tab-today"', ('id="tab-projects"',)),
                           'data-list="open"', ('<p class="empty-state" data-empty="open"',))
        order_due = [open_due.find(t) for t in ("예산 승인", "참고 자료 확인", "기획안 회신")]
        check("오늘 탭 줄도 같은 기한 · 그 마감순 (3일 안 · 그 이후)",
              'class="due lv-soon">D-3 · 9/14(월)' in open_due and "D-9 · 9/20(일)" in open_due
              and "오늘 마감" not in open_due and -1 not in order_due and order_due == sorted(order_due),
              str(order_due))
        check("숫자 칸도 같은 기한 (오늘 마감 0 · 지난 마감 1)",
              s_due["due_today"] == 0 and s_due["overdue"] == 1 and 'id="kpi-today">0<' in out_due)
        banner = section(out_due, 'data-project="오로라 스토어"', ('<details class="proj"', '<section class="client"'))
        banner_task = next((c for c in banner.split('<li class="task')[1:] if "배너 시안 제작" in c), "")
        check("프로젝트 탭 할 일도 같은 기한 (배너 시안 제작 9/30 → 직접 9/12 · D-1)",
              'data-deadline="2026-09-12"' in head(banner_task) and 'data-manual="1"' in head(banner_task)
              and "</i>D-1</span>" in banner_task, banner_task[:160])
        check("정할 때 문구 · 메일 기한을 기록에 남김",
              due_rows[ids["기획안 회신"]]["deadline"] == "2026-09-11"
              and due_rows[ids["참고 자료 확인"]]["text"] == "참고 자료 확인")
        back = {a["text"]: a for a in reverted["my_actions"]}["기획안 회신"]["deadline"]
        check("지우면 메일 기한으로 돌아감 + 기록은 로그에 남음",
              back["raw"] == "2026-09-11" and back["level"] == "today" and not back["manual"]
              and due_log == ["2026-09-20", ""], str(due_log))

        hdr("[10-2] 일정 — 최근 7일이 지난 대화")
        mine_old = {a["text"]: a for a in model_old["my_actions"]}
        check("기한 있는 남은 내 할 일은 창 밖 대화여도 남김 (오늘 탭 · 일정 탭)",
              "정산 자료 회신" in mine_old and mine_old["정산 자료 회신"]["thread"].get("stale")
              and "정산 자료 회신" in sit_rows(section(out_old, 'id="tab-schedule"', ('id="tab-blocked"',))))
        check("마감이 30일 넘게 지난 일 · 기한 없는 일은 뺌",
              "한참 지난 마감" not in out_old and "기한 없는 옛 일" not in out_old)
        today_old = section(out_old, 'id="tab-today"', ('id="tab-projects"',))
        check("오늘 탭 카드에 '지난 대화' 표시",
              "정산 자료 회신" in today_old and ">지난 대화</span>" in today_old)
        sch_o = section(out_old, 'id="tab-schedule"', ('id="tab-blocked"',))
        check("창 밖 대화의 기한 있는 프로젝트 할 일도 일정에 (기한 없는 건 뺌)",
              'data-kind="task"' in head(sit_rows(sch_o).get("정산표 검수", ""))
              and "기한 없는 옛 업무" not in out_old)
        check("창 밖 대화는 프로젝트 탭에 나오지 않음",
              "정산표 검수" not in section(out_old, 'id="tab-projects"', ('id="tab-schedule"',)))
        check("완료하면 남은 일에서 빠지고 완료 목록으로",
              "정산 자료 회신" not in {a["text"] for a in model_old_done["my_actions"]}
              and "정산 자료 회신" in {a["text"] for a in model_old_done["done_actions"]})
        check("프로젝트 완료가 새 메일로 풀렸으면 창 밖 대화의 프로젝트 할 일도 일정에",
              "정산표 검수" in sched_reopened and "API 연동 개발" in sched_reopened)
        check("삭제한 프로젝트의 할 일은 창 안 · 밖 모두 일정에서 뺌",
              "정산표 검수" not in sched_deleted and "API 연동 개발" not in sched_deleted
              and "기획안 회신" in sched_deleted)

        hdr("[11] 맨 아래 2depth — 메일 분류 규칙")
        from nwmail import usage as usage_mod
        from nwmail.projects import load_rules
        info = section(out, '<section class="info"', ("</main>",))
        check("탭 영역(카드) 아래에 붙어 어느 탭을 보든 보임",
              '</div><section class="info"' in out and out.find('<section class="info"') > out.find('id="tab-blocked"'))
        check("1depth 한 줄 (메일 분류 규칙), 처음엔 접힘",
              re.findall(r'data-info="(\w+)"', info) == ["rules"]
              and info.count('aria-expanded="false"') == 1
              and len(re.findall(r'class="info-b"[^>]*hidden>', info)) == 1)
        check("2depth 보기: 규칙 5, 첫 보기만 펼침",
              re.findall(r'data-sub="(\w+)"', info) == ["flow", "block", "proj", "task", "reply"]
              and re.findall(r'data-pane="(\w+)">', info) == ["flow"])
        check("토큰 사용 현황은 맨 아래가 아니라 따로 만든 페이지로 감",
              "토큰 사용 현황" not in info and 'data-pane="sum"' not in out)
        unknown = sum(th["basis"] == "unknown" for p in model["projects"] for th in p["threads"])
        check("접혀 있어도 보이는 요약 한 줄 (보호 도메인 · 규칙 · 세부 프로젝트 · 미분류)",
              f"보호 도메인 3 · 차단 규칙 1 · 세부 프로젝트 {len(load_rules().client_of)} · 최근 7일 미분류 "
              f"{unknown}건" in info and unknown == 1, str(unknown))
        groups = {k: section(info, f'<details class="ib-d ib-{k}">', ("</details>",))
                  for k in ("keyword", "model", "tag", "domain", "unknown")}
        counts = {k: int(re.search(r'class="ib-n num">(\d+)건', g).group(1)) for k, g in groups.items() if g}
        check("판정 근거별 대화 수의 합 = 최근 7일 대화 수", sum(counts.values()) == s["threads"], str(counts))
        check("근거마다 대화 제목 (키워드 · 태그 · 도메인 · 미분류)",
              "갤러리 티켓" in groups["keyword"] and "[누리마케팅] 신청페이지 수정" in groups["tag"]
              and "[NURI] 시그니처 연장" in groups["keyword"]           # '시그니처' 는 기타의 키워드
              and "신규 매장 오픈 고객 초청" in groups["domain"] and "주간 회의록" in groups["unknown"])
        check("세부 프로젝트 표: 키워드 · 회사 태그 · 도메인 (projects.json 그대로)",
              "키워드 AX9 멤버십 테니스" in info and "[NURI]" in info and "aurora.example" in info
              and "추출 제외: 일일 통계 데이터" in info)
        task_pane = section(info, 'data-pane="task"', ('data-pane="reply"',))
        pos = [task_pane.find(f"</span>{r}</span>") for r in ("수정", "개발", "디자인", "기타")]
        check("역할은 판정 순서대로 (수정 → 개발 → 디자인 → 기타)", -1 not in pos and pos == sorted(pos), str(pos))
        check("한눈에 보기 6단계 · 차단됨 탭으로 가는 링크",
              info.count('<li class="if">') == 6 and 'href="#blocked"' in info)
        check("펼친 줄 · 고른 보기는 브라우저에 기억 + 탭 링크",
              "var IK='nwmail.info'" in out and "addEventListener('hashchange'" in out)

        hdr("[11b] 토큰 사용 현황 — 따로 만든 페이지 (usage.html)")
        page0 = render_usage_html(model)
        check("대시보드 머리말에 테마 옆 버튼, 새 페이지로 감",
              '<div class="hero-acts">' in out and f'href="{USAGE_PAGE}"' in out
              and out.find('class="pill usage-link"') < out.find('id="theme"'))
        check("토큰 기록이 없으면 빈 상태 안내",
              "아직 기록 없음" in page0 and "아직 기록이 없습니다" in page0)
        check("파비콘은 두 페이지 모두에 (파일로 열어도 보이게 data URI)",
              out.count('<link rel="icon" href="data:image/svg+xml,') == 1
              and page0.count('<link rel="icon" href="data:image/svg+xml,') == 1)
        check("대시보드로 돌아가는 링크 · 테마 버튼은 같이 씀",
              '<a class="back" href="./">' in page0 and 'id="theme"' in page0
              and "nwmail.theme" in page0 and "nwmail.theme" in out)
        check("2depth 보기 3개, 첫 보기만 펼침 · 고른 보기는 주소와 브라우저에 남김",
              re.findall(r'data-sub="(\w+)"', page0) == ["sum", "where", "log"]
              and re.findall(r'data-pane="(\w+)">', page0) == ["sum"]
              and "nwmail.usageView" in page0)

        with Store(Path(tmp) / "d.db") as st:
            usage_mod.record_call(
                st, "extract", usage={"model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 500,
                                      "cache_read_tokens": 19000, "cache_write_tokens": 2000, "cost_usd": 0.25},
                seconds=80, threads=2, mails=2, at="2026-09-11T00:01:00+00:00",
                detail=[["<a1@t>", "[루미] <script>alert(1)</script> 기획 검토", "오로라 스토어", 3000, 1],
                        ["<gv@t>", "AX9 알림톡", "AX9 멤버십 테니스클럽", 1000, 1]])
            usage_mod.record_call(
                st, "extract", seconds=3, threads=1, mails=1, at="2026-09-10T04:00:00+00:00",
                error="ClaudeCliError: claude CLI 실패 (exit 1): You've hit your session limit · resets 1:30pm")
            st.conn.execute("UPDATE llm_calls SET trigger = 'auto'")
            st.conn.commit()
            usage_mod.record_call(st, "verify", usage={"model": "claude-haiku-4-5", "input_tokens": 537,
                                                       "output_tokens": 73, "cache_write_tokens": 18650},
                                  at="2026-09-11T02:00:00+00:00")
            model_u = build_model(st, me=ME, today=TODAY, now=NOW)
        info_u = render_usage_html(model_u)
        check("요약 줄: 오늘 호출 · 토큰 · 최근 7일 · 한도 초과",
              "오늘 호출 2회 · 41.8K 토큰 — 최근 7일 41.8K 토큰" in info_u and "한도 초과 1회" in info_u)
        sum_pane = section(info_u, 'data-pane="sum"', ('data-pane="where"',))
        check("사용량 칸 넷: 오늘 · 최근 7일 · 이번 달 · 누적", sum_pane.count('class="itile"') == 4
              and '<p class="it-l">이번 달 (9월)</p>' in sum_pane and '<p class="it-l">누적</p>' in sum_pane)
        check("칸마다 증가량 한 줄 (견준 기간 이름과 같이)",
              sum_pane.count('<p class="it-g">') == 4 and "어제 기록 없음" in sum_pane
              and "지난 7일 기록 없음" in sum_pane and "지난달 기록 없음" in sum_pane
              and "하루 평균 20.9K · 2일째" in sum_pane)
        check("일별 7줄, 한도 초과한 날 표시",
              len(re.findall(r'<span class="ib-l">\d\d/\d\d ', sum_pane)) == 7
              and '09/10 목 <span class="itag bad">한도 초과</span>' in sum_pane)
        check("일별 막대에도 하루 전보다 늘고 준 양", '<span class="it-d up">▲ 41.8K</span>' in sum_pane)
        check("회차별: 자동 09시 · 13시 · 17시", all(f'<span class="ib-l">{h}시</span>' in sum_pane
                                               for h in ("09", "13", "17")))
        where = section(info_u, 'data-pane="where"', ('data-pane="log"',))
        check("호출마다 붙는 기본 지시문 = 확인 호출 입력", "약 19.2K 토큰" in where)
        check("무거운 대화: 글자 수 비율로 나눈 추정 토큰 (3:1)", "16.9K" in where and "5.6K" in where)
        check("대화 제목은 이스케이프", "<script>alert(1)" not in info_u and "&lt;script&gt;alert(1)" in info_u)
        log_pane = section(info_u, 'data-pane="log"', ("</main>",))
        check("호출 기록: 최신순 · 자동 회차 · 한도 초과 풀리는 시각",
              log_pane.count("<tr><td") == 3 and log_pane.find("토큰 확인") < log_pane.find("자동 09시")
              and "한도 초과 · 13:30 풀림" in log_pane)
        check("대시보드 버튼에도 오늘 쓴 양", 'class="pill-n num">오늘 41.8K' in render_html(model_u))
        # 늘고 · 줄고 · 그대로는 위 기록만으로 다 나오지 않아 계산 · 문구를 따로 본다
        from nwmail.dashboard import _delta_text
        from nwmail.usage import delta
        up = delta({"tokens": 3000, "calls": 3, "cost": 0.3}, {"tokens": 2000, "calls": 1, "cost": 0.1})
        down = delta({"tokens": 500, "calls": 1, "cost": 0.1}, {"tokens": 2000, "calls": 4, "cost": 0.4})
        check("증가량 계산: 늘면 up · 얼마 · 몇 %", (up["dir"], up["tokens"], up["calls"]) == ("up", 1000, 2)
              and abs(up["pct"] - 0.5) < 1e-9, str(up))
        check("증가량 문구: ▲ 늘어난 양 · 퍼센트 · 견준 기간",
              '<span class="it-d up" title="어제 2K 토큰">▲ 1K · +50%<span class="it-w"> 어제보다</span>'
              in _delta_text(up, "어제"), _delta_text(up, "어제"))
        check("줄면 down (토큰은 적게 쓸수록 좋아 다른 색)",
              down["dir"] == "down" and '"it-d down"' in _delta_text(down, "어제")
              and "▼ 1.5K · -75%" in _delta_text(down, "어제"), _delta_text(down, "어제"))
        same = delta({"tokens": 2000, "calls": 2, "cost": 0.2}, {"tokens": 2000, "calls": 2, "cost": 0.2})
        check("그대로면 화살표 없이 같다고만", same["dir"] == "flat"
              and "어제와 같음" in _delta_text(same, "어제"), _delta_text(same, "어제"))
        none = delta({"tokens": 1000, "calls": 1, "cost": 0.1}, {"tokens": 0, "calls": 0, "cost": 0})
        check("이전 기간이 0 이면 몇 % 인지 말하지 않는다",
              none["dir"] == "new" and none["pct"] is None
              and "지난달 기록 없음" in _delta_text(none, "지난달"))

    hdr("[6-2] 문구가 바뀐 할 일 — 완료 표시 이어붙이기")
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "m.db") as st, FeedbackStore(Path(tmp) / "f.db") as fb:
            st.save_mails([
                mail(1, "[루미] 가을 시즌 이미지", "pm@lumi.example", "<r1@t>", to=[ME],
                     when="2026-09-08T09:00:00+09:00"),
                mail(2, "[루미] 코트 이용권", "pm@lumi.example", "<r2@t>", to=[ME],
                     when="2026-09-08T09:00:00+09:00"),
                mail(3, "RE: [루미] 코트 이용권", "pm@lumi.example", "<r3@t>", to=[ME],
                     refs=["<r2@t>"], when="2026-09-10T15:00:00+09:00"),
            ], me=ME)
            # 표시한 뒤 새 메일이 없는 대화: 문구만 바뀐 것이므로 표시를 옮겨야 한다
            kept_old = "가을 시즌 이미지를 갤러리 PC/MO 페이지에 반영"
            kept_new = "한도준 책임이 전달한 가을 시즌 이미지를 갤러리 PC/MO 페이지에 반영"
            # 표시한 뒤 새 메일이 온 대화: 다시 요청받은 일일 수 있어 옮기지 않는다
            again_old = "코트 이용권 가격표 PC/MO 시안 확인"
            again_new = "가격 수정된 코트 이용권 가격표 PC/MO 시안 확인"
            put_extraction(st, "<r1@t>", {"project": "루미", "summary": "s", "status": "진행중",
                                          "my_actions": [{"text": kept_new, "urgency": "medium"}]})
            put_extraction(st, "<r2@t>", {"project": "루미", "summary": "s", "status": "진행중",
                                          "my_actions": [{"text": again_new, "urgency": "medium"}]})
            kept_id = task_id("<r1@t>", "me", kept_old)
            again_id = task_id("<r2@t>", "me", again_old)
            for tid, key, text, at in ((kept_id, "<r1@t>", kept_old, "2026-09-10T00:00"),
                                       (again_id, "<r2@t>", again_old, "2026-09-09T00:00")):
                fb.apply(tid, "done", snapshot={"text": text, "thread_key": key, "kind": "me"},
                         now=datetime.fromisoformat(at).replace(tzinfo=timezone.utc))

            m6 = build_model(st, ME, today=TODAY, now=NOW, feedback=fb)
            texts6 = [a["text"] for a in m6["my_actions"]]
            states6 = fb.states()
            check("표시 뒤 새 메일이 없으면 문구가 바뀌어도 남은 일로 돌아오지 않음",
                  kept_new not in texts6, str(texts6))
            check("표시를 새 ID 로 옮김",
                  task_id("<r1@t>", "me", kept_new) in states6 and kept_id not in states6)
            check("완료 목록에는 바뀐 문구로 남음",
                  any(d["text"] == kept_new for d in m6["done_actions"]))
            check("표시 뒤 새 메일이 온 대화는 표시를 옮기지 않음",
                  again_new in texts6 and again_id in states6, str(texts6))
            check("대신 '전에 처리한 일과 비슷' 으로만 알림",
                  any(a["text"] == again_new and a.get("similar") for a in m6["my_actions"]))
            out6 = render_html(m6)
            check("화면에도 비슷하다는 표시가 붙음", "전에 완료한 일과 비슷" in out6)
            check("옮긴 기록을 남김 (reason 에 이전 ID)",
                  [r["reason"] for r in fb.conn.execute(
                      "SELECT reason FROM feedback_log WHERE action = 'moved'")] == [kept_id])

    hdr("[8] 업무명 정규화 (실제 제목)")
    from nwmail.projects import title_key, work_title
    cases = [
        ("RE: [RE][누리마케팅] VX 프레스티지, 시그니처 스토어 신청페이지 수정 관련 "
         "이미지 전달의 건_20260903",
         "VX 프레스티지, 시그니처 스토어 신청페이지 수정 관련 이미지 전달"),
        ("RE: [NURI] 시그니처 신청 기한 연장 요청의 건_홍○동 고객",
         "시그니처 신청 기한 연장 요청"),
        ("FW: [오로라갤러리] 닷컴 & 스토어 가을시즌 업데이트 요청 (9/8 런칭)",
         "닷컴 & 스토어 가을시즌 업데이트 요청"),
        ("FW: ['26 전사 워크숍] 참석자 명단 회신 요청 (~8/7(금) 10:00)",
         "참석자 명단 회신 요청"),
        ("[루미] 기프트 옵션 일원화에 따른 지점 기프트 현장 지급 매핑 불가 건",
         "기프트 옵션 일원화에 따른 지점 기프트 현장 지급 매핑 불가"),
        ("RE: RE: RE: 스토어몰 굿즈 판매 입점 관련 논의의 건", "스토어몰 굿즈 판매 입점 관련 논의"),
        ("RE: PLCC 3.0 배너 및 페이지 초안 전달드립니다.", "PLCC 3.0 배너 및 페이지 초안 전달드립니다"),
        ("사건 보고", "사건 보고"),
        ("[공지]", "[공지]"),
    ]
    for raw, want in cases:
        got = work_title(raw)
        check(f"{raw[:34]}… -> {want[:20]}", got == want, got)
    check("고객만 다른 제목은 같은 키",
          title_key(work_title("[NURI] 시그니처 신청 기한 연장 요청의 건_홍○동 고객"))
          == title_key(work_title("RE: [NURI] 시그니처 신청 기한 연장 요청의 건_홍○순 고객")))
    check("다른 업무는 다른 키",
          title_key(work_title("[NURI] EV 웰컴 쿠폰 신청 기한 연장 요청의 건_최○영 고객"))
          != title_key(work_title("[NURI] 시그니처 신청 기한 연장 요청의 건_홍○동 고객")))

    hdr("[8-1] 세부 프로젝트 분류 (실제 제목 · projects.json)")
    from nwmail.projects import classify
    G = "오로라"
    cases = [
        (("[누리마케팅] AX9  멤버십 테니스 클럽 레슨_알림톡 문구 변경 요청의 건", "jh@nurimktg.co.example"),
         (G, "AX9 멤버십 테니스클럽", "keyword")),
        (("FW: [요청] 오로라 포럼 - 2026 가을 음악회 초청공연 (10/25)",),
         (G, "오로라 포럼", "keyword")),
        (("RE: 오로라 한결카드 (오로라 PLCC 3.0) 카카오톡 안내 알림톡 발송 요청 건 (9/8)",),
         (G, "오로라 한결카드 PLCC", "keyword")),
        (("RE: [문의] 오로라 스토어 오클 티켓 프로모션 관련 (회원 전용 상품)",),
         (G, "오로라 클래식", "keyword")),                 # 위에 적힌 프로젝트가 먼저
        (("RE: RE: 스토어몰 굿즈 판매 입점 관련 논의의 건",), (G, "오로라 스토어", "keyword")),
        (("[누리마케팅] VX 오로라 CRM 리버앤우드 신청관리 노출 확인 필요의건_20260916",),
         (G, "오로라 운영(기타)", "keyword")),               # 애매한 주제는 기타
        (("[루미] 케어플러스 중도해지시 포인트 적립 처리 방안 건",), (G, "오로라 스토어", "tag")),
        (("RE: [NURI] CRM 화면_잔여 쿠폰 회차 삭제 요청의 건_박○수 고객",),
         (G, "오로라 운영(기타)", "tag")),
        (("사내 메신저 계정 발급 현황 문의", "a@lumi.example", "루미"), (G, "오로라 스토어", "model")),
        (("[루미] 추석 배송 일정", "a@lumi.example", "오로라 운영(기타)"), (G, "오로라 스토어", "tag")),
        (("제휴 문의", "", "PLCC 3.0"), (G, "오로라 한결카드 PLCC", "model")),
        (("갤러리 예약화면 문구 변경", "", "오로라 스토어"), (G, "오로라 갤러리", "keyword")),
        (("CRM 권한설정 점검 주기 문의", "", "'26 전사 워크숍"), ("", "'26 전사 워크숍", "model")),
        (("[잡스테이션] HR 인사이트 리포트", "m@jobstation.example"), ("", "잡스테이션", "tag")),
        (("신규 매장 오픈 고객 초청 이벤트", "k@aurora.example"), (G, "오로라 운영(기타)", "domain")),
        (("[KX볼트링크] API 확인", "k@voltlink.example"), ("KX볼트링크", "KX볼트링크", "tag")),
        (("주간 회의록", "c@lumi.example"), ("", "", "unknown")),
    ]
    for args, want in cases:
        got = classify(*args)
        check(f"{args[0][:30]}… -> {want[1] or '미분류'} ({want[2]})", got == want, str(got))
    cases = [
        ("[누리마케팅] AX9  멤버십 테니스 클럽 레슨_알림톡 문구 변경 요청의 건", "AX9 멤버십 테니스클럽",
         "알림톡 문구 변경 요청"),
        ("RE: 오로라 한결카드 (오로라 PLCC 3.0) 카카오톡 안내 알림톡 발송 요청 건 (9/8)",
         "오로라 한결카드 PLCC", "카카오톡 안내 알림톡 발송 요청"),
        ("FW: [요청] 오로라 포럼 - 2026 가을 음악회 초청공연 (10/25)", "오로라 포럼",
         "2026 가을 음악회 초청공연"),
        ("[루미] 오로라 클래식 가격표 작업 요청의 건", "오로라 클래식", "가격표 작업 요청"),
        ("RE: 스토어몰 굿즈 판매 입점 관련 논의의 건", "오로라 스토어",
         "스토어몰 굿즈 판매 입점 관련 논의"),                 # 이어진 낱말은 떼지 않음
        ("오로라 썬더 레이싱 시네마 워치파티 수정 요청", "오로라 운영(기타)",
         "오로라 썬더 레이싱 시네마 워치파티 수정 요청"),      # 기타의 키워드는 업무 주제라 남김
        ("[KX볼트링크] API 확인", "KX볼트링크", "API 확인"),
        ("오로라 포럼", "오로라 포럼", "오로라 포럼"),          # 다 떼면 빈 제목 → 그대로
    ]
    for raw, project, want in cases:
        got = work_title(raw, project)
        check(f"업무명({project}) {raw[:24]}… -> {want[:18]}", got == want, got)

    hdr("[9] 미리보기 정리 — 인용·서명·전화번호")
    from nwmail.dashboard import snippet
    s1 = snippet("확인했습니다. 운영 반영 부탁드립니다.\n감사합니다.\n윤서진 드림\n윤서진\n"
                 "디지털 운영팀 / 선임\nMobile +82 10 0000 0000\n\n"
                 "From: 정다온 <daon@lumi.example>\nSent: Thursday\n이전 메일 내용")
    check("본문 핵심은 유지", "운영 반영 부탁드립니다" in s1, s1)
    check("서명(소속·연락처) 제거", "디지털 운영팀" not in s1 and "Mobile" not in s1)
    check("인용된 이전 메일 제거", "From:" not in s1 and "이전 메일" not in s1)
    s2 = snippet("From: a@b.com\nSent: Mon\nTo: c@d.com\n\n전달드립니다. 010-1234-5678 로 "
                 "연락주세요.\n\n-----Original Message-----\n옛날 내용")
    check("맨 위 전달 헤더 블록 제거", not s2.startswith("From"), s2)
    check("휴대폰 번호 마스킹", "010-1234-5678" not in s2 and "[전화번호]" in s2)
    check("Original Message 이후 제거", "옛날 내용" not in s2)

    from nwmail.mailtext import preview_text
    p1 = preview_text("안녕하세요, 주임님.누리마케팅 서나윤 대리입니다.링크 공유 감사합니다.감사합니다."
                      "VIP사업부 대리│ 서나윤T070-4010-2507│E-mail : cl.kim@nurimktg.co.example"
                      "----- Original Message -----From : 홍길동 <gdhong@lumi.example>To : 서나윤 "
                      "<cl.kim@nurimktg.co.example>Subject : 링크 전달<mailplughead></mailplughtml>이전 내용")
    check("전체 보기: 줄바꿈 없이 온 본문도 인용(Original Message · From · To) 제거",
          "From" not in p1 and "To :" not in p1 and "이전 내용" not in p1, p1)
    check("전체 보기: 문장 끝에서 줄을 나눔",
          p1.startswith("안녕하세요, 주임님.\n누리마케팅 서나윤 대리입니다.\n"), p1)
    check("전체 보기: 맺음 인사 뒤 연락처 서명 제거",
          p1.endswith("감사합니다.") and "VIP사업부" not in p1 and "nurimktg" not in p1, p1)
    p2 = preview_text("From: a@b.com\nTo: c@d.com\n\n검토 부탁드립니다.\n\n- 개발계 (링크)\n- 운영계 (링크)\n"
                      "<xxmailplughead></xxmailplughtml>\n원본")
    check("전체 보기: 줄바꿈 유지, 메일플러그 태그 없음",
          p2 == "검토 부탁드립니다.\n\n- 개발계 (링크)\n- 운영계 (링크)\n원본", p2)
    p3 = preview_text("지점 현황 공유드립니다.\n" + "-" * 40 + "\n[서울] 금천점, 송파점\n[경기] 기흥점")
    check("전체 보기: 구분선 아래가 본문(목록)이면 자르지 않음", p3.endswith("[경기] 기흥점"), p3)
    check("전체 보기: 주소·소수는 나누지 않음",
          preview_text("www.nurimktg.co.example 에서 v1.5 확인") == "www.nurimktg.co.example 에서 v1.5 확인")

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
