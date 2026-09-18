"""대시보드 차단됨 탭의 규칙 · 보호 도메인 변경 요청을 처리한다 (serve.py 가 서버에 연결).

  add_rule      {kind, pattern, category, note}   규칙 추가 (보호 도메인 겨냥이면 거부)
  toggle_rule   {id, enabled}                      켜기 · 끄기
  remove_rule   {id}                               삭제 (그 규칙으로 차단된 메일은 되살아남)
  protect       {domain, note}                     보호 도메인 추가 (그 도메인 메일은 차단 해제)
  unprotect     {domain}                           보호 도메인 빼기 (고정 도메인은 거부)
  dismiss       {kind, pattern}                    차단 후보에서 빼기

바꾼 뒤에는 저장된 메일에 규칙을 다시 적용하고, 대시보드를 새로 만든다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

from .config import ROOT
from .filters import (
    CATEGORIES, KINDS, FilterError, add_filter, add_protected, dismiss_candidate, get_filter,
    reapply, remove_filter, remove_protected, set_enabled,
)
from .store import Store

_lock = threading.Lock()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise FilterError("규칙 번호가 올바르지 않습니다.") from None


def apply_op(store: Store, data: dict) -> str:
    """요청 하나를 적용하고 사용자에게 보여줄 문장을 돌려준다. 잘못된 요청은 FilterError."""
    op = str(data.get("op") or "")
    if op == "add_rule":
        kind = str(data.get("kind") or "")
        category = str(data.get("category") or "")
        if category not in CATEGORIES:
            raise FilterError("구분을 골라 주세요.")
        fid = add_filter(store, kind, str(data.get("pattern") or ""),
                         str(data.get("note") or ""), category)
        f = get_filter(store, fid)
        return f"규칙을 추가했어요 — {KINDS[kind]}: {f.pattern if f else ''} ({category})"
    if op == "toggle_rule":
        fid, on = _int(data.get("id")), bool(data.get("enabled"))
        if not get_filter(store, fid):
            raise FilterError("없는 규칙입니다.")
        set_enabled(store, fid, on)
        return f"규칙 {fid} 을 {'켰어요' if on else '껐어요 — 걸렸던 메일이 되살아납니다'}"
    if op == "remove_rule":
        fid = _int(data.get("id"))
        if not get_filter(store, fid):
            raise FilterError("없는 규칙입니다.")
        remove_filter(store, fid)
        return f"규칙 {fid} 을 삭제했어요 — 걸렸던 메일이 되살아납니다"
    if op == "protect":
        d = add_protected(store, str(data.get("domain") or ""), str(data.get("note") or ""))
        return f"{d} 를 보호 도메인에 추가했어요 — 이 도메인 메일은 차단되지 않습니다"
    if op == "unprotect":
        d = remove_protected(store, str(data.get("domain") or ""))
        return f"{d} 를 보호 도메인에서 뺐어요"
    if op == "dismiss":
        kind, pattern = str(data.get("kind") or ""), str(data.get("pattern") or "").strip()
        if kind not in KINDS or not pattern:
            raise FilterError("후보 정보가 올바르지 않습니다.")
        dismiss_candidate(store, kind, pattern)
        return f"{pattern} 를 차단 후보에서 뺐어요"
    raise FilterError(f"알 수 없는 요청: {op or '(없음)'}")


def regenerate(timeout: int = 120) -> bool:
    """대시보드를 새로 만든다. 자동 실행과 같은 경로(dashboard.py)를 쓰고 실패 알림도 유지한다."""
    try:
        sys.path.insert(0, str(ROOT))
        from auto import load_state, notice_from          # 자동 실행 실패 알림을 잃지 않도록
        notice = notice_from(load_state())
    except Exception:
        notice = ""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    # 창 없는 서버에서 띄우면 출력이 cp949 라 '—' 같은 글자에서 죽는다 (auto.py 와 같은 설정)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", NWMAIL_NONINTERACTIVE="1")
    try:
        p = subprocess.run([exe, "dashboard.py", "--notice", notice], cwd=ROOT, env=env,
                           capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def handle(data: dict, store_factory=Store, rebuild=regenerate) -> dict:
    """서버가 부르는 입구. {ok, message, blocked, unblocked, rebuilt} 또는 FilterError."""
    with _lock:
        with store_factory() as store:
            message = apply_op(store, data)
            changed = reapply(store)
        rebuilt = rebuild()
    return {"ok": True, "message": message, "rebuilt": rebuilt, **changed}
