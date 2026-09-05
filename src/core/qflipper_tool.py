r"""Locate + provision qFlipper, and drive it for Flipper Zero flashing & control.

CC does not vendor qFlipper (a ~65 MB Qt GUI app) into its installer — that would bloat every
download for the minority who flash a Flipper. Instead this module makes qFlipper "prepackaged"
the way it matters to the user: CC finds an already-installed qFlipper, and if none exists it
fetches the official portable build on demand into ``~/.cyber-controller/tools/qflipper/``,
verifies the vendor SHA-256, and drives the headless ``qFlipper-cli`` from there. So the user
never has to go install a second app and point CC at it — CC provisions it.

Verified ``qFlipper-cli`` (1.3.3) interface::

    firmware <file.dfu>          flash Core1 firmware from a raw DFU
    core2radio <bin>             flash Core2 radio stack
    core2fus <bin> <addr>        flash Core2 Firmware Update Service
    backup <dir> / restore <dir> internal storage backup / restore
    erase                        erase internal storage
    wipe                         wipe entire MCU flash
    (no subcommand)              Update/Repair to the latest official firmware on --update-channel
    -c, --update-channel <ch>    release | release-candidate | development
    -d, --debug-level <0|1|2>    log verbosity

Custom firmwares (Momentum / Unleashed / RogueMaster) ship as ``.tgz`` web-update bundles holding
``firmware.dfu`` + ``radio.bin`` + ``resources.ths`` + ``update.fuf`` + ``updater.bin``.
``qFlipper-cli firmware`` installs the Core1 ``firmware.dfu`` headlessly; the SD ``resources.ths``
(SubGHz / IR / NFC databases, apps) are applied by the on-device updater or the qFlipper GUI, NOT
the CLI. So a CLI custom-firmware flash is **core-firmware-only** and the flash path says exactly
that rather than pretending the resource set installed too.

Honesty (load-bearing): the auto-fetch is self-verifying + fail-closed — vendor SHA-256, then the
expected ``qFlipper-cli`` extracted, then it actually launches; a failure at any step raises and
installs nothing. The flash-write argv is grounded in the real ``--help`` above; on-glass flashing
of a Flipper is HW-validation-pending like the other external-tool backends.

Pure pieces (dir scan, argv builders, bundle inspection) are unit-testable with no network;
:func:`provision_qflipper` is the thin fetch layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

Line = Callable[[str], None]

#: The Flipper update server index. Lists every qFlipper channel + per-target download + SHA-256.
_DIRECTORY_URL = "https://update.flipperzero.one/qFlipper/directory.json"

#: Pinned fallback (release 1.3.3, windows/amd64 portable) used only if the directory index can't be
#: reached or parsed — the primary path reads the live index so CC tracks the current official build.
_FALLBACK_WIN_PORTABLE = (
    "https://update.flipperzero.one/builds/qFlipper/1.3.3/qFlipper-64bit-1.3.3.zip",
    "2dd15a98dc516eabb3d3ca711dd57c19df54ae5520247cfd362546188ad42809",
    "1.3.3",
)

#: Pre-verification byte ceiling for the portable download (the 1.3.3 zip is ~65 MB).
_DOWNLOAD_HARD_CAP_BYTES = 256 * 1024**2  # 256 MiB


# -- tools directory + resolution (pure) ------------------------------

def default_qflipper_dir() -> str:
    """Where a CC-provisioned qFlipper lives. Honors ``CC_QFLIPPER_DIR``; else
    ``~/.cyber-controller/tools/qflipper`` (parallel to the crack-tools dir; user-writable, no admin,
    survives app reinstalls)."""
    env = os.environ.get("CC_QFLIPPER_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".cyber-controller", "tools", "qflipper")


#: CLI first (headless — no GUI popup on a flash), GUI second (fallback for the file-picker install).
_CLI_NAMES = ("qFlipper-cli.exe", "qFlipper-cli", "qflipper-cli")
_GUI_NAMES = ("qFlipper.exe", "qFlipper", "qflipper")

_STD_INSTALL_DIRS = (
    r"C:\Program Files\qFlipper",
    r"C:\Program Files (x86)\qFlipper",
    "/usr/bin",
    "/usr/local/bin",
    "/Applications/qFlipper.app/Contents/MacOS",
)


@dataclass(frozen=True)
class QFlipperTools:
    """Resolved qFlipper executables. ``cli`` drives headless flashing/control; ``gui`` is the
    fallback for a full custom-firmware install (Install-from-file, which applies SD resources)."""

    cli: Optional[str]
    gui: Optional[str]
    source: str  # "env" | "provisioned" | "PATH" | "installed"

    @property
    def present(self) -> bool:
        return bool(self.cli or self.gui)


def _first_existing(directory: str, names: tuple[str, ...]) -> Optional[str]:
    """First of *names* that exists directly in *directory* or one subdir down (a provisioned
    portable unzips into ``qflipper/<version>/``). Returns an absolute path or None."""
    if not os.path.isdir(directory):
        return None
    for name in names:
        direct = os.path.join(directory, name)
        if os.path.isfile(direct):
            return direct
    for entry in sorted(os.listdir(directory)):
        if entry.startswith("."):
            continue  # skip in-progress staging dirs (.stage-*) and backups (.old-*)
        sub = os.path.join(directory, entry)
        if os.path.isdir(sub):
            for name in names:
                p = os.path.join(sub, name)
                if os.path.isfile(p):
                    return p
    return None


def find_qflipper(directory: Optional[str] = None) -> QFlipperTools:
    """Locate qFlipper without touching the network. Search order, most-specific first:
    ``CC_QFLIPPER`` (an explicit exe) → CC's provisioned tools dir → PATH → standard install dirs.
    Always prefers ``qFlipper-cli`` (headless) but also records the GUI path when found."""
    env = os.environ.get("CC_QFLIPPER")
    if env and os.path.isfile(env):
        base = os.path.basename(env).lower()
        if "cli" in base:
            return QFlipperTools(cli=env, gui=None, source="env")
        return QFlipperTools(cli=None, gui=env, source="env")

    directory = directory or default_qflipper_dir()
    cli = _first_existing(directory, _CLI_NAMES)
    gui = _first_existing(directory, _GUI_NAMES)
    if cli or gui:
        return QFlipperTools(cli=cli, gui=gui, source="provisioned")

    path_cli = next((shutil.which(n) for n in _CLI_NAMES if shutil.which(n)), None)
    path_gui = next((shutil.which(n) for n in _GUI_NAMES if shutil.which(n)), None)
    if path_cli or path_gui:
        return QFlipperTools(cli=path_cli, gui=path_gui, source="PATH")

    for d in _STD_INSTALL_DIRS:
        c = _first_existing(d, _CLI_NAMES)
        g = _first_existing(d, _GUI_NAMES)
        if c or g:
            return QFlipperTools(cli=c, gui=g, source="installed")
    return QFlipperTools(cli=None, gui=None, source="")


# -- argv builders (pure — grounded in qFlipper-cli --help) -----------

def build_firmware_argv(cli: str, dfu_path: str, debug: int = 1) -> list[str]:
    """``qFlipper-cli firmware <dfu>`` — flash a raw Core1 DFU headlessly."""
    return [cli, "-d", str(debug), "firmware", dfu_path]


def build_official_update_argv(cli: str, channel: str = "release", debug: int = 1) -> list[str]:
    """Argless Update/Repair to the latest OFFICIAL firmware on *channel* (release / release-candidate
    / development). No file needed — qFlipper-cli fetches it from the Flipper update server."""
    return [cli, "-d", str(debug), "--update-channel", channel]


def build_backup_argv(cli: str, backup_dir: str, debug: int = 1) -> list[str]:
    return [cli, "-d", str(debug), "backup", backup_dir]


def build_restore_argv(cli: str, backup_dir: str, debug: int = 1) -> list[str]:
    return [cli, "-d", str(debug), "restore", backup_dir]


def build_erase_argv(cli: str, debug: int = 1) -> list[str]:
    """Erase internal storage (keeps firmware)."""
    return [cli, "-d", str(debug), "erase"]


def build_wipe_argv(cli: str, debug: int = 1) -> list[str]:
    """Wipe the entire MCU flash — destructive; leaves the Flipper needing a firmware reinstall."""
    return [cli, "-d", str(debug), "wipe"]


#: qFlipper-cli control operations CC can offer, with a one-line meaning + whether it's destructive.
CONTROL_OPS: dict[str, dict] = {
    "update": {"label": "Update/Repair (official firmware)", "destructive": False,
               "help": "Install the latest official Flipper firmware from the update server."},
    "backup": {"label": "Backup internal storage", "destructive": False,
               "help": "Save the Flipper's internal memory to a folder on this PC."},
    "restore": {"label": "Restore internal storage", "destructive": False,
                "help": "Restore a previously saved internal-memory backup."},
    "erase": {"label": "Erase internal storage", "destructive": True,
              "help": "Erase internal storage (keeps firmware). Back up first."},
    "wipe": {"label": "Wipe MCU flash", "destructive": True,
             "help": "Wipe the entire MCU flash. The Flipper will need a firmware reinstall after."},
}


# -- custom-firmware bundle inspection (pure) -------------------------

#: Member basename of the Core1 firmware inside a Flipper web-update .tgz bundle.
_BUNDLE_FIRMWARE = "firmware.dfu"


def extract_firmware_dfu(tgz_path: str, dest_dir: str) -> str:
    """Extract ``firmware.dfu`` from a Flipper web-update ``.tgz`` bundle into *dest_dir* and return
    its path. Guards against path traversal. Raises RuntimeError if the bundle has no firmware.dfu
    (e.g. a scripts/SDK archive was passed instead of a firmware update package)."""
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(tgz_path) as tar:
        member = next(
            (m for m in tar.getmembers()
             if m.isfile() and os.path.basename(m.name) == _BUNDLE_FIRMWARE), None)
        if member is None:
            names = ", ".join(os.path.basename(m.name) for m in tar.getmembers() if m.isfile())
            raise RuntimeError(
                f"no {_BUNDLE_FIRMWARE} in the update bundle (found: {names or 'nothing'}). "
                "This is not a Flipper firmware-update package.")
        dest = os.path.join(dest_dir, _BUNDLE_FIRMWARE)
        # Path-traversal guard: stream the member out ourselves to a fixed name inside dest_dir.
        if not os.path.realpath(dest).startswith(os.path.realpath(dest_dir) + os.sep):
            raise RuntimeError("unsafe extract path")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"could not read {_BUNDLE_FIRMWARE} from the bundle")
        with extracted, open(dest, "wb") as out:
            shutil.copyfileobj(extracted, out)
    return dest


def bundle_has_resources(tgz_path: str) -> bool:
    """True if the bundle carries an SD ``resources.*`` blob the CLI cannot install (so the flash path
    can warn that a CLI install is core-firmware-only)."""
    try:
        with tarfile.open(tgz_path) as tar:
            return any(os.path.basename(m.name).startswith("resources.")
                       for m in tar.getmembers() if m.isfile())
    except (tarfile.TarError, OSError):
        return False


# -- provisioning (thin, self-verifying, fail-closed) -----------------

def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _windows_portable_from_index(data: dict) -> Optional[tuple[str, str, str]]:
    """Pull (url, sha256, version) for the release-channel windows/amd64 portable build from a parsed
    directory.json. Returns None if the shape isn't what we expect."""
    for ch in data.get("channels", []):
        if ch.get("id") != "release" or not ch.get("versions"):
            continue
        ver = ch["versions"][0]
        for f in ver.get("files", []):
            if f.get("target") == "windows/amd64" and f.get("type") == "portable":
                if f.get("url") and f.get("sha256"):
                    return (f["url"], f["sha256"], str(ver.get("version", "")))
    return None


def _resolve_portable(on_line: Line, *, timeout: float) -> tuple[str, str, str]:
    """Resolve the current release windows portable (url, sha256, version) from the live index, or
    fall back to the pinned build if the index is unreachable/misshaped."""
    try:
        req = urllib.request.Request(
            _DIRECTORY_URL, headers={"User-Agent": "cyber-controller-qflipper-provision"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        found = _windows_portable_from_index(data)
        if found:
            return found
        on_line("[qflipper] update index had no release/windows/portable entry — using pinned build")
    except Exception as exc:  # noqa: BLE001 — offline / changed index is non-fatal; pinned fallback
        on_line(f"[qflipper] could not read the update index ({exc}) — using pinned build")
    return _FALLBACK_WIN_PORTABLE


def _stream_capped(resp, out) -> None:
    written = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        written += len(chunk)
        if written > _DOWNLOAD_HARD_CAP_BYTES:
            raise RuntimeError("qFlipper download exceeded the size ceiling — aborting")
        out.write(chunk)


def _launches(cli: str) -> bool:
    try:
        subprocess.run([cli, "--version"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def provision_qflipper(on_line: Optional[Line] = None, *, directory: Optional[str] = None,
                       timeout: float = 240.0) -> str:
    """Download + verify + extract the official qFlipper portable build into the tools dir and return
    the resolved ``qFlipper-cli`` path. Windows-only today (the portable bundle CC provisions is the
    windows/amd64 zip); on Linux/macOS this raises with install guidance instead of pretending.

    Self-verifying + fail-closed: vendor SHA-256 → the CLI extracted → it launches; any failure raises
    and removes the partial tree. Only call this after the user consented to the download."""
    log: Line = on_line or (lambda *_a: None)
    if os.name != "nt":
        raise RuntimeError(
            "CC auto-provisions qFlipper only on Windows (the portable bundle is windows/amd64). "
            "Install qFlipper from https://flipperzero.one/update and CC will find it on PATH.")

    directory = directory or default_qflipper_dir()
    os.makedirs(directory, exist_ok=True)
    url, sha256, version = _resolve_portable(log, timeout=timeout)
    dest = os.path.join(directory, version or "release")
    log(f"[qflipper] downloading qFlipper {version or ''} portable…")

    # Everything happens in a private staging dir; the existing install at `dest` is only replaced
    # AFTER the download verifies, extracts, and the CLI launches — so a failed re-provision (network
    # drop, bad hash, changed layout) can never delete a working qFlipper.
    staging = tempfile.mkdtemp(prefix=".stage-qflipper-", dir=directory)
    tmp = tempfile.NamedTemporaryFile(prefix="cc-qflipper-", suffix=".zip", dir=directory, delete=False)
    tmp.close()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cyber-controller-qflipper-provision"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp.name, "wb") as out:
            _stream_capped(resp, out)
        got = _sha256_file(tmp.name)
        if got.lower() != sha256.lower():
            raise RuntimeError(f"SHA-256 mismatch (got {got[:12]}…, expected {sha256[:12]}…)")
        log("[qflipper] SHA-256 verified")
        with zipfile.ZipFile(tmp.name) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                target = os.path.join(staging, name)
                if not os.path.realpath(target).startswith(os.path.realpath(staging) + os.sep):
                    raise RuntimeError(f"unsafe archive member: {name!r}")
                os.makedirs(os.path.dirname(target) or staging, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        _rm(tmp.name)
        staged_cli = _first_existing(staging, _CLI_NAMES)
        if not staged_cli:
            raise RuntimeError("qFlipper-cli not found after extract (bundle layout changed?)")
        if not _launches(staged_cli):
            raise RuntimeError(
                "qFlipper-cli extracted but would not launch (missing system libraries?). "
                "Install qFlipper from https://flipperzero.one/update instead.")
        _atomic_promote(staging, dest)   # validated → swap in; the old install survived until now
        staging = None
    finally:
        _rm(tmp.name)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)   # nothing promoted → old install untouched

    cli = _first_existing(dest, _CLI_NAMES)
    if not cli:  # defensive — promote just placed a validated tree here
        raise RuntimeError("qFlipper-cli not found after install")
    log(f"[qflipper] ready: {cli}")
    return cli


def _atomic_promote(staging: str, dest: str) -> None:
    """Replace *dest* with the validated *staging* tree, keeping the old *dest* until the swap
    succeeds. Windows can't ``os.replace`` onto a non-empty dir, so: move any existing dest aside,
    move staging into place, then drop the backup — and restore the backup if the swap fails, so a
    failure never leaves the tool with no install."""
    backup = None
    if os.path.exists(dest):
        backup = dest + ".old-" + os.urandom(4).hex()
        os.replace(dest, backup)
    try:
        os.replace(staging, dest)
    except OSError:
        if backup is not None:
            os.replace(backup, dest)   # restore the previous install
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# -- high-level flash entry point -------------------------------------

def _default_runner(argv: list[str], on_line: Line) -> int:
    """Minimal streaming subprocess runner used when the caller doesn't inject one. The engine passes
    ``flash_core._run_stream`` instead (it has stdin/kill/reap handling); this keeps the module usable
    standalone/in tests without importing flash_core."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as exc:
        on_line(f"[error] could not launch qFlipper-cli: {exc}")
        return 1
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\r\n"))
    return proc.wait()


def flash_bundle(path: str, on_line: Line, *, allow_provision: bool = False,
                 runner: Optional[Callable[[list[str], Line], int]] = None) -> int:
    """Flash *path* (a Flipper web-update ``.tgz`` bundle or a raw ``.dfu``) to a connected Flipper via
    headless ``qFlipper-cli``. Returns 0 on success, non-zero otherwise.

    * Locates qFlipper (:func:`find_qflipper`); if no CLI is present and *allow_provision* is set, it
      downloads the official build first (consent is the caller's job — pass ``allow_provision`` only
      after the user agreed).
    * A ``.dfu`` is flashed directly. A ``.tgz`` bundle has its ``firmware.dfu`` extracted and flashed;
      if the bundle also carries SD ``resources.*`` those are **not** installed by the CLI, so the log
      says the install is core-firmware-only and points at the GUI / on-device updater for a full
      install with resources. It never claims the resource set was applied.

    Never reports success on a no-op: a missing CLI, a bundle with no firmware.dfu, or a non-zero
    qFlipper-cli exit all return failure with a clear message.
    """
    run = runner or _default_runner
    tools = find_qflipper()
    cli = tools.cli
    if not cli and allow_provision:
        try:
            cli = provision_qflipper(on_line)
        except Exception as exc:  # noqa: BLE001 — surface the provisioning failure, don't fake a flash
            on_line(f"[error] could not provision qFlipper: {exc}")
            return 1
    if not cli:
        on_line("[error] qFlipper-cli not found. CC can install it for you (Flipper tab → Get "
                "qFlipper), or install qFlipper from https://flipperzero.one/update.")
        if tools.gui:
            on_line(f"[info] A qFlipper GUI is installed at {tools.gui} — you can open it and use "
                    "'Install from file' on the downloaded package for a full install with resources.")
        return 1

    lower = path.lower()
    if lower.endswith(".dfu"):
        dfu = path
        cleanup = None
    elif lower.endswith((".tgz", ".tar.gz")):
        try:
            tmpdir = tempfile.mkdtemp(prefix="cc-flipper-fw-")
            dfu = extract_firmware_dfu(path, tmpdir)
            cleanup = tmpdir
        except Exception as exc:  # noqa: BLE001
            on_line(f"[error] {exc}")
            return 1
        if bundle_has_resources(path):
            on_line("[info] Installing the Core firmware headlessly (qFlipper-cli). SD resources "
                    "(SubGHz/IR/NFC databases, apps) are NOT installed this way. For a full install "
                    "with resources, use the qFlipper app's 'Install from file' on the package, or "
                    "the on-device updater.")
    else:
        on_line(f"[error] not a Flipper firmware package (.tgz) or DFU: {os.path.basename(path)}")
        return 1

    on_line(f"[qflipper] using {cli}")
    try:
        rc = run(build_firmware_argv(cli, dfu), on_line)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if rc != 0:
        on_line(f"[error] qFlipper-cli exited {rc} — firmware not flashed")
    return rc
