"""차단 검증: 보호 도메인 · 규칙 종류 · 차단 후보 · 차단됨 탭 변경(API) · 화면.

보호 도메인(lumi.example · aurora.example · nurimktg.co.example)은 어떤 경로로도 차단되면 안 된다.
실제 메일함 · LLM 불필요. 서버는 임시 포트로 실제 HTTP 요청을 보낸다.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from nwmail import filter_api
from nwmail.client import Mail
from nwmail.dashboard import _blocked_tab, build_model, render_html
from nwmail.feedback import FeedbackStore
from nwmail.filters import (
    PROTECTED_DOMAINS, FilterError, add_filter, add_protected, dismiss_candidate, load_filters,
    make_matcher, protected_domains, reapply, remove_protected, remove_protected_rules,
    set_enabled, suggest_filters,
)
from nwmail.server import TOKEN_HEADER, make_server
from nwmail.store import Store

ME = "gdhong@lumi.example"
NOW = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
ok = total = 0


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def mail(uid, subject, sender, to=(ME,), mid="", refs=(), unsub=False,
         when="2026-09-16T09:00:00+09:00", folder="") -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                received_time=when, status="Read", body="본문", to=list(to),
                message_id=mid or f"<m{uid}@t>", references=list(refs),
                in_reply_to=refs[-1] if refs else "", list_unsubscribe=unsub, folder=folder)


def refused(fn, *args, **kw) -> tuple[bool, str]:
    try:
        fn(*args, **kw)
    except FilterError as e:
        return True, str(e)
    return False, "거부되지 않음"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "f.db"

        hdr("[1] 보호 도메인은 어떤 규칙에도 차단되지 않음")
        check("요청한 3개 도메인이 고정 보호",
              set(PROTECTED_DOMAINS) == {"lumi.example", "aurora.example", "nurimktg.co.example"})
        rules = [load_filters.__globals__["Filter"](1, "subject", "(광고)", "", True, 0, "광고·홍보"),
                 load_filters.__globals__["Filter"](2, "sender", "kim", "", True, 0, "기타")]
        match = make_matcher(rules)
        for sender in ("pm@lumi.example", "kim@aurora.example", "x@nurimktg.co.example", "a@mail.aurora.example"):
            check(f"보호: {sender} 는 제목·주소 규칙에 걸려도 차단 안 함",
                  match(mail(1, "(광고) 이벤트", sender)) is None)
        check("보호 도메인이 아니면 차단", match(mail(1, "(광고) 이벤트", "ad@shop.com")) == 1)
        check("보낸 주소가 비어 있으면 주소 규칙에 안 걸림", match(mail(1, "업무", "")) is None)
        check("비슷한 이름의 다른 도메인은 보호 아님 (aurora.example.evil.io)",
              match(mail(1, "(광고)", "x@aurora.example.evil.io")) == 1)

        with Store(db) as st:
            hdr("[2] 보호 도메인을 겨냥한 규칙은 저장 · 다시 켜기 거부")
            for kind, pattern in (("domain", "aurora.example"), ("domain", "@lumi.example"),
                                  ("sender", "jiwoo.lee@aurora.example"), ("sender", "@nurimktg.co.example"),
                                  ("domain", "co.example"), ("domain", "mail.aurora.example")):
                bad, why = refused(add_filter, st, kind, pattern)
                check(f"거부: {kind} {pattern}", bad and "보호 도메인" in why, why)
            check("제목 규칙은 허용 (판정 때 보호)", add_filter(st, "subject", "(광고)", "", "광고·홍보") > 0)
            bad, why = refused(add_filter, st, "domain", "not a domain")
            check("도메인 형식 검사", bad and "도메인 형식" in why, why)
            # 예전에 만들어져 꺼져 있던 규칙(보호 도메인 겨냥)
            st.conn.execute("INSERT INTO filters (kind, pattern, note, enabled, created_at)"
                            " VALUES ('sender', 'legacy@lumi.example', '예전 규칙', 0, 'x')")
            st.conn.commit()
            legacy = next(f for f in load_filters(st, enabled_only=False) if f.pattern == "legacy@lumi.example")
            bad, why = refused(set_enabled, st, legacy.id, True)
            check("예전 규칙도 다시 켜기 거부", bad, why)
            gone = remove_protected_rules(st)
            check("보호 도메인 겨냥 규칙 일괄 삭제 (제목 규칙은 유지)",
                  [f.pattern for f in gone] == ["legacy@lumi.example"]
                  and [f.pattern for f in load_filters(st, enabled_only=False)] == ["(광고)"])

            hdr("[3] 도메인 규칙 · 재적용")
            add_filter(st, "domain", "Shop.COM", "", "뉴스레터·구독")
            st.save_mails([mail(10, "주간 소식", "news@shop.com"),
                           mail(11, "할인", "a@deals.shop.com"),
                           mail(12, "(광고) 사내 행사", "hr@lumi.example"),
                           mail(13, "견적", "b@shopping.com")], me=ME)
            r = reapply(st)
            blocked = {b["uid"].split(":")[-1]: b for b in st.blocked_mails(None)}
            check("도메인 규칙은 하위 도메인까지", {"10", "11"} <= set(blocked), str(sorted(blocked)))
            check("도메인 규칙은 글자만 비슷한 도메인은 안 걸림 (shopping.com)", "13" not in blocked)
            check("보호 도메인 메일은 제목 규칙에 걸려도 차단 안 됨", "12" not in blocked)
            check("차단 목록에 구분 · 규칙 상태가 함께", blocked["10"]["filter_category"] == "뉴스레터·구독"
                  and blocked["10"]["filter_enabled"] == 1)
            # 누군가 보호 도메인 메일을 직접 차단 표시해 두었어도 재적용하면 풀린다
            st.conn.execute("UPDATE mails SET blocked_by = 1 WHERE from_email = 'hr@lumi.example'")
            st.conn.commit()
            reapply(st)
            check("재적용하면 보호 도메인 메일의 차단이 풀림",
                  not any(b["from_email"] == "hr@lumi.example" for b in st.blocked_mails(None)))

            hdr("[4] 사용자 보호 도메인")
            check("추가 (정규화)", add_protected(st, "@Partner.io ") == "partner.io"
                  and any(p["domain"] == "partner.io" and not p["fixed"] for p in protected_domains(st)))
            bad, why = refused(add_filter, st, "domain", "partner.io")
            check("추가한 보호 도메인도 규칙 거부", bad, why)
            bad, why = refused(remove_protected, st, "aurora.example")
            check("고정 보호 도메인은 뺄 수 없음", bad and "고정" in why, why)
            remove_protected(st, "partner.io")
            check("추가한 보호 도메인은 뺄 수 있음", all(p["domain"] != "partner.io" for p in protected_domains(st)))

        hdr("[5] 차단 후보 — 근거가 있는 곳만, 보호 · 업무 상대 제외")
        with Store(Path(tmp) / "s.db") as st:
            st.save_mails([
                mail(1, "(광고)[플러스] 무료 교육 안내", "ks@plus.com", unsub=True),
                mail(2, "CadNova 플러그인 뉴스 - 9월", "a@cadnova.example", to=["team@lumi.example"]),
                mail(3, "CadNova3Dzine - September", "b@cadnova.example", to=["team@lumi.example"]),
                mail(4, "[잡스테이션] 채용 트렌드 리포트가 도착했습니다", "marketing@jobstation.example"),
                mail(5, "「주식 설명회」 참석 안내", "v@venture.or.kr"),
                mail(6, "View request", "support+notifications@designhub.example"),
                mail(7, "주간 이벤트 일정", "c@newpartner.io"),                  # 제목 키워드뿐
                mail(8, "프로모션 뉴스 공유", "x@aurora.example"),                   # 보호
                mail(9, "뉴스레터 기획 협의", "p@agency.com", mid="<ag1@t>"),     # 업무 상대
                mail(10, "RE: 뉴스레터 기획 협의", "pm@lumi.example", mid="<ag2@t>", refs=["<ag1@t>"],
                     to=["p@agency.com"]),
            ], me=ME)
            cands = {(c["kind"], c["pattern"]): c for c in suggest_filters(st, me=ME)}
            check("제목 광고 표기 → 제목 규칙 (광고·홍보)",
                  cands.get(("subject", "(광고)"), {}).get("category") == "광고·홍보", str(list(cands)))
            check("광고 표기로 잡힌 곳은 도메인 후보로 중복 제안 안 함", ("domain", "plus.com") not in cands)
            check("뉴스레터성 제목 → 도메인 규칙 (뉴스레터·구독)",
                  cands.get(("domain", "cadnova.example"), {}).get("category") == "뉴스레터·구독"
                  and cands[("domain", "cadnova.example")]["count"] == 2)
            check("marketing 발송 주소 → 뉴스레터·구독", ("domain", "jobstation.example") in cands)
            check("설명회 · 참석 안내 → 행사·안내",
                  cands.get(("domain", "venture.or.kr"), {}).get("category") == "행사·안내")
            check("자동 알림 한 통은 두고 봄 (업무 도구 알림일 수 있음)", ("domain", "designhub.example") not in cands)
            check("제목 키워드만으로는 제안 안 함", ("domain", "newpartner.io") not in cands)
            check("보호 도메인은 제안 안 함", not any("aurora.example" in p for _, p in cands))
            check("우리 회사가 답장한 대화 상대는 제안 안 함", ("domain", "agency.com") not in cands)
            check("근거를 함께 제시", all(c["reasons"] for c in cands.values()))
            add_filter(st, "domain", "cadnova.example", "", "뉴스레터·구독", enabled=False)
            dismiss_candidate(st, "domain", "jobstation.example")
            after = {(c["kind"], c["pattern"]) for c in suggest_filters(st, me=ME)}
            check("이미 규칙이 있으면(꺼져 있어도) 제안 안 함", ("domain", "cadnova.example") not in after)
            check("후보에서 뺀 것은 다시 제안 안 함", ("domain", "jobstation.example") not in after)
            check("제안만 하고 자동 차단은 안 함", not st.blocked_mails(None))

        hdr("[6] 차단됨 탭 변경 요청 (filter_api)")
        api_db = Path(tmp) / "api.db"
        with Store(api_db) as st:
            st.save_mails([mail(1, "주간 소식", "news@shop.com"), mail(2, "견적", "b@agency.io")], me=ME)
        rebuilt = []

        def run(data):
            return filter_api.handle(data, store_factory=lambda: Store(api_db),
                                     rebuild=lambda: rebuilt.append(1) or True)

        res = run({"op": "add_rule", "kind": "domain", "pattern": "shop.com", "category": "뉴스레터·구독"})
        check("규칙 추가 → 저장된 메일에 바로 적용 + 화면 새로 만듦",
              res["ok"] and res["blocked"] == 1 and rebuilt == [1] and "shop.com" in res["message"], str(res))
        with Store(api_db) as st:
            fid = load_filters(st)[0].id
        bad, why = refused(run, {"op": "add_rule", "kind": "domain", "pattern": "lumi.example",
                                 "category": "기타"})
        check("보호 도메인 규칙 요청 거부", bad and "보호 도메인" in why, why)
        bad, why = refused(run, {"op": "add_rule", "kind": "domain", "pattern": "x.com", "category": "스팸"})
        check("구분 값 검사", bad, why)
        res = run({"op": "toggle_rule", "id": fid, "enabled": False})
        check("끄기 → 메일 되살아남", res["unblocked"] == 1, str(res))
        res = run({"op": "toggle_rule", "id": fid, "enabled": True})
        check("켜기 → 다시 차단", res["blocked"] == 1)
        res = run({"op": "protect", "domain": "shop.com"})
        check("보호 도메인 추가 → 그 도메인 메일 차단 해제", res["unblocked"] == 1, str(res))
        bad, why = refused(run, {"op": "toggle_rule", "id": fid, "enabled": True})
        check("보호한 뒤에는 그 도메인 규칙을 켤 수 없음", bad, why)
        bad, why = refused(run, {"op": "unprotect", "domain": "nurimktg.co.example"})
        check("고정 보호 도메인 빼기 거부", bad, why)
        run({"op": "unprotect", "domain": "shop.com"})
        res = run({"op": "remove_rule", "id": fid})
        check("규칙 삭제", "삭제" in res["message"])
        bad, why = refused(run, {"op": "remove_rule", "id": 999})
        check("없는 규칙", bad, why)
        bad, why = refused(run, {"op": "drop_table"})
        check("알 수 없는 요청 거부", bad, why)
        import inspect
        src = inspect.getsource(filter_api.regenerate)
        check("화면 재생성은 UTF-8 출력으로 (창 없는 서버에서 cp949 로 죽지 않게)",
              'PYTHONIOENCODING="utf-8"' in src)

        hdr("[7] 서버 /api/filters — 토큰 · 오류 응답")
        fb = FeedbackStore(Path(tmp) / "fb.db")
        page = Path(tmp) / "dash.html"
        page.write_text("<html></html>", encoding="utf-8")
        server = make_server(0, page, fb)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def post(body: dict, token: str | None = fb.token()) -> tuple[int, dict]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"}
            if token:
                headers[TOKEN_HEADER] = token
            c.request("POST", "/api/filters", json.dumps(body).encode(), headers)
            r = c.getresponse()
            data = json.loads(r.read() or b"{}")
            c.close()
            return r.status, data

        status, _ = post({"op": "dismiss", "kind": "domain", "pattern": "a.com"})
        check("처리기가 없으면 503", status == 503, str(status))
        server.filters_handler = run
        status, _ = post({"op": "dismiss", "kind": "domain", "pattern": "a.com"}, token=None)
        check("토큰 없으면 403", status == 403, str(status))
        status, data = post({"op": "add_rule", "kind": "domain", "pattern": "aurora.example", "category": "기타"})
        check("보호 도메인 규칙은 400 + 이유", status == 400 and "보호 도메인" in data.get("error", ""),
              f"{status} {data}")
        status, data = post({"op": "add_rule", "kind": "subject", "pattern": "(광고)", "category": "광고·홍보"})
        check("정상 요청은 200 + 안내 문장", status == 200 and data.get("ok"), f"{status} {data}")
        server.shutdown()
        server.server_close()
        fb.close()

        hdr("[8] 차단됨 탭 화면")
        with Store(Path(tmp) / "d.db") as st:
            st.save_mails([mail(1, "(광고) <b>특가</b>", "ad@shop.com", unsub=True),
                           mail(2, "CadNova 뉴스 - 9월", "a@cadnova.example"),
                           mail(3, "CadNova3Dzine", "b@cadnova.example"),
                           mail(4, "업무 요청", "pm@aurora.example")], me=ME)
            add_filter(st, "subject", "(광고)", "법정 표기", "광고·홍보")
            add_filter(st, "sender", "old@x.com", "", "기타", enabled=False)
            add_protected(st, "bluead.example", "거래처")
            reapply(st)
            model = build_model(st, me=ME, now=NOW, today=date(2026, 9, 17))
            tab = _blocked_tab(model)
            html = render_html(model, server={"port": 8787, "token": "t"})
        check("구분별 보기 칩 (전체 · 광고·홍보)",
              'data-cat-filter="" aria-pressed="true">전체 1' in tab
              and 'data-cat-filter="광고·홍보" aria-pressed="false">광고·홍보 1' in tab)
        check("차단된 메일에 구분 · 걸린 규칙 · 규칙 끄기 · 도메인 보호 버튼",
              '<tr data-cat="광고·홍보">' in tab and 'data-fop="toggle_rule"' in tab
              and 'data-fop="protect" data-domain="shop.com"' in tab)
        check("메일 제목은 이스케이프", "&lt;b&gt;특가&lt;/b&gt;" in tab and "<b>특가</b>" not in tab)
        check("규칙 표: 켜진 규칙은 끄기, 꺼진 규칙은 켜기 + 삭제",
              'data-enabled="0">끄기</button>' in tab and 'data-enabled="1">켜기</button>' in tab
              and tab.count('data-fop="remove_rule"') == 2)
        check("규칙 추가 폼 (종류 · 문구 · 구분 · 메모)",
              'form class="fadd" data-fop="add_rule"' in tab and 'name="pattern"' in tab
              and '<option value="domain">보낸 도메인</option>' in tab)
        check("차단 후보: 규칙으로 추가 · 후보에서 빼기 · 구분 선택",
              'data-fop="add_rule" data-kind="domain" data-pattern="cadnova.example"' in tab
              and 'data-fop="dismiss"' in tab and "<option selected>뉴스레터·구독</option>" in tab)
        check("보호 도메인: 고정 3개는 뺄 수 없음, 추가한 곳은 빼기",
              tab.count('<span class="fix">고정</span>') == 3
              and 'data-fop="unprotect" data-domain="bluead.example"' in tab)
        check("보호 도메인 메일은 차단 목록에 없음", "pm@aurora.example" not in tab)
        check("탭 전용 CSS · JS 포함, 서버 주소가 아니면 잠금 안내",
              "<style>" in tab and "/api/filters" in tab and "대시보드 서버 주소" in tab)
        check("전체 페이지에 차단됨 탭으로 들어감", 'id="blk"' in html and "/api/filters" in html)

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
