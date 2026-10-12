"""외부 통신 없이 토큰 절감 보고서를 만들고 날짜별 기록을 보존한다."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from nwmail.store import DB_PATH, KST
from nwmail.usage_report import REPORT_DIR, build_report, save_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="토큰 절감 보고서 생성 (기존 사용량 기록 유지)")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="사용량 DB (기본 mail.db)")
    parser.add_argument("--out", type=Path, default=REPORT_DIR, help="보고서 누적 저장 폴더")
    parser.add_argument("--days", type=int, default=7, help="양쪽 비교기간 일수 (1~366, 기본 7)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="보고 기준 한국 날짜 YYYY-MM-DD (그 전날까지 비교)")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        parser.error("--days 는 1~366이어야 합니다")
    if not args.db.is_file():
        print("[X] 사용량 DB가 없습니다. 먼저 업무 정리를 실행하면 기록이 쌓입니다.")
        return 1
    now = (datetime.combine(args.as_of, datetime.min.time(), tzinfo=KST)
           if args.as_of else datetime.now(timezone.utc))
    try:
        # Store 초기화/마이그레이션 없이 read-only 연결로 보고서만 읽는다.
        conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            report = build_report(conn, now=now, days=args.days)
        finally:
            conn.close()
        if not report["available"]:
            print(f"[X] {report['headline']}")
            return 1
        path = save_report(report, args.out)
    except (OSError, sqlite3.Error):
        print("[X] 사용량 DB 또는 보고서 저장 경로를 읽거나 쓸 수 없습니다.")
        return 1
    print(f"[OK] {path}")
    print(f"  {report['headline']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
