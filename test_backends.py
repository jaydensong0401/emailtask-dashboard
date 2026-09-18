"""요약 백엔드 검증 — 어떤 백엔드가 지금 쓸 수 있고 얼마가 드는지 확인한다.

rules 백엔드는 네트워크도 키도 필요 없으므로 여기서 끝까지 실동작을 검증한다.
"""
from __future__ import annotations

import shutil

import requests

from nwmail.client import Mail
from nwmail.config import load_config
from nwmail.rules import RuleSummarizer

# 실제 업무 메일과 비슷한 검증용 샘플
SAMPLE_MAILS = [
    Mail(mail_id="1", subject="[긴급] 3분기 계약서 검토 요청",
         from_name="김영업", from_email="sales@partner.co.kr",
         received_time="2026-09-10T09:12:00+09:00", status="Unread", attach_count=2,
         body="안녕하세요.\n첨부된 계약서 9월 20일까지 검토 후 회신 부탁드립니다.\n"
              "감사합니다.\n--\n김영업 드림\n본 메일은 발신전용입니다."),
    Mail(mail_id="2", subject="주간 팀 미팅 일정 안내",
         from_name="박팀장", from_email="pm@company.com",
         received_time="2026-09-10T08:30:00+09:00", status="Unread",
         body="이번 주 목요일 오후 3시 회의실 A에서 진행합니다. 참석 부탁드립니다."),
    Mail(mail_id="3", subject="[뉴스레터] 이번 주 테크 트렌드",
         from_name="TechWeekly", from_email="noreply@techweekly.com",
         received_time="2026-09-09T07:00:00+09:00", status="Read",
         body="이번 주 주요 소식입니다. 수신거부는 하단 링크를 클릭하세요."),
    Mail(mail_id="4", subject="전자결재 상신 - 9월 출장비 정산",
         from_name="이대리", from_email="lee@company.com",
         received_time="2026-09-09T17:40:00+09:00", status="Unread",
         body="출장비 정산 건 결재 승인 부탁드립니다. 금주까지 처리 필요합니다."),
    Mail(mail_id="5", subject="사내 보안 교육 이수 안내",
         from_name="보안팀", from_email="security@company.com",
         received_time="2026-09-08T10:00:00+09:00", status="Read",
         body="전사 보안 교육 안내입니다. 자세한 내용은 사내 포털을 참고하세요."),
]


def test_backends(check, hdr, section: int = 7) -> None:
    hdr(section, "요약 백엔드 — 가용성과 비용")
    cfg = load_config()

    print("  <rules — LLM 없음 / 0원 / 오프라인>")
    try:
        result = RuleSummarizer().summarize(SAMPLE_MAILS)
        by_id = {i["mail_id"]: i for i in result["items"]}
        for label, ok in [
            ("5통 전부 처리", len(result["items"]) == 5),
            ("기한 추출 (9월 20일)", by_id["1"]["deadline"] == "9월 20일"),
            ("기한 추출 (금주까지)", "금주" in by_id["4"]["deadline"]),
            ("긴급 -> high", by_id["1"]["priority"] == "high"),
            ("서명/면책/인사말 제거", "발신전용" not in by_id["1"]["summary"]
                                      and not by_id["1"]["summary"].startswith("안녕")),
            ("회의 분류", by_id["2"]["category"] == "회의"),
            ("noreply -> 뉴스레터 + low", by_id["3"]["category"] == "뉴스레터"
                                           and by_id["3"]["priority"] == "low"),
            ("승인결재 분류", by_id["4"]["category"] == "승인결재"),
            ("공지 분류", by_id["5"]["category"] == "공지"),
            ("API 호출 없음 (과금 0)", "_usage" not in result),
        ]:
            check(f"rules: {label}", ok)
    except Exception as e:
        check("rules 백엔드", False, f"{type(e).__name__}: {e}")

    print("  <claude_code — 구독에 포함 / 추가 과금 없음>")
    from nwmail import claude_cli

    env = claude_cli.clean_env()
    check("환경변수 격리: ANTHROPIC_BASE_URL 제거",
          "ANTHROPIC_BASE_URL" not in env,
          "부모 Claude Code 세션의 프록시 상속 차단")
    check("환경변수 격리: CLAUDE_* 제거 (장기 토큰 변수만 예외)",
          not any(k.startswith("CLAUDE") and k != claude_cli.TOKEN_VAR for k in env))
    check("환경변수 격리: PATH 보존", "PATH" in env or "Path" in env)

    exe = claude_cli.find_cli()
    check("claude CLI 설치됨", bool(exe), exe or "미설치")
    if exe:
        status = claude_cli.auth_status(exe)
        logged_in = bool(status.get("loggedIn"))
        note = f"authMethod={status.get('authMethod', 'none')}"
        if not logged_in:
            note += " — `claude auth login` 또는 `claude setup-token` 1회 필요"
        check("claude CLI 로그인됨", logged_in or None, note)
        if not logged_in:
            try:
                claude_cli.preflight()
                check("미로그인 시 안내 예외", False, "예외가 발생해야 함")
            except claude_cli.NotLoggedIn as e:
                check("미로그인 시 해결법 안내",
                      "claude auth login" in str(e) and "--backend rules" in str(e))

    print("  <ollama — 로컬 모델 / 0원>")
    try:
        r = requests.get(f"{cfg.ollama_host}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        check("Ollama 서버 응답", True, f"모델 {len(models)}개: {', '.join(models[:3])}")
    except Exception:
        check("Ollama 서버 응답", None,
              f"{cfg.ollama_host} 미실행 — 쓰려면 https://ollama.com 설치 후 "
              f"`ollama pull {cfg.ollama_model}`")

    print("  <api — Anthropic 종량 과금>")
    check("ANTHROPIC_API_KEY", None if not cfg.anthropic_api_key else True,
          "미설정 (무료 백엔드를 쓰면 필요 없음)" if not cfg.anthropic_api_key
          else "설정됨 — 실행 시 과금됩니다")

    print(f"  <현재 선택: SUMMARY_BACKEND={cfg.summary_backend}>")
    check("선택한 백엔드가 유효함", cfg.summary_backend in BACKENDS_NAMES,
          "과금 대상" if cfg.summary_backend == "api" else "무료")


BACKENDS_NAMES = {"rules", "claude_code", "ollama", "api"}
