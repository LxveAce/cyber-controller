"""PyInstaller build script for Cyber Controller.

Detects the current platform and runs PyInstaller with the correct
options to produce a single-file executable.

Usage:
    python build.py            # default: --onefile (single self-extracting .exe)
    python build.py --onedir   # folder build (instant startup) — what the Windows installer packages
"""

from __future__ import annotations

import importlib
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


def _require_linux_runtime() -> None:
    """Fail before freezing when the Linux GUI or core web runtime cannot import."""
    required = (
        "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "qtpy.QtWebEngineWidgets",
        "webview", "flask", "flask_socketio", "engineio.async_drivers.threading",
        "cryptography.hazmat.primitives.ciphers.aead", "esptool", "pyzipper", "defusedxml",
        "esp_idf_nvs_partition_gen",
    )
    for name in required:
        try:
            importlib.import_module(name)
        except Exception as exc:
            raise RuntimeError(f"Linux runtime dependency {name} could not load: {exc}") from exc


def _detect_platform() -> str:
    """Return a short platform tag."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        if "arm" in machine or "aarch64" in machine:
            return "windows-arm64" if "64" in machine else "windows-arm"
        return "windows-x64" if "64" in machine or machine == "amd64" else "windows-x86"
    if system == "linux":
        if "arm" in machine or "aarch64" in machine:
            return "linux-arm64" if "64" in machine else "linux-arm"
        return "linux-x64"
    if system == "darwin":
        return "macos-arm64" if machine == "arm64" else "macos-x64"
    return f"{system}-{machine}"


def _module_available(name: str) -> bool:
    """Discoverable without executing anything: the build decides on presence, PyInstaller imports.

    Each dotted part is resolved by a path-based lookup (builtin/frozen finders first for the top
    level), so a parent package's __init__ never runs here; importlib.util.find_spec would import
    the parent. A missing level reports unavailable. A namespace-only package (a directory with no
    __init__) is reported unavailable on purpose: directory existence is not module availability.
    """
    from importlib.machinery import BuiltinImporter, FrozenImporter, PathFinder

    parts = name.split(".")
    search = None
    for depth in range(len(parts)):
        qualified = ".".join(parts[: depth + 1])
        spec = None
        if depth == 0:
            spec = BuiltinImporter.find_spec(qualified) or FrozenImporter.find_spec(qualified)
        if spec is None:
            if depth and not search:
                return False
            spec = PathFinder.find_spec(qualified, search)
        if spec is None or spec.origin is None:
            return False
        search = spec.submodule_search_locations
    return True


def _assemble_command(onedir: bool, *, available=_module_available) -> list[str]:
    """The PyInstaller argv for this checkout; optional inputs are decided through *available*."""
    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        # Non-interactive: overwrite an existing dist/ without the "…will be REMOVED! Continue? (y/N)"
        # prompt, which otherwise hangs (or aborts on EOFError) any headless/CI/automation rebuild.
        "--noconfirm",
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

    # CYD board-detection probe (merged ESP32 image). resource_path("src","config","probes",
    # "cyd_probe.bin") reads it in the frozen build for the Flash tab's "Detect board" button, so it
    # must be bundled or detection can't flash the probe. PyInstaller never touches a plain .bin.
    probes_dir = _ROOT / "src" / "config" / "probes"
    if probes_dir.is_dir():
        cmd.extend(["--add-data", f"{probes_dir}{sep}src/config/probes"])

    # Cyber-Controller-bundled Dead Man's Switch partition tables (e.g. the 8 MB guardian layout the
    # pinned submodule lacks). suicide_setup.partitions_csv resolves these before the submodule's copy.
    dms_parts_dir = _ROOT / "src" / "config" / "dms_partitions"
    if dms_parts_dir.is_dir():
        cmd.extend(["--add-data", f"{dms_parts_dir}{sep}src/config/dms_partitions"])

    # Flock heatmap world basemap (Natural Earth 110m, public domain). load_world_basemap() reads
    # resource_path("src","config","maps","world_110m.geojson") for the tab's toggleable world layer, so
    # it must be bundled or the basemap silently won't draw in the frozen build. PyInstaller ignores .geojson.
    maps_dir = _ROOT / "src" / "config" / "maps"
    if maps_dir.is_dir():
        cmd.extend(["--add-data", f"{maps_dir}{sep}src/config/maps"])

    # Bundled tiny WPA wordlist core (SecLists MIT subset) — bundled_wordlist_dir() reads
    # resource_path("src","config","wordlists") in the frozen build so the Crack Lab picker has
    # offline lists with no download. PyInstaller never touches a plain .txt data dir.
    wordlists_dir = _ROOT / "src" / "config" / "wordlists"
    if wordlists_dir.is_dir():
        cmd.extend(["--add-data", f"{wordlists_dir}{sep}src/config/wordlists"])

    # Bundled crack-tool packs (AES-encrypted aircrack-ng/... + manifests). tool_bundle.list_packs()
    # reads resource_path("src","config","tools") in the frozen build; encrypted so Defender can't
    # delete them at rest (they extract only into a user-excluded folder on opt-in). PyInstaller never
    # touches a plain .pack/.json data dir.
    tools_dir = _ROOT / "src" / "config" / "tools"
    if tools_dir.is_dir():
        cmd.extend(["--add-data", f"{tools_dir}{sep}src/config/tools"])

    # Software-OS flashing catalog (Kali / Tails / Arch / ...): bundle so the Software tab + --flash-os
    # work fully offline (resource_path resolves src/config/os_catalog.json in the frozen build).
    os_catalog = _ROOT / "src" / "config" / "os_catalog.json"
    if os_catalog.is_file():
        cmd.extend(["--add-data", f"{os_catalog}{sep}src/config"])

    # Bundled lookup tables (gzipped): the IEEE OUI MAC-vendor table (src/core/oui.py) and the Bluetooth-SIG
    # company-id table (src/core/ble_numbers.py). Both resolve via resource_path("src","config",...), so they
    # MUST be added here or the vendor/company enrichment silently returns "" in the frozen .exe (C-8 class —
    # previously the OUI table was never bundled at all).
    for _tbl in ("oui_table.tsv.gz", "ble_company_ids.tsv.gz"):
        _tbl_path = _ROOT / "src" / "config" / _tbl
        if _tbl_path.is_file():
            cmd.extend(["--add-data", f"{_tbl_path}{sep}src/config"])

    # Include assets (logo, icons)
    assets_dir = _ROOT / "assets"
    if assets_dir.is_dir():
        cmd.extend(["--add-data", f"{assets_dir}{sep}assets"])

    # In-app How-To guide (rendered by the How-To tab via resource_path).
    howto = _ROOT / "docs" / "HOWTO.md"
    if howto.is_file():
        cmd.extend(["--add-data", f"{howto}{sep}docs"])

    # Bundled starter macros (cc_*.json) seeded on first run by MacroRecorder.seed_default_macros()
    # via resource_path("src", "core", "default_macros"). Without this the installed Macros tab is
    # empty — PyInstaller's module analysis never touches a plain data dir, so it must be added here.
    default_macros_dir = _ROOT / "src" / "core" / "default_macros"
    if default_macros_dir.is_dir():
        cmd.extend(["--add-data", f"{default_macros_dir}{sep}src/core/default_macros"])

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

    # Textual TUI stylesheet — CyberControllerTUI.CSS_PATH resolves it via resource_path; without
    # bundling it the packaged `--ui tui` crashes on launch (Textual can't find styles.tcss in _MEIPASS).
    tui_styles = _ROOT / "src" / "ui" / "tui" / "styles.tcss"
    if tui_styles.is_file():
        cmd.extend(["--add-data", f"{tui_styles}{sep}src/ui/tui"])

    # Web Remote UI assets — Jinja templates + static (css/js/PWA/icons). app.py resolves these via
    # resource_path, so without bundling them every web page 500s (TemplateNotFound) in the frozen
    # build. --collect-submodules only gathers .py, never these data files.
    web_templates = _ROOT / "src" / "ui" / "web" / "templates"
    if web_templates.is_dir():
        cmd.extend(["--add-data", f"{web_templates}{sep}src/ui/web/templates"])
    web_static = _ROOT / "src" / "ui" / "web" / "static"
    if web_static.is_dir():
        cmd.extend(["--add-data", f"{web_static}{sep}src/ui/web/static"])

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
        # QtSvg: the tab/window icons load from assets/icons/*.svg via QSvgRenderer (src/ui/qt/icons.py).
        # Without this the module + its qsvg icon-engine plugin can be dropped from the frozen build and the
        # icons render blank. Assets themselves already ship via --add-data assets above.
        "--hidden-import", "PyQt5.QtSvg",
    ])
    # QtWebEngine powers the Qt desktop shell (src/ui/web/desktop_qt.py). Importing
    # QtWebEngineWidgets triggers PyInstaller's PyQt5 hook to bundle QtWebEngineProcess + the
    # Chromium resources/ICU. It is an OPTIONAL extra: the Windows [desktop] extra ships pywebview
    # instead, so on such a build these names would only produce "hidden import not found" errors.
    # Linux builds are required to have it (_require_linux_runtime), so this gate can never
    # silently drop the Linux renderer.
    if available("PyQt5.QtWebEngineWidgets"):
        cmd.extend([
            "--hidden-import", "PyQt5.QtWebEngineWidgets",
            "--hidden-import", "PyQt5.QtWebEngineCore",
        ])
    else:
        print("note: PyQtWebEngine not installed — the Qt desktop shell will not be bundled; "
              "pywebview (system webview) and the browser fallback remain.")
    cmd.extend([
        # QWebChannel ships with base PyQt5 and backs the native file bridge of the Qt shell.
        "--hidden-import", "PyQt5.QtWebChannel",
        "--hidden-import", "PyQt5.QtNetwork",
        "--hidden-import", "PyQt5.QtPrintSupport",
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

    # esptool must be fully COLLECTED — its submodules, its stub_flasher/*.json data, and its deps
    # (reedsolo / bitstring / ecdsa) — so the in-process `--_run-esptool` dispatcher (src/app.py) can
    # run it in the frozen app. flash_core only ever shelled it as `-m esptool` before, so PyInstaller's
    # dependency graph never saw it and left it out of the bundle entirely.
    cmd.extend(["--collect-all", "esptool"])
    if platform.system() == "Linux":
        cmd.extend(["--collect-all", "qtpy"])

    # The provisioner imports esp_idf_nvs_partition_gen dynamically, so collect the complete package.
    # The current native Linux ARM workflow installs the desktop package with dependencies;
    # _require_linux_runtime() rejects a missing NVS generator before freezing on Linux.
    if available("esp_idf_nvs_partition_gen"):
        cmd.extend(["--collect-all", "esp_idf_nvs_partition_gen"])
        print("Bundling esp_idf_nvs_partition_gen (Dead Man's Switch NVS provisioning).")
    else:
        print("note: esp_idf_nvs_partition_gen not installed — DMS NVS provisioning won't be bundled.")

    # pyzipper (+ its pycryptodome AES backend) decrypts the bundled crack-tool packs at runtime
    # (src/core/tool_bundle.py). Collect it fully so the frozen app can unpack aircrack-ng/... on opt-in.
    if available("pyzipper"):
        cmd.extend(["--collect-all", "pyzipper"])
    else:
        print("note: pyzipper not installed — bundled crack-tool packs can't be unpacked in this build.")

    # Collect the Flask/Socket.IO web runtime and template dependencies for the Reform UI.
    # Linux builds must pass _require_linux_runtime() before collection; missing required web or
    # renderer imports stop the build. The per-package guards remain for other build environments.
    for _webpkg in ("flask", "flask_socketio", "engineio", "socketio", "jinja2", "werkzeug"):
        if available(_webpkg):
            cmd.extend(["--collect-all", _webpkg])
        else:
            print(f"note: {_webpkg} not installed — the qtweb/web UI won't be fully bundled in this build.")

    # pywebview is the system-webview shell for the DEFAULT "Normal GUI" (WebView2 on Windows,
    # WebKitGTK on Linux). It is a LAZY import inside launch_desktop, so PyInstaller's static analysis
    # never sees it — collect it explicitly (with its per-platform backends) or the packaged app has NO
    # webview, the Normal GUI can't open a window, and a --windowed build "installs but won't launch".
    # (The install step must include the [desktop] extra so pywebview is present to collect.)
    if available("webview"):
        cmd.extend(["--collect-all", "webview"])
        # pywebview's WINDOWS WebView2 backend is .NET via pythonnet (clr). PyInstaller auto-follows the
        # `import clr` from webview.platforms.edgechromium and bundles it — BUT only if pythonnet is
        # actually installed. The [desktop] extra pulls `pythonnet` on win32 so the CI build gets a real
        # native window instead of dropping to the browser fallback. (Verified: with pythonnet present,
        # the frozen build opens the native window; without it, ~16 MB smaller and browser-only.)
    else:
        print("note: pywebview not installed — the Normal GUI (system webview) will NOT be bundled; "
              "the packaged app would fall back to the browser UI. Install .[desktop] before building.")

    # Collect submodules for all UI variants
    cmd.extend([
        "--collect-submodules", "src.ui.qt",
        "--collect-submodules", "src.ui.tk",
        "--collect-submodules", "src.ui.tui",
        "--collect-submodules", "src.ui.web",
    ])

    cmd.append(str(_ENTRY))
    return cmd


def _build() -> int:
    onedir = "--onedir" in sys.argv[1:]
    plat = _detect_platform()
    if platform.system() == "Linux":
        _require_linux_runtime()
    print(f"Platform : {plat}")
    print(f"Mode     : {'--onedir (folder; instant startup, for the installer)' if onedir else '--onefile'}")
    print(f"Entry    : {_ENTRY}")
    print(f"Icon     : {_ICON if _ICON.exists() else '(not found, skipping)'}")
    print()

    cmd = _assemble_command(onedir)

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
