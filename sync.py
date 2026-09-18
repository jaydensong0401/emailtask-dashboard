"""메일 동기화: 직전 평일부터 온 메일을 받고, 그 메일이 속한 대화의 과거 이력까지 모은다.

1) 받은메일함에서 직전 평일 0시 이후 메일을 받는다 (화~금 D-1, 월 D-3). 이미 받은 메일은
   건너뛴다. 마지막 동기화가 그보다 오래됐으면(휴가 등) 그 날부터 받는다.
2) 새로 받은 메일이 속한 대화의 과거 메일과 내가 보낸 답장을 메일함 전체에서 찾는다.
   (References / In-Reply-To / Message-ID 기준, 기간 제한 없음. 휴지통·스팸 제외)
"""
import argparse
import sys

from nwmail.config import load_config
from nwmail.filters import load_filters, make_matcher, protected_set, suggest_filters
from nwmail.store import KST, Store, window_start


def main() -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description="네이버웍스 메일 로컬 동기화")
    p.add_argument("-d", "--days", type=int,
                   help="받아올 기간(일). 생략하면 직전 평일부터 (화~금 1, 월 3)")
    p.add_argument("--no-history", action="store_true",
                   help="과거 이력(이전 메일·내 답장)을 찾지 않음")
    p.add_argument("--full-history", action="store_true",
                   help="새 메일이 없는 대화까지 과거 이력을 다시 찾음")
    p.add_argument("--suggest", action="store_true",
                   help="차단 후보를 함께 출력")
    p.add_argument("--rebuild", action="store_true",
                   help="저장된 메일을 비우고 처음부터 다시 수집 (차단 규칙은 유지)")
    args = p.parse_args()

    from nwmail.config import ENV_LOAD_ERROR
    if ENV_LOAD_ERROR:
        print(f"[!] {ENV_LOAD_ERROR}\n")
    if missing := cfg.missing_source:
        print(f"[X] 설정 누락: {', '.join(missing)}")
        return 1
    if cfg.mail_source != "imap":
        print("[X] sync.py 는 MAIL_SOURCE=imap 에서만 동작합니다.")
        return 1

    from nwmail.config import MissingPassword
    try:
        cfg = cfg.with_prompted_password()
    except MissingPassword as e:
        print(f"[X] {e}")
        return 1

    from nwmail.context import collect_context, seeds_from_store
    from nwmail.imap_client import ImapError, NaverWorksImap

    with Store() as store:
        if args.rebuild or store.needs_rebuild():
            n = store.reset_mails()
            if args.rebuild:
                print(f"기존 메일 {n}통을 비웠습니다 (차단 규칙은 유지).")
            elif n:
                print(f"[i] 저장 형식이 바뀌어 기존 메일 {n}통을 비우고 다시 받습니다 "
                      "(차단 규칙은 유지, 업무 추출은 다시 필요).")
        # 보호 도메인(우리 회사·고객사 + 사용자가 더한 곳) 메일은 어떤 규칙에도 차단하지 않는다
        matcher = make_matcher(load_filters(store), protected_set(store))
        days = args.days if args.days is not None else store.read_window_days("sync")
        since = window_start(days).astimezone(KST).date()

        try:
            with NaverWorksImap(cfg.imap_user, cfg.imap_password) as imap:
                print(f"\n받은메일함 {since:%m/%d} 이후 메일 확인 중...")
                new = imap.fetch_since(since=since, skip_uids=store.known_uids("INBOX"))
                result = store.save_mails(new, me=cfg.imap_user, matcher=matcher)
                print(f"  새 메일 {result.inserted}통 (차단 {result.blocked}통)")

                if not args.no_history:
                    active = {t["thread_key"] for t in store.threads(days)}
                    keys = active if args.full_history else (result.thread_keys & active)
                    seeds = seeds_from_store(store, keys)
                    if seeds:
                        print(f"\n대화 {len(seeds)}개의 과거 이력을 찾는 중 "
                              "(메일함 전체, 휴지통·스팸·임시보관함 제외)...")
                        hist = collect_context(
                            imap, seeds, store.known_keys(), store.known_message_ids(),
                            progress=lambda s: print("  " + s))
                        saved = store.save_mails(hist.mails, me=cfg.imap_user,
                                                 matcher=matcher)
                        mine = sum(1 for m in hist.mails
                                   if m.from_email.lower() == cfg.imap_user.lower())
                        print(f"  과거 메일 {saved.inserted}통 추가 (내가 보낸 메일 {mine}통)")
                        for w in hist.warnings:
                            print(f"  [!] {w}")
                    else:
                        print("\n과거 이력을 찾을 새 대화가 없습니다.")
        except ImapError as e:
            print(f"[X] {e}")
            return 1

        store.start_run(result.inserted, kind="sync")

        s = store.stats(days)
        print(f"\n{since:%m/%d} 이후: 대화 {s['threads']}개 · 받은 메일 {s['window']}통 "
              f"(직접 {s['direct']} · 안읽음 {s['unread']}) · 차단 {s['blocked']}통")
        print(f"대화에 붙은 과거 메일 {s['history']}통 · 그중 내가 보낸 메일 {s['mine']}통")

        if args.suggest:
            if cands := suggest_filters(store, me=cfg.imap_user):
                print(f"\n차단 후보 {len(cands)}건:")
                for c in cands:
                    print(f"  - {c['pattern']}  ({c['count']}통) "
                          f"[{', '.join(c['reasons'])}]")
                    print(f"      예: {c['sample'][:60]}")
                print("\n  등록: python filter.py --add-sender \"주소\"")
            else:
                print("\n차단 후보 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
