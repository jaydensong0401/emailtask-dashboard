"""대시보드 생성: 로컬 저장소를 읽어 out/dashboard.html 을 만든다.

메일함에 접속하지 않으므로 비밀번호가 필요 없다. 새 메일을 반영하려면
먼저 `python sync.py`, 업무·결정사항을 채우려면 `python extract.py`.
완료 · 제외 · 업무삭제 버튼 기록은 대시보드 서버(`python serve.py`)로 열었을 때 저장된다.
"""
import argparse
import socket
import sys
import webbrowser
from pathlib import Path

from nwmail.config import ROOT, load_config
from nwmail.dashboard import build_model, render_html
from nwmail.feedback import FeedbackStore
from nwmail.store import Store


def server_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description="메일 업무 대시보드 생성")
    p.add_argument("-o", "--out", type=Path, default=ROOT / "out" / "dashboard.html",
                   help="출력 경로 (기본 out/dashboard.html)")
    p.add_argument("-d", "--days", type=int, default=cfg.window_days,
                   help=f"최근 며칠 안에 메일이 온 대화를 보여줄지 (기본 {cfg.window_days}일)")
    p.add_argument("--open", action="store_true",
                   help="생성 후 브라우저로 열기 (서버가 켜져 있으면 서버 주소로)")
    p.add_argument("--notice", default="", help="상단에 띄울 알림 (자동 실행 실패 안내용)")
    args = p.parse_args()

    with Store() as store:
        if not store.stats(args.days)["total"]:
            print("[X] 저장된 메일이 없습니다. 먼저 `python sync.py` 를 실행하세요.")
            return 1
        if store.needs_rebuild():
            print("[X] 메일 저장 형식이 바뀌었습니다. 먼저 `python sync.py` 로 다시 받아주세요.")
            return 1
        with FeedbackStore() as feedback:
            model = build_model(store, me=cfg.imap_user, window_days=args.days,
                                feedback=feedback)
            server = {"port": cfg.dashboard_port, "token": feedback.token()}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")        # 브라우저가 쓰는 도중의 파일을 읽지 않도록
    tmp.write_text(render_html(model, notice=args.notice, server=server), encoding="utf-8")
    tmp.replace(args.out)

    s = model["stats"]
    print(f"[OK] {args.out}")
    skipped = f" (정기 메일 {s['excluded']}개 제외)" if s["excluded"] else ""
    print(f"  최근 {args.days}일 대화 {s['threads']}개 (과거 메일 {s['history']}통 포함) · "
          f"프로젝트 {len(model['projects'])} · "
          f"업무 추출 {s['extracted']}/{s['threads'] - s['excluded']}{skipped} · "
          f"미응답 {s['unanswered']} · 차단 {s['blocked']} · 신규 {s['new']}")
    if s["extracted"] < s["threads"] - s["excluded"]:
        print("  추출 안 된 대화가 있습니다 — python extract.py")
    if args.open:
        if server_running(cfg.dashboard_port):
            webbrowser.open(f"http://127.0.0.1:{cfg.dashboard_port}/")
        else:
            print("  대시보드 서버가 꺼져 있어 파일로 엽니다. 완료·제외 기록은 서버에서만 저장됩니다"
                  " — python serve.py")
            webbrowser.open(args.out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
