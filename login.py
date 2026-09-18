"""1회 실행: 네이버웍스 계정으로 로그인해 토큰을 저장한다."""
import sys

from nwmail.auth import AuthError, interactive_login
from nwmail.config import load_config


def main() -> int:
    cfg = load_config()
    if missing := cfg.missing_nw:
        print(f"[X] .env 에 다음 값이 없습니다: {', '.join(missing)}")
        print("    .env.example 을 .env 로 복사한 뒤 Developer Console 값을 채우세요.")
        return 1
    try:
        tokens = interactive_login(cfg)
    except AuthError as e:
        print(f"[X] {e}")
        return 1
    print(f"\n[OK] 토큰 저장 완료 (.tokens.json)")
    print(f"     scope={tokens.get('scope', cfg.scope)}  expires_in={tokens.get('expires_in')}s")
    print("     이제 `python run.py` 로 메일을 요약할 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
