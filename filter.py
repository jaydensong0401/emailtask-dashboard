"""차단 규칙 관리 (요구 4). 대시보드 차단됨 탭에서도 같은 일을 할 수 있다.

차단해도 메일은 지워지지 않는다. 대시보드의 '차단됨' 탭에서 확인할 수 있고,
규칙을 지우거나 끄면 해당 메일이 다시 살아난다.
보호 도메인(lumi.example · aurora.example · nurimktg.co.example + 추가한 곳)은 어떤 규칙에도 차단되지 않는다.
"""
import argparse
import sys

from nwmail.filters import (
    CATEGORIES, DEFAULT_CATEGORY, KINDS, FilterError, add_filter, add_protected, load_filters,
    protected_domains, reapply, remove_filter, remove_protected, remove_protected_rules,
    rule_counts, set_enabled, suggest_filters,
)
from nwmail.store import Store


def show(store: Store) -> None:
    print("보호 도메인 (차단 안 함): " + ", ".join(
        p["domain"] + ("" if p["fixed"] else " (추가)") for p in protected_domains(store)))
    filters = load_filters(store, enabled_only=False)
    if not filters:
        print("\n등록된 규칙이 없습니다.")
        print('  추가: python filter.py --add-subject "(광고)" --category 광고·홍보')
        return
    counts = rule_counts(store)
    print(f"\n{'ID':>4}  {'구분':<8} {'종류':<6} {'상태':<4} {'차단':>4}  패턴")
    print("-" * 72)
    for f in filters:
        print(f"{f.id:>4}  {f.category:<8} {KINDS.get(f.kind, f.kind):<6} "
              f"{'사용' if f.enabled else '해제':<4} {counts.get(f.id, 0):>4}  {f.pattern}"
              + (f"   # {f.note}" if f.note else ""))


def main() -> int:
    p = argparse.ArgumentParser(description="메일 차단 규칙 관리")
    p.add_argument("--add-subject", metavar="문구", help="제목에 포함되면 차단")
    p.add_argument("--add-sender", metavar="주소", help="보낸 주소에 포함되면 차단")
    p.add_argument("--add-domain", metavar="도메인", help="보낸 도메인(하위 도메인 포함)이면 차단")
    p.add_argument("--category", default=DEFAULT_CATEGORY, choices=CATEGORIES,
                   help=f"구분 (기본 {DEFAULT_CATEGORY})")
    p.add_argument("--note", default="", help="규칙 설명")
    p.add_argument("--remove", type=int, metavar="ID", help="규칙 삭제 (메일 복구)")
    p.add_argument("--disable", type=int, metavar="ID", help="규칙 일시 해제")
    p.add_argument("--enable", type=int, metavar="ID", help="규칙 재사용")
    p.add_argument("--protect", metavar="도메인", help="보호 도메인 추가 (차단 안 함)")
    p.add_argument("--unprotect", metavar="도메인", help="추가한 보호 도메인 빼기")
    p.add_argument("--purge-protected", action="store_true",
                   help="보호 도메인을 겨냥한 규칙 삭제")
    p.add_argument("--suggest", action="store_true", help="차단 후보 보기")
    p.add_argument("--accept-all", action="store_true", help="차단 후보를 전부 등록")
    args = p.parse_args()

    acted = False
    try:
        with Store() as store:
            for kind, value in (("subject", args.add_subject), ("sender", args.add_sender),
                                ("domain", args.add_domain)):
                if value:
                    fid = add_filter(store, kind, value, args.note, args.category)
                    print(f"[OK] {KINDS[kind]} 규칙 등록 (id={fid}, {args.category}): {value}")
                    acted = True
            if args.remove is not None:
                remove_filter(store, args.remove)
                print(f"[OK] 규칙 {args.remove} 삭제 — 해당 메일을 복구했습니다.")
                acted = True
            if args.disable is not None:
                set_enabled(store, args.disable, False)
                print(f"[OK] 규칙 {args.disable} 해제")
                acted = True
            if args.enable is not None:
                set_enabled(store, args.enable, True)
                print(f"[OK] 규칙 {args.enable} 사용")
                acted = True
            if args.protect:
                print(f"[OK] 보호 도메인 추가: {add_protected(store, args.protect, args.note)}")
                acted = True
            if args.unprotect:
                print(f"[OK] 보호 도메인 뺌: {remove_protected(store, args.unprotect)}")
                acted = True
            if args.purge_protected:
                gone = remove_protected_rules(store)
                print(f"[OK] 보호 도메인을 겨냥한 규칙 {len(gone)}개 삭제"
                      + (": " + ", ".join(f"{f.id}({f.pattern})" for f in gone) if gone else ""))
                acted = True

            if args.suggest or args.accept_all:
                from nwmail.config import load_config
                cands = suggest_filters(store, me=load_config().imap_user)
                if not cands:
                    print("차단 후보 없음")
                elif args.accept_all:
                    for c in cands:
                        fid = add_filter(store, c["kind"], c["pattern"],
                                         ", ".join(c["reasons"]), c["category"])
                        print(f"[OK] 등록 (id={fid}, {c['category']}): {c['pattern']} ({c['count']}통)")
                    acted = True
                else:
                    print(f"차단 후보 {len(cands)}건 — 자동 등록하지 않았습니다.\n")
                    for c in cands:
                        print(f"  [{c['category']}] {KINDS[c['kind']]}: {c['pattern']}  "
                              f"({c['count']}통) — {', '.join(c['reasons'])}")
                        print(f"    예: {c['sample'][:60]}")
                    print('\n  개별 등록: python filter.py --add-domain "도메인" --category 뉴스레터·구독')

            if acted:
                r = reapply(store)
                print(f"\n기존 메일에 재적용: 차단 +{r['blocked']}통 / 복구 {r['unblocked']}통")
            if not acted and not args.suggest:
                show(store)
    except FilterError as e:
        print(f"[X] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
