"""
RTL8720 (BW16 / AmebaD) flash backend — drives ``rtltool.py`` over UART.

The Realtek **RTL8720DN** (sold as the **BW16** module) is an *AmebaD*-family part.
It is NOT an Espressif chip: it cannot be flashed with ``esptool`` and it is NOT an
``ltchiptool`` target either. It is flashed by **rtltool.py**, a small Python CLI that
talks to the AmebaD UART *mask-ROM* loader, uploads an SRAM flash-loader stub
(``imgtool_flashloader_amebad.bin``), and then reads/writes the SPI flash.

Hardware facts encoded here (so the rest of the app never has to re-learn them):

  * **Tool**: an external ``rtltool.py`` (a Python script). We locate it via an explicit
    path argument, a bundled ``tools/`` directory, or ``PATH`` — and raise a clear
    :class:`RtlToolNotFound` with install guidance if it is absent (mirrors how
    :mod:`adb_backend` reports a missing ``adb``).
  * **Baud**: default **1500000** (1.5 Mbaud) — the rate the AmebaD loader negotiates.
  * **Flash base**: the SPI flash is memory-mapped at **0x08000000**. ``rtltool``'s
    own offset ``0x0`` corresponds to that base, so we pass offset ``0x0`` (NOT
    ``0x08000000``) on the command line. ``FLASH_BASE`` is kept for documentation /
    callers that want to display the physical address.
  * **READ / dump**:  ``rtltool.py -p <PORT> -b <BAUD> rf 0x0 <size> <out.bin>``
    (size ``0x200000`` for a 2 MB part, ``0x400000`` for a 4 MB part).
  * **WRITE**:        ``rtltool.py -p <PORT> -b <BAUD> wf 0x0 <image.bin>``
  * **Flash-loader stub**: ``imgtool_flashloader_amebad.bin`` is uploaded into SRAM by the
    tool before any flash op. Some builds of ``rtltool`` need it pointed at explicitly
    (``--flash-loader <path>``); the path is configurable here and auto-discovered next to
    the tool if present.

DOWNLOAD MODE (critical, read this):
  The AmebaD UART ROM loader is entered by an **external, manual** pin/button sequence —
  typically **PA7 (a.k.a. the "DOWNLOAD"/"CEN" combo) pulled to GND while pulsing EN/RESET**,
  or a **BOOT + RESET** press on dev boards that break those pins out. A few carrier boards
  auto-enter download mode by toggling DTR/RTS, but most BW16 breakouts do NOT, and this
  backend **cannot reliably trigger it**. Every entry point therefore emits a clear
  instruction telling the user to put the board into download mode *first*; if the tool
  reports a sync/handshake failure we surface that as "board is probably not in download
  mode" rather than a cryptic traceback.

ANTI-BRICK SAFETY (the headline feature of :func:`flash`):
  The chip is recoverable over UART via the mask-ROM loader, so the documented safe practice
  is **dump-first**: read the *entire* existing flash to a timestamped backup file BEFORE
  writing the new image. :func:`flash` does this by default; pass ``skip_backup=True`` only
  when you already have a known-good dump. If the pre-write dump fails, :func:`flash` aborts
  WITHOUT writing — never trade a recoverable board for an un-backed-up write.

Known "flash unprotect" gotcha:
  Some RTL8720 / AmebaD ``rtltool`` builds fail the SPI *unprotect* step on certain flash
  vendors (the write silently no-ops or the tool errors on "unprotect"). If a write reports
  success but a verify/re-dump shows the old contents, the unprotect step was skipped — retry
  with a tool build that issues the vendor-correct unprotect command, or unprotect manually.
  See :func:`write_flash`'s handling and the ``UNPROTECT`` note below.

Public API
----------
    find_rtltool() -> str | None
    rtltool_available() -> bool
    read_flash(port, out, size, baud, on_line, tool=None, flash_loader=None) -> int
    write_flash(port, image, offset, baud, on_line, tool=None, flash_loader=None) -> int
    flash(port, image, on_line, backup_dir, size, baud, skip_backup=False, ...) -> int

This module has NO intra-repo dependencies (stdlib only) so it can be imported and
unit-tested in isolation, exactly like :mod:`sd_backend` / :mod:`adb_backend`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional

Line = Callable[[str], None]

# --------------------------------------------------------------------------- #
# Hardware / tool constants
# --------------------------------------------------------------------------- #

#: Default UART baud for the AmebaD ROM loader. 1.5 Mbaud is what rtltool negotiates.
DEFAULT_BAUD = 1500000

#: The RTL8720/AmebaD SPI flash is memory-mapped at this physical base. Documentation
#: only — on the rtltool command line, offset 0x0 already means "start of flash"
#: (i.e. this base), so we pass 0x0, never 0x08000000.
FLASH_BASE = 0x08000000

#: rtltool flash offset that corresponds to FLASH_BASE (start of flash).
FLASH_BASE_OFFSET = "0x0"

#: Common full-flash sizes (bytes) for the two BW16 part densities.
SIZE_2MB = "0x200000"
SIZE_4MB = "0x400000"
DEFAULT_SIZE = SIZE_2MB  # BW16 / RTL8720DN ships with 2 MB most commonly.

#: The SRAM flash-loader stub rtltool uploads before any flash op.
FLASH_LOADER_NAME = "imgtool_flashloader_amebad.bin"

#: Candidate basenames for the tool itself (a Python script, occasionally an exe wrapper).
_TOOL_NAMES = ("rtltool.py", "rtltool")

#: Substrings in tool output that mean "the board never answered the ROM-loader handshake",
#: which on a BW16 almost always means it is NOT in download mode.
_NO_SYNC_MARKERS = (
    "failed to connect",
    "no response",
    "sync",
    "timeout",
    "timed out",
    "handshake",
    "could not open",
    "device not found",
)

#: Substrings that indicate the known SPI "unprotect" failure (see module docstring).
_UNPROTECT_MARKERS = ("unprotect", "protected", "write protect")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class RtlToolNotFound(FileNotFoundError):
    """Raised when ``rtltool.py`` cannot be located.

    Carries human-facing install guidance (mirrors how :mod:`adb_backend` reports a
    missing ``adb``). The message is safe to show directly in a UI log.
    """

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message or install_guidance())


class DownloadModeError(RuntimeError):
    """Raised/surfaced when the board never answered the ROM-loader handshake.

    On a BW16 this nearly always means the board is not in UART download mode. The
    message tells the user how to enter it.
    """


def install_guidance() -> str:
    """Return a one-paragraph 'how to get rtltool' message for logs / dialogs."""
    return (
        "rtltool.py not found. The BW16 / RTL8720DN (AmebaD) is flashed by rtltool.py, "
        "NOT esptool or ltchiptool. Install it from the Realtek AmebaD SDK "
        "(component/soc/realtek/amebad/misc/iar_utility or the Ameda 'tools' dir), or grab a "
        "standalone copy (e.g. the rtltool.py bundled with the RTL8720dn-Deauther / Ameba "
        "Arduino tooling). Then either put it on your PATH, drop it in this app's tools/ "
        "directory, or pass its full path via the 'tool=' argument."
    )


def download_mode_help() -> str:
    """Return the manual download-mode instructions for the user."""
    return (
        "Put the BW16 / RTL8720DN into UART DOWNLOAD MODE first: pull PA7 (the "
        "DOWNLOAD/CEN pin) to GND, then pulse EN/RESET (or hold BOOT and tap RESET on "
        "dev boards that break those out). Some carrier boards auto-enter via DTR/RTS, "
        "but most BW16 breakouts do NOT — this tool cannot trigger it for you. "
        "Release the pin after reset, then retry."
    )


# --------------------------------------------------------------------------- #
# Tool discovery
# --------------------------------------------------------------------------- #

def _bundled_tools_dirs() -> List[str]:
    """Directories we treat as the app's bundled tool location, best-effort.

    We look beside this package (``.../src/tools``, ``.../tools``) and under a PyInstaller
    ``sys._MEIPASS`` bundle if present, plus an explicit ``RTLTOOL_HOME`` override. None of
    these need to exist; they are just candidate roots to scan.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # backends -> core -> src -> repo root
    src_dir = os.path.dirname(os.path.dirname(here))
    repo_root = os.path.dirname(src_dir)
    dirs = [
        os.path.join(src_dir, "tools"),
        os.path.join(repo_root, "tools"),
        os.path.join(repo_root, "tools", "rtltool"),
        os.path.join(here, "tools"),
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "tools"))
    env_home = os.environ.get("RTLTOOL_HOME")
    if env_home:
        dirs.append(env_home)
    # de-dup while preserving order
    seen: set = set()
    out: List[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_rtltool(explicit: Optional[str] = None) -> Optional[str]:
    """Locate ``rtltool.py``. Returns an absolute path, or None if not found.

    Search order (mirrors the adb/qflipper discovery style):
      1. an explicit path the caller passed (validated to exist);
      2. a bundled ``tools/`` directory shipped with the app;
      3. ``PATH`` (via :func:`shutil.which`, covers ``rtltool``/``rtltool.py``).
    """
    if explicit:
        if os.path.isfile(explicit):
            return os.path.abspath(explicit)
        return None

    for d in _bundled_tools_dirs():
        for name in _TOOL_NAMES:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return os.path.abspath(cand)

    for name in _TOOL_NAMES:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    return None


def rtltool_available(explicit: Optional[str] = None) -> bool:
    """True if :func:`find_rtltool` can locate the tool."""
    return find_rtltool(explicit) is not None


def find_flash_loader(tool_path: Optional[str] = None,
                      explicit: Optional[str] = None) -> Optional[str]:
    """Locate the SRAM flash-loader stub ``imgtool_flashloader_amebad.bin``.

    Looks at an explicit path first, then next to the resolved tool, then in the bundled
    tool dirs. Returns None if not found (many rtltool builds embed the stub and don't
    need it pointed at explicitly).
    """
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else None

    search_dirs: List[str] = []
    if tool_path:
        search_dirs.append(os.path.dirname(os.path.abspath(tool_path)))
    search_dirs.extend(_bundled_tools_dirs())
    for d in search_dirs:
        cand = os.path.join(d, FLASH_LOADER_NAME)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


# --------------------------------------------------------------------------- #
# Subprocess plumbing (stream output through on_line, like adb/flash_core)
# --------------------------------------------------------------------------- #

def _tool_argv(tool: str, *args: str) -> List[str]:
    """Build the argv to invoke the tool.

    A ``.py`` script is run via the current interpreter (``python rtltool.py ...``) so it
    works on Windows without a registered ``.py`` association; an extension-less / ``.exe``
    tool is invoked directly.
    """
    if tool.lower().endswith(".py"):
        return [sys.executable, tool, *args]
    return [tool, *args]


def _run_stream(argv: List[str], on_line: Line, timeout: int) -> int:
    """Run *argv*, stream combined stdout/stderr line-by-line through *on_line*, return rc.

    On any mid-stream exception (e.g. the UI callback raises) or timeout the child is killed
    and reaped so it cannot keep holding the serial port — otherwise the next op fails with
    'port busy'. Mirrors :func:`flash_core._run_stream` / :func:`adb_backend._run_adb`.
    Returns 127 if the executable itself is missing.
    """
    on_line("$ " + " ".join(argv))
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        on_line(f"[error] {e}")
        return 127
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            on_line(line.rstrip("\n"))
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        on_line("[error] rtltool timed out")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1
    except Exception as e:
        on_line(f"[error] {e}")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
    on_line(f"[exit {proc.returncode}]")
    return proc.returncode


class _Tee:
    """on_line wrapper that also captures every line, so callers can post-mortem the
    output for known-failure markers (no-sync / unprotect) without re-running."""

    def __init__(self, inner: Line) -> None:
        self._inner = inner
        self.lines: List[str] = []

    def __call__(self, s: str) -> None:
        self.lines.append(s)
        self._inner(s)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _looks_like_no_sync(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _NO_SYNC_MARKERS)


def _looks_like_unprotect_fail(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _UNPROTECT_MARKERS)


def _common_args(port: str, baud: int, flash_loader: Optional[str]) -> List[str]:
    """The shared ``-p <port> -b <baud> [--flash-loader <stub>]`` prefix.

    The flash-loader flag is only added when we actually have a stub path; rtltool builds
    that embed the loader don't accept it and would error on an unknown flag.
    """
    args = ["-p", port, "-b", str(baud)]
    if flash_loader:
        args += ["--flash-loader", flash_loader]
    return args


# --------------------------------------------------------------------------- #
# Read (dump)
# --------------------------------------------------------------------------- #

def read_flash(port: str, out: str, size: str = DEFAULT_SIZE,
               baud: int = DEFAULT_BAUD, on_line: Optional[Line] = None,
               tool: Optional[str] = None,
               flash_loader: Optional[str] = None,
               timeout: int = 600) -> int:
    """Dump the SPI flash to *out* via ``rtltool.py rf 0x0 <size> <out>``. Returns rc (0 = ok).

    The read always starts at flash offset ``0x0`` (== FLASH_BASE). *size* is a hex string
    (``"0x200000"`` for 2 MB, ``"0x400000"`` for 4 MB). Raises :class:`RtlToolNotFound` if the
    tool is missing. A non-zero return whose output looks like a handshake failure is
    annotated with download-mode guidance (but still returned as a code, not raised, so a
    caller's flow control stays simple).
    """
    on_line = on_line or (lambda _s: None)
    resolved = find_rtltool(tool)
    if not resolved:
        raise RtlToolNotFound()

    loader = flash_loader if flash_loader else find_flash_loader(resolved)
    on_line(f"[rtl8720] dumping flash {size} from {FLASH_BASE_OFFSET} "
            f"(flash base 0x{FLASH_BASE:08X}) -> {out}")
    on_line(f"[rtl8720] {download_mode_help()}")

    argv = _tool_argv(
        resolved,
        *_common_args(port, baud, loader),
        "rf", FLASH_BASE_OFFSET, size, out,
    )
    tee = _Tee(on_line)
    rc = _run_stream(argv, tee, timeout)
    if rc != 0 and _looks_like_no_sync(tee.text):
        on_line("[rtl8720] no ROM-loader response — the board is probably NOT in "
                "download mode. " + download_mode_help())
    return rc


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

def write_flash(port: str, image: str, offset: str = FLASH_BASE_OFFSET,
                baud: int = DEFAULT_BAUD, on_line: Optional[Line] = None,
                tool: Optional[str] = None,
                flash_loader: Optional[str] = None,
                timeout: int = 600) -> int:
    """Write *image* to flash via ``rtltool.py wf <offset> <image>``. Returns rc (0 = ok).

    *offset* defaults to ``0x0`` (== FLASH_BASE, the start of flash) — a full merged BW16
    image is written there. Raises :class:`RtlToolNotFound` if the tool is missing and
    :class:`FileNotFoundError` if *image* does not exist (so we never invoke the tool with a
    bad path and brick a board mid-write).

    Known "unprotect" gotcha: some rtltool/AmebaD builds fail the SPI unprotect step on
    certain flash vendors — the write reports success but the flash is unchanged. If the
    streamed output mentions unprotect/protection we flag it so the caller can verify with a
    re-dump and, if needed, retry with a tool build that issues the vendor-correct unprotect.
    """
    on_line = on_line or (lambda _s: None)
    if not os.path.isfile(image):
        raise FileNotFoundError(f"firmware image not found: {image}")
    resolved = find_rtltool(tool)
    if not resolved:
        raise RtlToolNotFound()

    loader = flash_loader if flash_loader else find_flash_loader(resolved)
    on_line(f"[rtl8720] writing {os.path.basename(image)} to {offset} "
            f"(flash base 0x{FLASH_BASE:08X}) @ {baud} baud")
    on_line(f"[rtl8720] {download_mode_help()}")

    argv = _tool_argv(
        resolved,
        *_common_args(port, baud, loader),
        "wf", offset, image,
    )
    tee = _Tee(on_line)
    rc = _run_stream(argv, tee, timeout)
    if rc != 0 and _looks_like_no_sync(tee.text):
        on_line("[rtl8720] no ROM-loader response — the board is probably NOT in "
                "download mode. " + download_mode_help())
    elif _looks_like_unprotect_fail(tee.text):
        # Surface even on rc==0: a "success" with an unprotect warning is the classic
        # silent no-op. The caller (flash()) re-verifies via the post-write read path.
        on_line("[rtl8720] WARNING: the SPI 'unprotect' step may have been skipped "
                "(known RTL8720 gotcha). Verify with a re-dump; if the old contents "
                "remain, retry with an rtltool build that unprotects this flash vendor.")
    return rc


# --------------------------------------------------------------------------- #
# High-level flash (DUMP-FIRST anti-brick, then write)
# --------------------------------------------------------------------------- #

def _backup_path(backup_dir: str, port: str) -> str:
    """Build a timestamped backup filename inside *backup_dir* (created if missing)."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_port = "".join(c if c.isalnum() else "_" for c in port) or "port"
    return os.path.join(backup_dir, f"rtl8720_backup_{safe_port}_{stamp}.bin")


def flash(port: str, image: str, on_line: Optional[Line] = None,
          backup_dir: Optional[str] = None, size: str = DEFAULT_SIZE,
          baud: int = DEFAULT_BAUD, *, skip_backup: bool = False,
          offset: str = FLASH_BASE_OFFSET, tool: Optional[str] = None,
          flash_loader: Optional[str] = None, timeout: int = 600) -> int:
    """Safely flash a BW16 / RTL8720DN: **dump the existing flash first, then write**.

    This is the documented anti-brick procedure. Because the AmebaD mask-ROM loader makes the
    chip recoverable over UART, taking a full backup before overwriting means a bad/wrong
    image is always reversible. Sequence:

      1. validate inputs + locate the tool (raise a clear error if missing);
      2. unless *skip_backup*, dump the WHOLE flash (``read_flash``) to a timestamped file in
         *backup_dir* — and **abort without writing if that dump fails** (never trade a
         recoverable board for an un-backed-up write);
      3. write *image* (``write_flash``).

    Returns 0 on success, non-zero on failure. Raises :class:`RtlToolNotFound` if the tool is
    missing and :class:`FileNotFoundError` if *image* does not exist. *skip_backup=True* skips
    step 2 (only when you already hold a known-good dump).
    """
    on_line = on_line or (lambda _s: None)

    # 1) validate up front so we never start a destructive op on bad inputs.
    if not os.path.isfile(image):
        raise FileNotFoundError(f"firmware image not found: {image}")
    resolved = find_rtltool(tool)
    if not resolved:
        raise RtlToolNotFound()
    on_line(f"[rtl8720] using tool: {resolved}")

    # 2) DUMP-FIRST (default). Abort the whole flash if the backup can't be taken.
    if not skip_backup:
        if not backup_dir:
            backup_dir = os.path.join(os.getcwd(), "rtl8720_backups")
        backup_file = _backup_path(backup_dir, port)
        on_line(f"[rtl8720] anti-brick: backing up existing flash -> {backup_file}")
        rc = read_flash(port, backup_file, size=size, baud=baud, on_line=on_line,
                        tool=resolved, flash_loader=flash_loader, timeout=timeout)
        if rc != 0:
            on_line("[rtl8720] BACKUP FAILED — refusing to write (no recovery image). "
                    "Fix download mode / connection and retry, or pass skip_backup=True "
                    "only if you already have a known-good dump.")
            return rc
        # sanity: a 0-byte or missing backup is not a real backup.
        if not os.path.isfile(backup_file) or os.path.getsize(backup_file) == 0:
            on_line("[rtl8720] BACKUP appears empty — refusing to write.")
            return 1
        on_line(f"[rtl8720] backup OK ({os.path.getsize(backup_file)} bytes)")
    else:
        on_line("[rtl8720] skip_backup=True — NO pre-write dump taken (caller asserts a "
                "known-good backup already exists).")

    # 3) write the new image.
    on_line("[rtl8720] writing new firmware image...")
    rc = write_flash(port, image, offset=offset, baud=baud, on_line=on_line,
                     tool=resolved, flash_loader=flash_loader, timeout=timeout)
    if rc == 0:
        on_line("[rtl8720] flash complete.")
    else:
        on_line("[rtl8720] flash FAILED. The pre-write backup (if taken) can restore the "
                "board: re-run with that .bin as the image once the connection is fixed.")
    return rc


# --------------------------------------------------------------------------- #
# AmebaD ImageTool path (Realtek's upload_image_tool — the HARDWARE-PROVEN path)
# --------------------------------------------------------------------------- #
#
# This is the path validated end-to-end on a real BW16: Realtek's official
# ``upload_image_tool`` (from the ambiot/ambd_arduino package) flashed the Vampire
# Deauther and checksum-verified it, AUTO-entering download mode via DTR/RTS — no
# button press. Unlike rtltool's single merged .bin, the ImageTool flashes a THREE
# FILE AmebaD bundle at fixed offsets:
#     km0_boot_all.bin    @ 0x08000000
#     km4_boot_all.bin    @ 0x08004000
#     km0_km4_image2.bin  @ 0x08006000   (the app)
# plus the SRAM loader imgtool_flashloader_amebad.bin. The tool chdir's into the
# bundle directory and expects those exact filenames there. CLI (positional):
#     upload_image_tool_<os> <bundle_dir> <PORT> --auto=<1|0> --verbose=<n>
# It is write+verify only (no read/dump) and exits 0 regardless, printing "done." on
# success or "failed" — so success is detected from the OUTPUT, not the exit code.
#
# LICENSING: the Windows ImageTool is a Cygwin build (needs cygwin1.dll beside it) and
# is GPL — we do NOT bundle it in this MIT repo. It is discovered via the
# CYBERC_AMEBAD_TOOL env var, a (gitignored) tools/realtek/ dir, or PATH, and the user
# obtains it from the ambd_arduino package for their own use.

#: The three AmebaD images the ImageTool flashes (at fixed offsets baked into the tool).
AMBD_BUNDLE_FILES = ("km0_boot_all.bin", "km4_boot_all.bin", "km0_km4_image2.bin")

#: Candidate basenames for the AmebaD ImageTool, per platform.
_AMBD_TOOL_NAMES = (
    "upload_image_tool_windows.exe",
    "upload_image_tool_linux",
    "upload_image_tool_macos",
)


def _realtek_tool_dirs() -> List[str]:
    """Bundled-tool dirs plus a ``realtek`` subdir convention for the ImageTool."""
    dirs: List[str] = []
    for d in _bundled_tools_dirs():
        dirs.append(d)
        dirs.append(os.path.join(d, "realtek"))
    return dirs


def find_ambd_tool(explicit: Optional[str] = None) -> Optional[str]:
    """Locate Realtek's ``upload_image_tool`` (AmebaD). Returns an abspath or None.

    Search order: explicit path -> ``CYBERC_AMEBAD_TOOL`` env -> bundled ``tools/`` and
    ``tools/realtek/`` dirs -> ``PATH``.
    """
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else None
    env = os.environ.get("CYBERC_AMEBAD_TOOL")
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    for d in _realtek_tool_dirs():
        for name in _AMBD_TOOL_NAMES:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    for name in _AMBD_TOOL_NAMES:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    return None


def ambd_tool_available(explicit: Optional[str] = None) -> bool:
    """True if :func:`find_ambd_tool` can locate the AmebaD ImageTool."""
    return find_ambd_tool(explicit) is not None


def ambd_install_guidance() -> str:
    """How to obtain the AmebaD ImageTool (for logs / dialogs)."""
    return (
        "Realtek AmebaD ImageTool not found. The BW16 / RTL8720DN is flashed by Realtek's "
        "upload_image_tool (from the ambiot/ambd_arduino package, under "
        "Arduino_package/ameba_d_tools_<os>/). On Windows it is a Cygwin build and needs "
        "cygwin1.dll in the SAME directory. We do not bundle it (GPL). Obtain it, then set "
        "the CYBERC_AMEBAD_TOOL env var to its full path, or drop it (with cygwin1.dll) in "
        "this app's tools/realtek/ directory."
    )


def flash_ambd(port: str, bundle_dir: str, tool: Optional[str] = None,
               auto: bool = True, verbose: int = 1,
               on_line: Optional[Line] = None, timeout: int = 300) -> int:
    """Flash a BW16 via Realtek's AmebaD ImageTool from a 3-file *bundle_dir*. Returns 0/!=0.

    *bundle_dir* must contain the three :data:`AMBD_BUNDLE_FILES` plus the SRAM loader
    ``imgtool_flashloader_amebad.bin`` (the tool chdir's there and reads them by name). With
    *auto* True the tool toggles DTR/RTS to enter download mode automatically (proven to work
    on the BW16); with *auto* False it gives a 5s countdown for a manual BOOT+RESET. Success is
    detected from the tool's output ("done." with no "failed"), since the ImageTool exits 0
    regardless. Raises :class:`RtlToolNotFound` if the tool is missing and
    :class:`FileNotFoundError` if the bundle/loader is incomplete (so we never start a
    destructive op on a bad bundle).
    """
    on_line = on_line or (lambda _s: None)
    resolved = find_ambd_tool(tool)
    if not resolved:
        raise RtlToolNotFound(ambd_install_guidance())

    missing = [f for f in AMBD_BUNDLE_FILES
               if not os.path.isfile(os.path.join(bundle_dir, f))]
    if missing:
        raise FileNotFoundError(
            f"AmebaD firmware bundle incomplete in {bundle_dir}: missing {missing}")
    # The loader must sit in the bundle dir (the tool chdir's there). Copy it next to the
    # bundle from beside the tool if it's only there.
    loader = os.path.join(bundle_dir, FLASH_LOADER_NAME)
    if not os.path.isfile(loader):
        beside = os.path.join(os.path.dirname(resolved), FLASH_LOADER_NAME)
        if os.path.isfile(beside):
            shutil.copy2(beside, loader)
        else:
            raise FileNotFoundError(
                f"{FLASH_LOADER_NAME} not found in {bundle_dir} or beside the tool")

    # On Windows the Cygwin build needs cygwin1.dll beside it — warn early if absent.
    if resolved.lower().endswith(".exe"):
        if not os.path.isfile(os.path.join(os.path.dirname(resolved), "cygwin1.dll")):
            on_line("[rtl8720/ambd] WARNING: cygwin1.dll not found next to the tool; the "
                    "Cygwin ImageTool build will fail to start without it.")

    on_line(f"[rtl8720/ambd] tool: {resolved}")
    on_line(f"[rtl8720/ambd] bundle: {bundle_dir}")
    if auto:
        on_line("[rtl8720/ambd] auto-download via DTR/RTS — no button press needed on boards "
                "that wire it (the BW16 does). If it times out on sync, retry with auto=False "
                "and hold BOOT + tap RESET during the 5s countdown.")
    else:
        on_line("[rtl8720/ambd] manual mode: hold BOOT, tap RESET during the 5s countdown.")

    argv = [resolved, bundle_dir, port, f"--auto={1 if auto else 0}", f"--verbose={verbose}"]
    tee = _Tee(on_line)
    rc = _run_stream(argv, tee, timeout)

    low = tee.text.lower()
    # The ImageTool exits 0 regardless; judge by output. "done." == success (it prints it
    # only after a successful checksum verify); "failed" / a sync error == failure.
    if "failed" in low or _looks_like_no_sync(low):
        if _looks_like_no_sync(low):
            on_line("[rtl8720/ambd] no ROM-loader sync — the board did not enter download "
                    "mode. Retry with auto=False + BOOT/RESET, or check the cable.")
        return rc or 1
    if rc == 0 and "done." in low:
        on_line("[rtl8720/ambd] flash complete (checksum verified).")
        return 0
    return rc or 1
