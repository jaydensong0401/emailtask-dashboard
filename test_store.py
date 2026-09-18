"""1~2단계 검증: 저장소 · 스레드 묶기 · 증분 수집 · 필터 · 최근 7일 기준.

실제 메일함 없이 합성 데이터로 끝까지 검증한다.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from nwmail.client import Mail
from nwmail.filters import (
    add_filter, load_filters, reapply, remove_filter, suggest_filters,
)
from nwmail.store import Store, normalize_subject, thread_key_for, to_utc_iso

ME = "gdhong@lumi.example"
NOW = datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)      # 창 시작: 9/4 00:00 KST
ok = total = 0


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def mail(uid, subject, sender, to=(), cc=(), mid="", irt="", refs=(),
         unsub=False, body="본문", when="2026-09-10T09:00:00+09:00", folder="") -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                received_time=when, status="Unread", body=body,
                to=list(to), cc=list(cc), message_id=mid, in_reply_to=irt,
                references=list(refs), list_unsubscribe=unsub, folder=folder)


def main() -> int:
    hdr("[1] 제목 정규화 (스레드 묶기 기반)")
    cases = [
        ("RE: 계약서 검토", "계약서 검토"),
        ("Re: RE: 계약서 검토", "계약서 검토"),
        ("FW: 계약서 검토", "계약서 검토"),
        ("답장: 계약서 검토", "계약서 검토"),
        ("전달: 계약서   검토", "계약서 검토"),
        ("[RE][누리마케팅] VX 검토", "[누리마케팅] vx 검토"),
        ("계약서 검토", "계약서 검토"),
    ]
    for raw, want in cases:
        got = normalize_subject(raw)
        check(f"{raw!r} -> {want!r}", got == want, got)

    hdr("[2] 스레드 키 — 헤더 체인 우선")
    root = mail(1, "[알파] 기획 리뷰", "pm@co.kr", mid="<a1@co.kr>")
    reply = mail(2, "RE: [알파] 기획 리뷰", "dev@co.kr", mid="<a2@co.kr>",
                 irt="<a1@co.kr>", refs=["<a1@co.kr>"])
    reply2 = mail(3, "RE: RE: [알파] 기획 리뷰", "design@co.kr", mid="<a3@co.kr>",
                  irt="<a2@co.kr>", refs=["<a1@co.kr>", "<a2@co.kr>"])
    check("References 뿌리로 묶임",
          thread_key_for(reply) == thread_key_for(reply2) == "<a1@co.kr>")

    hdr("[3] 스레드 키 — 헤더 없으면 제목으로 묶임")
    n1 = mail(10, "월간 보고", "a@co.kr")
    n2 = mail(11, "RE: 월간 보고", "b@co.kr")
    check("제목 기반 묶임", thread_key_for(n1) == thread_key_for(n2),
          thread_key_for(n1))
    n3 = mail(12, "주간 보고", "c@co.kr")
    check("다른 건은 안 묶임", thread_key_for(n1) != thread_key_for(n3))

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"

        hdr("[4] 저장 · 직접수신 판별 · 증분 수집")
        with Store(db) as st:
            batch = [
                root, reply, reply2,
                mail(4, "[베타] 디자인 시안", "design@co.kr", to=[ME], mid="<b1@co.kr>"),
                mail(5, "전사 공지", "hr@co.kr", to=["all@co.kr"], mid="<c1@co.kr>"),
                mail(6, "[광고] 세미나 초대", "noreply@promo.com",
                     to=["all@co.kr"], unsub=True, mid="<ad@co.kr>"),
            ]
            r = st.save_mails(batch, me=ME)
            check("6통 저장", r.inserted == 6, f"inserted={r.inserted}")

            r2 = st.save_mails(batch, me=ME)
            check("재실행 시 중복 저장 안 됨", r2.inserted == 0 and r2.skipped == 6)

            known = st.known_uids("INBOX")
            check("받은메일함 UID 로 증분 가능", known == {"1", "2", "3", "4", "5", "6"},
                  str(sorted(known)))
            check("저장 키는 '메일함:UID'", "INBOX:4" in st.known_keys())

            th = st.threads(7, now=NOW)
            keys = {t["thread_key"]: t for t in th}
            check("스레드 3+1 개로 묶임", len(th) == 4, f"{len(th)}개")
            check("알파 스레드에 3통", keys["<a1@co.kr>"]["mail_count"] == 3)

            direct = [t for t in th if t["is_direct"]]
            check("직접수신 스레드만 1개", len(direct) == 1,
                  direct[0]["subject"] if direct else "없음")

            s = st.stats(7, now=NOW)
            check("stats.direct = 1", s["direct"] == 1, str(s["direct"]))

        hdr("[5] 필터 — 차단은 삭제가 아니다")
        with Store(db) as st:
            fid = add_filter(st, "subject", "[광고]", "광고 메일")
            check("규칙 등록", fid > 0, f"id={fid}")

            res = reapply(st)
            check("기존 메일에 소급 적용", res["blocked"] == 1, str(res))

            blocked = st.blocked_mails(7, now=NOW)
            check("차단 목록에서 조회 가능", len(blocked) == 1,
                  blocked[0]["subject"] if blocked else "없음")
            check("차단 사유(규칙 패턴) 함께 보임",
                  blocked and blocked[0]["filter_pattern"] == "[광고]")

            check("차단 메일은 스레드 목록에서 빠짐",
                  all("[광고]" not in t["subject"] for t in st.threads(7, now=NOW)))
            check("include_blocked=True 면 다시 보임",
                  any("[광고]" in t["subject"]
                      for t in st.threads(7, now=NOW, include_blocked=True)))

            s = st.stats(7, now=NOW)
            check("메일 자체는 삭제 안 됨", s["total"] == 6, f"total={s['total']}")

        hdr("[6] 필터 삭제 시 메일 복구")
        with Store(db) as st:
            fid = load_filters(st)[0].id
            remove_filter(st, fid)
            check("규칙 삭제됨", not load_filters(st))
            check("차단 해제됨", not st.blocked_mails(7, now=NOW))
            check("스레드 목록에 복귀",
                  any("[광고]" in t["subject"] for t in st.threads(7, now=NOW)))

        hdr("[7] 자동 차단 후보 제안")
        with Store(db) as st:
            promo = [mail(100 + i, f"[홍보] 이벤트 {i}", "marketing@promo.com",
                          to=["all@co.kr"], unsub=True, mid=f"<p{i}@x>") for i in range(4)]
            # 오탐 방지 확인용: 제목에 본업 용어가 있지만 광고가 아닌 메일들
            work = [
                *(mail(200 + i, f"[루미] 프로모션 이벤트 기획 {i}", "pm@lumi.example",
                       mid=f"<e{i}@x>") for i in range(3)),               # 우리 회사
                *(mail(210 + i, f"프로모션 이벤트 공유 {i}", "x@aurora.example",
                       mid=f"<g{i}@x>") for i in range(2)),               # 프로젝트 거래처
                *(mail(220 + i, f"이벤트 일정 {i}", "c@partner.io",
                       mid=f"<k{i}@x>") for i in range(3)),               # 제목 키워드뿐
            ]
            news = [mail(230, "Weekly digest", "a@news.example", unsub=True, mid="<n1@x>"),
                    mail(231, "Product update", "b@news.example", unsub=True, mid="<n2@x>")]
            st.save_mails(promo + work + news, me=ME)
            cands = suggest_filters(st, me=ME)
            pats = [c["pattern"] for c in cands]
            hit = [c for c in cands if c["pattern"] == "promo.com" and c["kind"] == "domain"]
            check("반복 광고 발신처를 후보로 제안 (같은 도메인은 도메인 규칙으로)", bool(hit), str(pats))
            check("근거 · 구분 제시", hit and len(hit[0]["reasons"]) >= 2
                  and hit[0]["category"] == "뉴스레터·구독",
                  ", ".join(hit[0]["reasons"]) if hit else "")
            check("여러 주소로 오는 뉴스레터는 도메인 단위", "news.example" in pats)
            check("우리 회사 메일은 제안 안 함 (프로모션·이벤트 제목이어도)",
                  not any("lumi.example" in p for p in pats))
            check("프로젝트 거래처는 제안 안 함", not any("aurora.example" in p for p in pats))
            check("제목 키워드만으로는 제안 안 함", not any("partner.io" in p for p in pats))
            check("자동 차단은 하지 않음",
                  not st.blocked_mails(7, now=NOW), "제안만 하고 차단 안 함")

        hdr("[8] 미응답 추적 — 내가 보낸 메일 기준")
        with Store(db) as st:
            th = {t["thread_key"]: t for t in st.threads(7, now=NOW)}
            check("직접 받은 디자인 시안은 미응답", th["<b1@co.kr>"]["awaiting_my_reply"])
            st.save_mails([mail(9, "RE: [베타] 디자인 시안", ME, to=["design@co.kr"],
                                mid="<mine1@co.kr>", refs=["<b1@co.kr>"], irt="<b1@co.kr>",
                                when="2026-09-10T11:00:00+09:00", folder="Sent")], me=ME)
            th = {t["thread_key"]: t for t in st.threads(7, now=NOW)}
            b = th["<b1@co.kr>"]
            check("내 답장이 대화에 들어감 (2통)", b["mail_count"] == 2)
            check("답한 뒤에는 미응답 아님, 상대 차례",
                  not b["awaiting_my_reply"] and b["replied_by_me"])
            check("내 메일은 차단·직접수신 계산에서 제외",
                  all(not m["is_direct"] for m in b["mails"] if m["is_mine"]))
            st.save_mails([mail(13, "RE: RE: [베타] 디자인 시안", "design@co.kr", to=[ME],
                                mid="<b3@co.kr>", refs=["<b1@co.kr>", "<mine1@co.kr>"],
                                irt="<mine1@co.kr>", when="2026-09-10T15:00:00+09:00")], me=ME)
            b = {t["thread_key"]: t for t in st.threads(7, now=NOW)}["<b1@co.kr>"]
            check("그 뒤 새 메일이 오면 다시 미응답", b["awaiting_my_reply"] and not b["replied_by_me"])

        hdr("[9] 최근 7일 창")
        with Store(db) as st:
            st.save_mails([
                mail(300, "[감마] 지난달 논의", "old@co.kr", to=[ME], mid="<o1@co.kr>",
                     when="2026-08-01T10:00:00+09:00"),
                mail(301, "RE: [감마] 지난달 논의", "old@co.kr", to=[ME], mid="<o2@co.kr>",
                     refs=["<o1@co.kr>"], when="2026-09-03T23:30:00+09:00"),
            ], me=ME)
            active = {t["thread_key"] for t in st.threads(7, now=NOW)}
            check("최근 7일 안에 받은 메일이 없는 대화는 빠짐", "<o1@co.kr>" not in active)
            check("창 시작(9/4 00:00 KST) 직전 메일도 빠짐 (9/3 23:30)", "<o1@co.kr>" not in active)
            picked = st.threads(7, now=NOW, keys=["<o1@co.kr>", "<o1@co.kr>", "<none@co.kr>"])
            check("대화 키를 주면 창 밖 대화도 (중복 · 없는 키는 무시)",
                  [t["thread_key"] for t in picked] == ["<o1@co.kr>"]
                  and picked[0]["mail_count"] == 2 and picked[0]["recent_count"] == 0,
                  str([(t["thread_key"], t["mail_count"], t["recent_count"]) for t in picked]))
            st.save_mails([mail(302, "RE: RE: [감마] 지난달 논의", "old@co.kr", to=[ME],
                                mid="<o3@co.kr>", refs=["<o1@co.kr>", "<o2@co.kr>"],
                                when="2026-09-04T00:30:00+09:00")], me=ME)
            g = {t["thread_key"]: t for t in st.threads(7, now=NOW)}.get("<o1@co.kr>")
            check("새 메일이 오면 과거 메일까지 통째로 포함",
                  g and g["mail_count"] == 3 and g["recent_count"] == 1,
                  f"전체 {g['mail_count']} / 최근 {g['recent_count']}" if g else "없음")
            only_mine = mail(303, "보고서 송부", ME, to=["boss@co.kr"], mid="<s1@co.kr>",
                             folder="Sent")
            st.save_mails([only_mine], me=ME)
            check("내가 보내기만 한 대화는 활성 아님",
                  "<s1@co.kr>" not in {t["thread_key"] for t in st.threads(7, now=NOW)})

        hdr("[10] 흔한 제목의 서로 다른 대화는 합치지 않음")
        with Store(db) as st:
            st.save_mails([
                mail(400, "RE: 문의", "a@x.com", mid="<q1r@x>", refs=["<q1@x>"], irt="<q1@x>"),
                mail(401, "RE: 문의", "b@y.com", mid="<q2r@y>", refs=["<q2@y>"], irt="<q2@y>"),
            ], me=ME)
            keys = {t["thread_key"] for t in st.threads(7, now=NOW) if t["subject"] == "RE: 문의"}
            check("답장 헤더가 다르면 별개 대화", keys == {"<q1@x>", "<q2@y>"}, str(keys))

        hdr("[11] 같은 메일이 여러 메일함에 있으면 한 번만")
        with Store(db) as st:
            cc_me = mail(500, "공지 초안", ME, to=["team@co.kr"], cc=[ME], mid="<dup@co.kr>")
            st.save_mails([cc_me], me=ME)
            st.save_mails([mail(8, "공지 초안", ME, to=["team@co.kr"], cc=[ME],
                                mid="<dup@co.kr>", folder="Sent")], me=ME)
            n = st.conn.execute("SELECT COUNT(*) FROM mails WHERE message_id = '<dup@co.kr>'"
                                ).fetchone()[0]
            check("Message-ID 가 같으면 중복 저장 안 함", n == 1, f"{n}통")

        hdr("[12] 시각은 UTC 로 통일 (서버마다 시간대가 다름)")
        check("+09:00 → UTC", to_utc_iso("2026-09-13T20:00:00+09:00") == "2026-09-13T11:00:00+00:00")
        check("-04:00 → UTC", to_utc_iso("2026-09-13T08:00:00-04:00") == "2026-09-13T12:00:00+00:00")
        with Store(db) as st:
            st.save_mails([
                mail(600, "[델타] 해외 파트너", "us@partner.com", to=[ME], mid="<t1@z>",
                     when="2026-09-10T08:00:00-04:00"),             # 12:00 UTC
                mail(601, "RE: [델타] 해외 파트너", "kr@co.kr", to=[ME], mid="<t2@z>",
                     refs=["<t1@z>"], when="2026-09-10T20:00:00+09:00"),   # 11:00 UTC
            ], me=ME)
            d = {t["thread_key"]: t for t in st.threads(7, now=NOW)}["<t1@z>"]
            check("실제 시각 순서로 정렬 (문자열 순서가 아니라)",
                  [m["uid"] for m in d["mails"]] == ["INBOX:601", "INBOX:600"],
                  str([m["uid"] for m in d["mails"]]))

        hdr("[13] 예전 형식 데이터 감지")
        with Store(db) as st:
            check("현재 형식이면 재수집 불필요", not st.needs_rebuild())
            st.conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
            st.conn.commit()
            check("형식 표시가 없고 메일이 있으면 재수집 필요", st.needs_rebuild())
            filters_before = len(load_filters(st, enabled_only=False))
            st.reset_mails()
            check("비우면 최신 형식으로 표시", not st.needs_rebuild())
            check("차단 규칙은 유지", len(load_filters(st, enabled_only=False)) == filters_before)

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
