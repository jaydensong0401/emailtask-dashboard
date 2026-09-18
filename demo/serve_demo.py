"""데모 대시보드 서버: http://127.0.0.1:8790 (이 PC 에서만 접속).

  python demo/serve_demo.py            데모 DB 가 없으면 만들고 서버를 켠다
  python demo/serve_demo.py --reset    누른 기록을 지우고 처음 상태로
  python demo/serve_demo.py --cloud    클라우드(Cloud Run) 데모 - Dockerfile 이 이렇게 켠다

완료 · 제외 · 기한 · 답변완료 · 차단 규칙 버튼이 실제로 동작한다. 기록은 demo/ 의 데모 DB 에만
저장되고 실제 메일함에는 접속하지 않는다.

--cloud 는 $PORT(기본 8080)에서 열고, 켤 때마다 데모 데이터를 처음 상태로 만든다. 주소 검사는
그대로 두고, 환경변수 DEMO_HOSTS 에 적은 주소(쉼표로 구분, 예: nwmail-demo-123.asia-northeast3.run.app)만
더 받는다. 페이지는 연 주소의 서버로 기록한다.
"""
from __future__ import annotations

import argparse
import os
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
    p.add_argument("--cloud", action="store_true",
                   help="클라우드 데모: $PORT 에서 열고 켤 때마다 처음 상태로. 받을 주소는 DEMO_HOSTS")
    args = p.parse_args()
    port = int(os.environ.get("PORT", "8080")) if args.cloud else args.port
    hosts = [x.strip() for x in os.environ.get("DEMO_HOSTS", "").split(",") if x.strip()] if args.cloud else []
    if args.reset or args.cloud or not DEMO_MAIL.exists():
        seed()

    def rebuild() -> bool:
        write(SERVED, render({"port": port, "same_origin": args.cloud}))
        return True

    rebuild()
    feedback = FeedbackStore(DEMO_FB)
    try:
        server = make_server(port, SERVED, feedback, host="0.0.0.0" if args.cloud else "127.0.0.1",
                             allowed_hosts=hosts)
    except OSError as e:
        print(f"[X] 포트 {port} 를 쓸 수 없습니다: {e}")
        feedback.close()
        return 1
    server.filters_handler = lambda data: filter_api.handle(
        data, store_factory=lambda: Store(DEMO_MAIL), rebuild=rebuild)
    if args.cloud:
        print(f"[OK] 클라우드 데모: 포트 {port}, 받는 주소 {', '.join(hosts) or '없음 (DEMO_HOSTS 를 정하세요)'}",
              flush=True)
    else:
        print(f"[OK] 데모 대시보드: http://127.0.0.1:{port}/  (끄려면 Ctrl+C)")
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
