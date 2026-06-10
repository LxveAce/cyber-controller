"""PyInstaller build script for Cyber Controller.

Detects the current platform and runs PyInstaller with the correct
options to produce a single-file executable.

Usage:
    python build.py
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_ENTRY = _ROOT / "src" / "app.py"
_ICON = _ROOT / "assets" / "icon.ico"
_NAME = "cyber-controller"


def _detect_platform() -> str:
    """Return a short platform tag."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows-x64" if "64" in machine or machine == "amd64" else "windows-x86"
    if system == "linux":
        if "arm" in machine or "aarch64" in machine:
            return "linux-arm64" if "64" in machine else "linux-arm"
        return "linux-x64"
    if system == "darwin":
        return "macos-arm64" if machine == "arm64" else "macos-x64"
    return f"{system}-{machine}"


def _build() -> int:
    plat = _detect_platform()
    print(f"Platform : {plat}")
    print(f"Entry    : {_ENTRY}")
    print(f"Icon     : {_ICON if _ICON.exists() else '(not found, skipping)'}")
    print()

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", _NAME,
    ]

    if _ICON.exists():
        cmd.extend(["--icon", str(_ICON)])

    # Add data files
    sep = ";" if platform.system() == "Windows" else ":"
    profiles_dir = _ROOT / "src" / "config" / "profiles"
    if profiles_dir.is_dir():
        cmd.extend(["--add-data", f"{profiles_dir}{sep}config/profiles"])

    missions_dir = _ROOT / "src" / "config" / "missions"
    if missions_dir.is_dir():
        cmd.extend(["--add-data", f"{missions_dir}{sep}config/missions"])

    # Hidden imports that PyInstaller may miss
    cmd.extend([
        "--hidden-import", "serial",
        "--hidden-import", "serial.tools.list_ports",
        "--hidden-import", "PyQt5",
        "--hidden-import", "PyQt5.sip",
    ])

    cmd.append(str(_ENTRY))

    print(f"Running: {' '.join(cmd)}")
    print()

    start = time.time()
    result = subprocess.run(cmd)
    elapsed = time.time() - start

    print()
    if result.returncode == 0:
        dist = _ROOT / "dist"
        exes = list(dist.glob(f"{_NAME}*"))
        print("Build succeeded.")
        print(f"  Time   : {elapsed:.1f}s")
        print(f"  Output : {dist}")
        for exe in exes:
            size_mb = exe.stat().st_size / (1024 * 1024)
            print(f"  Binary : {exe.name} ({size_mb:.1f} MB)")
    else:
        print(f"Build FAILED (exit code {result.returncode})")

    return result.returncode


if __name__ == "__main__":
    sys.exit(_build())
