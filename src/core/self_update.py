"""In-place self-update (Phase 2) — download the platform's release binary, verify its SHA-256, and
swap it in so the user never has to re-download anything by hand.

Layering (why this is separate from :mod:`src.core.updater`)
------------------------------------------------------------
* :mod:`src.core.updater` stays the pure *decision + check* layer — is a newer release out, and
  should we prompt? It never touches disk or downloads.
* This module is the *apply* layer. It picks the release asset matching the running platform,
  verifies it against that release's own ``SHA256SUMS.txt``, and replaces the running executable.
  The genuinely destructive steps (moving a file over the live binary, re-exec) are isolated in
  small functions that **refuse to run unless we are a frozen onefile build**, so the unit tests
  can exercise all the selection/verification logic without ever touching a real binary.

Trust model
-----------
HTTPS to the allowlisted GitHub host set (reusing :mod:`src.core.flash_core`'s SSRF-hardened opener
+ redirect allowlist — release downloads legitimately 302 from ``github.com`` to
``objects.githubusercontent.com``), **plus a mandatory SHA-256 match** against the release's
published ``SHA256SUMS.txt``. We fail CLOSED on any mismatch or missing sum. There is no
code-signing yet — a signed manifest is the next hardening step (it would defend against a
compromised release); until then the checksum + HTTPS is the integrity floor.

The apply strategy is platform-shaped: a running executable can't be overwritten on Windows, so we
hand a tiny helper script the swap-and-relaunch once our process exits; on Unix the path can be
replaced live while running (the kernel holds the old inode), so we swap in place and re-exec.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import struct
import subprocess
import sys
import tempfile
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from src.core import flash_core, install, update_exe_format, update_select, updater

log = logging.getLogger(__name__)

# Match the check layer's short, non-lingering default.
DEFAULT_TIMEOUT = 30.0

# (bytes_done, bytes_total) — total may be 0 if the server sends no Content-Length.
ProgressCb = Callable[[int, int], None]


class SelfUpdateError(Exception):
    """A download/verify/apply failure. Callers surface it and stay on the old build."""


# ── Environment ──────────────────────────────────────────────────────────────────────────────────

def is_frozen() -> bool:
    """True only for a frozen (PyInstaller) build, where ``sys.executable`` IS our binary. A source
    checkout returns False — self-update is meaningless there and MUST be refused."""
    return bool(getattr(sys, "frozen", False))


def current_exe() -> str:
    """Absolute, symlink-resolved path of the running binary (the thing we replace)."""
    return os.path.realpath(sys.executable)


def installed_kind() -> str:
    """Which build shape are we running? One of:

    * source  — not frozen (a dev checkout).
    * onefile — a PyInstaller single-exe. sys._MEIPASS is a per-run _MEI… extraction dir, which may
      sit anywhere, including inside the exe's own folder when the exe is run from a temp dir.
    * onedir  — a one-folder build (the shape the Windows installer lays down). _MEIPASS is the app
      dir itself (legacy layout) or its _internal child (PyInstaller >= 6).
    * unknown — frozen but the layout matches neither positively; we do NOT guess onedir.

    Load-bearing for updates: the in-place swap replaces sys.executable itself. Right for a onefile
    binary, but WRONG for a onedir build, where sys.executable is a bootstrap loading the app from
    _internal/ — overwriting it orphans that folder and corrupts the install, so a onedir build must
    update via its installer. Detection is POSITIVE-only (U2): a onefile run from its own extraction
    parent must not be read as onedir, so we key on the specific onedir layouts and on the _MEI…
    extraction naming, never on a bare ancestor relationship. Reports the build SHAPE, not installer
    ownership — a hand-copied onedir folder is onedir but not an installed product."""
    if not is_frozen():
        return "source"
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return "unknown"   # frozen but no extraction bundle reported → unidentified, don't guess
    try:
        exe_dir = os.path.realpath(os.path.dirname(current_exe()))
        bundle = os.path.realpath(meipass)
    except (ValueError, OSError):
        return "unknown"
    # Positive onedir signals: the bundle IS the exe dir (legacy one-folder) or its _internal child.
    if bundle == exe_dir or bundle == os.path.join(exe_dir, "_internal"):
        return "onedir"
    # Onefile signal: a PyInstaller per-run extraction dir is named _MEIxxxxxx (wherever it lives,
    # including as a child of a temp exe dir — exactly the case that used to misclassify).
    if os.path.basename(bundle).startswith("_MEI"):
        return "onefile"
    return "unknown"   # ambiguous layout → don't assume onedir (and don't refuse below)


def can_self_update_in_place() -> bool:
    """True only for a positively-identified ONEFILE build — the one shape the in-place binary swap
    safely handles. onedir, unknown, and source all return False (an ``unknown`` layout is never
    offered an in-place swap). See :func:`installed_kind`."""
    return installed_kind() == "onefile"


def _non_onefile_refusal(kind: str) -> str:
    """The user-facing message when an in-place update is refused because the build isn't a swap-safe
    onefile — shape-specific so the UI (which falls to the release page) is truthful."""
    if kind == "onedir":
        return ("this is a one-folder (installer) build; in-place auto-update isn't available for "
                "it yet — download the latest installer from the release page")
    return ("couldn't identify this build's layout, so in-place auto-update is disabled for safety "
            "— download the latest build from the release page to update")


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """Canonical asset key for the current (or given) platform — matches the release asset naming
    (``cyber-controller-<tag>-<key>``). Parameterized so the mapping is table-testable.

    Only the four published (system, machine) shapes resolve, through the strict
    :func:`update_select.supported_platform_key`. Every other host (32-bit or ARM64 Windows, Intel
    macOS, 32-bit ARM, RISC-V or i686 Linux) is refused with a finite error instead of being coerced
    to a published key of another architecture: that asset would download, pass its own checksum and
    replace this binary with one that is not a supported automatic-update target for the host (and
    that most of these hosts cannot execute at all; Windows on ARM may only emulate it). The UI then
    offers the release page.
    """
    system = system if system is not None else platform.system()
    machine = machine if machine is not None else platform.machine()
    try:
        return update_select.supported_platform_key(system, machine)
    except update_select.UnsupportedPlatform as exc:
        raise SelfUpdateError(
            f"no published build for this machine ({system}/{machine}); download the right build "
            "from the release page") from exc


# ── Pure selection + verification ────────────────────────────────────────────────────────────────

def select_asset(assets: Sequence[Mapping[str, Any]], key: str, *,
                 installer: bool = False) -> dict | None:
    """Pick a release asset for *key*. For a Windows key, ``installer=True`` selects the setup
    installer (``…-setup.exe`` — the correct upgrade for a onedir/installer build); the default
    selects the standalone onefile binary (skipping the installer, since the in-place swap replaces
    that binary). ``installer`` is a no-op for non-Windows keys. Returns the raw asset dict, or None
    when this release has no matching asset."""
    want_exe = key.startswith("windows")
    for a in assets:
        name = str(a.get("name", ""))
        low = name.lower()
        if low.startswith("sha256sums"):
            continue
        is_setup = "setup" in low
        if want_exe:
            if installer != is_setup:   # want the installer XOR this is the installer → not a match
                continue
        elif is_setup:
            continue
        if key not in name:
            continue
        if want_exe and not low.endswith(".exe"):
            continue
        if not want_exe and low.endswith((".exe", ".txt", ".sha256")):
            continue
        return dict(a)
    return None


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``sha256sum``-style file (``<64-hex>  <name>`` per line; binary marker ``*``
    tolerated) into ``{filename: digest}``. Malformed / comment lines are skipped, not fatal."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower()
        name = parts[-1].lstrip("*")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            sums[name] = digest
    return sums


def sha256_file(path: str, _chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (never loads a whole binary into memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def validate_staged_executable(path: str, key: str) -> None:
    """Read-only header and architecture check of *path* for the published *key*, before any swap.

    One open handle and its fstat length; the exact prefix the validator needs, planned from the
    file's own headers by :func:`update_exe_format.required_prefix_length` under its named
    inspected-prefix limit; then :func:`update_exe_format.validate_onefile_executable`. Deletes
    nothing: the caller decides what it owns. Any refusal, short read or filesystem error becomes a
    finite :class:`SelfUpdateError` with the underlying error as context; no ``struct.error`` or
    ``OSError`` escapes to the UI. Format and CPU only, never runtime compatibility.
    """
    name = os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            length = os.fstat(fh.fileno()).st_size

            def read(offset: int, size: int) -> bytes:
                fh.seek(offset)
                return fh.read(size)

            need = update_exe_format.required_prefix_length(read, key, length)
            fh.seek(0)
            prefix = fh.read(need)
            if len(prefix) != need:
                raise update_exe_format.ExecutableFormatError(
                    f"short read of the validation prefix: wanted {need} bytes, got {len(prefix)}")
            update_exe_format.validate_onefile_executable(prefix, key)
    except update_exe_format.ExecutableFormatError as exc:
        raise SelfUpdateError(f"{name} is not a {key} executable: {exc}") from exc
    except struct.error as exc:  # defence in depth; planner and validator bound every unpack
        raise SelfUpdateError(
            f"{name} could not be parsed as a {key} executable: {exc}") from exc
    except OSError as exc:
        raise SelfUpdateError(f"could not inspect {name}: {exc}") from exc


def find_release(releases: Sequence[Mapping[str, Any]], tag: str) -> dict | None:
    """The release dict whose tag matches *tag* (tolerant of a ``v`` prefix/suffix via _parse)."""
    want = install._parse(tag)
    for rel in releases:
        if not isinstance(rel, Mapping):
            continue
        rel_tag = str(rel.get("tag_name") or "")
        if rel_tag == tag or install._parse(rel_tag) == want:
            return dict(rel)
    return None


# ── Network (SSRF-hardened, reuses the check layer's trusted opener) ──────────────────────────────

def _open(url: str, timeout: float):
    """Open *url* via flash_core's allowlisted opener (redirects confined to GitHub hosts)."""
    flash_core._require_allowed_url(url)
    req = urllib.request.Request(url, headers=flash_core._UA)
    return flash_core._OPENER.open(req, timeout=timeout)


def fetch_sums(assets: Sequence[Mapping[str, Any]],
               timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Download + parse the release's ``SHA256SUMS.txt`` asset. Raise if the release has none — with
    no checksums we cannot verify, and self-update fails CLOSED rather than install unverified
    bytes."""
    for a in assets:
        if str(a.get("name", "")).lower().startswith("sha256sums"):
            url = str(a.get("browser_download_url") or "")
            try:
                with _open(url, timeout) as resp:
                    return parse_sha256sums(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise SelfUpdateError(f"could not fetch SHA256SUMS.txt: {exc}") from exc
    raise SelfUpdateError("release has no SHA256SUMS.txt — refusing to self-update unverified")


def download_asset(url: str, dest: str, timeout: float = DEFAULT_TIMEOUT,
                   progress: ProgressCb | None = None) -> str:
    """Stream *url* to *dest*. On ANY failure, delete the partial file and raise SelfUpdateError,
    so a torn download can never be mistaken for a complete one."""
    try:
        with _open(url, timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except Exception as exc:  # noqa: BLE001
        _quiet_remove(dest)
        raise SelfUpdateError(f"download failed: {exc}") from exc
    return dest


# ── Apply (destructive — guarded) ─────────────────────────────────────────────────────────────────

_DETACHED = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)


def failed_update_marker(cur_exe: str | None = None) -> str:
    """Path of the breadcrumb the swap helper drops beside the exe when the swap fails. Read on the
    next launch (:func:`read_failed_update`) so a silently-failed update surfaces instead of the app
    quietly coming back on the old build."""
    exe = cur_exe if cur_exe is not None else current_exe()
    return exe + ".update-failed"


def read_failed_update(cur_exe: str | None = None) -> str | None:
    """The breadcrumb's message if a previous swap failed, else None. The app calls this at startup
    to tell the user 'the update did not apply' rather than pretending it installed."""
    try:
        with open(failed_update_marker(cur_exe), "r", encoding="ascii", errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def clear_failed_update(cur_exe: str | None = None) -> None:
    """Dismiss this executable's failure notice without deleting staged files.

    The notice has no durable record of which files an update attempt owns. A sibling's name or
    suffix is not sufficient proof, so acknowledgment must not sweep the installation directory.
    """
    exe = cur_exe if cur_exe is not None else current_exe()
    _quiet_remove(failed_update_marker(exe))


def win_swap_script(pid: int, new_exe: str, cur_exe: str) -> str:
    """The helper batch that waits for our PID to exit, swaps the new binary over the old,
    relaunches, then deletes itself. Kept pure (returns the text) so it's tested without running.

    The wait loop leans on ``tasklist | find "<pid>"``: while the PID is listed the loop sleeps ~1s
    via ``ping`` (no dependency on ``timeout.exe``, unavailable to a detached, console-less child);
    once the process is gone ``find`` fails and we fall through to the swap.

    The swap RETRIES before giving up. The just-released binary is very often still briefly locked —
    antivirus scans an executable the instant its last handle closes, and a lingering child handle can
    outlive the parent by a moment — so a single ``move`` spuriously fails and the old build sticks.
    We retry the rename ~10× at ~1s each, which clears the common transient lock. Only if every attempt
    fails (e.g. the exe dir is genuinely not writable by a non-elevated user — an app installed under
    ``Program Files``) do we drop a breadcrumb (:func:`failed_update_marker`) the next launch reads and
    leave the verified ``*.new`` in place; we still relaunch the old exe so the app comes back. A
    successful move clears any stale breadcrumb."""
    marker = failed_update_marker(cur_exe)
    # cmd.exe expands %VAR% even inside double quotes, and a literal percent must be doubled (%%).
    # '%' is a legal NTFS path char, so escape it in the interpolated PATHS — otherwise an install
    # dir like C:\Tools\100%CPU\ mangles every move/start/breadcrumb target, the swap silently dies,
    # AND the breadcrumb lands at the wrong path so read_failed_update() never surfaces it. Escape
    # ONLY the paths; the script's own %tries% / %~f0 are real cmd tokens that must stay literal.
    cur_q = cur_exe.replace("%", "%%")
    new_q = new_exe.replace("%", "%%")
    marker_q = marker.replace("%", "%%")
    return (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        "set /a tries=0\r\n"
        ":try\r\n"
        f'move /Y "{new_q}" "{cur_q}" >nul\r\n'
        "if not errorlevel 1 goto swapped\r\n"
        "set /a tries+=1\r\n"
        "if %tries% lss 10 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto try\r\n"
        ")\r\n"
        f'>"{marker_q}" echo update did not apply - could not replace the running binary. '
        f'staged update left at "{new_q}"\r\n'
        "goto relaunch\r\n"
        ":swapped\r\n"
        f'del "{marker_q}" >nul 2>nul\r\n'
        ":relaunch\r\n"
        f'start "" "{cur_q}"\r\n'
        'del "%~f0"\r\n'
    )


def _oem_encoding() -> str:
    """The console/OEM code page cmd.exe uses to interpret a .cmd file's bytes (e.g. ``'cp850'``).

    cmd.exe parses a batch file's bytes in the console's OEM code page, so the swap script — whose
    text embeds the full exe paths — must be written in that same code page for a non-ASCII path (an
    accented Windows username like ``José``) to survive intact into the ``move``/``start`` lines.
    Writing the script as ``ascii`` (the old behavior) raised ``UnicodeEncodeError`` on any such path
    and aborted the update AFTER the new binary was already downloaded, verified, and staged. Falls
    back to ``utf-8`` when the OEM code page can't be queried or Python has no codec for it (the
    helper only runs on Windows, where the query succeeds)."""
    try:
        import codecs
        import ctypes  # Windows-only; any failure falls through to the utf-8 fallback below.

        cp = int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
        codec = f"cp{cp}"
        codecs.lookup(codec)  # raises LookupError if Python has no such codec
        return codec
    except Exception:  # noqa: BLE001 — any failure => safe universal fallback
        return "utf-8"


def _apply_windows(cur_exe: str, new_exe: str, pid: int) -> None:
    """Spawn the detached swap helper and return; the CALLER then exits so the helper can swap the
    (now unlocked) binary and relaunch it.

    The script text embeds the full ``cur_exe``/``new_exe`` paths, which can contain non-ASCII
    characters (e.g. a Windows username with an accent). It is written in the console OEM code page
    (:func:`_oem_encoding`) — the code page cmd.exe reads the .cmd in — so those paths reach the
    ``move``/``start`` commands intact. If a path genuinely can't be encoded there we fail CLOSED as
    :class:`SelfUpdateError` (the module's contract) rather than leak a raw ``UnicodeEncodeError``."""
    script_text = win_swap_script(pid, new_exe, cur_exe)
    try:
        # errors='strict' so an un-encodable path surfaces as UnicodeEncodeError (caught below),
        # never silently mangled into a wrong path the swap would target.
        data = script_text.encode(_oem_encoding(), errors="strict")
    except UnicodeEncodeError as exc:
        raise SelfUpdateError(
            f"cannot encode the self-update swap script for path {cur_exe!r} in the console code "
            f"page ({exc}); update staged but not applied") from exc
    # Binary mode: the script's CRLF line endings are written verbatim (no text-mode translation).
    fd, script = tempfile.mkstemp(prefix="cc-update-", suffix=".cmd")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    subprocess.Popen(["cmd", "/c", script], close_fds=True, creationflags=_DETACHED)  # noqa: S603,S607
    log.info("self-update: swap helper spawned (%s); app should exit now", script)


def _apply_unix(cur_exe: str, new_file: str, argv: Sequence[str]) -> None:
    """Replace the binary in place (safe while running — the kernel holds the old inode) and re-exec
    the new one. Does not return on success (os.execv replaces the process image)."""
    os.chmod(new_file, 0o755)
    os.replace(new_file, cur_exe)  # same-dir staging guarantees same filesystem → atomic
    log.info("self-update: replaced %s, re-executing", cur_exe)
    os.execv(cur_exe, [cur_exe, *list(argv)[1:]])


def apply(cur_exe: str, staged: str, key: str, pid: int | None = None,
          argv: Sequence[str] | None = None) -> None:
    """Swap the verified *staged* binary into *cur_exe* and relaunch. Refuses on a non-frozen build
    so a source checkout can never clobber ``sys.executable`` (the Python interpreter)."""
    if not is_frozen():
        raise SelfUpdateError("refusing to self-update a non-frozen (source) build")
    # Defense in depth: apply() is a separate entry point (the UI calls it after staging). Only a
    # positively-identified onefile is swap-safe; refuse onedir (corrupts the install) and unknown.
    kind = installed_kind()
    if kind != "onefile":
        raise SelfUpdateError(_non_onefile_refusal(kind))
    # Read-only guard before the irreversible step: the caller owns *staged*, so a refusal
    # raises and deletes nothing.
    validate_staged_executable(staged, key)
    if key.startswith("windows"):
        _apply_windows(cur_exe, staged, pid if pid is not None else os.getpid())
    else:
        _apply_unix(cur_exe, staged, argv if argv is not None else sys.argv)


# ── Orchestration ─────────────────────────────────────────────────────────────────────────────────

def self_update(result: "updater.CheckResult", releases: list[dict] | None = None,
                timeout: float = DEFAULT_TIMEOUT, progress: ProgressCb | None = None,
                restart: bool = True) -> str:
    """Full download → verify → swap for the newest release in *result*.

    Steps, each failing CLOSED: resolve the release for ``result.latest_tag`` → pick this platform's
    asset → fetch + require its checksum → download to a sibling ``*.part`` of the running binary
    (same filesystem, so the later replace is atomic) → verify SHA-256 (delete + raise on mismatch)
    → stage as ``*.new`` → (if *restart*) apply + relaunch. Returns the staged path.
    """
    if not is_frozen():
        raise SelfUpdateError("refusing to self-update a non-frozen (source) build")
    kind = installed_kind()
    if kind != "onefile":
        # Only a positively-identified onefile is swap-safe. A onedir build must update via its
        # installer (a swap orphans _internal/ and corrupts it); an unknown layout is refused for
        # safety. Fail BEFORE any download; the UI already falls back to the release page.
        raise SelfUpdateError(_non_onefile_refusal(kind))
    tag = result.latest_tag
    if not tag:
        raise SelfUpdateError("no target release tag to update to")
    if releases is None:
        releases = updater.latest_releases(timeout)
    rel = find_release(releases, tag)
    if rel is None:
        raise SelfUpdateError(f"release {tag!r} not found")
    assets = list(rel.get("assets") or [])
    key = platform_key()
    asset = select_asset(assets, key)
    if asset is None:
        raise SelfUpdateError(f"release {tag} has no {key} binary")
    name = str(asset["name"])
    sums = fetch_sums(assets, timeout)
    expected = sums.get(name)
    if not expected:
        raise SelfUpdateError(f"no checksum published for {name}")

    cur = current_exe()
    dst_dir = os.path.dirname(cur)
    part = os.path.join(dst_dir, name + ".part")
    download_asset(str(asset["browser_download_url"]), part, timeout, progress)

    got = sha256_file(part)
    if got != expected:
        _quiet_remove(part)
        raise SelfUpdateError(
            f"checksum mismatch for {name}: got {got[:12]}…, expected {expected[:12]}…")

    # Header/architecture gate BEFORE the file becomes a staged .new. The only file this attempt
    # owns is the .part it downloaded above (owned by construction, not by its suffix), so a
    # refusal removes exactly that and stages nothing; the UI is never handed a rejected file.
    try:
        validate_staged_executable(part, key)
    except SelfUpdateError:
        _quiet_remove(part)
        raise

    staged = os.path.join(dst_dir, name + ".new")
    os.replace(part, staged)
    log.info("self-update: %s verified + staged at %s", name, staged)
    if restart:
        apply(cur, staged, key)
    return staged


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
