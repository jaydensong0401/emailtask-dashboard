"""대시보드 로컬 서버 실행: http://127.0.0.1:8787 (이 PC 에서만 접속).

대시보드의 완료 · 제외 · 업무삭제 버튼 기록을 feedback.db 에 저장하고,
차단됨 탭의 규칙 · 보호 도메인 변경을 mail.db 에 반영한다.
자동 실행(setup_schedule.ps1 의 nwmail-server)이 로그인해 있는 동안 켜 두고,
꺼지면 5분 안에 다시 켠다. 직접 켤 때:

  python serve.py
"""
import argparse
import sys
from datetime import datetime

from nwmail.config import ROOT, load_config
from nwmail.feedback import FeedbackStore
from nwmail.server import make_server

LOG = ROOT / "out" / "server.log"


def log(text: str) -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def main() -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description="대시보드 로컬 서버")
    p.add_argument("--port", type=int, default=cfg.dashboard_port,
                   help=f"포트 (기본 {cfg.dashboard_port}, .env 의 DASHBOARD_PORT)")
    args = p.parse_args()

    feedback = FeedbackStore()
    try:
        server = make_server(args.port, ROOT / "out" / "dashboard.html", feedback)
    except OSError as e:
        # 이미 켜져 있으면(작업 스케줄러가 5분마다 확인) 조용히 끝낸다
        msg = f"포트 {args.port} 를 쓸 수 없습니다 (이미 실행 중이거나 다른 프로그램이 사용): {e}"
        print(f"[X] {msg}")
        log(msg)
        feedback.close()
        return 1
    from nwmail.filter_api import handle as filters_handle
    server.filters_handler = filters_handle     # 차단됨 탭의 규칙 · 보호 도메인 변경
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[OK] 대시보드: {url}  (끄려면 Ctrl+C)")
    log(f"시작 {url}")
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
