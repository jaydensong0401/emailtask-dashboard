"""데모 대시보드 서버: http://127.0.0.1:8790 (이 PC 에서만 접속).

  python demo/serve_demo.py            데모 DB 가 없으면 만들고 서버를 켠다
  python demo/serve_demo.py --reset    누른 기록을 지우고 처음 상태로

완료 · 제외 · 기한 · 답변완료 · 차단 규칙 버튼이 실제로 동작한다. 기록은 demo/ 의 데모 DB 에만
저장되고 실제 메일함에는 접속하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo.build_demo import DEMO_FB, DEMO_MAIL, PORT, SERVED, render, seed, write  # noqa: E402
from nwmail import filter_api                                                      # noqa: E402
from nwmail.feedback import FeedbackStore                                          # noqa: E402
from nwmail.server import make_server                                              # noqa: E402
from nwmail.store import Store                                                     # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="데모 대시보드 서버")
    p.add_argument("--port", type=int, default=PORT, help=f"포트 (기본 {PORT})")
    p.add_argument("--reset", action="store_true", help="누른 기록을 지우고 처음 상태로")
    args = p.parse_args()
    if args.reset or not DEMO_MAIL.exists():
        seed()

    def rebuild() -> bool:
        write(SERVED, render({"port": args.port}))
        return True

    rebuild()
    feedback = FeedbackStore(DEMO_FB)
    try:
        server = make_server(args.port, SERVED, feedback)
    except OSError as e:
        print(f"[X] 포트 {args.port} 를 쓸 수 없습니다: {e}")
        feedback.close()
        return 1
    server.filters_handler = lambda data: filter_api.handle(
        data, store_factory=lambda: Store(DEMO_MAIL), rebuild=rebuild)
    print(f"[OK] 데모 대시보드: http://127.0.0.1:{args.port}/  (끄려면 Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        feedback.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
