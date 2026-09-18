"""메일함을 읽어 요약한다."""
import argparse
import dataclasses
import sys
from pathlib import Path

from nwmail.agent import MailSummaryAgent, render, save_json
from nwmail.auth import AuthError
from nwmail.client import MailApiError
from nwmail.config import load_config
from nwmail.summarizer import BACKENDS


def main() -> int:
    p = argparse.ArgumentParser(description="네이버웍스 메일 요약 에이전트")
    p.add_argument("-n", "--limit", type=int, default=15, help="요약할 메일 수 (기본 15)")
    p.add_argument("-u", "--unread", action="store_true", help="안 읽은 메일만")
    p.add_argument("-q", "--query", help="검색어")
    p.add_argument("-o", "--out", type=Path, help="결과 JSON 저장 경로")
    p.add_argument("--source", choices=["api", "imap"], help="수집 경로 (.env 덮어쓰기)")
    p.add_argument("--backend", choices=list(BACKENDS),
                   help="요약 백엔드 (.env 덮어쓰기). api 만 과금됩니다")
    p.add_argument("--doctor", action="store_true",
                   help="실행 준비 상태를 진단하고 막힌 지점의 해결책을 출력")
    args = p.parse_args()

    cfg = load_config()
    if args.source:
        cfg = dataclasses.replace(cfg, mail_source=args.source)
    if args.backend:
        cfg = dataclasses.replace(cfg, summary_backend=args.backend)

    from nwmail.config import ENV_LOAD_ERROR
    if ENV_LOAD_ERROR:
        print(f"[!] {ENV_LOAD_ERROR}")
        print("    OS 환경변수 또는 실행 시 입력으로 대신합니다.\n")

    if args.doctor:
        from nwmail.doctor import render_report
        report, ready = render_report(cfg.with_prompted_password())
        print(report)
        return 0 if ready else 1

    cfg = cfg.with_prompted_password()

    if missing := cfg.missing_source:
        print(f"[X] MAIL_SOURCE={cfg.mail_source} 설정 누락: {', '.join(missing)}")
        print("    .env.example 을 참고해 .env 를 채우세요.")
        return 1
    if missing := cfg.missing_llm:
        print(f"[X] SUMMARY_BACKEND={cfg.summary_backend} 설정 누락: {', '.join(missing)}")
        return 1

    if cfg.is_paid:
        print(f"[!] SUMMARY_BACKEND=api 는 종량 과금됩니다 "
              f"(무료: rules / claude_code / ollama)\n")

    try:
        with MailSummaryAgent(cfg) as agent:
            result = agent.run(limit=args.limit, unread_only=args.unread, query=args.query)
    except AuthError as e:
        print(f"[X] 인증 오류: {e}")
        return 1
    except MailApiError as e:
        print(f"[X] 메일 API 오류: {e}")
        return 1
    except Exception as e:
        print(f"[X] {type(e).__name__}: {e}")
        return 1

    print(render(result))
    if args.out:
        save_json(result, args.out)
        print(f"\n[OK] JSON 저장: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
