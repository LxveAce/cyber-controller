r"""In-app installer + resolver for the offline-crack external tools.

The crack pipeline (:mod:`src.core.crack_pipeline`) shells out to three tools: hcxpcapngtool, hashcat
and aircrack-ng. CC does not vendor these GPL binaries into the app; instead this module gives an
honest "get the tool" experience — auto-fetch where an official prebuilt binary exists, and accurate
install guidance where it doesn't — and a tools directory that :func:`crack_pipeline.detect_tools`
falls back to (so a tool installed here, hand-dropped here, or on PATH is all found the same way).

Reality (verified 2026-07-11; see command-center research):

* **aircrack-ng** — Windows: an official ``.zip`` with a vendor-published SHA1. It is a COMPLETE crack
  backend on its own (reads the ``.pcap`` directly — no converter needed), so this one auto-fetch gives
  a fully working cracker. Linux/macOS: no official prebuilt binary (package manager / source).
* **hashcat** — Windows/Linux x64: an official ``.7z`` ONLY (needs a 7-Zip extractor CC doesn't bundle),
  and its backend also needs hcxpcapngtool. Not auto-fetched — detected + guided.
* **hcxpcapngtool** — no official prebuilt binary on ANY OS. Not auto-fetched — detected + guided
  (WSL / ``apt`` / ``brew`` / compile).

Honesty (load-bearing): the auto-fetch is **self-verifying and fail-closed** — it verifies the vendor
hash, then that the expected executable extracted, then that it actually launches; a failure at any step
raises and installs nothing, never a fake success. The install path could not be exercised in the build
sandbox (no network to the vendor host, no ``.7z`` tool), so like the flash offsets it is flagged for
real-machine validation; the *runtime* self-checks are what keep it honest on a user's machine.

Structure mirrors :mod:`src.core.wordlist_manager`: the pure pieces (spec table, platform logic, dir
scan, guidance copy) are unit-testable with no network; :func:`install_tool` is the thin fetch layer.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from .crack_pipeline import AIRCRACK, CONVERTER, HASHCAT

Line = Callable[[str], None]

#: Absolute pre-verification byte ceiling for a tool download (backstop when a spec has no size).
_DOWNLOAD_HARD_CAP_BYTES = 512 * 1024**2  # 512 MiB


def platform_key() -> str:
    """Coarse OS key used to pick a spec / guidance: ``"windows"`` | ``"linux"`` | ``"macos"`` | other."""
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    return s or "unknown"


# -- tools directory + resolution (pure) ------------------------------

def default_tools_dir() -> str:
    """Where installed tool binaries live. Honors ``CC_TOOLS_DIR``; else ``~/.cyber-controller/tools``.
    User-writable (no admin), outside the app bundle so it survives reinstalls — parallel to the
    wordlist dir."""
    env = os.environ.get("CC_TOOLS_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".cyber-controller", "tools")


#: Executable basenames per crack-pipeline tool name (any of these on disk counts as that tool).
_EXE_NAMES: dict[str, tuple[str, ...]] = {
    AIRCRACK: ("aircrack-ng.exe", "aircrack-ng"),
    HASHCAT: ("hashcat.exe", "hashcat.bin", "hashcat"),
    CONVERTER: ("hcxpcapngtool.exe", "hcxpcapngtool"),
}


def installed_tools(directory: Optional[str] = None) -> dict[str, str]:
    """Scan the tools dir (one level deep — each tool installs into its own subdir) and return
    ``{crack_pipeline_tool_name: absolute_exe_path}`` for every recognized executable found. This is
    the fallback :func:`crack_pipeline.detect_tools` consults after PATH."""
    directory = directory or default_tools_dir()
    if not os.path.isdir(directory):
        return {}
    wanted = {exe: tool for tool, exes in _EXE_NAMES.items() for exe in exes}
    found: dict[str, str] = {}
    # Look both at the top level and one subdir down (aircrack installs into tools/aircrack-ng/).
    candidates: list[str] = []
    for entry in sorted(os.listdir(directory)):
        p = os.path.join(directory, entry)
        if os.path.isfile(p):
            candidates.append(p)
        elif os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp):
                    candidates.append(sp)
    for path in candidates:
        tool = wanted.get(os.path.basename(path))
        if tool and tool not in found:
            found[tool] = path
    return found


# -- install spec table (pure data) -----------------------------------

@dataclass(frozen=True)
class ToolInstallSpec:
    """One auto-fetchable tool for one OS. Extracts every archive member under ``member_prefix`` into
    ``tools/<tool>/`` (so a Cygwin-built exe keeps its sibling DLLs), then resolves ``exe_name`` there.
    ``sha1``/``sha256`` are integrity anchors (at least one should be set); ``sha1`` is the vendor value
    for aircrack-ng."""

    tool: str
    os_key: str
    version: str
    url: str
    archive: str          # "zip" (stdlib) | "7z" (needs py7zr / system 7z — not currently shipped)
    member_prefix: str    # dir inside the archive whose contents install (flattened into tools/<tool>/)
    exe_name: str
    license: str
    sha1: str = ""
    sha256: str = ""
    size_bytes: int = 0
    notes: str = ""


#: The auto-fetchable specs. Deliberately small: only where an official prebuilt binary + a real
#: integrity anchor exist. aircrack-ng/Windows is the one complete-backend auto-fetch today.
INSTALL_SPECS: tuple[ToolInstallSpec, ...] = (
    ToolInstallSpec(
        tool=AIRCRACK,
        os_key="windows",
        version="1.7",
        url="https://download.aircrack-ng.org/aircrack-ng-1.7-win.zip",
        archive="zip",
        member_prefix="aircrack-ng-1.7-win/bin/64bit/",
        exe_name="aircrack-ng.exe",
        license="GPL-2.0",
        sha1="872ef4f731080626d7cee893ef42c8f630ce90cd",  # vendor-published (cross-verified in research)
        notes="Official Windows build. A complete crack backend on its own (reads the .pcap directly).",
    ),
)


def spec_for(tool: str, os_key: Optional[str] = None) -> Optional[ToolInstallSpec]:
    """The auto-install spec for *tool* on this OS (default the current platform), or None."""
    os_key = os_key or platform_key()
    return next((s for s in INSTALL_SPECS if s.tool == tool and s.os_key == os_key), None)


def installable_tools(os_key: Optional[str] = None) -> list[str]:
    """Tool names that can be auto-fetched on this OS (may be empty, e.g. on macOS)."""
    os_key = os_key or platform_key()
    return [s.tool for s in INSTALL_SPECS if s.os_key == os_key]


# -- guidance copy for the non-auto-fetch cases (pure) ----------------

def guidance_for(tool: str, os_key: Optional[str] = None) -> str:
    """Honest 'how to get this tool here' text when it can't be auto-fetched on this OS."""
    os_key = os_key or platform_key()
    if tool == AIRCRACK:
        if os_key == "windows":
            return "Auto-install available (official build)."
        if os_key == "linux":
            return "No prebuilt binary — install via your package manager: `sudo apt install aircrack-ng`."
        if os_key == "macos":
            return "No prebuilt binary — `brew install aircrack-ng`."
        return "Install aircrack-ng from your package manager."
    if tool == HASHCAT:
        if os_key in ("windows", "linux"):
            return ("hashcat ships only as a .7z from hashcat.net (needs 7-Zip to unpack) and its crack "
                    "path also needs hcxpcapngtool. Download hashcat-7.x.7z from hashcat.net, unpack it, "
                    "and put hashcat on your PATH — CC will then use it.")
        return "hashcat has no macOS binary — `brew install hashcat` or build from source."
    if tool == CONVERTER:
        if os_key == "windows":
            return ("hcxpcapngtool (hcxtools) has no official Windows binary. Use WSL (`sudo apt install "
                    "hcxtools`), or aircrack-ng instead — it needs no converter.")
        if os_key == "linux":
            return "Install hcxtools: `sudo apt install hcxtools` (or build from github.com/ZerBea/hcxtools)."
        if os_key == "macos":
            return "Install hcxtools: `brew install hcxtools`."
        return "Install hcxtools (github.com/ZerBea/hcxtools) — source only, no official binary."
    return ""


# -- install (thin, best-effort, self-verifying, fail-closed) ---------

def _sha1_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_archive(path: str, spec: ToolInstallSpec) -> tuple[bool, str]:
    """Check a downloaded archive against the spec's integrity anchor. Prefers SHA-256, then SHA-1
    (aircrack's vendor value), then size. Returns (ok, message). Never fabricates a pass."""
    if spec.sha256:
        got = _sha256_file(path)
        if got.lower() != spec.sha256.lower():
            return (False, f"SHA-256 mismatch (got {got[:12]}…, expected {spec.sha256[:12]}…)")
        return (True, "SHA-256 verified")
    if spec.sha1:
        got = _sha1_file(path)
        if got.lower() != spec.sha1.lower():
            return (False, f"SHA-1 mismatch (got {got[:12]}…, expected {spec.sha1[:12]}…)")
        return (True, "SHA-1 verified (vendor-published)")
    if spec.size_bytes:
        actual = os.path.getsize(path)
        if actual != spec.size_bytes:
            return (False, f"size mismatch (got {actual}, expected {spec.size_bytes})")
        return (True, "size verified — integrity NOT hash-pinned")
    return (False, "no integrity anchor on this spec — refusing to install unverified")


def install_tool(spec: ToolInstallSpec, directory: Optional[str] = None,
                 on_line: Optional[Line] = None, *, timeout: float = 180.0) -> str:
    """Download + verify + extract *spec* into ``tools/<tool>/`` and return the resolved exe path.

    Self-verifying + fail-closed: download to a temp file, verify the integrity anchor, extract only the
    members under ``member_prefix``, confirm the expected exe landed, and finally probe that it launches
    — any failure raises RuntimeError and leaves nothing half-installed. Only ``.zip`` is supported here
    (stdlib); a ``.7z`` spec raises a clear 'needs a 7-Zip extractor' error rather than pretending."""
    log: Line = on_line or (lambda *_a: None)
    directory = directory or default_tools_dir()
    if spec.archive != "zip":
        raise RuntimeError(
            f"{spec.tool}: only .zip auto-install is supported; {spec.archive} needs a 7-Zip extractor "
            "CC doesn't bundle — see the install guidance instead.")

    tool_dir = os.path.join(directory, spec.tool)
    os.makedirs(tool_dir, exist_ok=True)
    log(f"[install] downloading {spec.tool} {spec.version} from {spec.url}")

    tmp = tempfile.NamedTemporaryFile(prefix="cc-tool-", suffix=".zip", dir=directory, delete=False)
    tmp.close()
    req = urllib.request.Request(spec.url, headers={"User-Agent": "cyber-controller-tool-installer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp.name, "wb") as out:
            _stream_capped(resp, out, spec.size_bytes)
    except Exception as exc:  # noqa: BLE001 — surface any network error honestly
        _rm(tmp.name)
        raise RuntimeError(f"download failed: {exc}") from exc

    ok, msg = verify_archive(tmp.name, spec)
    if not ok:
        _rm(tmp.name)
        raise RuntimeError(f"integrity check failed, not installing: {msg}")
    log(f"[install] {msg}")

    try:
        try:
            exe_path = _extract_zip_subtree(tmp.name, spec, tool_dir, log)
        finally:
            _rm(tmp.name)

        if not os.path.isfile(exe_path):
            raise RuntimeError(f"expected {spec.exe_name} not found after extract (archive layout changed?)")
        if os.name != "nt":
            os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if not _launches(exe_path):
            raise RuntimeError(
                f"{spec.tool} installed to {exe_path} but the binary would not launch (it may need system "
                "libraries). Install it another way — nothing was left on PATH.")
    except Exception:
        # Fail clean: an extract that raised, wrote the wrong layout, or produced a non-launching binary must
        # not leave a partial tree behind for installed_tools()/detect_tools() to later resolve as usable.
        shutil.rmtree(tool_dir, ignore_errors=True)
        raise
    log(f"[install] {spec.tool} ready: {exe_path}")
    return exe_path


def _extract_zip_subtree(zip_path: str, spec: ToolInstallSpec, tool_dir: str, log: Line) -> str:
    """Extract every member under ``spec.member_prefix`` into *tool_dir* (flattened), guarding against
    zip-slip. Returns the resolved exe path."""
    prefix = spec.member_prefix
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
        if not members:
            raise RuntimeError(f"archive has no members under {prefix!r} (layout changed?)")
        for name in members:
            rel = name[len(prefix):]
            dest = os.path.join(tool_dir, rel)
            # zip-slip guard: the resolved path must stay inside tool_dir.
            if not os.path.realpath(dest).startswith(os.path.realpath(tool_dir) + os.sep):
                raise RuntimeError(f"unsafe archive member path: {name!r}")
            os.makedirs(os.path.dirname(dest) or tool_dir, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    log(f"[install] extracted {len(members)} file(s) into {tool_dir}")
    return os.path.join(tool_dir, spec.exe_name)


def _launches(exe_path: str) -> bool:
    """True if the binary runs at all (best-effort). aircrack-ng has no --version; --help prints the
    banner and exits non-zero, so we only require that it EXECUTES without an OS-level failure."""
    try:
        subprocess.run([exe_path, "--help"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _stream_capped(resp, out, size_bytes: int) -> None:
    """Copy the response body in chunks, aborting before an oversized upstream can fill the disk."""
    limit = int(size_bytes * 1.25) if size_bytes else _DOWNLOAD_HARD_CAP_BYTES
    written = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise RuntimeError("response exceeded the size ceiling — aborting to protect disk")
        out.write(chunk)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# -- status summary (pure-ish: reads PATH + the tools dir) ------------

@dataclass
class ToolAvailability:
    """A UI-facing status row for one tool: is it present, can we auto-install it, and the guidance."""

    tool: str
    present: bool = False
    source: str = ""            # "PATH" | "installed" | ""
    can_autofetch: bool = False
    guidance: str = ""
    extra: dict = field(default_factory=dict)


def tool_availability(os_key: Optional[str] = None) -> list[ToolAvailability]:
    """One :class:`ToolAvailability` per crack tool: whether it's on PATH or in the tools dir, whether
    CC can auto-fetch it here, and the install guidance otherwise. Drives the 'Get tools' panel."""
    os_key = os_key or platform_key()
    inst = installed_tools()
    rows: list[ToolAvailability] = []
    for tool in (CONVERTER, HASHCAT, AIRCRACK):
        on_path = shutil.which(tool)
        present = bool(on_path) or tool in inst
        source = "PATH" if on_path else ("installed" if tool in inst else "")
        rows.append(ToolAvailability(
            tool=tool, present=present, source=source,
            can_autofetch=(not present) and spec_for(tool, os_key) is not None,
            guidance=guidance_for(tool, os_key)))
    return rows
