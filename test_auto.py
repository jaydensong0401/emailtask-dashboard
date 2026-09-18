"""자동 실행 검증: 읽는 기간 · 정기 메일 제외 · 비밀번호 · 잠금 · 실패 알림.

실제 메일함·LLM·작업 스케줄러 불필요.
"""
from __future__ import annotations

import dataclasses
import io
import json
import re
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import auto
from nwmail import config as nwconfig
from nwmail import secret
from nwmail.client import Mail
from nwmail.dashboard import build_model, new_since, render_html
from nwmail.projects import skip_extract
from nwmail.store import KST, Store, weekday_window_days

ME = "gdhong@lumi.example"
ok = total = 0


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def kst(y, mo, d, h=10) -> datetime:
    return datetime(y, mo, d, h, tzinfo=KST)


def mail(uid, subject, when, sender="pm@lumi.example") -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                received_time=when, status="Read", body="본문", to=[ME],
                message_id=f"<m{uid}@t>", references=[])


def main() -> int:
    hdr("[1] 읽는 기간 — 평일 D-1, 월요일 D-3")
    # 2026-09-14 월 … 2026-09-20 일
    want = {14: 3, 15: 1, 16: 1, 17: 1, 18: 1, 19: 1, 20: 2}
    got = {d: weekday_window_days(date(2026, 9, d)) for d in want}
    check("월 3(금부터) · 화~토 1(어제부터) · 일 2(금부터)", got == want, str(got))

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "a.db") as st:
            tue = kst(2026, 9, 15).astimezone(timezone.utc)
            check("실행 기록이 없으면 평일 규칙 (화요일 1일)", st.read_window_days("sync", tue) == 1)
            st.start_run(0, kind="sync", started_at=kst(2026, 9, 14, 17))
            check("어제 동기화했으면 평일 규칙 그대로", st.read_window_days("sync", tue) == 1)
            st.start_run(0, kind="extract", started_at=kst(2026, 9, 10, 17))
            check("마지막 정리가 5일 전(휴가)이면 그 날부터 5일",
                  st.read_window_days("extract", tue) == 5, str(st.read_window_days("extract", tue)))
            mon = kst(2026, 9, 14, 9).astimezone(timezone.utc)
            check("월요일은 금요일부터 (3일)", st.read_window_days("sync", mon) == 3)
        with Store(Path(tmp) / "b.db") as st:
            st.start_run(0, kind="sync", started_at=kst(2026, 7, 1))
            check("오래 비었으면 최대 30일",
                  st.read_window_days("sync", kst(2026, 9, 15).astimezone(timezone.utc)) == 30)

        hdr("[2] 실행 기록 종류 · 예전 DB 호환")
        old = Path(tmp) / "old.db"
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT,"
                  " new_mails INTEGER DEFAULT 0)")
        c.execute("INSERT INTO runs (started_at, new_mails) VALUES ('2026-09-10T00:00:00+00:00', 3)")
        c.commit()
        c.close()
        with Store(old) as st:
            check("예전 runs 표에 kind 열이 추가되고 기존 기록은 sync",
                  st.last_run_at("sync", offset=0) == "2026-09-10T00:00:00+00:00"
                  and st.last_run_at("extract", offset=0) is None)
            st.start_run(0, kind="sync", started_at=kst(2026, 9, 15, 9))
            st.start_run(0, kind="extract", started_at=kst(2026, 9, 15, 9))
            st.start_run(0, kind="sync", started_at=kst(2026, 9, 15, 9) + timedelta(minutes=10))
            check("정리 기록이 1개면 NEW 기준은 그 정리",
                  new_since(st) == kst(2026, 9, 15, 9).isoformat(), str(new_since(st)))
            st.start_run(0, kind="extract", started_at=kst(2026, 9, 15, 13))
            check("정리 기록이 2개 이상이면 NEW 기준은 직전 정리 (10분 동기화와 무관)",
                  new_since(st) == kst(2026, 9, 15, 9).isoformat(), str(new_since(st)))

        hdr("[3] 정기 메일은 추출하지 않고 대시보드에만")
        check("제목에 '일일 통계 데이터' 가 들어가면 제외",
              skip_extract("9월 10일 기준 일일 통계 데이터 공유(9월 누적)"))
        check("일반 업무 메일은 제외 안 함", not skip_extract("[루미] 기획안 검토 요청의 건"))
        now = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        with Store(Path(tmp) / "d.db") as st:
            st.save_mails([mail(1, "9월 14일 기준 일일 통계 데이터 공유", "2026-09-15T08:50:00+09:00"),
                           mail(2, "[루미] 기획안 검토", "2026-09-15T09:30:00+09:00")], me=ME)
            stale = {"project": "", "summary": "옛 추출", "status": "진행중", "decisions": [],
                     "tasks": [{"text": "예전에 뽑힌 통계 할 일", "role": "기타"}], "my_actions": []}
            st.conn.execute("INSERT INTO extractions (thread_key, project, payload, source_hash,"
                            " backend, extracted_at) VALUES ('<m1@t>', '', ?, 'x', 't', 'x')",
                            (json.dumps(stale, ensure_ascii=False),))
            st.conn.commit()
            st.start_run(0, kind="sync", started_at=kst(2026, 9, 15, 11))
            st.start_run(0, kind="extract", started_at=kst(2026, 9, 15, 9))
            m = build_model(st, me=ME, now=now)
            out = render_html(m, notice="메일 받기 실패(09/15 10:00): <로그인 거부>")
        check("정기 메일 대화는 '추출 안 함' 으로 표시", "정기 메일 — 추출 안 함" in out)
        check("예전에 뽑아 둔 결과는 쓰지 않음 (갱신이 안 되므로)", "예전에 뽑힌 통계 할 일" not in out)
        check("요약 완료 · 정리 대기 배너는 정기 메일을 빼고 셈 (0/1)",
              '0<span class="den">/1</span>' in out and "업무·결정사항 정리가 아직 없습니다" in out)
        check("집계에 제외 수", m["stats"]["excluded"] == 1)
        check("실패 알림을 상단에 띄움 (이스케이프)",
              'class="banner warn" role="alert">메일 받기 실패(09/15 10:00): &lt;로그인 거부&gt;' in out)
        check("머리에 마지막 정리 시각", '마지막 정리 <b class="num">09/15 09:00</b>' in out)
        check("자동 새로고침 코드 (조작 중이면 미룸, 스크롤 유지)",
              "location.reload()" in out and "nwmail.view" in out and "120000" in out)

    hdr("[4] 비밀번호 — 자동 실행은 묻지 않고 실패")
    cfg = dataclasses.replace(nwconfig.load_config(), mail_source="imap",
                              imap_user="nobody@example.com", imap_password="")
    orig_load, orig_stdin = secret.load_imap_password, sys.stdin
    try:
        secret.load_imap_password = lambda user: ""
        sys.stdin = io.StringIO("")
        try:
            cfg.with_prompted_password()
            check("저장된 비밀번호가 없고 입력창도 없으면 MissingPassword", False)
        except nwconfig.MissingPassword as e:
            check("저장된 비밀번호가 없고 입력창도 없으면 MissingPassword",
                  "set_password.py" in str(e))
        sys.stdin = orig_stdin
        import os
        os.environ[nwconfig.NONINTERACTIVE_ENV] = "1"      # NUL 입력은 Windows 에서 tty 로 보임
        try:
            cfg.with_prompted_password()
            check("자동 실행 표시(환경변수)만 있어도 입력을 기다리지 않음", False)
        except nwconfig.MissingPassword:
            check("자동 실행 표시(환경변수)만 있어도 입력을 기다리지 않음", True)
        finally:
            os.environ.pop(nwconfig.NONINTERACTIVE_ENV, None)
        secret.load_imap_password = lambda user: "saved-pw"
        check("저장된 비밀번호가 있으면 그것을 씀",
              cfg.with_prompted_password().imap_password == "saved-pw")
        check("환경변수 값이 있으면 저장소를 보지 않음",
              dataclasses.replace(cfg, imap_password="env-pw").with_prompted_password()
              .imap_password == "env-pw")
    finally:
        secret.load_imap_password, sys.stdin = orig_load, orig_stdin

    if secret.supported():
        target = "nwmail/test/roundtrip-자동검증"
        try:
            secret.write(target, "tester", "p@ss 한글")
            check("자격 증명 관리자 저장·읽기 (한글 포함)", secret.read(target) == "p@ss 한글")
        finally:
            removed = secret.delete(target)
        check("삭제 후에는 없음", removed and secret.read(target) is None)
        check("없는 항목 삭제는 False", secret.delete(target) is False)
    else:
        print("  [SKIP] Windows 가 아니라 자격 증명 관리자 검증 생략")

    hdr("[5] auto.py — 단계 순서 · 실패 알림 · 잠금")
    with tempfile.TemporaryDirectory() as tmp:
        auto.OUT = Path(tmp)
        auto.LOG, auto.STATE, auto.LOCK = (Path(tmp) / "auto.log", Path(tmp) / "auto_state.json",
                                           Path(tmp) / "auto.lock")
        calls: list[tuple[str, list[str]]] = []
        results = {"sync": (1, "받은메일함 확인 중...\n[X] IMAP 로그인 실패: AUTHENTICATIONFAILED"),
                   "extract": (0, "완료: 추출 2 · 캐시 5 · 실패 0"), "dashboard": (0, "[OK]")}

        def fake(step, extra):
            calls.append((step, extra))
            return results[step]

        rc = auto.run("quick", runner=fake)
        check("quick 은 받기 → 대시보드 (정리 안 함)", [s for s, _ in calls] == ["sync", "dashboard"])
        check("받기가 실패하면 종료코드 1", rc == 1)
        notice = calls[-1][1][1] if len(calls[-1][1]) == 2 else ""
        check("대시보드에 실패 알림 전달 ([X] 줄)",
              "메일 받기 실패" in notice and "IMAP 로그인 실패: AUTHENTICATIONFAILED" in notice, notice)

        calls.clear()
        results["sync"] = (0, "새 메일 3통")
        rc = auto.run("full", runner=fake)
        check("full 은 받기 → 정리 → 대시보드", [s for s, _ in calls] == ["sync", "extract", "dashboard"])
        check("같은 단계가 성공하면 알림 사라짐", calls[-1][1] == ["--notice", ""] and rc == 0)

        calls.clear()
        results["extract"] = (1, "  [1/2] 처리 중...\n      실패: ClaudeCliError: 토큰 만료")
        auto.run("full", runner=fake)
        results["extract"] = (0, "완료")
        calls.clear()
        auto.run("quick", runner=fake)
        check("정리 실패 알림은 quick 이 성공해도 유지 (정리가 성공해야 사라짐)",
              "업무 정리 실패" in calls[-1][1][1] and "토큰 만료" in calls[-1][1][1],
              calls[-1][1][1])
        log = auto.LOG.read_text(encoding="utf-8")
        check("기록 파일에 실행과 단계별 종료코드", "quick ===" in log and "--- sync (종료코드 1" in log)

        first = auto.acquire_lock(0)
        second = auto.acquire_lock(0)
        check("이미 잠겨 있으면 두 번째 실행은 잠금 실패 (quick 은 건너뜀)",
              first is not None and second is None)
        auto.release_lock(first)
        third = auto.acquire_lock(0)
        check("끝나면 다시 잡힘", third is not None)
        auto.release_lock(third)
        check("알 수 없는 모드는 사용법 출력", auto.main(["hourly"]) == 2)

    check("error_line: [X] 없으면 '실패' 줄",
          auto.error_line("a\n  실패: TimeoutError\nb") == "실패: TimeoutError")
    check("error_line: 둘 다 없으면 마지막 줄", auto.error_line("a\nlast") == "last")
    check("자식은 콘솔 python 으로 (pythonw 출력 못 받음)",
          not re.search(r"pythonw\.exe$", auto.console_python(), re.I))

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
