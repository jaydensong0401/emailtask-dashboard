"""Claude Code 장기 토큰 설정 도우미.

실행하면 입력창이 뜨고, `claude setup-token` 이 출력한 토큰을 붙여넣으면 된다.
명령어를 고칠 필요가 없다.

  1) 입력은 화면에 표시되지 않는다
  2) 줄바꿈 복사로 섞인 공백·비가시 문자를 자동으로 제거한다
  3) 저장하기 전에 Claude 에 아주 짧은 요청을 한 번 보내 실제로 동작하는지 확인한다
  4) 확인되면 사용자 환경변수 CLAUDE_CODE_OAUTH_TOKEN 에 저장한다 (setx)

토큰 값은 어디에도 출력하지 않는다. 확인 호출이 쓴 토큰은 대시보드 '토큰 사용 현황'에 남긴다
(짧은 요청이라 호출마다 붙는 Claude Code 기본 지시문의 크기가 그대로 보인다).
"""
from __future__ import annotations

import getpass
import subprocess
import sys
import time

from nwmail import claude_cli
from nwmail.config import normalize_password

VERIFY_MODEL = "claude-haiku-4-5"      # 확인용 호출은 가장 가벼운 모델로
VERIFY_PROMPT = "OK 라고만 답하세요."


def record_verify(res: dict | None, seconds: float) -> None:
    """확인 호출의 사용량을 남긴다. 기록하지 못해도 토큰 저장은 그대로 진행한다."""
    if not res:
        return
    try:
        from nwmail import usage
        from nwmail.store import Store
        with Store() as store:
            usage.record_call(store, "verify", usage=claude_cli.result_from(res).usage(),
                              backend="claude_code", seconds=seconds,
                              prompt_chars=len(VERIFY_PROMPT))
    except Exception:
        pass


def check_and_save(token: str, save: bool = True) -> tuple[bool, str]:
    """(성공 여부, 안내 메시지). 토큰 값은 메시지에 넣지 않는다."""
    token = normalize_password(token)     # 공백·줄바꿈·비가시 문자 제거
    if problem := claude_cli.token_problem(token):
        return False, problem
    if not (exe := claude_cli.find_cli()):
        return False, "`claude` CLI 를 찾지 못했습니다."

    env = claude_cli.clean_env()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    started = time.monotonic()
    try:
        p = subprocess.run(
            [exe, "-p", "--output-format", "json", "--disallowed-tools", claude_cli.NO_TOOLS,
             "--model", VERIFY_MODEL],
            input=VERIFY_PROMPT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "확인 요청이 시간 초과됐습니다. 네트워크를 확인하고 다시 실행하세요."
    res = claude_cli.result_json(p.stdout)
    if p.returncode != 0 or (res and res.get("is_error")):
        detail = claude_cli.redact(str((res or {}).get("result") or p.stderr or p.stdout or "").strip())
        return False, ("토큰이 거부되었습니다. 저장하지 않았습니다.\n"
                       f"  응답: {detail[:300]}\n"
                       "  `claude setup-token` 으로 새로 발급받아 다시 실행하세요.")
    record_verify(res, time.monotonic() - started)

    if not save:
        return True, "토큰 확인 완료 (저장은 건너뜀)."
    r = subprocess.run(["setx", "CLAUDE_CODE_OAUTH_TOKEN", token],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, "토큰은 정상이지만 환경변수 저장(setx)에 실패했습니다."
    return True, ("토큰 확인 및 저장 완료.\n"
                  "  같은 창에서 바로 실행하면 됩니다:  python extract.py -n 3")


def main() -> int:
    print("`claude setup-token` 이 출력한 토큰(sk-ant- 로 시작)을 붙여넣고 Enter 를 누르세요.")
    print("입력한 내용은 화면에 표시되지 않습니다. (붙여넣기: 마우스 우클릭)\n")
    token = getpass.getpass("토큰: ")
    if not token.strip():
        print("[X] 입력이 비어 있습니다.")
        return 1
    print("\n토큰 확인 중 — Claude 에 짧은 요청을 한 번 보냅니다 (수십 초 걸릴 수 있음)...")
    ok, msg = check_and_save(token)
    print(("[OK] " if ok else "[X] ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
