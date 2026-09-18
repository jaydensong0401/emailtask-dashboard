"""Claude Code CLI 호출 래퍼 (구독 사용량으로 처리 — API 과금 없음).

네 가지 함정을 처리한다.

1. 환경변수 오염
   Claude Code 세션 안에서 스크립트를 돌리면 부모가 ANTHROPIC_BASE_URL 과
   CLAUDE_CODE_* 를 주입해둔 상태다. 자식 `claude` 가 이를 상속하면 부모 세션의
   내부 프록시로 붙으려다 "OAuth session expired" 로 죽는다. 그래서 걷어낸다.

2. CLI 미로그인
   데스크톱 앱으로 Claude Code 를 쓰고 있어도 CLI 자체는 별도 로그인이 필요하다.
   `claude auth status` 로 미리 확인해 정확한 안내를 낸다.

3. 토큰 복사 사고
   장기 토큰은 길어서 터미널에서 줄이 넘어가고, 그대로 복사하면 중간에 공백이나
   줄바꿈이 섞인다. 실제 토큰에는 공백이 없으므로 자식에게 넘기기 전에 걷어낸다.

4. 이미 떠 있는 창에는 setx 가 반영되지 않음
   setx 로 저장한 값은 그 뒤에 새로 시작하는 프로세스에만 들어간다. Claude 앱과
   그 안에서 여는 터미널은 앱이 켜질 때의 환경을 물려받으므로 새 값을 모른다.
   게다가 창에 직접 넣은 값($env:)이 있으면 그게 우선한다. 그래서 창의 값이
   없거나 잘못됐으면 저장된 값(레지스트리)을 직접 읽어서 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

# 자식 프로세스에서 제거해야 하는 환경변수
STRIP_EXACT = {"ANTHROPIC_BASE_URL", "CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
STRIP_PREFIX = ("CLAUDE_", "CLAUDECODE_")

# `claude setup-token` 으로 발급한 장기 토큰이 담기는 환경변수.
# 접두사 규칙에 걸리지만 반드시 자식에게 넘겨야 한다. 데스크톱 앱이
# ~/.claude/.credentials.json 을 덮어써도 이 토큰은 영향을 받지 않는다.
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
KEEP_ALWAYS = {TOKEN_VAR}

NO_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"

# 복사할 때 딸려 오는 비가시 문자 (zero-width space / non-joiner / joiner, BOM)
INVISIBLE = ("​", "‌", "‍", "﻿")


def normalize_token(token: str | None) -> str | None:
    """공백·줄바꿈·비가시 문자를 걷어낸다. 실제 토큰에는 공백이 없다."""
    if token is None:
        return None
    for ch in INVISIBLE:
        token = token.replace(ch, "")
    return "".join(token.split())


def saved_token() -> str | None:
    """setx 로 저장된 사용자 환경변수 값 (Windows 레지스트리). 없으면 None."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, TOKEN_VAR)
            return value
    except OSError:
        return None


def resolve_token() -> str | None:
    """쓸 토큰을 고른다. 현재 창의 값이 멀쩡하면 그것, 아니면 저장된 값.

    둘 다 문제가 있으면 원인을 안내할 수 있도록 창의 값(없으면 저장된 값)을 돌려준다.
    """
    current = os.environ.get(TOKEN_VAR)
    if current is not None and not token_problem(current):
        return normalize_token(current)
    saved = saved_token()
    if saved is not None and not token_problem(saved):
        return normalize_token(saved)
    return current if current is not None else saved


def redact(text: str) -> str:
    """오류 메시지에 섞여 나오는 인증 값을 가린다 (콘솔·로그·스크린샷 노출 방지)."""
    text = re.sub(r"(Bearer\s+)[^\s'\"]+", r"\1***", text)
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-***", text)


class ClaudeCliError(RuntimeError):
    pass


class NotLoggedIn(ClaudeCliError):
    """CLI 는 있는데 로그인이 안 된 상태."""


# 자동 실행(창 없는 pythonw)에서 claude 를 부를 때 콘솔 창이 깜빡이지 않게 한다
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_cli() -> str | None:
    return shutil.which("claude")


def clean_env() -> dict[str, str]:
    """부모 Claude Code 세션이 주입한 변수를 걷어내고, 토큰은 골라서 정리해 넘긴다."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in KEEP_ALWAYS and k not in STRIP_EXACT and not k.startswith(STRIP_PREFIX)
    }
    if (token := resolve_token()) is not None:
        env[TOKEN_VAR] = normalize_token(token)
    return env


def auth_status(exe: str | None = None, timeout: int = 60) -> dict:
    """`claude auth status` 결과. 실패해도 예외 대신 dict 을 돌려준다."""
    exe = exe or find_cli()
    if not exe:
        return {"loggedIn": False, "authMethod": "none", "error": "cli-not-found"}
    try:
        p = subprocess.run(
            [exe, "auth", "status"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=clean_env(), timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"loggedIn": False, "authMethod": "none", "error": str(e)}

    blob = (p.stdout or "").strip() or (p.stderr or "").strip()
    try:
        return json.loads(blob[blob.index("{"):blob.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"loggedIn": False, "authMethod": "none", "raw": blob[:200]}


LOGIN_HELP = """
Claude Code CLI 인증이 없습니다.

`claude auth login` 으로 로그인해도 풀리지 않는 경우가 있습니다. 로그인 정보가
~/.claude/.credentials.json 에 저장되는데, Claude Code 데스크톱 앱이 같은 파일을
관리하기 때문에 앱이 이를 덮어쓰면 CLI 로그인이 사라집니다.

그래서 장기 토큰을 권장합니다 (추가 요금 없음, 구독 사용량으로 처리):

  1) claude setup-token     을 실행해 토큰(sk-ant- 로 시작)을 발급받고
  2) python set_token.py    을 실행해 입력창에 그 토큰을 붙여넣은 뒤
                            (명령어 수정 불필요, 저장 전에 동작을 확인합니다)
  3) 같은 창에서 그대로 다시 실행하면 됩니다

이 토큰은 파일이 아니라 환경변수에 저장되므로 데스크톱 앱의 영향을 받지 않습니다.

인증이 번거로우면 무료 대안으로 백엔드를 바꾸세요:
  --backend ollama     로컬 모델 (설치 필요, 메일이 PC 밖으로 안 나감)
  --backend rules      요약 품질은 낮지만 즉시 동작 (요약 전용, 추출은 불가)
""".strip()


def preflight() -> str:
    """호출 가능한 상태인지 확인하고 실행 파일 경로를 돌려준다."""
    exe = find_cli()
    if not exe:
        raise ClaudeCliError(
            "`claude` CLI 를 찾지 못했습니다.\n"
            "  설치: https://claude.com/claude-code\n"
            "  또는 백엔드를 바꾸세요: python run.py --backend rules"
        )
    if msg := token_problem(resolve_token()):
        raise ClaudeCliError(msg)
    status = auth_status(exe)
    if not status.get("loggedIn"):
        raise NotLoggedIn(LOGIN_HELP)
    return exe


def token_problem(token: str | None) -> str | None:
    """토큰이 명백히 잘못됐으면 이유를, 아니면 None.

    `claude auth status` 는 토큰이 설정돼 있기만 하면 로그인으로 보고하고 실제 값은
    검증하지 않는다. 그래서 잘못된 값이 들어가면 실제 호출에서야 실패한다.
    공백·줄바꿈은 clean_env 가 걷어내고 넘기므로 여기서 문제로 보지 않는다.
    """
    if token is None:
        return None
    t = normalize_token(token)
    fix = ("  `claude setup-token` 으로 토큰(sk-ant- 로 시작)을 발급받은 뒤\n"
           "  python set_token.py 를 실행해 입력창에 붙여넣으세요 (명령어 수정 불필요).\n"
           "  토큰은 채팅에 붙여넣지 마세요.")
    if not t:
        return f"{TOKEN_VAR} 이 비어 있습니다.\n" + fix
    if "<" in t or ">" in t or not t.isascii():
        return (f"{TOKEN_VAR} 에 토큰에 쓰일 수 없는 글자(한글, < > 등)가 들어 있습니다. "
                "안내문의 예시 문구가 저장된 것 같습니다.\n" + fix)
    if len(t) < 40:
        return f"{TOKEN_VAR} 이 너무 짧아 토큰 일부만 복사된 것 같습니다.\n" + fix
    return None


def run_prompt(prompt: str, model: str = "", timeout: int = 300,
               exe: str | None = None) -> str:
    """print 모드로 프롬프트를 던지고 표준출력을 그대로 돌려준다."""
    exe = exe or preflight()
    cmd = [exe, "-p", "--disallowed-tools", NO_TOOLS]
    if model:
        cmd += ["--model", model]
    try:
        p = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=clean_env(), timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeCliError(
            f"claude CLI 응답 시간 초과 ({timeout}초). "
            "메일 수를 줄이거나 CLAUDE_CODE_TIMEOUT 을 늘리세요."
        ) from None

    if p.returncode != 0:
        detail = redact((p.stderr or p.stdout or "").strip())
        low = detail.lower()
        if "authorization" in low and "invalid" in low or "token is invalid" in low:
            raise ClaudeCliError(
                "Claude 가 토큰을 거부했습니다 (만료됐거나 잘못된 토큰).\n"
                "  `claude setup-token` 으로 다시 발급한 뒤 python set_token.py 로 저장하세요.")
        if "login" in low or "not logged in" in low:
            raise NotLoggedIn(LOGIN_HELP)
        raise ClaudeCliError(f"claude CLI 실패 (exit {p.returncode}): {detail[:400]}")
    if not (out := (p.stdout or "").strip()):
        raise ClaudeCliError("claude CLI 가 빈 응답을 반환했습니다.")
    return out
