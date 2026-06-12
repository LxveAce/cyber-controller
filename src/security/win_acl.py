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

import getpass
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


def _current_user() -> str | None:
    try:
        return getpass.getuser() or None
    except Exception:
        return None


def _run_icacls(path: Path, grant_spec: str) -> bool:
    user = _current_user()
    if not user:
        log.debug("win_acl: could not resolve current user; leaving %s on inherited ACL", path)
        return False
    cmd = [
        "icacls", str(path),
        "/inheritance:r",
        "/grant:r", f"{user}:{grant_spec}",
        "/grant:r", f"{_SYSTEM_SID}:{grant_spec}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_ICACLS_TIMEOUT, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("win_acl: icacls failed to launch for %s: %s", path, exc)
        return False
    if proc.returncode != 0:
        log.debug("win_acl: icacls rc=%s for %s: %s", proc.returncode, path, proc.stderr.strip())
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
