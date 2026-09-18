"""환경설정 로딩."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# .env 가 없거나 읽을 수 없어도 죽지 않는다.
# 사내 DRM(문서보안)이 파일을 암호화해 두면 텍스트로 파싱되지 않는데,
# 그 경우에도 OS 환경변수로 값을 받아 정상 동작해야 한다.
ENV_LOAD_ERROR: str | None = None
if ENV_FILE.exists():
    try:
        load_dotenv(ENV_FILE, encoding="utf-8")
    except UnicodeDecodeError:
        head = ENV_FILE.read_bytes()[:16]
        if b"DocuRay" in head or b"DRM" in head.upper():
            ENV_LOAD_ERROR = (
                ".env 가 사내 DRM(문서보안)으로 암호화되어 읽을 수 없습니다. "
                "메모장 등으로 저장하면 DRM 이 파일을 암호화합니다."
            )
        else:
            ENV_LOAD_ERROR = (
                ".env 인코딩이 UTF-8 이 아니라 읽을 수 없습니다. "
                "메모장에서 [다른 이름으로 저장] > 인코딩을 UTF-8 로 지정하세요."
            )
    except OSError as e:
        ENV_LOAD_ERROR = f".env 를 읽지 못했습니다: {e}"

# 공식 문서: https://developers.worksmobile.com/kr/docs/auth-oauth
AUTHORIZE_URL = "https://auth.worksmobile.com/oauth2/v2.0/authorize"
TOKEN_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
# 공식 문서: https://developers.worksmobile.com/kr/docs/mail
API_BASE = "https://www.worksapis.com/v1.0"

TOKEN_FILE = ROOT / ".tokens.json"
CERT_DIR = ROOT / ".certs"

# 추가 과금이 발생하는 백엔드
PAID_BACKENDS = {"api"}


@dataclass(frozen=True)
class Config:
    # 메일 수집 경로: "imap" (앱 불필요) | "api" (Developer Console 앱 필요)
    mail_source: str
    # 요약 백엔드: "rules" | "claude_code" | "ollama" | "api"
    summary_backend: str

    # -- 수집: API 경로 --
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str

    # -- 수집: IMAP 경로 --
    imap_user: str
    imap_password: str

    # -- 요약: claude_code --
    claude_code_model: str
    claude_code_timeout: int

    # -- 요약: ollama --
    ollama_host: str
    ollama_model: str
    ollama_timeout: int

    # -- 요약: api --
    anthropic_api_key: str
    anthropic_model: str

    # -- 대시보드 기간: 최근 N일 안에 받은 메일이 있는 대화만 다룬다 --
    window_days: int = 7
    # -- 대시보드 로컬 서버 (127.0.0.1) 포트 --
    dashboard_port: int = 8787

    @property
    def is_paid(self) -> bool:
        return self.summary_backend in PAID_BACKENDS

    @property
    def missing_source(self) -> list[str]:
        """선택한 수집 경로에 필요한데 비어 있는 설정값.

        IMAP 비밀번호는 실행 시 입력받을 수 있으므로 여기서 제외한다.
        """
        if self.mail_source == "imap":
            return [] if self.imap_user else ["NW_IMAP_USER"]
        return [k for k, v in (("NW_CLIENT_ID", self.client_id),
                               ("NW_CLIENT_SECRET", self.client_secret)) if not v]

    def with_prompted_password(self) -> Config:
        """비밀번호를 환경변수 → Windows 자격 증명 관리자 → 입력 순으로 구한다.

        사내 DRM 이 걸린 환경에서는 .env 에 비밀번호를 저장하는 것 자체가 불가능하고,
        저장하지 않는 편이 보안상으로도 낫다. 자동 실행처럼 입력할 사람이 없으면
        기다리지 않고 MissingPassword 를 던진다.
        """
        import dataclasses
        import getpass
        import sys

        from .secret import load_imap_password

        if self.mail_source != "imap" or self.imap_password:
            return self
        if saved := load_imap_password(self.imap_user):
            return dataclasses.replace(self, imap_password=saved)
        # Windows 에서는 입력을 NUL 로 막아도 isatty() 가 True 라서 환경변수로도 판별한다
        if os.getenv(NONINTERACTIVE_ENV) or sys.stdin is None or not sys.stdin.isatty():
            raise MissingPassword(
                "저장된 IMAP 비밀번호가 없어 자동 실행에서 메일을 받을 수 없습니다. "
                "터미널에서 `python set_password.py` 로 한 번 저장하세요.")
        print(f"{self.imap_user}")
        print("  계정 비밀번호가 아니라 '외부 앱 비밀번호' 를 입력하세요.")
        print("  (네이버웍스 PC웹 > 우측 상단 프로필 > 보안 > 외부 앱 비밀번호 생성하기)")
        pw = getpass.getpass("  외부 앱 비밀번호 (화면에 표시되지 않습니다): ")
        return dataclasses.replace(self, imap_password=normalize_password(pw))


    @property
    def missing_llm(self) -> list[str]:
        """선택한 요약 백엔드에 필요한데 비어 있는 설정값."""
        if self.summary_backend == "api":
            return [] if self.anthropic_api_key else ["ANTHROPIC_API_KEY"]
        return []   # rules / claude_code / ollama 는 키가 필요 없다


NONINTERACTIVE_ENV = "NWMAIL_NONINTERACTIVE"     # auto.py 가 설정. 입력을 절대 기다리지 않는다


class MissingPassword(RuntimeError):
    """자동 실행 중인데 비밀번호를 구할 곳이 없다."""


def normalize_password(pw: str) -> str:
    """붙여넣기 사고를 막는다.

    외부 앱 비밀번호는 화면에 4자리씩 끊어 보여주는 경우가 있어 공백째로 복사되기
    쉽고, 줄바꿈이나 비가시 문자(zero-width, BOM)가 딸려 오기도 한다.
    실제 비밀번호는 영문+숫자 조합이므로 공백류를 전부 제거하는 편이 안전하다.
    """
    if not pw:
        return ""
    for ch in ("​", "‌", "‍", "﻿", " "):
        pw = pw.replace(ch, "")
    return "".join(pw.split())


def load_config() -> Config:
    return Config(
        mail_source=os.getenv("MAIL_SOURCE", "imap").strip().lower(),
        summary_backend=os.getenv("SUMMARY_BACKEND", "claude_code").strip().lower(),
        client_id=os.getenv("NW_CLIENT_ID", "").strip(),
        client_secret=os.getenv("NW_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv("NW_REDIRECT_URI", "https://localhost:8443/callback").strip(),
        scope=os.getenv("NW_SCOPE", "mail.read").strip(),
        imap_user=os.getenv("NW_IMAP_USER", "").strip(),
        imap_password=os.getenv("NW_IMAP_PASSWORD", "").strip(),
        claude_code_model=os.getenv("CLAUDE_CODE_MODEL", "").strip(),
        claude_code_timeout=int(os.getenv("CLAUDE_CODE_TIMEOUT", "300")),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b").strip(),
        ollama_timeout=int(os.getenv("OLLAMA_TIMEOUT", "600")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip(),
        window_days=int(os.getenv("WINDOW_DAYS", "7")),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8787")),
    )
