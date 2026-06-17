"""
SD-card imaging backend for Raspberry Pi-based devices (Pwnagotchi, RaspyJack, Kali ARM).

Downloads compressed .img archives from GitHub releases, decompresses them in a streaming
fashion (never loads the full image into RAM), and writes block-level to a removable SD card.
Cross-platform: Windows via ctypes PhysicalDriveN, Linux/macOS via dd subprocess.

Safety invariants enforced BEFORE any write:
  * Target must be a removable drive (never a fixed/system disk).
  * Target must be < 256 GB (SD card sanity check).
  * Caller must pass confirmed=True (no accidental writes).
"""

import ctypes
import gzip
import hashlib
import json
import lzma
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

Line = Callable[[str], None]

# 1 MiB read/write chunk — large enough for throughput, small enough for progress granularity
_CHUNK = 1 << 20

# SD card sanity ceiling: refuse to write to anything >= 256 GB
_MAX_SD_BYTES = 256 * (1 << 30)

# --------------------------------------------------------------------------- #
# SSRF / download hardening (mirrors flasher.py approach, extended for Pi hosts)
# --------------------------------------------------------------------------- #

_ALLOWED_HOSTS = frozenset((
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "kali.download",
))
_ALLOWED_HOST_SUFFIXES = (".githubusercontent.com", ".kali.download")


def _host_allowed(host: Optional[str]) -> bool:
    """True if host is on the allowlist or matches a suffix."""
    if not host:
        return False
    h = host.lower().split("@")[-1].split(":")[0]
    if h in _ALLOWED_HOSTS:
        return True
    return any(h.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


def _require_allowed_url(url: str) -> str:
    """Validate url is https to an allowlisted host; raise ValueError otherwise."""
    if not isinstance(url, str) or not url:
        raise ValueError("refusing empty/invalid download URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"refusing non-https URL scheme {parts.scheme!r}: {url!r}")
    if not _host_allowed(parts.hostname):
        raise ValueError(f"refusing URL to non-allowlisted host {parts.hostname!r}: {url!r}")
    return url


def _safe_filename(name: str) -> str:
    """Reject path-traversal in a downloaded file name (mirrors flasher._safe_cache_name)."""
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


# --------------------------------------------------------------------------- #
# Pi image profile registry
# --------------------------------------------------------------------------- #

PI_IMAGE_PROFILES: Dict[str, Dict] = {
    "pwnagotchi": {
        "id": "pwnagotchi",
        "label": "Pwnagotchi (jayofelony)",
        "repo": "jayofelony/pwnagotchi",
        "file_pattern": r"pwnagotchi.*\.img\.(xz|gz|zip)$",
    },
    "raspyjack": {
        "id": "raspyjack",
        "label": "RaspyJack (7h30th3r0n3)",
        "repo": "7h30th3r0n3/Raspyjack",
        "file_pattern": r".*\.(img|img\.xz|img\.gz|zip)$",
    },
    "kali-arm": {
        "id": "kali-arm",
        "label": "Kali Linux ARM64",
        "repo": None,
        "download_url": "https://kali.download/arm-images/",
        "file_pattern": r"kali-linux.*arm64.*\.img\.xz$",
    },
}


def list_pi_profiles() -> List[Tuple[str, str]]:
    """Return [(id, label) ...] for every registered Pi image profile."""
    return [(p["id"], p["label"]) for p in PI_IMAGE_PROFILES.values()]


def get_pi_profile(profile_id: str) -> Dict:
    """Return the profile dict for an id (raises KeyError on unknown id)."""
    return PI_IMAGE_PROFILES[profile_id]


# --------------------------------------------------------------------------- #
# GitHub release discovery
# --------------------------------------------------------------------------- #

_UA = {"User-Agent": "universal-flasher"}


def _github_release_assets(repo: str) -> Tuple[str, List[Dict]]:
    """GET /repos/{repo}/releases/latest and return (tag, [{name, url, size}, ...])."""
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    _require_allowed_url(api_url)
    resp = requests.get(api_url, headers=_UA, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name", "latest")
    assets = []
    for a in data.get("assets", []):
        url = a.get("browser_download_url", "")
        if url:
            _require_allowed_url(url)
        assets.append({
            "name": a.get("name", ""),
            "url": url,
            "size": a.get("size", 0),
        })
    return tag, assets


def discover_images(profile_id: str, on_line: Line) -> List[Dict]:
    """Return downloadable image assets for a Pi profile, filtered by file_pattern."""
    prof = get_pi_profile(profile_id)
    pat = re.compile(prof["file_pattern"], re.IGNORECASE)
    if prof.get("repo"):
        on_line(f"[sd] querying latest release for {prof['repo']}...")
        tag, assets = _github_release_assets(prof["repo"])
        on_line(f"[sd] release {tag}: {len(assets)} asset(s)")
        matched = [a for a in assets if pat.search(a["name"])]
        on_line(f"[sd] {len(matched)} image(s) match pattern")
        return matched
    on_line(f"[sd] {prof['label']}: no GitHub release — use download_url directly")
    return []


# --------------------------------------------------------------------------- #
# Download with progress
# --------------------------------------------------------------------------- #

def download_image(url: str, dest_dir: str, on_line: Line,
                   on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Download url into dest_dir with streaming progress. Returns the local path."""
    _require_allowed_url(url)
    os.makedirs(dest_dir, exist_ok=True)
    name = _safe_filename(url.rsplit("/", 1)[-1].split("?")[0])
    dest = os.path.join(dest_dir, name)
    # defense-in-depth: confirm dest stays inside dest_dir
    real_dir = os.path.realpath(dest_dir)
    real_dest = os.path.realpath(dest)
    if not (real_dest == os.path.join(real_dir, name) or real_dest.startswith(real_dir + os.sep)):
        raise ValueError(f"refusing download dest that escapes the cache dir: {dest!r}")
    on_line(f"[download] {name}")
    resp = requests.get(url, headers=_UA, stream=True, timeout=60,
                        allow_redirects=False)
    max_redirects = 10
    for _ in range(max_redirects):
        if not (resp.is_redirect or resp.is_permanent_redirect):
            break
        redirect_url = resp.headers.get("Location", "")
        _require_allowed_url(redirect_url)
        resp = requests.get(redirect_url, headers=_UA, stream=True, timeout=60,
                            allow_redirects=False)
    else:
        if resp.is_redirect or resp.is_permanent_redirect:
            raise ValueError(f"too many redirects (>{max_redirects}) downloading {url!r}")
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    written = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=_CHUNK):
            f.write(chunk)
            written += len(chunk)
            if on_progress and total > 0:
                on_progress(min(written / total, 1.0))
    on_line(f"[download] {written} bytes -> {dest}")
    return dest


# --------------------------------------------------------------------------- #
# Decompression (streaming — never loads full image into RAM)
# --------------------------------------------------------------------------- #

def _decompress_xz(src: str, dest: str, on_line: Line,
                   on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Decompress .xz to dest, streaming chunk-by-chunk."""
    on_line(f"[decompress] xz: {os.path.basename(src)}")
    src_size = os.path.getsize(src)
    read_total = 0
    with lzma.open(src, "rb") as fin, open(dest, "wb") as fout:
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            read_total += len(chunk)
            if on_progress and src_size > 0:
                # compressed-read position is approximate; clamp to 1.0
                on_progress(min(read_total / (src_size * 3), 1.0))
    on_line(f"[decompress] -> {dest} ({os.path.getsize(dest)} bytes)")
    return dest


def _decompress_gz(src: str, dest: str, on_line: Line,
                   on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Decompress .gz to dest, streaming chunk-by-chunk."""
    on_line(f"[decompress] gz: {os.path.basename(src)}")
    src_size = os.path.getsize(src)
    read_total = 0
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            read_total += len(chunk)
            if on_progress and src_size > 0:
                on_progress(min(read_total / (src_size * 3), 1.0))
    on_line(f"[decompress] -> {dest} ({os.path.getsize(dest)} bytes)")
    return dest


def _decompress_zip(src: str, dest: str, on_line: Line,
                    on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Extract first .img from a zip archive, streaming."""
    on_line(f"[decompress] zip: {os.path.basename(src)}")
    with zipfile.ZipFile(src, "r") as zf:
        imgs = [n for n in zf.namelist() if n.lower().endswith(".img")]
        if not imgs:
            raise ValueError(f"no .img file found inside {src}")
        img_name = imgs[0]
        info = zf.getinfo(img_name)
        total = info.file_size
        on_line(f"[decompress] extracting {img_name} ({total} bytes)")
        written = 0
        with zf.open(img_name) as fin, open(dest, "wb") as fout:
            while True:
                chunk = fin.read(_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                written += len(chunk)
                if on_progress and total > 0:
                    on_progress(min(written / total, 1.0))
    on_line(f"[decompress] -> {dest} ({os.path.getsize(dest)} bytes)")
    return dest


def decompress(src: str, dest_dir: str, on_line: Line,
               on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Auto-detect compression and decompress src into dest_dir. Returns .img path."""
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(src)
    if base.endswith(".img.xz"):
        img_name = base[:-3]  # strip .xz
    elif base.endswith(".img.gz"):
        img_name = base[:-3]  # strip .gz
    elif base.endswith(".zip"):
        img_name = base[:-4] + ".img"
    elif base.endswith(".img"):
        on_line(f"[decompress] already an .img, no decompression needed")
        return src
    else:
        raise ValueError(f"unsupported archive format: {base}")
    dest = os.path.join(dest_dir, _safe_filename(img_name))
    if base.endswith(".img.xz"):
        return _decompress_xz(src, dest, on_line, on_progress)
    elif base.endswith(".img.gz"):
        return _decompress_gz(src, dest, on_line, on_progress)
    elif base.endswith(".zip"):
        return _decompress_zip(src, dest, on_line, on_progress)
    raise ValueError(f"unsupported archive format: {base}")


# --------------------------------------------------------------------------- #
# SD card detection (cross-platform)
# --------------------------------------------------------------------------- #

def _detect_sd_windows(on_line: Line) -> List[Dict]:
    """List removable disk drives on Windows via WMI/PowerShell."""
    cards: List[Dict] = []
    # wmic is deprecated but still universal; PowerShell Get-Disk is more reliable
    ps_cmd = (
        "Get-Disk | Where-Object { $_.BusType -ne 'NVMe' -and $_.BusType -ne 'SATA' -and $_.BusType -ne 'RAID' } "
        "| Select-Object Number, FriendlyName, Size, BusType, MediaType "
        "| ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            on_line(f"[sd] PowerShell disk query failed: {r.stderr.strip()}")
            return cards
        text = r.stdout.strip()
        if not text:
            return cards
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        for d in data:
            num = d.get("Number")
            size = d.get("Size", 0)
            name = d.get("FriendlyName", f"Disk {num}")
            bus = d.get("BusType", "")
            media = d.get("MediaType", "")
            # only offer genuinely removable media (USB card readers show as USB bus)
            if media == "Fixed" and bus not in ("USB",):
                continue
            if size and size >= _MAX_SD_BYTES:
                continue
            cards.append({
                "device": f"\\\\.\\PhysicalDrive{num}",
                "name": name,
                "size": size,
                "bus": bus,
                "removable": media != "Fixed" or bus == "USB",
            })
    except Exception as e:
        on_line(f"[sd] Windows disk detection error: {e}")
    return cards


def _detect_sd_linux(on_line: Line) -> List[Dict]:
    """List removable block devices on Linux via lsblk."""
    cards: List[Dict] = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,RM,TYPE,TRAN,MODEL"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            on_line(f"[sd] lsblk failed: {r.stderr.strip()}")
            return cards
        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            rm = dev.get("rm")
            # rm can be bool, int, or string "1"/"0"/"true"/"false" depending on lsblk version
            if isinstance(rm, str):
                removable = rm.lower() in ("1", "true")
            else:
                removable = bool(rm)
            tran = (dev.get("tran") or "").lower()
            # accept removable disks, or USB-connected disks (card readers)
            if not removable and tran != "usb":
                continue
            size = int(dev.get("size") or 0)
            if size >= _MAX_SD_BYTES:
                continue
            name = dev.get("model") or dev.get("name", "")
            cards.append({
                "device": f"/dev/{dev['name']}",
                "name": name.strip(),
                "size": size,
                "bus": tran,
                "removable": True,
            })
    except Exception as e:
        on_line(f"[sd] Linux disk detection error: {e}")
    return cards


def _detect_sd_macos(on_line: Line) -> List[Dict]:
    """List removable disk devices on macOS via diskutil."""
    cards: List[Dict] = []
    try:
        r = subprocess.run(
            ["diskutil", "list", "-plist", "external"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            on_line(f"[sd] diskutil failed: {r.stderr.strip()}")
            return cards
        # parse the plist to find external whole disks
        import plistlib
        plist = plistlib.loads(r.stdout.encode("utf-8"))
        whole_disks = plist.get("WholeDisks", [])
        for disk_id in whole_disks:
            dev = f"/dev/{disk_id}"
            # get size via diskutil info
            info_r = subprocess.run(
                ["diskutil", "info", "-plist", disk_id],
                capture_output=True, text=True, timeout=10,
            )
            if info_r.returncode != 0:
                continue
            info = plistlib.loads(info_r.stdout.encode("utf-8"))
            size = info.get("TotalSize", info.get("Size", 0))
            removable = info.get("Removable", info.get("RemovableMedia", False))
            name = info.get("MediaName", disk_id)
            if size >= _MAX_SD_BYTES:
                continue
            # only offer removable or external media
            if not (removable or info.get("Internal", True) is False):
                continue
            cards.append({
                "device": dev,
                "name": name,
                "size": size,
                "bus": info.get("BusProtocol", ""),
                "removable": True,
            })
    except Exception as e:
        on_line(f"[sd] macOS disk detection error: {e}")
    return cards


def detect_sd_cards(on_line: Line) -> List[Dict]:
    """Return a list of removable SD-card-sized drives. Each dict has:
    device, name, size, bus, removable."""
    system = platform.system()
    on_line(f"[sd] scanning for removable drives ({system})...")
    if system == "Windows":
        cards = _detect_sd_windows(on_line)
    elif system == "Linux":
        cards = _detect_sd_linux(on_line)
    elif system == "Darwin":
        cards = _detect_sd_macos(on_line)
    else:
        on_line(f"[sd] unsupported platform: {system}")
        cards = []
    on_line(f"[sd] found {len(cards)} candidate drive(s)")
    for c in cards:
        size_gb = c["size"] / (1 << 30) if c["size"] else 0
        on_line(f"  {c['device']}  {c['name']}  {size_gb:.1f} GB  [{c['bus']}]")
    return cards


# --------------------------------------------------------------------------- #
# SD card write (cross-platform block-level)
# --------------------------------------------------------------------------- #

def _validate_write_target(device: str, cards: List[Dict], on_line: Line) -> Dict:
    """Confirm device is in the detected-removable list. Returns the card dict or raises."""
    for c in cards:
        if c["device"] == device:
            if not c.get("removable"):
                raise ValueError(f"refusing to write to non-removable drive: {device}")
            if c["size"] and c["size"] >= _MAX_SD_BYTES:
                raise ValueError(f"refusing to write: drive size {c['size']} exceeds 256 GB limit")
            return c
    raise ValueError(f"device {device!r} not found in detected removable drives — re-scan first")


def _write_dd(img_path: str, device: str, on_line: Line,
              on_progress: Optional[Callable[[float], None]] = None) -> int:
    """Write img to device using dd (Linux/macOS). Returns exit code."""
    img_size = os.path.getsize(img_path)
    # unmount any mounted partitions first
    system = platform.system()
    if system == "Darwin":
        on_line(f"[sd] unmounting {device}...")
        subprocess.run(["diskutil", "unmountDisk", device],
                       capture_output=True, timeout=15)
        # macOS: use rdiskN for raw (unbuffered) write
        raw_dev = device.replace("/dev/disk", "/dev/rdisk")
    else:
        raw_dev = device
        # Linux: unmount all partitions of this device
        on_line(f"[sd] unmounting partitions on {device}...")
        try:
            r = subprocess.run(["lsblk", "-n", "-o", "MOUNTPOINT", device],
                               capture_output=True, text=True, timeout=10)
            for mp in r.stdout.strip().splitlines():
                mp = mp.strip()
                if mp:
                    subprocess.run(["umount", mp], capture_output=True, timeout=10)
        except Exception:
            pass

    on_line(f"[sd] writing {os.path.basename(img_path)} ({img_size} bytes) to {raw_dev}...")
    # dd with status=progress for user feedback; we also track ourselves via file position
    bs = "4m" if platform.system() == "Darwin" else "4M"
    argv = ["dd", f"if={img_path}", f"of={raw_dev}", f"bs={bs}", "conv=fsync", "status=progress"]
    # dd requires root on Linux/macOS
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        argv = ["sudo"] + argv

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError as e:
        on_line(f"[error] {e}")
        return 127

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            on_line(line)
            # parse dd progress: "123456789 bytes (123 MB, 117 MiB) copied"
            m = re.search(r"(\d+)\s+bytes", line)
            if m and on_progress and img_size > 0:
                on_progress(min(int(m.group(1)) / img_size, 1.0))
        proc.wait()
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
    on_line(f"[sd] dd finished with exit code {proc.returncode}")
    return proc.returncode


def _write_windows(img_path: str, device: str, on_line: Line,
                   on_progress: Optional[Callable[[float], None]] = None) -> int:
    """Write img to PhysicalDriveN on Windows using ctypes CreateFile + WriteFile."""
    img_size = os.path.getsize(img_path)
    on_line(f"[sd] writing {os.path.basename(img_path)} ({img_size} bytes) to {device}...")

    GENERIC_WRITE = 0x40000000
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p

    # open physical drive for raw write
    handle = kernel32.CreateFileW(
        device,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE:
        err = ctypes.GetLastError()
        on_line(f"[error] CreateFile failed for {device} (error {err})")
        on_line("[error] run as Administrator for raw disk access")
        return 1

    try:
        # lock and dismount the volume so Windows doesn't interfere
        dummy = ctypes.c_ulong(0)
        kernel32.DeviceIoControl(handle, FSCTL_LOCK_VOLUME, None, 0, None, 0,
                                 ctypes.byref(dummy), None)
        kernel32.DeviceIoControl(handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
                                 ctypes.byref(dummy), None)

        written_total = 0
        buf_size = 4 * _CHUNK  # 4 MiB aligned writes
        written_dword = ctypes.c_ulong(0)

        with open(img_path, "rb") as fin:
            while True:
                data = fin.read(buf_size)
                if not data:
                    break
                # pad last chunk to sector alignment (512 bytes)
                remainder = len(data) % 512
                if remainder:
                    data += b"\x00" * (512 - remainder)
                buf = ctypes.create_string_buffer(data)
                ok = kernel32.WriteFile(
                    handle, buf, len(data), ctypes.byref(written_dword), None,
                )
                if not ok:
                    err = ctypes.GetLastError()
                    on_line(f"[error] WriteFile failed at offset {written_total} (error {err})")
                    return 1
                written_total += written_dword.value
                if on_progress and img_size > 0:
                    on_progress(min(written_total / img_size, 1.0))

        # flush
        kernel32.FlushFileBuffers(handle)
        on_line(f"[sd] wrote {written_total} bytes to {device}")
    finally:
        kernel32.CloseHandle(handle)

    return 0


def write_image(img_path: str, device: str, on_line: Line,
                on_progress: Optional[Callable[[float], None]] = None,
                confirmed: bool = False) -> int:
    """Write a raw .img to an SD card device. Returns 0 on success.

    Safety: device MUST have been returned by detect_sd_cards(). The confirmed
    parameter must be True — this is a destructive operation that overwrites the
    entire drive."""
    if not confirmed:
        raise ValueError("write_image requires confirmed=True — data on the target will be destroyed")
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"image not found: {img_path}")

    # re-detect and validate the target is still a removable drive
    cards = detect_sd_cards(on_line)
    card = _validate_write_target(device, cards, on_line)
    on_line(f"[sd] target confirmed: {card['name']} ({card['device']})")

    system = platform.system()
    if system == "Windows":
        return _write_windows(img_path, device, on_line, on_progress)
    elif system in ("Linux", "Darwin"):
        return _write_dd(img_path, device, on_line, on_progress)
    else:
        on_line(f"[error] unsupported platform for SD write: {system}")
        return 1


# --------------------------------------------------------------------------- #
# Verify (SHA256)
# --------------------------------------------------------------------------- #

def sha256_file(path: str, on_line: Line,
                on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Return lowercase hex SHA-256 of a file, streamed with progress."""
    size = os.path.getsize(path)
    on_line(f"[verify] hashing {os.path.basename(path)} ({size} bytes)...")
    h = hashlib.sha256()
    read_total = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
            read_total += len(chunk)
            if on_progress and size > 0:
                on_progress(min(read_total / size, 1.0))
    digest = h.hexdigest()
    on_line(f"[verify] SHA-256: {digest}")
    return digest


def verify_write(img_path: str, device: str, on_line: Line,
                 on_progress: Optional[Callable[[float], None]] = None) -> bool:
    """Read back img_size bytes from device and compare SHA-256 against the image file."""
    img_size = os.path.getsize(img_path)
    on_line(f"[verify] computing image hash...")
    img_hash = sha256_file(img_path, on_line)

    on_line(f"[verify] reading {img_size} bytes back from {device}...")
    h = hashlib.sha256()
    read_total = 0
    system = platform.system()

    if system == "Windows":
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x1
        FILE_SHARE_WRITE = 0x2
        OPEN_EXISTING = 3
        INVALID_HANDLE = ctypes.c_void_p(-1).value
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            device, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        if handle == INVALID_HANDLE:
            on_line(f"[verify] failed to open {device} for reading")
            return False
        try:
            buf_size = 4 * _CHUNK
            buf = ctypes.create_string_buffer(buf_size)
            read_dword = ctypes.c_ulong(0)
            while read_total < img_size:
                to_read = min(buf_size, img_size - read_total)
                # align to 512
                aligned = to_read + (512 - to_read % 512) % 512
                ok = kernel32.ReadFile(handle, buf, aligned, ctypes.byref(read_dword), None)
                if not ok or read_dword.value == 0:
                    break
                actual = min(read_dword.value, img_size - read_total)
                h.update(buf[:actual])
                read_total += actual
                if on_progress and img_size > 0:
                    on_progress(min(read_total / img_size, 1.0))
        finally:
            kernel32.CloseHandle(handle)
    else:
        # Linux/macOS: read the raw device file
        dev = device
        if system == "Darwin":
            dev = device.replace("/dev/disk", "/dev/rdisk")
        try:
            with open(dev, "rb") as f:
                while read_total < img_size:
                    to_read = min(_CHUNK, img_size - read_total)
                    data = f.read(to_read)
                    if not data:
                        break
                    h.update(data)
                    read_total += len(data)
                    if on_progress and img_size > 0:
                        on_progress(min(read_total / img_size, 1.0))
        except PermissionError:
            on_line("[verify] permission denied reading device — run as root/Administrator")
            return False

    dev_hash = h.hexdigest()
    on_line(f"[verify] device SHA-256: {dev_hash}")
    if img_hash == dev_hash:
        on_line("[verify] MATCH — write verified successfully")
        return True
    else:
        on_line(f"[verify] MISMATCH — image {img_hash} != device {dev_hash}")
        return False


# --------------------------------------------------------------------------- #
# High-level: download + decompress + write + verify pipeline
# --------------------------------------------------------------------------- #

def sd_cache_dir() -> str:
    """Return (and create) the temp cache directory for Pi image downloads."""
    d = os.path.join(tempfile.gettempdir(), "uf_sd_images")
    os.makedirs(d, exist_ok=True)
    return d


def flash_sd(profile_id: str, asset: Dict, device: str, on_line: Line,
             on_progress: Optional[Callable[[float], None]] = None,
             confirmed: bool = False, verify: bool = True) -> int:
    """Full pipeline: download -> decompress -> write -> verify. Returns 0 on success."""
    if not confirmed:
        raise ValueError("flash_sd requires confirmed=True — all data on the target will be destroyed")

    cache = sd_cache_dir()

    # download
    on_line(f"[sd] downloading {asset['name']}...")
    archive_path = download_image(asset["url"], cache, on_line, on_progress)

    # decompress
    on_line("[sd] decompressing image...")
    img_path = decompress(archive_path, cache, on_line, on_progress)

    # write
    on_line(f"[sd] writing to {device}...")
    rc = write_image(img_path, device, on_line, on_progress, confirmed=True)
    if rc != 0:
        on_line(f"[sd] write FAILED (exit {rc})")
        return rc

    # verify
    if verify:
        on_line("[sd] verifying write...")
        ok = verify_write(img_path, device, on_line, on_progress)
        if not ok:
            on_line("[sd] verification FAILED — the SD card may be corrupted")
            return 1
        on_line("[sd] verification passed")

    on_line("[sd] done — SD card is ready")
    return 0
