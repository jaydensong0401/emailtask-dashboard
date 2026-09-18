"""자동 실행 (Windows 작업 스케줄러가 부른다. setup_schedule.ps1 참고).

  python auto.py quick   10분마다   메일 받기 → 대시보드 갱신          (Claude 사용량 없음)
  python auto.py full    9·13·17시  메일 받기 → 업무 정리 → 대시보드 갱신

- 두 작업이 겹치지 않게 잠근다. quick 은 다른 작업이 돌고 있으면 건너뛰고, full 은 기다린다.
- 기록은 out/auto.log (1MB 넘으면 auto.log.1 로 넘긴다).
- 실패하면 대시보드 상단에 알림을 띄우고, 같은 단계가 다음에 성공하면 알림을 지운다.
- 비밀번호는 Windows 자격 증명 관리자에서 읽는다 (python set_password.py 로 한 번 저장).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
LOG = OUT / "auto.log"
STATE = OUT / "auto_state.json"
LOCK = OUT / "auto.lock"
LOG_MAX = 1_000_000
KST = timezone(timedelta(hours=9))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

STEP_LABEL = {"sync": "메일 받기", "extract": "업무 정리", "dashboard": "대시보드 생성"}
TIMEOUT = {"sync": 15 * 60, "extract": 50 * 60, "dashboard": 5 * 60}
FULL_WAIT = 20 * 60          # full 은 quick 이 끝나기를 이만큼 기다린다


def now_kst() -> datetime:
    return datetime.now(KST)


def console_python() -> str:
    """자식 스크립트는 콘솔용 python.exe 로 돌린다 (pythonw 는 출력을 받을 수 없다)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (alt := exe.with_name("python.exe")).exists():
        return str(alt)
    return str(exe)


def run_step(name: str, extra: list[str] | None = None) -> tuple[int, str]:
    """스크립트 하나를 창 없이 실행한다. 비밀번호 등 입력을 기다리다 멈추지 않게 한다.

    입력을 NUL 로 막으면 Windows 는 여전히 콘솔로 보므로, 빈 파이프 + 환경변수로 알린다.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               NWMAIL_NONINTERACTIVE="1")
    try:
        p = subprocess.run(
            [console_python(), f"{name}.py", *(extra or [])], cwd=ROOT, env=env,
            input="", capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT[name], creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return 124, f"[X] 시간 초과 ({TIMEOUT[name] // 60}분)"
    except OSError as e:
        return 1, f"[X] 실행 실패: {e}"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def error_line(output: str) -> str:
    """알림에 띄울 한 줄: [X] 줄 → 실패 줄 → 마지막 줄."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for pick in (lambda ln: ln.startswith("[X]"), lambda ln: "실패" in ln):
        if found := [ln for ln in lines if pick(ln)]:
            return found[0].removeprefix("[X]").strip()[:200]
    return lines[-1][:200] if lines else "원인 불명 (out/auto.log 확인)"


# ── 상태 · 알림 ─────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    OUT.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def notice_from(state: dict) -> str:
    parts = [f"{STEP_LABEL[k]} 실패({v['at']}): {v['msg']}"
             for k in ("sync", "extract") if (v := state.get(k))]
    return ("자동 실행 문제 — " + " · ".join(parts) + " · 기록: out/auto.log") if parts else ""


# ── 기록 · 잠금 ─────────────────────────────────────────────────────────
def log(text: str) -> None:
    OUT.mkdir(exist_ok=True)
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def acquire_lock(wait_seconds: float):
    """잠금 파일 핸들을 돌려준다. 못 잡으면 None. 프로세스가 죽으면 OS 가 풀어 준다."""
    OUT.mkdir(exist_ok=True)
    f = open(LOCK, "a+")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            f.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(min(15, max(0.1, deadline - time.monotonic())))


def release_lock(f) -> None:
    try:
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        f.close()


# ── 실행 ────────────────────────────────────────────────────────────────
def run(mode: str, runner=run_step) -> int:
    steps = ["sync", "dashboard"] if mode == "quick" else ["sync", "extract", "dashboard"]
    state = load_state()
    log(f"=== {now_kst():%Y-%m-%d %H:%M:%S} {mode} ===")
    ok = True
    for step in steps:
        extra = ["--notice", notice_from(state)] if step == "dashboard" else []
        t0 = time.monotonic()
        rc, out = runner(step, extra)
        # 단계마다 바로 남긴다. 중간에 멈추거나 죽어도 어디까지 갔는지 보인다
        log("\n".join([f"--- {step} (종료코드 {rc}, {time.monotonic() - t0:.0f}초)",
                       *out.splitlines()[-15:]]))
        if step in ("sync", "extract"):
            state[step] = None if rc == 0 else {"at": f"{now_kst():%m/%d %H:%M}",
                                                "msg": error_line(out)}
            save_state(state)
        ok &= rc == 0
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = args[0] if args else ""
    if mode not in ("quick", "full"):
        print(__doc__)
        return 2
    lock = acquire_lock(FULL_WAIT if mode == "full" else 0)
    if lock is None:
        log(f"=== {now_kst():%Y-%m-%d %H:%M:%S} {mode} 건너뜀 (다른 작업 실행 중) ===")
        return 0
    try:
        return run(mode)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
