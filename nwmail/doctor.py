"""현재 설정으로 실제 실행이 가능한지 진단하고, 막힌 지점의 해결책을 알려준다."""
from __future__ import annotations

import shutil

from . import claude_cli
from .config import Config

OK, NO, WARN = "[OK]  ", "[X]   ", "[!]   "


def _line(tag: str, label: str, detail: str = "") -> str:
    return f"{tag}{label}" + (f" — {detail}" if detail else "")


def diagnose(cfg: Config) -> tuple[list[str], list[str]]:
    """(출력 줄, 조치사항) 을 돌려준다. 조치사항이 비면 바로 실행 가능."""
    lines: list[str] = []
    todo: list[str] = []

    # -- 메일 수집 --
    lines.append(f"[메일 수집]  MAIL_SOURCE={cfg.mail_source}")
    if missing := cfg.missing_source:
        lines.append(_line(NO, f"설정 누락: {', '.join(missing)}"))
        if cfg.mail_source == "imap":
            todo.append(".env 에 NW_IMAP_USER / NW_IMAP_PASSWORD 를 채우세요. "
                        "관리자가 Admin > 보안 > 서비스 권한 에서 IMAP 을 허용해야 합니다.")
        else:
            todo.append(".env 에 NW_CLIENT_ID / NW_CLIENT_SECRET 을 채우세요.")
    else:
        who = cfg.imap_user if cfg.mail_source == "imap" else f"client_id={cfg.client_id[:6]}..."
        lines.append(_line(OK, "자격증명 설정됨", who))
        if cfg.mail_source == "imap":
            lines.extend(_check_imap_live(cfg, todo))
        else:
            from .config import TOKEN_FILE
            if TOKEN_FILE.exists():
                lines.append(_line(OK, "OAuth 토큰 있음", TOKEN_FILE.name))
            else:
                lines.append(_line(NO, "OAuth 토큰 없음"))
                todo.append("python login.py 를 실행해 최초 1회 로그인하세요.")

    # -- 요약 백엔드 --
    lines.append("")
    lines.append(f"[요약 백엔드]  SUMMARY_BACKEND={cfg.summary_backend}"
                 f"  ({'종량 과금' if cfg.is_paid else '추가 비용 없음'})")

    if cfg.summary_backend == "claude_code":
        if not (exe := claude_cli.find_cli()):
            lines.append(_line(NO, "claude CLI 미설치"))
            todo.append("Claude Code 를 설치하거나 --backend rules 로 바꾸세요.")
        else:
            lines.append(_line(OK, "claude CLI", exe))
            status = claude_cli.auth_status(exe)
            if status.get("loggedIn"):
                lines.append(_line(OK, "CLI 로그인됨",
                                   f"authMethod={status.get('authMethod')}"))
            else:
                lines.append(_line(NO, "CLI 로그인 안 됨",
                                   f"authMethod={status.get('authMethod', 'none')}"))
                todo.append("터미널에서 `claude auth login` 또는 `claude setup-token` 을 "
                            "한 번 실행하세요 (추가 요금 없음). 확인: claude auth status")
            if cfg.claude_code_model:
                lines.append(_line(OK, "모델 지정", cfg.claude_code_model))
            else:
                lines.append(_line(OK, "모델", "Claude Code 기본값"))

    elif cfg.summary_backend == "rules":
        lines.append(_line(OK, "규칙 기반 — 설치·키·네트워크 불필요"))

    elif cfg.summary_backend == "ollama":
        import requests
        try:
            r = requests.get(f"{cfg.ollama_host}/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
            lines.append(_line(OK, "Ollama 서버", cfg.ollama_host))
            if cfg.ollama_model in names:
                lines.append(_line(OK, "모델 준비됨", cfg.ollama_model))
            else:
                lines.append(_line(NO, "모델 없음", cfg.ollama_model))
                todo.append(f"ollama pull {cfg.ollama_model}")
        except Exception:
            lines.append(_line(NO, "Ollama 서버 응답 없음", cfg.ollama_host))
            todo.append("https://ollama.com 설치 후 `ollama serve` 를 띄우세요.")

    elif cfg.summary_backend == "api":
        if cfg.anthropic_api_key:
            lines.append(_line(WARN, "API 키 설정됨 — 실행할 때마다 과금됩니다",
                               cfg.anthropic_model))
        else:
            lines.append(_line(NO, "ANTHROPIC_API_KEY 없음"))
            todo.append("ANTHROPIC_API_KEY 를 채우거나, 무료인 "
                        "--backend claude_code / rules / ollama 를 쓰세요.")
        if not shutil.which("python") or not _has_anthropic():
            lines.append(_line(NO, "anthropic 패키지 미설치"))
            todo.append("pip install anthropic")

    return lines, todo


def _check_imap_live(cfg: Config, todo: list[str]) -> list[str]:
    """실제로 IMAP 로그인을 시도해 '관리자가 IMAP 을 열어줬는지' 를 판정한다.

    메뉴를 뒤지는 것보다 이 결과가 확실하다.
    """
    from .imap_client import IMAP_HOST, IMAP_PORT, NaverWorksImap

    out: list[str] = []
    try:
        with NaverWorksImap(cfg.imap_user, cfg.imap_password) as imap:
            out.append(_line(OK, f"IMAP 로그인 성공 ({IMAP_HOST}:{IMAP_PORT})"))
            folders = imap.folders()
            out.append(_line(OK, "메일함 조회", f"{len(folders)}개"))
            unread = imap.unread_count()
            out.append(_line(OK, "받은메일함 안 읽음", f"{unread}통"))
        return out
    except Exception as e:
        msg = str(e)
        out.append(_line(NO, "IMAP 로그인 실패"))
        low = msg.lower()
        if "authentication" in low or "login" in low or "credential" in low:
            out.append(_line(WARN, "인증 거부됨"))
            # IMAP 만 막힌 건지, 메일 접근 자체가 막힌 건지 POP3 로 가른다
            pop_ok, pop_note = _try_pop3(cfg)
            if pop_ok:
                out.append(_line(OK, "POP3 는 통과함", "IMAP 만 선택적으로 막힌 상태"))
                todo.append(
                    "IMAP 은 거부됐지만 POP3 로는 로그인됩니다. 관리자에게 IMAP 도 "
                    "함께 허용해달라고 요청하세요 (같은 화면: Admin > 보안 > 서비스 권한)."
                )
            else:
                out.append(_line(WARN, "POP3 도 거부됨", pop_note))
                todo.append(
                    "IMAP·POP3 가 모두 거부되었습니다. 가능성이 높은 순서입니다:\n"
                    "        (1) '외부 앱 비밀번호' 를 쓰지 않음  ← 가장 흔한 원인\n"
                    "            네이버웍스는 v3.6 부터 외부 앱 접속 시 전용 비밀번호를 "
                    "의무화했습니다.\n"
                    "            계정 비밀번호로는 절대 로그인되지 않습니다.\n"
                    "            발급: PC웹 로그인 > 우측 상단 프로필 > 보안 >\n"
                    "                  외부 앱 비밀번호 생성하기 > 새 비밀번호 생성\n"
                    "        (2) 관리자가 메일 외부 접근을 허용하지 않음 "
                    "— Admin > 보안 > 서비스 권한 (반영 5~10분)\n"
                    "        (3) 요금제에 메일 서비스가 없음 "
                    "— Lite 는 메일 미제공. Standard 이상 필요"
                )
        else:
            out.append(_line(WARN, "접속 오류", msg.splitlines()[0][:120]))
            todo.append("네트워크/방화벽에서 993 포트(IMAPS)가 열려 있는지 확인하세요.")
        return out


POP3_HOST, POP3_PORT = "pop.worksmobile.com", 995


def _try_pop3(cfg: Config) -> tuple[bool, str]:
    """IMAP 이 막혔을 때 POP3 로도 막혔는지 확인해 원인 범위를 좁힌다."""
    import poplib

    try:
        M = poplib.POP3_SSL(POP3_HOST, POP3_PORT, timeout=15)
        try:
            M.user(cfg.imap_user)
            M.pass_(cfg.imap_password)
            M.stat()
            return True, f"{POP3_HOST}:{POP3_PORT}"
        finally:
            try:
                M.quit()
            except Exception:
                pass
    except poplib.error_proto as e:
        return False, str(e)[:100]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:100]


def _has_anthropic() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def render_report(cfg: Config) -> tuple[str, bool]:
    lines, todo = diagnose(cfg)
    out = ["=" * 68, "실행 준비 상태 진단", "=" * 68, ""]
    out += lines
    out += ["", "=" * 68]
    if todo:
        out.append(f"조치 필요 {len(todo)}건")
        out.append("-" * 68)
        for i, t in enumerate(todo, 1):
            out.append(f"  {i}. {t}")
    else:
        out.append("모든 항목 통과 — `python run.py` 로 바로 실행할 수 있습니다.")
    out.append("=" * 68)
    return "\n".join(out), not todo
