"""PyInstaller build script for Cyber Controller.

Detects the current platform and runs PyInstaller with the correct
options to produce a single-file executable.

Usage:
    python build.py            # default: --onefile (single self-extracting .exe)
    python build.py --onedir   # folder build (instant startup) — what the Windows installer packages
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
_LOGO = _ROOT / "assets" / "cc-logo.png"
_NAME = "CyberController"


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
    onedir = "--onedir" in sys.argv[1:]
    plat = _detect_platform()
    print(f"Platform : {plat}")
    print(f"Mode     : {'--onedir (folder; instant startup, for the installer)' if onedir else '--onefile'}")
    print(f"Entry    : {_ENTRY}")
    print(f"Icon     : {_ICON if _ICON.exists() else '(not found, skipping)'}")
    print()

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--onedir" if onedir else "--onefile",
        "--windowed",
        "--name", _NAME,
    ]

    if _ICON.exists():
        cmd.extend(["--icon", str(_ICON)])

    # Splash screen — CRITICAL UX for the ONEFILE build only: a --onefile --windowed exe extracts ~80MB
    # to a temp dir on launch (10-20s on first run / slow disks) with NO visible feedback, so users think
    # the app failed to start ("installation error"). The splash shows instantly during extraction and
    # is closed by the app once the main window is ready (see launch_qt -> pyi_splash.close()).
    # PyInstaller splash is supported on Windows + Linux only (not macOS). A --onedir build starts
    # instantly (no self-extract), so it needs no splash.
    if not onedir and platform.system() in ("Windows", "Linux") and _LOGO.exists():
        cmd.extend(["--splash", str(_LOGO)])

    # Add data files
    sep = ";" if platform.system() == "Windows" else ":"
    profiles_dir = _ROOT / "src" / "config" / "profiles"
    if profiles_dir.is_dir():
        cmd.extend(["--add-data", f"{profiles_dir}{sep}src/config/profiles"])

    # Software-OS flashing catalog (Kali / Tails / Arch / ...): bundle so the Software tab + --flash-os
    # work fully offline (resource_path resolves src/config/os_catalog.json in the frozen build).
    os_catalog = _ROOT / "src" / "config" / "os_catalog.json"
    if os_catalog.is_file():
        cmd.extend(["--add-data", f"{os_catalog}{sep}src/config"])

    missions_dir = _ROOT / "src" / "config" / "missions"
    if missions_dir.is_dir():
        cmd.extend(["--add-data", f"{missions_dir}{sep}config/missions"])

    # Include assets (logo, icons)
    assets_dir = _ROOT / "assets"
    if assets_dir.is_dir():
        cmd.extend(["--add-data", f"{assets_dir}{sep}assets"])

    # In-app How-To guide (rendered by the How-To tab via resource_path).
    howto = _ROOT / "docs" / "HOWTO.md"
    if howto.is_file():
        cmd.extend(["--add-data", f"{howto}{sep}docs"])

    # Dead Man's Switch submodule: the host provisioner + partition CSVs that --deadman-setup
    # imports at runtime (resolved via resource_path). Bundled only when the submodule is checked
    # out — CI uses `submodules: recursive`; locally run `git submodule update --init deadmans-switch`.
    ds_host = _ROOT / "deadmans-switch" / "host"
    if ds_host.is_dir():
        cmd.extend(["--add-data", f"{ds_host}{sep}deadmans-switch/host"])
    ds_parts = _ROOT / "deadmans-switch" / "firmware" / "partitions"
    if ds_parts.is_dir():
        cmd.extend(["--add-data", f"{ds_parts}{sep}deadmans-switch/firmware/partitions"])

    # QSS theme stylesheets
    theme_dir = _ROOT / "src" / "ui" / "qt" / "theme"
    for qss in theme_dir.glob("*.qss"):
        cmd.extend(["--add-data", f"{qss}{sep}src/ui/qt/theme"])

    # Hidden imports — all UI variants + serial + launcher
    cmd.extend([
        # Serial / device comms
        "--hidden-import", "serial",
        "--hidden-import", "serial.tools.list_ports",
        # PyQt5 (full GUI + launcher dialog)
        "--hidden-import", "PyQt5",
        "--hidden-import", "PyQt5.sip",
        "--hidden-import", "PyQt5.QtCore",
        "--hidden-import", "PyQt5.QtGui",
        "--hidden-import", "PyQt5.QtWidgets",
        # Tkinter (lightweight GUI)
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "tkinter.filedialog",
        # Textual (TUI)
        "--hidden-import", "textual",
        "--hidden-import", "textual.app",
        # Launcher
        "--hidden-import", "src.ui.launcher",
    ])

    # Collect submodules for all UI variants
    cmd.extend([
        "--collect-submodules", "src.ui.qt",
        "--collect-submodules", "src.ui.tk",
        "--collect-submodules", "src.ui.tui",
        "--collect-submodules", "src.ui.web",
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
        print("Build succeeded.")
        print(f"  Time   : {elapsed:.1f}s")
        print(f"  Output : {dist}")
        if onedir:
            folder = dist / _NAME
            print(f"  Folder : {folder}  (package this with the installer)")
        else:
            for exe in dist.glob(f"{_NAME}*"):
                if exe.is_file():
                    size_mb = exe.stat().st_size / (1024 * 1024)
                    print(f"  Binary : {exe.name} ({size_mb:.1f} MB)")
    else:
        print(f"Build FAILED (exit code {result.returncode})")

    return result.returncode


if __name__ == "__main__":
    sys.exit(_build())
