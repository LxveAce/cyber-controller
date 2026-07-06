"""Windows NTFS ACL hardening for the per-user config dir and secret files (audit L-1).

On POSIX the app already creates these paths ``0600``/``0700``. On Windows those mode bits are
silently ignored, so ``~/.cyber-controller`` (the Flask secret key, the encrypted vault, and
settings.json) is protected only by the *inherited* NTFS ACL — typically readable by other local
accounts. A local user who can read the secret key can forge authenticated session cookies for the
web remote, so on the Windows-primary deployment this is the ACL that actually matters.

This module replaces the inherited ACL with an explicit owner-only one via ``icacls``:
``/inheritance:r`` strips inherited ACEs, then full control is granted to the current user and
SYSTEM only. Directories get ``(OI)(CI)`` so files created inside inherit the restrictive ACL.
Everything here is best-effort: non-Windows is a no-op, and any failure is logged, never raised —
the caller's POSIX ``chmod`` path stays the fallback.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Well-known, locale-independent SID for the LocalSystem account ("SYSTEM" / "SYSTÈME" / …).
_SYSTEM_SID = "*S-1-5-18"
_ICACLS_TIMEOUT = 15
# Avoid a console flash when launched from a GUI build.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _token_user_sid_ctypes() -> str | None:
    """Resolve the process token user SID via advapi32 (ctypes), for when pywin32 isn't installed."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Declare the Win32 signatures. Without argtypes/restype, ctypes assumes 32-bit ints, so on 64-bit
    # Windows the process HANDLE and SID pointers get truncated and every call below fails (SID=None) —
    # which made restrict_to_current_user() silently no-op and leave the secret on its inherited ACL in
    # frozen builds (pywin32 isn't bundled, so the ctypes fallback is the only path). See ledger W-1.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.LocalFree.restype = wintypes.HGLOBAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    htok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(htok)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(htok, TokenUser, None, 0, ctypes.byref(size))  # size probe
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(htok, TokenUser, buf, size, ctypes.byref(size)):
            return None
        # TOKEN_USER begins with SID_AND_ATTRIBUTES whose first member is the PSID.
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_ptr = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(str_ptr)):
            return None
        try:
            return str_ptr.value
        finally:
            kernel32.LocalFree(str_ptr)
    finally:
        kernel32.CloseHandle(htok)


def _current_user_sid() -> str | None:
    """The current process token's user SID (e.g. ``S-1-5-21-…``), locale- and env-independent.

    icacls is granted this SID (as ``*<sid>``) rather than a :func:`getpass.getuser` name, which is
    read from the ``USER``/``LOGNAME``/``USERNAME`` env vars and is therefore spoofable under Git
    Bash/MSYS — a spoofed name could hand the file to another account or, if unresolvable, make
    icacls fail and leave the secret on its readable inherited ACL. Mirrors how SYSTEM is already
    granted by well-known SID."""
    try:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                                win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        pass
    try:
        return _token_user_sid_ctypes()
    except Exception:
        return None


def _run_icacls(path: Path, grant_spec: str) -> bool:
    sid = _current_user_sid()
    if not sid:
        # Fail SAFE: never fall back to a spoofable account name. Leave the inherited ACL (the POSIX
        # chmod / best-effort fallback the module documents) rather than risk granting a wrong user.
        log.warning("win_acl: could not resolve current-user SID — %s is left on its INHERITED ACL "
                    "(may be readable by other local accounts)", path)
        return False
    cmd = [
        "icacls", str(path),
        "/inheritance:r",
        "/grant:r", f"*{sid}:{grant_spec}",
        "/grant:r", f"{_SYSTEM_SID}:{grant_spec}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_ICACLS_TIMEOUT, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("win_acl: icacls failed to launch for %s: %s — file left on its INHERITED ACL "
                    "(may be readable by other local accounts)", path, exc)
        return False
    if proc.returncode != 0:
        log.warning("win_acl: icacls rc=%s for %s: %s — file left on its INHERITED ACL "
                    "(may be readable by other local accounts)", proc.returncode, path, proc.stderr.strip())
        return False
    return True


def restrict_to_current_user(path: str | Path, *, is_dir: bool = False) -> bool:
    """Restrict *path* to the current user + SYSTEM via an explicit NTFS ACL.

    No-op (returns False) off Windows or if *path* doesn't exist. For directories pass
    ``is_dir=True`` so the grant carries ``(OI)(CI)`` and new children inherit the owner-only ACL.
    Returns True only if the ACL was applied.
    """
    if sys.platform != "win32":
        return False
    p = Path(path)
    if not p.exists():
        return False
    grant_spec = "(OI)(CI)F" if is_dir else "F"
    return _run_icacls(p, grant_spec)


def secure_dir(path: str | Path) -> bool:
    """Create *path* (parents too) and apply the owner-only ACL with child inheritance."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return restrict_to_current_user(p, is_dir=True)
