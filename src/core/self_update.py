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

import glob
import hashlib
import logging
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from src.core import flash_core, install, updater

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


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """Canonical asset key for the current (or given) platform — matches the release asset naming
    (``cyber-controller-<tag>-<key>``). Parameterized so the mapping is table-testable.

    We only publish x64 Windows and arm64 macOS, so those collapse to a single key regardless of the
    reported machine; Linux splits x64 vs arm64.
    """
    system = (system if system is not None else platform.system()).lower()
    machine = (machine if machine is not None else platform.machine()).lower()
    if system.startswith("win"):
        return "windows-x64"
    if system == "darwin":
        return "macos-arm64"
    if system == "linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    raise SelfUpdateError(f"unsupported platform for self-update: {system}/{machine}")


# ── Pure selection + verification ────────────────────────────────────────────────────────────────

def select_asset(assets: Sequence[Mapping[str, Any]], key: str) -> dict | None:
    """Pick the onefile release binary for *key*. Skips the Windows setup installer (self-update
    swaps the standalone binary, not the installer) and the checksums file. Returns the raw asset
    dict (``name`` + ``browser_download_url``) or None if the platform isn't in this release."""
    want_exe = key.startswith("windows")
    for a in assets:
        name = str(a.get("name", ""))
        low = name.lower()
        if "setup" in low or low.startswith("sha256sums"):
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
    """Dismiss the breadcrumb and sweep the orphaned ``*.new`` staged binaries left beside the exe by
    a failed swap, so a reported failure can be acknowledged and the leftovers don't linger forever."""
    exe = cur_exe if cur_exe is not None else current_exe()
    _quiet_remove(failed_update_marker(exe))
    for orphan in glob.glob(os.path.join(os.path.dirname(exe), "*.new")):
        _quiet_remove(orphan)


def win_swap_script(pid: int, new_exe: str, cur_exe: str) -> str:
    """The helper batch that waits for our PID to exit, swaps the new binary over the old,
    relaunches, then deletes itself. Kept pure (returns the text) so it's tested without running.

    The wait loop leans on ``tasklist | find "<pid>"``: while the PID is listed the loop sleeps ~1s
    via ``ping`` (no dependency on ``timeout.exe``, unavailable to a detached, console-less child);
    once the process is gone ``find`` fails and we fall through to the swap.

    The swap is gated on ``move``'s ``errorlevel``: if the rename fails (exe dir not writable by a
    non-elevated user, an AV/lock on the just-released binary), we DO NOT pretend the update took —
    we drop a breadcrumb (:func:`failed_update_marker`) the next launch reads and leave the verified
    ``*.new`` in place, then still relaunch the old exe so the app comes back. Only a successful move
    clears any stale breadcrumb."""
    marker = failed_update_marker(cur_exe)
    return (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'move /Y "{new_exe}" "{cur_exe}" >nul\r\n'
        "if errorlevel 1 (\r\n"
        f'  >"{marker}" echo update did not apply - could not replace the running binary. '
        f'staged update left at "{new_exe}"\r\n'
        ") else (\r\n"
        f'  del "{marker}" >nul 2>nul\r\n'
        ")\r\n"
        f'start "" "{cur_exe}"\r\n'
        'del "%~f0"\r\n'
    )


def _apply_windows(cur_exe: str, new_exe: str, pid: int) -> None:
    """Spawn the detached swap helper and return; the CALLER then exits so the helper can swap the
    (now unlocked) binary and relaunch it."""
    fd, script = tempfile.mkstemp(prefix="cc-update-", suffix=".cmd")
    with os.fdopen(fd, "w", encoding="ascii", newline="") as fh:
        fh.write(win_swap_script(pid, new_exe, cur_exe))
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
