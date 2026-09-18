r"""바탕화면 아이콘용: 대시보드를 브라우저로 연다.

서버가 꺼져 있으면 먼저 켜고 연다 (창은 뜨지 않는다). 작업 스케줄러가 서버를
5분마다 확인하지만, PC 를 막 켰을 때는 아직 안 떠 있을 수 있다.

  python open_dashboard.py       직접 실행
  .\make_shortcut.ps1            바탕화면에 바로가기 만들기
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from nwmail.config import ROOT, load_config

# 아이콘으로 실행해도 콘솔 창이 깜빡이지 않게 한다
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
WAIT_SECONDS = 10.0


def server_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def pythonw() -> str:
    """창 없는 파이썬. 없으면 그냥 현재 파이썬."""
    exe = Path(sys.executable)
    alt = exe.with_name("pythonw.exe")
    return str(alt if alt.exists() else exe)


def start_server(port: int, wait: float = WAIT_SECONDS) -> bool:
    """서버를 켜고 포트가 열릴 때까지 기다린다. 못 켜면 False."""
    try:
        subprocess.Popen([pythonw(), str(ROOT / "serve.py")], cwd=ROOT,
                         creationflags=DETACHED, close_fds=True)
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if server_running(port):
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    port = load_config().dashboard_port
    if server_running(port) or start_server(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0

    # 서버를 못 켰으면 파일로라도 보여준다. 완료·제외 기록은 저장되지 않는다
    page = ROOT / "out" / "dashboard.html"
    if page.exists():
        webbrowser.open(page.resolve().as_uri())
        print("[X] 서버를 켜지 못해 파일로 엽니다 (완료·제외 기록이 저장되지 않습니다)")
    else:
        print("[X] 대시보드가 없습니다. python dashboard.py 를 먼저 실행하세요.")
    print("    기록: out\\server.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
