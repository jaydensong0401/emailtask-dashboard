"""직전 평일부터 메일이 온 대화에서 프로젝트 업무·결정사항을 추출한다.

대화마다 과거 메일과 내가 보낸 답장까지 전체를 보고 판단한다 (sync.py 가 모아 둔 것).
기간: 화~금 D-1, 월 D-3. 마지막 추출이 그보다 오래됐으면 그 날부터.
내용이 그대로인 대화는 캐시를 쓰고, projects.json 의 extract_skip 제목(정기 보고)은 건너뛴다.
"""
import argparse
import sys

from nwmail.config import load_config
from nwmail.extract import Extractor
from nwmail.store import Store


def main() -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description="대화 업무·결정사항 추출")
    p.add_argument("-f", "--force", action="store_true",
                   help="캐시를 무시하고 전부 다시 추출")
    p.add_argument("-n", "--limit", type=int,
                   help="최근 N개 대화만 (시험 실행용)")
    p.add_argument("-d", "--days", type=int,
                   help="며칠 전부터 메일이 온 대화를 다룰지. 생략하면 직전 평일부터 (화~금 1, 월 3)")
    p.add_argument("--backend", choices=["claude_code", "ollama", "api"],
                   help="요약 백엔드 (.env 덮어쓰기)")
    args = p.parse_args()

    import dataclasses
    if args.backend:
        cfg = dataclasses.replace(cfg, summary_backend=args.backend)
    if cfg.summary_backend == "rules":
        # 추출은 의미 분석이 필요해 rules 로는 불가능하다. .env 가 요약용으로 rules 여도
        # 추출은 구독 경로(claude_code)로 돌린다. run.py 의 요약 동작에는 영향 없음.
        print("[i] SUMMARY_BACKEND=rules 는 추출을 못 하므로 claude_code 로 실행합니다.")
        cfg = dataclasses.replace(cfg, summary_backend="claude_code")

    with Store() as store:
        if store.needs_rebuild():
            print("[X] 메일 저장 형식이 바뀌었습니다. 먼저 `python sync.py` 로 다시 받아주세요.")
            return 1
        from datetime import datetime, timezone

        from nwmail.projects import skip_extract
        from nwmail.store import KST, window_start

        started = datetime.now(timezone.utc)
        days = args.days if args.days is not None else store.read_window_days("extract")
        since = window_start(days).astimezone(KST)
        threads = store.threads(days)
        skipped = [t for t in threads if skip_extract(t["subject"])]
        threads = [t for t in threads if not skip_extract(t["subject"])]
        if skipped:
            print(f"[i] 정기 메일 {len(skipped)}개 대화는 추출하지 않습니다 (projects.json extract_skip)")
        if not threads:
            print(f"{since:%m/%d} 이후 메일이 온 대화 중 추출할 것이 없습니다.")
            store.start_run(0, kind="extract", started_at=started)
            return 0

        if cfg.summary_backend == "claude_code":
            # 인증이 없으면 배치마다 잘린 오류를 내지 말고, 시작 전에 전체 안내를 한 번 보여준다
            from nwmail import claude_cli
            try:
                claude_cli.preflight()
            except claude_cli.ClaudeCliError as e:
                print(f"[X] {e}")
                return 1

        if args.limit:
            threads = threads[:args.limit]

        total = sum(t["mail_count"] for t in threads)
        history = sum(t["mail_count"] - t["recent_count"] for t in threads)
        print(f"{since:%m/%d} 이후 대화 {len(threads)}개 · 메일 {total}통 "
              f"(과거 이력 {history}통 포함) / 백엔드 {cfg.summary_backend}")
        r = Extractor(cfg, store).run(threads, force=args.force, window_days=days)
        print(f"\n완료: 추출 {r['extracted']} · 캐시 {r['cached']} · 실패 {r['failed']}")
        if not args.limit and not r["failed"]:
            # 전부 성공했을 때만 기록한다. 실패가 있으면 다음 실행이 같은 기간을 다시 본다
            store.start_run(r["extracted"], kind="extract", started_at=started)

        active = {t["thread_key"] for t in threads}
        counts: dict[str, int] = {}
        for row in store.conn.execute("SELECT thread_key, project FROM extractions"):
            if row["thread_key"] in active and row["project"]:
                counts[row["project"]] = counts.get(row["project"], 0) + 1
        if counts:
            print("\n프로젝트별 대화:")
            for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {n:>3}개  {name}")
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
