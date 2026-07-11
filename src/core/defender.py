r"""Windows Defender helpers for the OPTIONAL bundled-tools path.

Crack Lab works out of the box with CC's built-in native cracker. The bundled external tools
(aircrack-ng / hashcat) are an OPT-IN extra: Windows Defender flags them as PUA and deletes them, so
to use them the user has to add a one-time Defender EXCLUSION for CC's tools folder.

This module only:
  * reports Defender's PUA/real-time status (read-only query), and
  * gives the exact exclusion command for the user to run (transparent, user-controlled), plus an
    optional one-click ELEVATED (UAC) add.
It never touches antivirus silently and only ever names CC's own tools folder. No-op / honest ``None``
off Windows.
"""
from __future__ import annotations

import os
import subprocess


def is_windows() -> bool:
    return os.name == "nt"


def _ps(command: str, timeout: float = 20.0) -> tuple[int, str]:
    """Run a NON-elevated PowerShell command; return (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 — a status probe must never crash the app
        return 1, ""


def pua_protection_on() -> "bool | None":
    """True if Defender PUA protection is enabled (it deletes aircrack/hashcat), False if off, None if
    it can't be determined (non-Windows / query failed)."""
    if not is_windows():
        return None
    rc, out = _ps("(Get-MpPreference).PUAProtection")
    if rc != 0 or not out:
        return None
    try:
        return int(out.split()[0]) == 1
    except ValueError:
        return None


def realtime_on() -> "bool | None":
    if not is_windows():
        return None
    rc, out = _ps("(Get-MpComputerStatus).RealTimeProtectionEnabled")
    if rc != 0 or not out:
        return None
    return out.split()[0].lower() == "true"


def exclusion_command(path: str) -> str:
    """The exact PowerShell command that adds a Defender folder exclusion. Shown to the user verbatim so
    they can run it themselves in an elevated (admin) PowerShell — nothing hidden."""
    return f"Add-MpPreference -ExclusionPath '{path}'"


def add_exclusion_elevated(path: str, timeout: float = 180.0) -> bool:
    """One-click convenience: launch an ELEVATED (UAC) PowerShell that runs :func:`exclusion_command`.

    Interactive — the user sees a standard UAC prompt and can decline. Returns True if the elevated
    process completed (best-effort; the real proof is that the tools then extract + run). The command is
    written to a temp script so there is no fragile nested quoting, and the file is removed after."""
    if not is_windows():
        return False
    import tempfile
    fd, ps1 = tempfile.mkstemp(suffix=".ps1", prefix="cc-defender-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(exclusion_command(path) + "\n")
        launch = ("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                  f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{ps1}'")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", launch],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — a declined UAC / failure is an honest False, never a crash
        return False
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass


def exe_runs(exe_path: str, timeout: float = 10.0) -> bool:
    """True if *exe_path* actually executes (best-effort). Used to confirm the bundled tool survived +
    launches after the exclusion — a Defender block raises OSError, so this returns False honestly."""
    try:
        subprocess.run([exe_path, "--help"], capture_output=True, timeout=timeout)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
