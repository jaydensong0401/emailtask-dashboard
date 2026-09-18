"""네이버웍스 OAuth 2.0 (구성원 계정 인증).

메일 API는 구성원 계정 Access Token 만 허용한다. Service Account(JWT) 토큰으로는
호출할 수 없으므로 Authorization Code Grant 를 사용한다.
문서: https://developers.worksmobile.com/kr/docs/auth-oauth
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import secrets
import ssl
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from .config import AUTHORIZE_URL, CERT_DIR, TOKEN_FILE, TOKEN_URL, Config


class AuthError(RuntimeError):
    pass


# ── 토큰 저장소 ──────────────────────────────────────────────────────────────
def save_tokens(data: dict) -> None:
    data = dict(data)
    data["obtained_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass  # Windows 에서는 무시


def load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def _is_expired(tokens: dict, skew: int = 120) -> bool:
    obtained = dt.datetime.fromisoformat(tokens["obtained_at"])
    age = (dt.datetime.now(dt.timezone.utc) - obtained).total_seconds()
    return age >= int(tokens.get("expires_in", 3600)) - skew


# ── 1단계: Authorization Code 요청 URL ───────────────────────────────────────
def build_authorize_url(cfg: Config, state: str) -> str:
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scope,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ── 2단계: Access Token 교환 ────────────────────────────────────────────────
def exchange_code(cfg: Config, code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "redirect_uri": cfg.redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"토큰 발급 실패 HTTP {resp.status_code}: {resp.text}")
    return resp.json()


# ── 3단계: Refresh ──────────────────────────────────────────────────────────
def refresh_tokens(cfg: Config, refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"토큰 갱신 실패 HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def get_access_token(cfg: Config) -> str:
    """저장된 토큰을 읽고, 만료되었으면 refresh 해서 유효한 Access Token 을 돌려준다."""
    tokens = load_tokens()
    if not tokens:
        raise AuthError("저장된 토큰이 없습니다. 먼저 `python login.py` 를 실행하세요.")
    if _is_expired(tokens):
        rt = tokens.get("refresh_token")
        if not rt:
            raise AuthError("Access Token 이 만료되었고 refresh_token 도 없습니다. 다시 로그인하세요.")
        new = refresh_tokens(cfg, rt)
        new.setdefault("refresh_token", rt)  # rotation 미사용 시 기존 값 유지
        save_tokens(new)
        tokens = load_tokens()
    return tokens["access_token"]


# ── 로컬 HTTPS 콜백 서버 ────────────────────────────────────────────────────
# 네이버웍스는 Redirect URL 에 HTTPS 를 요구하므로 self-signed 인증서로 서버를 띄운다.
def ensure_self_signed_cert() -> tuple[Path, Path]:
    CERT_DIR.mkdir(exist_ok=True)
    cert_path, key_path = CERT_DIR / "localhost.pem", CERT_DIR / "localhost-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def interactive_login(cfg: Config) -> dict:
    """브라우저를 열어 로그인하고, 콜백으로 받은 code 를 토큰으로 교환한다."""
    parsed = urllib.parse.urlparse(cfg.redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            ok = "code" in result and result.get("state") == state
            msg = "인증 완료. 터미널로 돌아가세요." if ok else f"인증 실패: {result}"
            body = f"<html><meta charset='utf-8'><body><h3>{msg}</h3></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *_):  # 서버 로그 억제
            pass

    cert, key = ensure_self_signed_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    httpd = http.server.HTTPServer((host, port), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    url = build_authorize_url(cfg, state)
    print(f"브라우저에서 로그인하세요:\n  {url}\n")
    print(f"(self-signed 인증서 경고가 뜨면 '고급 → 계속 진행' 을 선택하세요. 콜백: {cfg.redirect_uri})")
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    httpd.handle_request()   # 콜백 1회 수신
    httpd.server_close()

    if "code" not in result:
        raise AuthError(f"Authorization Code 를 받지 못했습니다: {result}")
    if result.get("state") != state:
        raise AuthError("state 불일치 — CSRF 가능성이 있어 중단합니다.")

    tokens = exchange_code(cfg, result["code"])
    save_tokens(tokens)
    return tokens
