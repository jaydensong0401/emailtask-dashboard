"""네이버웍스 메일 요약 에이전트 - 실현 가능성 검증.

자격증명 없이 도는 검증(1~7)과, .env 가 채워졌을 때만 도는 실제 호출(8~9).
수집 경로는 두 가지이며 둘 다 검증한다.
  - api  : Developer Console 앱 필요 (관리자 권한 필요)
  - imap : 앱 불필요 (관리자가 IMAP 허용만 하면 됨)
"""
from __future__ import annotations

import email
import imaplib
import smtplib
import socket
import ssl
import sys
import threading
import urllib.parse
from email.message import EmailMessage

import requests

from nwmail.auth import build_authorize_url, ensure_self_signed_cert
from nwmail.client import NaverWorksMail, _html_to_text
from nwmail.config import API_BASE, AUTHORIZE_URL, TOKEN_URL, load_config
from nwmail.imap_client import (
    IMAP_HOST, IMAP_PORT, SMTP_HOST, SMTP_PORT,
    _count_attachments, _decode, _extract_body,
)
from nwmail.summarizer import SCHEMA, build_prompt
from test_backends import test_backends

PASS, FAIL, SKIP = "  [PASS]", "  [FAIL]", "  [SKIP]"
results: list[tuple[str, bool | None]] = []


def check(name: str, ok: bool | None, detail: str = "") -> None:
    tag = SKIP if ok is None else (PASS if ok else FAIL)
    print(f"{tag} {name}" + (f" - {detail}" if detail else ""))
    results.append((name, ok))


def hdr(n: int, title: str) -> None:
    print(f"\n[{n}] {title}\n" + "-" * 70)


# -- 1. 의존성 ---------------------------------------------------------------
def test_deps() -> None:
    hdr(1, "런타임 의존성")
    for mod, needed in [("requests", True), ("bs4", True), ("cryptography", True),
                        ("dotenv", True), ("anthropic", False)]:
        try:
            __import__(mod)
            check(f"{mod} 설치됨", True)
        except ImportError:
            check(f"{mod} 설치됨", False if needed else None,
                  "pip install -r requirements.txt" if needed else "요약 실행 시 필요")


# -- 2. OAuth 엔드포인트 (api 경로) ------------------------------------------
def test_oauth_endpoints() -> None:
    hdr(2, "[api 경로] OAuth 2.0 엔드포인트")
    try:
        r = requests.get(AUTHORIZE_URL, params={
            "client_id": "FEASIBILITY_PROBE",
            "redirect_uri": "https://localhost:8443/callback",
            "scope": "mail.read", "response_type": "code", "state": "probe",
        }, timeout=15, allow_redirects=False)
        check("authorize 엔드포인트 응답", r.status_code in (200, 302),
              f"HTTP {r.status_code}")
    except Exception as e:
        check("authorize 엔드포인트 응답", False, str(e))

    try:
        r = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": "PROBE",
            "client_id": "PROBE", "client_secret": "PROBE",
        }, timeout=15)
        body = r.json()
        ok = r.status_code == 401 and body.get("error") == "unauthorized_client"
        check("token 엔드포인트가 OAuth 규격 오류 반환", ok,
              f"HTTP {r.status_code} / {body.get('error')}")
    except Exception as e:
        check("token 엔드포인트가 OAuth 규격 오류 반환", False, str(e))


# -- 3. Mail API 엔드포인트 (api 경로) ---------------------------------------
def test_mail_endpoints() -> None:
    hdr(3, "[api 경로] Mail API 엔드포인트 도달성")
    endpoints = {
        "메일함 목록": "/users/me/mail/mailfolders",
        "메일 목록": "/users/me/mail/mailfolders/0/children?count=5",
        "메일 검색": "/users/me/mail/search?query=test&count=5",
        "메일 상세": "/users/me/mail/1",
        "안읽음 개수": "/users/me/mail/unread-count",
    }
    for label, path in endpoints.items():
        try:
            r = requests.get(f"{API_BASE}{path}",
                             headers={"Authorization": "Bearer PROBE"}, timeout=15)
            body = r.json()
            ok = r.status_code == 401 and body.get("code") == "UNAUTHORIZED"
            check(f"{label} - 살아있음", ok, f"HTTP {r.status_code} / {body.get('code')}")
        except Exception as e:
            check(f"{label} - 살아있음", False, str(e))


# -- 4. IMAP / SMTP 서버 (imap 경로) -----------------------------------------
def test_imap_server() -> None:
    hdr(4, "[imap 경로] 메일 서버 도달성 (앱 등록 불필요한 경로)")
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
        caps = M.capabilities
        M.logout()
        check(f"IMAPS {IMAP_HOST}:{IMAP_PORT} 접속", "IMAP4REV1" in caps,
              f"caps={','.join(caps[:5])}")
    except Exception as e:
        check(f"IMAPS {IMAP_HOST}:{IMAP_PORT} 접속", False, str(e))

    try:
        S = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        S.ehlo()
        S.starttls()
        S.ehlo()
        has_auth = "auth" in S.esmtp_features
        S.quit()
        check(f"SMTP+STARTTLS {SMTP_HOST}:{SMTP_PORT} 접속", has_auth, "AUTH 지원")
    except Exception as e:
        check(f"SMTP {SMTP_HOST}:{SMTP_PORT} 접속", False, str(e))

    try:
        imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15).login("probe@invalid", "x")
        check("잘못된 자격증명 거부", False, "로그인이 통과되면 안 됨")
    except imaplib.IMAP4.error:
        check("잘못된 자격증명 거부", True, "인증 요구 확인")
    except Exception as e:
        check("잘못된 자격증명 거부", False, str(e))


# -- 5. HTTPS 콜백 서버 (api 경로의 Redirect URL HTTPS 요구) -----------------
def test_https_callback() -> None:
    hdr(5, "[api 경로] 로컬 HTTPS 콜백 서버")
    try:
        cert, key = ensure_self_signed_cert()
        check("self-signed 인증서 생성", cert.exists() and key.exists(), cert.name)
    except Exception as e:
        check("self-signed 인증서 생성", False, str(e))
        return

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def serve():
            conn, _ = srv.accept()
            with ctx.wrap_socket(conn, server_side=True) as s:
                s.recv(1024)
                s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        threading.Thread(target=serve, daemon=True).start()
        r = requests.get(f"https://127.0.0.1:{port}/callback?code=X",
                         verify=False, timeout=10)
        srv.close()
        check("HTTPS 콜백 수신", r.status_code == 200 and r.text == "ok", f"포트 {port}")
    except Exception as e:
        check("HTTPS 콜백 수신", False, str(e))


# -- 6. 파싱 + 프롬프트 -------------------------------------------------------
API_SAMPLE = {
    "mailId": "123456", "folderId": 0, "status": "Unread",
    "from": {"name": "김영업", "email": "sales@partner.co.kr"},
    "to": [{"name": "나", "email": "me@company.com"}],
    "subject": "[협의요청] 3분기 계약 갱신 건",
    "body": "<html><body><p>안녕하세요.</p><p>9월 20일까지 회신 부탁드립니다.</p>"
            "<script>alert(1)</script></body></html>",
    "receivedTime": "2026-09-10T09:12:00+09:00", "attachCount": 2,
}

IMAP_SUBJECT = "[협의요청] 3분기 계약 갱신 건"
IMAP_SENDER_NAME = "김영업"
IMAP_HTML = ("<html><body><p>안녕하세요.</p>"
             "<p>9월 20일까지 회신 부탁드립니다.</p></body></html>")


def _make_mime() -> bytes:
    """검증용 MIME 메시지를 표준 라이브러리로 생성한다.

    RFC 2047 헤더 인코딩을 손으로 쓰지 않으므로 픽스처 자체가 틀릴 여지가 없다.
    """
    msg = EmailMessage()
    msg["From"] = f"{IMAP_SENDER_NAME} <sales@partner.co.kr>"
    msg["To"] = "me@company.com"
    msg["Subject"] = IMAP_SUBJECT
    msg["Date"] = "Thu, 10 Sep 2026 09:12:00 +0900"
    msg.set_content(IMAP_HTML, subtype="html")
    msg.add_attachment(b"Hello", maintype="application", subtype="pdf",
                       filename="contract.pdf")
    return msg.as_bytes()


def test_parsing() -> None:
    hdr(6, "응답 파싱 + 프롬프트 구성")

    print("  <api 경로: 공식 문서 응답 스키마>")
    try:
        m = NaverWorksMail._to_mail(API_SAMPLE)
        for ok, label in [
            (m.mail_id == "123456", "mailId"),
            (m.from_email == "sales@partner.co.kr", "from.email"),
            (m.is_unread is True, "status->안읽음"),
            (m.attach_count == 2, "attachCount"),
            (m.to == ["me@company.com"], "to[]"),
        ]:
            check(f"필드 매핑 {label}", ok)
        text = _html_to_text(API_SAMPLE["body"])
        check("HTML 본문 -> 텍스트 (script 제거)",
              "9월 20일" in text and "alert(1)" not in text, repr(text[:36]))
    except Exception as e:
        check("api 응답 파싱", False, f"{type(e).__name__}: {e}")

    print("  <imap 경로: 실제 RFC822 MIME 메시지>")
    try:
        raw = _make_mime()
        check("MIME 헤더가 실제로 인코딩됨", b"=?utf-8?" in raw, f"{len(raw)}바이트")
        msg = email.message_from_bytes(raw)
        subject = _decode(msg.get("Subject"))
        sender = _decode(msg.get("From"))
        body = _extract_body(msg)
        attach = _count_attachments(msg)
        check("MIME 헤더 디코딩 (한글 제목)", subject == IMAP_SUBJECT, repr(subject))
        check("보낸사람 디코딩", IMAP_SENDER_NAME in sender, repr(sender[:32]))
        check("multipart 본문 추출 + HTML 정리",
              "9월 20일" in body and "<p>" not in body, repr(body[:36]))
        check("첨부파일 개수 계산", attach == 1, f"{attach}건")
    except Exception as e:
        check("imap 메시지 파싱", False, f"{type(e).__name__}: {e}")

    print("  <요약 레이어>")
    try:
        prompt = build_prompt([NaverWorksMail._to_mail(API_SAMPLE)])
        check("요약 프롬프트 생성",
              "mail_id: 123456" in prompt and "9월 20일" in prompt, f"{len(prompt)}자")
        req = SCHEMA["properties"]["items"]["items"]["required"]
        check("출력 JSON 스키마 정의",
              "priority" in req and "action_required" in req, f"필드 {len(req)}개")
    except Exception as e:
        check("요약 프롬프트 생성", False, f"{type(e).__name__}: {e}")


# -- 7. 실제 메일 조회 -------------------------------------------------------
def test_live_mail() -> bool:
    cfg = load_config()
    hdr(8, f"실제 메일 조회 (MAIL_SOURCE={cfg.mail_source})")
    if missing := cfg.missing_source:
        check("자격증명", None, f"미설정: {', '.join(missing)}")
        return False
    # 비밀번호는 실행 시 입력받으므로 .env 에 없을 수 있다. 검증에서는 묻지 않고 건너뛴다.
    if cfg.mail_source == "imap" and not cfg.imap_password:
        check("자격증명", None,
              "비밀번호 미설정 — 실행 시 입력받습니다. 여기서 검증하려면 "
              "NW_IMAP_PASSWORD 환경변수를 지정하고 다시 실행하세요")
        return False
    check("자격증명", True, f"source={cfg.mail_source}")

    try:
        if cfg.mail_source == "imap":
            from nwmail.imap_client import NaverWorksImap
            with NaverWorksImap(cfg.imap_user, cfg.imap_password) as imap:
                check("IMAP 로그인", True, cfg.imap_user)
                check("메일함 목록", True, f"{len(imap.folders())}개")
                mails = imap.list_mails(limit=3)
                first = len(mails[0].body) if mails else 0
                check("메일 조회 (본문 포함)", bool(mails),
                      f"{len(mails)}통, 첫 본문 {first}자")
        else:
            from nwmail.auth import get_access_token
            url = build_authorize_url(cfg, "teststate")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            check("authorize URL 구성", q.get("response_type") == ["code"],
                  f"scope={q.get('scope', [''])[0]}")
            api = NaverWorksMail(get_access_token(cfg))
            check("메일함 목록", True, f"{len(api.folders())}개")
            mails = api.list_mails(api.inbox_folder_id(), limit=3)
            check("메일 목록", True, f"{len(mails)}통")
            if mails:
                full = api.get_mail(mails[0].mail_id)
                check("메일 본문 조회", bool(full.subject), f"본문 {len(full.body)}자")
        return True
    except Exception as e:
        check("실제 메일 조회", False, f"{type(e).__name__}: {e}")
        return False


# -- 8. 실제 요약 ------------------------------------------------------------
def test_live_summary(mail_ok: bool) -> None:
    hdr(9, "실제 요약 실행")
    cfg = load_config()
    if cfg.missing_llm:
        check(f"{cfg.summary_backend} 설정", None,
              f"미설정: {', '.join(cfg.missing_llm)}")
        return
    if not mail_ok:
        check("요약 실행", None, "8단계 미통과로 건너뜀")
        return
    try:
        from nwmail.agent import MailSummaryAgent, render
        with MailSummaryAgent(cfg) as agent:
            result = agent.run(limit=3)
        check("메일 요약 생성", bool(result.get("items")), f"{result.get('count')}통")
        print("\n" + render(result))
    except Exception as e:
        check("메일 요약 생성", False, f"{type(e).__name__}: {e}")


def main() -> int:
    print("=" * 70)
    print("네이버웍스 메일 요약 에이전트 - 실현 가능성 검증")
    print("=" * 70)
    requests.packages.urllib3.disable_warnings()

    test_deps()
    test_oauth_endpoints()
    test_mail_endpoints()
    test_imap_server()
    test_https_callback()
    test_parsing()
    test_backends(check, hdr, section=7)
    mail_ok = test_live_mail()
    test_live_summary(mail_ok)

    passed = sum(1 for _, ok in results if ok is True)
    failed = sum(1 for _, ok in results if ok is False)
    skipped = sum(1 for _, ok in results if ok is None)
    print("\n" + "=" * 70)
    print(f"결과: {passed} PASS / {failed} FAIL / {skipped} SKIP")
    print("=" * 70)
    if failed == 0 and skipped:
        print("\n결론: 두 경로 모두 구조적으로 구현 가능. 남은 것은 자격증명뿐입니다.")
    elif failed == 0:
        print("\n결론: 전 구간 실동작 확인 완료.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
