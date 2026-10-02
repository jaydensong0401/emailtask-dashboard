"""Claude 사용량 기록 검증: CLI JSON 읽기 · 실패 원인 · 업무 정리 기록 · 대시보드 집계.

실제 CLI 출력(짧은 확인 호출, 2026-09-18)을 그대로 넣어 읽는다. LLM 호출 없음.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from nwmail import claude_cli, usage
from nwmail import extract as ex
from nwmail.client import Mail
from nwmail.config import load_config
from nwmail.store import Store

ME = "gdhong@lumi.example"
ok = total = 0

# claude -p --output-format json 실제 출력 (session_id · uuid 는 뺐다). 모델이 둘인 게 정상이다:
# 본 호출(claude-haiku-4-5)과 Claude Code 의 보조 호출(claude-haiku-4-5-20251001)
REAL = {
    "is_error": False, "duration_api_ms": 2561, "num_turns": 1, "stop_reason": "end_turn",
    "total_cost_usd": 0.038202,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 18650, "cache_read_input_tokens": 0,
              "output_tokens": 56, "service_tier": "standard"},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"inputTokens": 527, "outputTokens": 17, "cacheReadInputTokens": 0,
                                      "cacheCreationInputTokens": 0, "costUSD": 0.000612,
                                      "canonicalModel": "claude-haiku-4-5"},
        "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 56, "cacheReadInputTokens": 0,
                             "cacheCreationInputTokens": 18650, "costUSD": 0.03759,
                             "canonicalModel": "claude-haiku-4-5"}},
    "subtype": "success", "api_error_status": None, "result": "OK", "type": "result",
    "duration_ms": 1969,
}
LIMIT_MSG = "You've hit your session limit · resets 1:30pm (Asia/Seoul)"


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


class FakeRun:
    """claude_cli 안의 subprocess.run 대신. 넘긴 명령도 기억한다."""

    def __init__(self, rc=0, stdout="", stderr="", timeout=False):
        self.rc, self.stdout, self.stderr, self.timeout, self.cmd = rc, stdout, stderr, timeout, None

    def __call__(self, cmd, **kw):
        self.cmd = cmd
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)


def cli_with(fake):
    real = claude_cli.subprocess.run
    claude_cli.subprocess.run = fake
    return real


def mail(uid, subject, sender, mid, when, body, to=(ME,), refs=(), folder="INBOX") -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                received_time=when, status="Read", body=body, to=list(to),
                message_id=mid, references=list(refs),
                in_reply_to=refs[-1] if refs else "", folder=folder)


def fake_model(tokens: dict):
    """프롬프트의 대화 키마다 빈 결과를 돌려주는 가짜 run_prompt_json."""
    def call(prompt, model="", timeout=300, exe=None):
        keys = re.findall(r'<thread key="([^"]+)">', prompt)
        text = json.dumps({"threads": [{"thread_key": k, "project": "", "summary": "요약",
                                        "status": "진행중", "decisions": [], "tasks": [],
                                        "my_actions": []} for k in keys]}, ensure_ascii=False)
        return claude_cli.CliResult(text=text, model="claude-opus-5", **tokens)
    return call


def main() -> int:
    hdr("[1] CLI JSON 읽기 (실제 출력)")
    res = claude_cli.result_json(json.dumps(REAL))
    r = claude_cli.result_from(res, 5.2)
    check("답", r.text == "OK", r.text)
    check("모델별 사용량을 더함 (보조 호출 포함)",
          (r.input_tokens, r.output_tokens, r.cache_write_tokens, r.cache_read_tokens) == (537, 73, 18650, 0),
          str(r.usage()))
    check("본 호출 모델 이름", r.model == "claude-haiku-4-5", r.model)
    check("API 환산 금액은 CLI 값 그대로", abs(r.cost_usd - 0.038202) < 1e-9, str(r.cost_usd))
    noisy = "경고 한 줄\n" + json.dumps(REAL) + "\n"
    check("앞뒤에 다른 줄이 섞여도 찾음", claude_cli.result_json(noisy) is not None)
    check("JSON 이 아니면 None", claude_cli.result_json("그냥 글자") is None)
    only_usage = {"result": "x", "usage": {"input_tokens": 3, "output_tokens": 4,
                                           "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}}
    r2 = claude_cli.result_from(only_usage)
    check("modelUsage 가 없으면 usage 로",
          (r2.input_tokens, r2.output_tokens, r2.cache_read_tokens, r2.cache_write_tokens) == (3, 4, 5, 6))

    hdr("[2] run_prompt_json — 성공 · 한도 초과 · 예전 출력 · 시간 초과")
    fake = FakeRun(stdout=json.dumps(REAL))
    real_run = cli_with(fake)
    try:
        got = claude_cli.run_prompt_json("안녕", exe="claude")
        check("JSON 출력을 요청", fake.cmd[fake.cmd.index("--output-format") + 1] == "json")
        check("도구는 계속 막음", "--disallowed-tools" in fake.cmd)
        check("답과 사용량", got.text == "OK" and got.cache_write_tokens == 18650)
        check("run_prompt 는 답만 (기존 호출 그대로)", claude_cli.run_prompt("안녕", exe="claude") == "OK")

        err = {**REAL, "is_error": True, "result": LIMIT_MSG,
               "modelUsage": {}, "usage": {}, "total_cost_usd": 0}
        claude_cli.subprocess.run = FakeRun(rc=1, stdout=json.dumps(err))
        try:
            claude_cli.run_prompt_json("안녕", exe="claude")
            check("한도 초과는 실패로", False)
        except claude_cli.ClaudeCliError as e:
            check("한도 초과 메시지가 예전과 같은 꼴", str(e).startswith("claude CLI 실패 (exit 1):")
                  and "session limit" in str(e), str(e))
            check("실패에도 사용량(0)을 담음", e.result is not None and e.result.input_tokens == 0)
            check("원인: 한도 초과, 13:30 풀림",
                  usage.error_kind(str(e)) == "limit" and usage.reset_label(str(e)) == "13:30")

        claude_cli.subprocess.run = FakeRun(rc=0, stdout=json.dumps({**REAL, "is_error": True}))
        try:
            claude_cli.run_prompt_json("안녕", exe="claude")
            check("종료코드 0 이어도 is_error 면 실패", False)
        except claude_cli.ClaudeCliError:
            check("종료코드 0 이어도 is_error 면 실패", True)

        claude_cli.subprocess.run = FakeRun(stdout='{"threads": []}')
        got = claude_cli.run_prompt_json("안녕", exe="claude")
        check("JSON 결과 객체가 아니면 예전처럼 출력 그대로 (사용량 0)",
              got.text == '{"threads": []}' and got.input_tokens == 0)

        claude_cli.subprocess.run = FakeRun(stdout="")
        try:
            claude_cli.run_prompt_json("안녕", exe="claude")
            check("빈 출력은 실패", False)
        except claude_cli.ClaudeCliError as e:
            check("빈 출력은 실패", "빈 응답" in str(e))

        claude_cli.subprocess.run = FakeRun(timeout=True)
        try:
            claude_cli.run_prompt_json("안녕", exe="claude", timeout=7)
            check("시간 초과는 실패", False)
        except claude_cli.ClaudeCliError as e:
            check("시간 초과 → 원인 timeout", usage.error_kind(str(e)) == "timeout", str(e))
    finally:
        claude_cli.subprocess.run = real_run

    hdr("[3] 실패 원인 · 풀리는 시각")
    cases = [(LIMIT_MSG, "limit"), ("Claude AI usage limit reached|1758290400", "limit"),
             ("claude CLI 응답 시간 초과 (300초).", "timeout"),
             ("Claude 가 토큰을 거부했습니다 (만료됐거나 잘못된 토큰).", "auth"),
             ("claude CLI 실패 (exit 1): API Error: 500", "error"), ("", "error")]
    for msg, want in cases:
        check(f"{msg[:30] or '(빈 값)'} → {want}", usage.error_kind(msg) == want, usage.error_kind(msg))
    for msg, want in [("resets 1:30pm (Asia/Seoul)", "13:30"), ("resets 12am", "00:00"),
                      ("resets 12pm", "12:00"), ("resets 2:20pm", "14:20"),
                      ("weekly limit · resets Sep 20, 3pm", "9/20 15:00"), ("한도 초과", "")]:
        check(f"'{msg}' → '{want}'", usage.reset_label(msg) == want, usage.reset_label(msg))

    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "m.db")
        st.save_mails([
            mail(1, "[루미] 스토어 배너 교체 요청", "pm@aurora.example", "<a0@x>",
                 "2026-09-17T10:00:00+09:00", "배너 교체 부탁드립니다. 9월 22일까지."),
            mail(2, "AX9 멤버십 테니스 클럽 알림톡 문구", "ops@nurimktg.co.example", "<b0@x>",
                 "2026-09-17T11:00:00+09:00", "문구 수정 요청드립니다." * 40),
        ], me=ME)
        threads = st.threads(None)
        cfg = dataclasses.replace(load_config(), summary_backend="claude_code", imap_user=ME)
        tokens = {"input_tokens": 1200, "output_tokens": 800, "cache_read_tokens": 18000,
                  "cache_write_tokens": 3000, "cost_usd": 0.12}

        hdr("[4] 업무 정리 기록 — 호출 · 정리 한 번")
        real_call = claude_cli.run_prompt_json
        claude_cli.run_prompt_json = fake_model(tokens)
        try:
            r1 = ex.Extractor(cfg, st).run(threads, progress=lambda *_: None, skipped=1)
            calls = [dict(x) for x in st.conn.execute("SELECT * FROM llm_calls")]
            runs = [dict(x) for x in st.conn.execute("SELECT * FROM llm_runs")]
            check("정리 결과는 그대로",
                  r1 == {"extracted": 2, "cached": 0, "failed": 0, "delta": 0, "excluded": 0}, str(r1))
            check("호출 1번 기록 (대화 2개를 묶음)", len(calls) == 1 and calls[0]["threads"] == 2)
            c = calls[0]
            check("토큰 · 모델 · 금액 기록",
                  (c["input_tokens"], c["output_tokens"], c["cache_read_tokens"], c["cache_write_tokens"])
                  == (1200, 800, 18000, 3000) and c["model"] == "claude-opus-5" and c["cost_usd"] == 0.12)
            check("용도 · 실행 · 성공", (c["purpose"], c["trigger"], c["ok"]) == ("extract", "manual", 1))
            det = json.loads(c["detail"])
            check("대화별 글자 수 · 메일 수 · 프로젝트 · 방식", len(det) == 2
                  and all(len(x) == 6 and x[3] > 0 and x[5] == "full" for x in det)
                  and {x[2] for x in det} == {"오로라 스토어", "AX9 멤버십 테니스클럽"}, str(det)[:200])
            check("호출에도 방식을 남김 (처음이라 full)", c["mode"] == "full", str(c["mode"]))
            check("프롬프트 글자 수", c["prompt_chars"] >= sum(x[3] for x in det))
            check("정리 한 번 기록 (정기 메일 제외 수 포함)",
                  len(runs) == 1 and (runs[0]["threads"], runs[0]["skipped"], runs[0]["extracted"],
                                      runs[0]["cached"], runs[0]["calls"]) == (2, 1, 2, 0, 1), str(runs))

            r2 = ex.Extractor(cfg, st).run(st.threads(None), progress=lambda *_: None)
            runs = [dict(x) for x in st.conn.execute("SELECT * FROM llm_runs ORDER BY id")]
            check("내용이 그대로면 호출 없이 재사용으로 기록",
                  r2["cached"] == 2 and (runs[-1]["cached"], runs[-1]["calls"]) == (2, 0)
                  and st.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1)

            def limited(prompt, model="", timeout=300, exe=None):
                raise claude_cli.ClaudeCliError(f"claude CLI 실패 (exit 1): {LIMIT_MSG}",
                                                claude_cli.CliResult(text=""))
            claude_cli.run_prompt_json = limited
            r3 = ex.Extractor(cfg, st).run(st.threads(None), force=True, progress=lambda *_: None)
            bad = dict(st.conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone())
            check("한도 초과는 실패로 기록 (원인 · 메시지)", r3["failed"] == 2 and bad["ok"] == 0
                  and bad["error_kind"] == "limit" and "resets 1:30pm" in bad["error"], bad["error"])

            def broken_json(prompt, model="", timeout=300, exe=None):
                return claude_cli.CliResult(text="이건 JSON 이 아님", model="claude-opus-5", **tokens)
            claude_cli.run_prompt_json = broken_json
            ex.Extractor(cfg, st).run(st.threads(None), force=True, progress=lambda *_: None)
            bad = dict(st.conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone())
            check("답을 못 읽어도 쓴 토큰은 남김", bad["ok"] == 0 and bad["input_tokens"] == 1200)
        finally:
            claude_cli.run_prompt_json = real_call

        hdr("[5] 기록이 안 돼도 정리는 계속")
        st.conn.execute("DROP TABLE llm_calls")
        st.conn.commit()
        claude_cli.run_prompt_json = fake_model(tokens)
        try:
            r4 = ex.Extractor(cfg, st).run(st.threads(None), force=True, progress=lambda *_: None)
            check("표가 없어도 정리 성공", r4["extracted"] == 2, str(r4))
        finally:
            claude_cli.run_prompt_json = real_call
        st.close()

    hdr("[6] 대시보드 집계")
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "m.db")
        now = datetime(2026, 9, 18, 8, 30, tzinfo=timezone.utc)          # 09/18 17:30 KST

        def add(at_kst: str, purpose="extract", trig="auto", tok=1000, err="", detail=None):
            at = datetime.fromisoformat(at_kst + "+09:00").astimezone(timezone.utc).isoformat()
            usage.record_call(st, purpose, usage={"model": "claude-opus-5", "input_tokens": tok,
                                                  "output_tokens": tok // 10, "cost_usd": tok / 1e5},
                              error=err, detail=detail, at=at)
            st.conn.execute("UPDATE llm_calls SET trigger = ? WHERE id = (SELECT MAX(id) FROM llm_calls)",
                            (trig,))
            st.conn.commit()

        add("2026-09-18T09:05:00", detail=[["t1", "큰 대화", "오로라 스토어", 30000, 83],
                                           ["t2", "작은 대화", "오로라 갤러리", 10000, 5]])
        add("2026-09-18T13:01:00", err=f"ClaudeCliError: claude CLI 실패 (exit 1): {LIMIT_MSG}",
            tok=0, detail=[["t1", "큰 대화", "오로라 스토어", 30000, 83]])
        add("2026-09-18T17:02:00", tok=2000, detail=[["t1", "큰 대화", "오로라 스토어", 31000, 84]])
        add("2026-09-18T15:40:00", trig="manual", tok=500)
        add("2026-09-18T16:00:00", purpose="verify", trig="manual", tok=19000)
        add("2026-09-11T09:00:00", tok=7000)               # 7일 밖 (최근 7일 = 09/12~09/18) · 이번 달 안
        add("2026-08-31T09:00:00", tok=9000)               # 지난 달
        s = usage.summary(st, now=now)
        check("기록 있음 · 기록 시작", s["has_data"] and s["first_at"].startswith("2026-08-31"))
        check("오늘: 호출 5 · 실패 1 · 한도 1", (s["today"]["calls"], s["today"]["fails"], s["today"]["limit"])
              == (5, 1, 1), str(s["today"]))
        check("토큰 = 입력 + 출력 (+ 캐시)", s["today"]["tokens"] == sum(
            t + t // 10 for t in (1000, 0, 2000, 500, 19000)), str(s["today"]["tokens"]))
        check("최근 7일은 09/11 제외, 이번 달은 포함",
              s["week"]["calls"] == 5 and s["month"]["calls"] == 6, f'{s["week"]["calls"]}/{s["month"]["calls"]}')
        check("일별 7칸, 마지막이 오늘", len(s["day_list"]) == 7
              and s["day_list"][-1]["day"].isoformat() == "2026-09-18"
              and s["day_list"][0]["day"].isoformat() == "2026-09-12")
        slots = {x["label"]: x["calls"] for x in s["slots"]}
        check("회차별: 09시 1 · 13시 1 · 17시 1 · 직접 실행 2", slots == {"09시": 1, "13시": 1, "17시": 1,
                                                                   "직접 실행": 2}, str(slots))
        uses = {u["label"]: u["calls"] for u in s["uses"]}
        check("어디에 쓰나: 자동 3 · 직접 1 · 요약 0 · 확인 1",
              list(uses.values()) == [3, 1, 0, 1], str(uses))
        check("비율 합 1", abs(sum(u["share"] for u in s["uses"]) - 1) < 1e-9)
        t1, t2 = s["threads"]["t1"], s["threads"]["t2"]
        check("대화별 추정: 글자 수 비율 (09시 호출 30:10) + 17시 전부",
              t1["tokens"] == round(1100 * 0.75 + 2200) and t2["tokens"] == round(1100 * 0.25),
              f'{t1["tokens"]}/{t2["tokens"]}')
        check("보낸 횟수는 실패한 호출도 셈 · 최신 크기", t1["sent"] == 3 and t1["chars"] == 31000 and t1["mails"] == 84)
        check("한도 초과: 1회 · 13:30 풀림", (s["limit"]["count"], s["limit"]["reset"]) == (1, "13:30"))
        check("기본 지시문 측정 = 가장 최근 확인 호출 입력", s["probe"]["tokens"] == 19000, str(s["probe"]))
        check("호출 기록은 최신순", s["log"][0]["when"] == "09/18 17:02" and s["log"][0]["slot"] == "17시")
        check("모델", s["models"] == ["claude-opus-5"])

        # 사용량과 증가량 — 직전 같은 길이의 기간(어제 · 지난 7일 · 지난달)과 견준다
        add("2026-09-17T09:00:00", tok=1000)               # 어제
        s = usage.summary(st, now=now)
        check("어제 · 지난 7일(09/05~09/11) · 지난달(8월) 도 같이 집계",
              (s["prev"]["today"]["tokens"], s["prev"]["week"]["calls"], s["prev"]["month"]["tokens"])
              == (1100, 1, 9900),
              f'{s["prev"]["today"]["tokens"]}/{s["prev"]["week"]["calls"]}/{s["prev"]["month"]["tokens"]}')
        check("증가량 = 이번 - 직전, 퍼센트는 직전 기준",
              s["delta"]["today"]["tokens"] == s["today"]["tokens"] - 1100
              and s["delta"]["today"]["dir"] == "up"
              and abs(s["delta"]["month"]["pct"] - (s["month"]["tokens"] - 9900) / 9900) < 1e-9,
              str(s["delta"]["today"]))
        check("일별 첫 줄의 증가량을 재도록 창 하루 앞(09/11)도 담는다",
              s["prev"]["day0"]["tokens"] == 7700, str(s["prev"]["day0"]))
        check("누적은 기간과 상관없이 전체 · 하루 평균",
              s["total"]["calls"] == 8 and s["total"]["span"] == 19
              and s["total"]["per_day"] == round(s["total"]["tokens"] / 19), str(s["total"]))
        for cur, prev, want in [({"tokens": 300}, {"tokens": 200}, "up"), ({"tokens": 100}, {"tokens": 200}, "down"),
                                ({"tokens": 200}, {"tokens": 200}, "flat"), ({"tokens": 200}, {"tokens": 0}, "new")]:
            d = usage.delta(cur, prev)
            check(f'{prev["tokens"]} → {cur["tokens"]} = {want}', d["dir"] == want, d["dir"])
        check("직전이 0 이면 몇 % 인지 말하지 않는다",
              usage.delta({"tokens": 200}, {"tokens": 0})["pct"] is None)
        st.close()

    hdr("[7] 기록이 없을 때")
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "m.db")
        s = usage.summary(st)
        check("기록 없음 표시", not s["has_data"] and s["week"]["calls"] == 0 and len(s["day_list"]) == 7)
        check("용도 네 줄은 그대로 (0)", len(s["uses"]) == 4 and all(u["tokens"] == 0 for u in s["uses"]))
        st.close()

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
