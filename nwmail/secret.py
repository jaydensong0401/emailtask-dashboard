"""IMAP 외부 앱 비밀번호를 Windows 자격 증명 관리자에 보관한다.

자동 실행(작업 스케줄러)에서는 비밀번호를 입력할 사람이 없다. .env 는 사내 DRM 이
암호화해 버리고, 환경변수는 레지스트리에 평문으로 남는다. 자격 증명 관리자는 Windows
로그인 계정으로 암호화되므로 같은 사용자로 로그인했을 때만 읽힌다.

외부 패키지(keyring) 없이 advapi32 의 Cred* API 를 직접 부른다.
확인: 제어판 > 자격 증명 관리자 > Windows 자격 증명 > 일반 자격 증명 > nwmail/imap/…
"""
from __future__ import annotations

import ctypes
import sys

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


def target_for(user: str) -> str:
    return f"nwmail/imap/{(user or '').strip().lower()}"


def supported() -> bool:
    return sys.platform == "win32"


if supported():
    from ctypes import wintypes as wt

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wt.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wt.DWORD]
    _advapi.CredWriteW.restype = wt.BOOL
    _advapi.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    _advapi.CredDeleteW.restype = wt.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None


def read(target: str) -> str | None:
    """저장된 값. 없거나 Windows 가 아니면 None."""
    if not supported():
        return None
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return None
        raise OSError(err, f"자격 증명 읽기 실패 ({err})")
    try:
        cred = ptr.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        _advapi.CredFree(ptr)


def write(target: str, user: str, value: str) -> None:
    if not supported():
        raise OSError("자격 증명 관리자는 Windows 에서만 쓸 수 있습니다.")
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.Comment = "nwmail IMAP 외부 앱 비밀번호"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        err = ctypes.get_last_error()
        raise OSError(err, f"자격 증명 저장 실패 ({err})")


def delete(target: str) -> bool:
    """지웠으면 True, 원래 없었으면 False."""
    if not supported():
        return False
    if _advapi.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    err = ctypes.get_last_error()
    if err == ERROR_NOT_FOUND:
        return False
    raise OSError(err, f"자격 증명 삭제 실패 ({err})")


def load_imap_password(user: str) -> str:
    try:
        return read(target_for(user)) or ""
    except OSError:
        return ""


def save_imap_password(user: str, password: str) -> None:
    write(target_for(user), user, password)
