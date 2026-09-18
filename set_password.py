"""IMAP 외부 앱 비밀번호를 Windows 자격 증명 관리자에 저장한다 (자동 실행용).

  1) 입력은 화면에 표시되지 않는다
  2) 붙여넣기로 섞인 공백·비가시 문자를 자동으로 제거한다
  3) 저장하기 전에 실제로 IMAP 로그인을 해 보고, 성공한 비밀번호만 저장한다

비밀번호 값은 어디에도 출력하지 않는다.
  python set_password.py            저장(덮어쓰기)
  python set_password.py --check    저장돼 있는지만 확인
  python set_password.py --delete   삭제
"""
from __future__ import annotations

import argparse
import getpass
import sys

from nwmail import secret
from nwmail.config import load_config, normalize_password


def main() -> int:
    p = argparse.ArgumentParser(description="IMAP 비밀번호 저장 (Windows 자격 증명 관리자)")
    p.add_argument("--check", action="store_true", help="저장 여부만 확인")
    p.add_argument("--delete", action="store_true", help="저장된 비밀번호 삭제")
    args = p.parse_args()

    cfg = load_config()
    if not cfg.imap_user:
        print("[X] NW_IMAP_USER 가 설정되지 않았습니다 (.env).")
        return 1
    if not secret.supported():
        print("[X] 자격 증명 관리자는 Windows 에서만 쓸 수 있습니다.")
        return 1
    target = secret.target_for(cfg.imap_user)

    if args.check:
        saved = bool(secret.load_imap_password(cfg.imap_user))
        print(f"{'[OK] 저장돼 있음' if saved else '[X] 저장돼 있지 않음'} — {target}")
        return 0 if saved else 1
    if args.delete:
        print(f"[OK] 삭제했습니다 — {target}" if secret.delete(target)
              else f"[i] 원래 저장돼 있지 않습니다 — {target}")
        return 0

    print(cfg.imap_user)
    print("  계정 비밀번호가 아니라 '외부 앱 비밀번호' 를 입력하세요.")
    print("  (네이버웍스 PC웹 > 우측 상단 프로필 > 보안 > 외부 앱 비밀번호 생성하기)")
    pw = normalize_password(getpass.getpass("  외부 앱 비밀번호 (화면에 표시되지 않습니다): "))
    if not pw:
        print("[X] 입력이 비어 있습니다.")
        return 1

    print("\nIMAP 로그인 확인 중...")
    from nwmail.imap_client import ImapError, NaverWorksImap
    try:
        with NaverWorksImap(cfg.imap_user, pw) as imap:
            unread = imap.unread_count()
    except ImapError as e:
        print(f"[X] 로그인 실패 — 저장하지 않았습니다.\n  {e}")
        return 1
    secret.save_imap_password(cfg.imap_user, pw)
    print(f"[OK] 로그인 확인(안 읽은 메일 {unread}통) 후 저장했습니다 — {target}")
    print("  이제 python sync.py 도 비밀번호를 묻지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
