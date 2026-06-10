"""
ADB backend -- firmware installation for ADB-based devices (RayHunter on Orbic RC400L, etc.).

Uses subprocess for ADB commands and urllib for GitHub downloads (SSRF-hardened, matching
flasher.py). Provides device detection, firmware install/uninstall, port forwarding, and
status checks through a profile-driven registry so new ADB-based firmwares can be added
without touching the plumbing.

Key facts (RayHunter on Orbic RC400L):
  * The Orbic RC400L is a Qualcomm-based mobile hotspot (USB vendor 0x05c6).
  * RayHunter installs via ADB: push the daemon binary + config + init scripts,
    then reboot. The release zip is platform-specific (contains the installer binary
    + the daemon binary + support files pre-built for the target arch).
  * After install, the daemon listens on port 8080. Reachable via WiFi at
    192.168.1.1:8080 or via `adb forward tcp:8080 tcp:8080` at localhost:8080.
  * /api/analysis is the primary health/status endpoint.
  * A deactivated SIM must be inserted for the device to boot its radio stack.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

Line = Callable[[str], None]

_UA = {"User-Agent": "universal-flasher"}

# ---------------------------------------------------------------------------
# SSRF / download hardening (mirrors flasher.py)
# ---------------------------------------------------------------------------

_ALLOWED_HOSTS = frozenset((
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
))
_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"


def _host_allowed(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.lower().split("@")[-1].split(":")[0]
    return h in _ALLOWED_HOSTS or h.endswith(_ALLOWED_HOST_SUFFIX)


def _require_allowed_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("refusing empty/invalid download URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"refusing non-https URL scheme {parts.scheme!r}: {url!r}")
    if not _host_allowed(parts.hostname):
        raise ValueError(f"refusing URL to non-allowlisted host {parts.hostname!r}: {url!r}")
    return url


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme.lower() != "https" or not _host_allowed(parts.hostname):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing redirect to non-allowlisted location: {newurl!r}",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirectHandler())


def _http_get(url: str) -> bytes:
    _require_allowed_url(url)
    req = urllib.request.Request(url, headers=_UA)
    with _OPENER.open(req, timeout=60) as r:
        return r.read()


def _safe_filename(name: str) -> str:
    if not isinstance(name, str) or name in ("", ".", ".."):
        raise ValueError(f"refusing unsafe file name: {name!r}")
    if os.path.basename(name) != name:
        raise ValueError(f"refusing non-basename file name: {name!r}")
    if os.path.isabs(name):
        raise ValueError(f"refusing absolute file name: {name!r}")
    drive, _ = os.path.splitdrive(name)
    if drive:
        raise ValueError(f"refusing file name with drive/UNC prefix: {name!r}")
    norm = name.replace(chr(92), "/")
    if ".." in norm.split("/") or "/" in norm:
        raise ValueError(f"refusing file name with path separator/'..': {name!r}")
    return name


# ---------------------------------------------------------------------------
# ADB plumbing
# ---------------------------------------------------------------------------

def find_adb() -> Optional[str]:
    path = shutil.which("adb")
    if path:
        return path
    # common install locations
    candidates: List[str] = []
    if sys.platform == "win32":
        for env_key in ("LOCALAPPDATA", "USERPROFILE"):
            base = os.environ.get(env_key, "")
            if base:
                candidates.append(os.path.join(base, "Android", "Sdk", "platform-tools", "adb.exe"))
        candidates.append(r"C:\platform-tools\adb.exe")
    elif sys.platform == "darwin":
        candidates.append(os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))
    else:
        candidates.append(os.path.expanduser("~/Android/Sdk/platform-tools/adb"))
        candidates.append("/usr/local/bin/adb")
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def adb_available() -> bool:
    return find_adb() is not None


def _adb_argv(*args: str) -> List[str]:
    adb = find_adb()
    if not adb:
        raise FileNotFoundError("adb not found on PATH or in known locations")
    return [adb, *args]


def _run_adb(args: List[str], on_line: Line, timeout: int = 120) -> Tuple[int, str]:
    on_line("$ " + " ".join(args))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError as e:
        on_line(f"[error] {e}")
        return 127, ""
    lines: List[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip("\n")
            lines.append(stripped)
            on_line(stripped)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        on_line("[error] adb command timed out")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1, "\n".join(lines)
    except Exception as e:
        on_line(f"[error] {e}")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1, "\n".join(lines)
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
    on_line(f"[exit {proc.returncode}]")
    return proc.returncode, "\n".join(lines)


def _run_adb_quiet(args: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception:
        return -1, ""


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

def list_devices(on_line: Line) -> List[Dict[str, str]]:
    """Return a list of dicts with 'serial' and 'state' for each connected ADB device."""
    try:
        argv = _adb_argv("devices")
    except FileNotFoundError:
        on_line("[error] adb not found")
        return []
    rc, output = _run_adb(argv, on_line)
    if rc != 0:
        return []
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def wait_for_device(on_line: Line, serial: Optional[str] = None, timeout: int = 60) -> bool:
    try:
        args = _adb_argv()
    except FileNotFoundError:
        on_line("[error] adb not found")
        return False
    if serial:
        args += ["-s", serial]
    args += ["wait-for-device"]
    on_line("[adb] waiting for device...")
    rc, _ = _run_adb(args, on_line, timeout=timeout)
    return rc == 0


# ---------------------------------------------------------------------------
# ADB operations
# ---------------------------------------------------------------------------

def adb_shell(command: str, on_line: Line,
              serial: Optional[str] = None) -> Tuple[int, str]:
    args = _adb_argv()
    if serial:
        args += ["-s", serial]
    args += ["shell", command]
    return _run_adb(args, on_line)


def adb_push(local: str, remote: str, on_line: Line,
             serial: Optional[str] = None) -> int:
    args = _adb_argv()
    if serial:
        args += ["-s", serial]
    args += ["push", local, remote]
    rc, _ = _run_adb(args, on_line)
    return rc


def adb_forward(local_port: int, remote_port: int, on_line: Line,
                serial: Optional[str] = None) -> int:
    args = _adb_argv()
    if serial:
        args += ["-s", serial]
    args += ["forward", f"tcp:{local_port}", f"tcp:{remote_port}"]
    rc, _ = _run_adb(args, on_line)
    return rc


def adb_forward_remove(local_port: int, on_line: Line,
                       serial: Optional[str] = None) -> int:
    args = _adb_argv()
    if serial:
        args += ["-s", serial]
    args += ["forward", "--remove", f"tcp:{local_port}"]
    rc, _ = _run_adb(args, on_line)
    return rc


def adb_reboot(on_line: Line, serial: Optional[str] = None) -> int:
    args = _adb_argv()
    if serial:
        args += ["-s", serial]
    args += ["reboot"]
    rc, _ = _run_adb(args, on_line, timeout=30)
    return rc


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------

ADB_PROFILES: Dict[str, Dict] = {
    "rayhunter": {
        "id": "rayhunter",
        "label": "RayHunter IMSI Catcher Detector (EFF)",
        "repo": "EFForg/rayhunter",
        "device": "Orbic RC400L",
        "api_endpoint": "/api/analysis",
        "api_port": 8080,
    },
}


# ---------------------------------------------------------------------------
# GitHub release helpers
# ---------------------------------------------------------------------------

_RELEASE_API_TMPL = "https://api.github.com/repos/{repo}/releases/latest"


def _github_latest(repo: str) -> Tuple[str, List[Dict]]:
    url = _RELEASE_API_TMPL.format(repo=repo)
    data = json.loads(_http_get(url).decode("utf-8"))
    tag = data.get("tag_name", "latest")
    return tag, data.get("assets", [])


def _pick_platform_asset(assets: List[Dict]) -> Optional[Dict]:
    """Select the release zip matching the current OS + arch."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    # map platform.machine() -> rayhunter's naming convention
    arch_map: Dict[str, List[str]] = {
        "x86_64": ["x86_64", "x64"],
        "amd64": ["x86_64", "x64"],
        "aarch64": ["aarch64", "arm64", "arm"],
        "arm64": ["aarch64", "arm64", "arm"],
        "armv7l": ["armv7"],
    }

    os_tokens: List[str] = []
    if system == "windows":
        os_tokens = ["windows"]
    elif system == "darwin":
        os_tokens = ["macos"]
    else:
        os_tokens = ["linux"]

    arch_tokens = arch_map.get(machine, [machine])

    # score each asset: must match os AND arch, prefer exact arch match
    best: Optional[Dict] = None
    best_score = -1
    for a in assets:
        name = a.get("name", "").lower()
        if not name.endswith(".zip"):
            continue
        if name.endswith(".sha256"):
            continue
        os_match = any(t in name for t in os_tokens)
        if not os_match:
            continue
        for i, at in enumerate(arch_tokens):
            if at in name:
                score = len(arch_tokens) - i
                if score > best_score:
                    best = a
                    best_score = score
                break
    return best


# ---------------------------------------------------------------------------
# Cache / download
# ---------------------------------------------------------------------------

def cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "rayhunter_fw")
    os.makedirs(d, exist_ok=True)
    return d


def _download_to(url: str, dest_dir: str, name: str, on_line: Line) -> str:
    safe = _safe_filename(name)
    dest = os.path.join(dest_dir, safe)
    real_dir = os.path.realpath(dest_dir)
    real_dest = os.path.realpath(dest)
    if real_dest != os.path.join(real_dir, safe) and not real_dest.startswith(real_dir + os.sep):
        raise ValueError(f"refusing download dest that escapes the cache dir: {dest!r}")
    on_line(f"[download] {safe}")
    data = _http_get(url)
    with open(dest, "wb") as f:
        f.write(data)
    on_line(f"[download] {len(data)} bytes -> {dest}")
    return dest


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def installed_version(on_line: Line, serial: Optional[str] = None) -> Optional[str]:
    """Read the installed RayHunter version from the device (daemon --version)."""
    try:
        args = _adb_argv()
    except FileNotFoundError:
        return None
    if serial:
        args += ["-s", serial]
    args += ["shell", "/data/rayhunter/rayhunter-daemon --version 2>/dev/null || echo NOTFOUND"]
    rc, output = _run_adb_quiet(args)
    if rc != 0 or "NOTFOUND" in output:
        return None
    # output is typically "rayhunter-daemon <version>"
    for line in output.strip().splitlines():
        line = line.strip()
        if line and "NOTFOUND" not in line:
            parts = line.split()
            return parts[-1] if parts else line
    return None


def latest_version(profile_id: str = "rayhunter") -> Tuple[Optional[str], Optional[str]]:
    """Return (tag, download_url) for the latest release, or (None, None) on error."""
    profile = ADB_PROFILES.get(profile_id)
    if not profile:
        return None, None
    try:
        tag, assets = _github_latest(profile["repo"])
    except Exception:
        return None, None
    pick = _pick_platform_asset(assets)
    url = pick.get("browser_download_url") if pick else None
    return tag, url


def check_version(on_line: Line, serial: Optional[str] = None,
                  profile_id: str = "rayhunter") -> Dict[str, Optional[str]]:
    """Return {'installed': ..., 'latest': ..., 'update_available': bool}."""
    inst = installed_version(on_line, serial=serial)
    ltag, _ = latest_version(profile_id)
    update = False
    if inst and ltag:
        # strip leading 'v' for comparison
        iv = inst.lstrip("v")
        lv = ltag.lstrip("v")
        update = iv != lv
    return {"installed": inst, "latest": ltag, "update_available": update}


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def _extract_zip(zip_path: str, dest_dir: str, on_line: Line) -> str:
    """Extract a zip, enforcing path-traversal safety. Returns the extraction directory."""
    on_line(f"[extract] {os.path.basename(zip_path)}")
    real_dest = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            # reject absolute paths and path traversal
            if info.filename.startswith("/") or ".." in info.filename.split("/"):
                raise ValueError(f"refusing zip entry with path traversal: {info.filename!r}")
            target = os.path.realpath(os.path.join(dest_dir, info.filename))
            if not target.startswith(real_dest + os.sep) and target != real_dest:
                raise ValueError(f"refusing zip entry that escapes dest: {info.filename!r}")
        zf.extractall(dest_dir)
    on_line(f"[extract] done -> {dest_dir}")
    return dest_dir


def _find_installer_binary(extract_dir: str) -> Optional[str]:
    """Locate the installer binary inside the extracted release."""
    for root, _dirs, files in os.walk(extract_dir):
        for f in files:
            if f.startswith("installer") and not f.endswith((".sha256", ".md")):
                return os.path.join(root, f)
    return None


def install_rayhunter(on_line: Line, serial: Optional[str] = None,
                      admin_password: Optional[str] = None,
                      admin_ip: str = "192.168.1.1",
                      method: str = "network") -> int:
    """Download and install RayHunter on the connected Orbic RC400L.

    method:
      'network'  -- use the EFF network installer (./installer orbic --admin-password ...).
                    Requires the host to be on the Orbic's WiFi or USB tethered.
      'usb'      -- use the legacy USB+ADB installer (./installer orbic-usb).
                    Windows support is limited; Linux/macOS preferred.

    Returns 0 on success, non-zero on failure.
    """
    profile = ADB_PROFILES["rayhunter"]
    on_line(f"[rayhunter] fetching latest release from {profile['repo']}...")

    try:
        tag, assets = _github_latest(profile["repo"])
    except Exception as e:
        on_line(f"[error] failed to fetch release info: {e}")
        return 1

    on_line(f"[rayhunter] latest release: {tag}")
    pick = _pick_platform_asset(assets)
    if not pick:
        on_line("[error] no release asset found for this platform "
                f"({platform.system()} {platform.machine()})")
        return 1

    dl_url = pick.get("browser_download_url")
    if not dl_url:
        on_line("[error] no download URL in release asset")
        return 1

    cache = cache_dir()
    zip_name = _safe_filename(pick["name"])

    try:
        zip_path = _download_to(dl_url, cache, zip_name, on_line)
    except Exception as e:
        on_line(f"[error] download failed: {e}")
        return 1

    # verify sha256 if a checksum asset exists
    sha_asset = None
    for a in assets:
        if a.get("name") == zip_name + ".sha256":
            sha_asset = a
            break
    if sha_asset and sha_asset.get("browser_download_url"):
        on_line("[rayhunter] verifying sha256...")
        try:
            expected_raw = _http_get(sha_asset["browser_download_url"]).decode("utf-8").strip()
            expected_hash = expected_raw.split()[0].lower()
            import hashlib
            _h = hashlib.sha256()
            with open(zip_path, "rb") as _f:
                for _blk in iter(lambda: _f.read(1 << 20), b""):
                    _h.update(_blk)
            actual = _h.hexdigest().lower()
            if actual != expected_hash:
                on_line(f"[error] sha256 mismatch: expected {expected_hash}, got {actual}")
                return 1
            on_line("[rayhunter] sha256 OK")
        except Exception as e:
            on_line(f"[warning] could not verify sha256: {e}")

    extract_dir = os.path.join(cache, f"rayhunter-{tag}")
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)

    try:
        _extract_zip(zip_path, extract_dir, on_line)
    except Exception as e:
        on_line(f"[error] extraction failed: {e}")
        return 1

    installer_bin = _find_installer_binary(extract_dir)
    if not installer_bin:
        on_line("[error] installer binary not found in release archive")
        return 1

    # make executable on POSIX
    if sys.platform != "win32":
        try:
            os.chmod(installer_bin, 0o755)
        except OSError:
            pass

    on_line(f"[rayhunter] found installer: {os.path.basename(installer_bin)}")
    on_line(f"[rayhunter] install method: {method}")

    # build installer command
    argv: List[str] = [installer_bin]
    if method == "usb":
        argv.append("orbic-usb")
    else:
        argv.append("orbic")
        if admin_password:
            argv += ["--admin-password", admin_password]
        if admin_ip != "192.168.1.1":
            argv += ["--admin-ip", admin_ip]

    on_line(f"[rayhunter] running installer...")
    rc, _ = _run_adb(argv, on_line, timeout=300)
    if rc == 0:
        on_line("[rayhunter] install complete")
    else:
        on_line(f"[rayhunter] installer exited with code {rc}")
    return rc


# ---------------------------------------------------------------------------
# Manual install (direct ADB push, for when the EFF installer is unavailable)
# ---------------------------------------------------------------------------

# paths on the Orbic device
_DEVICE_BASE = "/data/rayhunter"
_DEVICE_DAEMON = f"{_DEVICE_BASE}/rayhunter-daemon"
_DEVICE_CONFIG = f"{_DEVICE_BASE}/config.toml"
_DEVICE_INIT = "/etc/init.d/rayhunter_daemon"

_DEFAULT_CONFIG = """\
qmdl_store_path = "/data/rayhunter/qmdl"
port = 8080
debug_mode = false
colorblind_mode = false
device = "orbic"
ui_level = 1
key_input_mode = 0
auto_check_updates = false
wifi_enabled = false

[analyzers]
imsi_requested = true
connection_redirect_2g_downgrade = true
lte_sib6_and_7_downgrade = true
null_cipher = true
nas_null_cipher = true
incomplete_sib = true
test_analyzer = false
diagnostic_analyzer = true
"""

_INIT_SCRIPT = """\
#! /bin/sh
set -e
case "$1" in
start)
    echo -n "Starting rayhunter: "
    start-stop-daemon -S -b --make-pidfile --pidfile /tmp/rayhunter.pid \\
    --startas /bin/sh -- -c "RUST_LOG=info exec /data/rayhunter/rayhunter-daemon /data/rayhunter/config.toml > /data/rayhunter/rayhunter.log 2>&1"
    echo "done"
    ;;
stop)
    echo -n "Stopping rayhunter: "
    start-stop-daemon -K -p /tmp/rayhunter.pid
    echo "done"
    ;;
restart)
    $0 stop
    $0 start
    ;;
*)
    echo "Usage rayhunter_daemon { start | stop | restart }" >&2
    exit 1
    ;;
esac
exit 0
"""


def install_manual(daemon_binary_path: str, on_line: Line,
                   serial: Optional[str] = None) -> int:
    """Push a pre-built rayhunter-daemon binary + config + init script via ADB.

    For advanced users who have already rooted their device and have the daemon
    binary (e.g. cross-compiled from source). Returns 0 on success.
    """
    if not os.path.isfile(daemon_binary_path):
        on_line(f"[error] daemon binary not found: {daemon_binary_path}")
        return 1

    on_line("[rayhunter] creating directories on device...")
    rc, _ = adb_shell(f"mkdir -p {_DEVICE_BASE}/qmdl {_DEVICE_BASE}/scripts {_DEVICE_BASE}/bin",
                      on_line, serial=serial)
    if rc != 0:
        on_line("[error] failed to create device directories")
        return rc

    on_line("[rayhunter] pushing daemon binary...")
    rc = adb_push(daemon_binary_path, _DEVICE_DAEMON, on_line, serial=serial)
    if rc != 0:
        on_line("[error] failed to push daemon binary")
        return rc

    adb_shell(f"chmod 755 {_DEVICE_DAEMON}", on_line, serial=serial)

    # push config (only if not already present)
    rc, output = adb_shell(f"test -f {_DEVICE_CONFIG} && echo EXISTS || echo MISSING",
                           on_line, serial=serial)
    if "MISSING" in output:
        on_line("[rayhunter] pushing default config...")
        config_tmp = os.path.join(tempfile.gettempdir(), "rayhunter_config.toml")
        try:
            with open(config_tmp, "w", encoding="utf-8") as f:
                f.write(_DEFAULT_CONFIG)
            adb_push(config_tmp, _DEVICE_CONFIG, on_line, serial=serial)
        finally:
            try:
                os.unlink(config_tmp)
            except OSError:
                pass
    else:
        on_line("[rayhunter] config already exists on device, skipping")

    # push init script
    on_line("[rayhunter] pushing init script...")
    init_tmp = os.path.join(tempfile.gettempdir(), "rayhunter_daemon_init")
    try:
        with open(init_tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(_INIT_SCRIPT)
        adb_push(init_tmp, _DEVICE_INIT, on_line, serial=serial)
    finally:
        try:
            os.unlink(init_tmp)
        except OSError:
            pass

    adb_shell(f"chmod 755 {_DEVICE_INIT}", on_line, serial=serial)

    on_line("[rayhunter] manual install complete. Reboot the device to start RayHunter.")
    return 0


# ---------------------------------------------------------------------------
# Port forwarding
# ---------------------------------------------------------------------------

def setup_forward(on_line: Line, serial: Optional[str] = None,
                  local_port: int = 8080, remote_port: int = 8080) -> int:
    on_line(f"[rayhunter] forwarding tcp:{local_port} -> tcp:{remote_port}")
    return adb_forward(local_port, remote_port, on_line, serial=serial)


def remove_forward(on_line: Line, serial: Optional[str] = None,
                   local_port: int = 8080) -> int:
    on_line(f"[rayhunter] removing forward on tcp:{local_port}")
    return adb_forward_remove(local_port, on_line, serial=serial)


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------

def check_status(on_line: Line, profile_id: str = "rayhunter",
                 host: str = "localhost", timeout: float = 5.0) -> Dict:
    """Query the RayHunter API to check if it's running.

    Tries localhost (assumes port forward is active) first.
    Returns a dict with 'running' (bool), 'status_code', 'response', 'error'.
    """
    profile = ADB_PROFILES.get(profile_id)
    if not profile:
        return {"running": False, "error": f"unknown profile: {profile_id}"}

    port = profile["api_port"]
    endpoint = profile["api_endpoint"]
    url = f"http://{host}:{port}{endpoint}"

    on_line(f"[rayhunter] checking {url}")
    try:
        r = requests.get(url, timeout=timeout)
        running = r.status_code == 200
        result: Dict = {
            "running": running,
            "status_code": r.status_code,
            "error": None,
        }
        try:
            result["response"] = r.json()
        except Exception:
            result["response"] = r.text[:500] if r.text else None
        on_line(f"[rayhunter] status: {'running' if running else 'not running'} "
                f"(HTTP {r.status_code})")
        return result
    except requests.ConnectionError:
        on_line("[rayhunter] connection refused -- RayHunter not reachable "
                "(is port forwarding active?)")
        return {"running": False, "status_code": None, "response": None,
                "error": "connection refused"}
    except requests.Timeout:
        on_line("[rayhunter] request timed out")
        return {"running": False, "status_code": None, "response": None,
                "error": "timeout"}
    except Exception as e:
        on_line(f"[rayhunter] status check failed: {e}")
        return {"running": False, "status_code": None, "response": None,
                "error": str(e)}


def is_running(on_line: Line, serial: Optional[str] = None,
               profile_id: str = "rayhunter") -> bool:
    """Quick check: is the RayHunter daemon process alive on the device?"""
    rc, output = adb_shell("pgrep -f rayhunter-daemon", on_line, serial=serial)
    alive = rc == 0 and output.strip() != ""
    on_line(f"[rayhunter] daemon process: {'alive' if alive else 'not found'}")
    return alive


# ---------------------------------------------------------------------------
# Start / Stop / Restart
# ---------------------------------------------------------------------------

def start_daemon(on_line: Line, serial: Optional[str] = None) -> int:
    on_line("[rayhunter] starting daemon...")
    rc, _ = adb_shell(f"{_DEVICE_INIT} start", on_line, serial=serial)
    return rc


def stop_daemon(on_line: Line, serial: Optional[str] = None) -> int:
    on_line("[rayhunter] stopping daemon...")
    rc, _ = adb_shell(f"{_DEVICE_INIT} stop", on_line, serial=serial)
    return rc


def restart_daemon(on_line: Line, serial: Optional[str] = None) -> int:
    on_line("[rayhunter] restarting daemon...")
    rc, _ = adb_shell(f"{_DEVICE_INIT} restart", on_line, serial=serial)
    return rc


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall_rayhunter(on_line: Line, serial: Optional[str] = None,
                        keep_data: bool = False) -> int:
    """Remove RayHunter from the device. Optionally preserve /data/rayhunter/qmdl."""
    on_line("[rayhunter] stopping daemon...")
    adb_shell(f"{_DEVICE_INIT} stop 2>/dev/null; true", on_line, serial=serial)

    # kill any lingering process
    adb_shell("pkill -f rayhunter-daemon 2>/dev/null; true", on_line, serial=serial)

    on_line("[rayhunter] removing init script...")
    adb_shell(f"rm -f {_DEVICE_INIT}", on_line, serial=serial)

    if keep_data:
        on_line("[rayhunter] preserving recording data, removing binaries + config...")
        adb_shell(f"rm -f {_DEVICE_DAEMON}", on_line, serial=serial)
        adb_shell(f"rm -f {_DEVICE_CONFIG}", on_line, serial=serial)
        adb_shell(f"rm -rf {_DEVICE_BASE}/bin {_DEVICE_BASE}/scripts", on_line, serial=serial)
        adb_shell(f"rm -f {_DEVICE_BASE}/rayhunter.log", on_line, serial=serial)
    else:
        on_line("[rayhunter] removing all RayHunter files...")
        # handle both real dir and symlink
        adb_shell(f"rm -rf {_DEVICE_BASE}", on_line, serial=serial)

    # remove the misc-daemon init script if present (installed by EFF installer)
    adb_shell("rm -f /etc/init.d/misc-daemon 2>/dev/null; true", on_line, serial=serial)

    on_line("[rayhunter] uninstall complete")
    return 0


# ---------------------------------------------------------------------------
# Convenience: full flow (install + forward + verify)
# ---------------------------------------------------------------------------

def full_install(on_line: Line, serial: Optional[str] = None,
                 admin_password: Optional[str] = None,
                 admin_ip: str = "192.168.1.1",
                 method: str = "network",
                 setup_port_forward: bool = True) -> int:
    """Full install flow: download, install, optionally forward, verify."""
    rc = install_rayhunter(on_line, serial=serial, admin_password=admin_password,
                           admin_ip=admin_ip, method=method)
    if rc != 0:
        return rc

    if setup_port_forward:
        fwd_rc = setup_forward(on_line, serial=serial)
        if fwd_rc != 0:
            on_line("[warning] port forwarding failed -- you can still access "
                    f"RayHunter at http://{admin_ip}:8080 via WiFi")

        status = check_status(on_line)
        if status["running"]:
            on_line("[rayhunter] verified: dashboard reachable at http://localhost:8080")
        else:
            on_line("[rayhunter] install finished but dashboard not yet reachable "
                    "(device may still be rebooting)")
    return 0
