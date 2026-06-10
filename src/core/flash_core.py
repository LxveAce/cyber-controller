"""
Flash core — flash ESP32 firmware from inside the app.

Wraps `esptool` (subprocess, streamed) and pulls firmware straight from the official
GitHub release + the repo's FlashFiles/ tree, so you can flash a brand-new board or
update an existing one without leaving the GUI/TUI.

Ported faithfully from uf_core/flasher.py (universal-flasher) into cyber-controller as the
self-contained flash foundation (src.core.flash_core). It has NO intra-repo dependencies —
only the Python standard library plus esptool at runtime — so other modules can import its
public symbols (esptool_argv, _run_stream, detect_chip, download_to, erase, FirmwareProfile,
PROFILES, get_profile, list_profiles, flash_suicide, the SSRF helpers, etc.) directly.

Key facts baked in (verified against the v1.12.1 release):
  * Releases ship ONLY app .bins (board-specific). There is no generic "esp32" build —
    classic ESP32 dev boards use `_old_hardware` / `_lddb` / etc., S3 uses `_multiboardS3`.
  * bootloader / partitions / boot_app0 are NOT in the release — they live in FlashFiles/:
        MarauderV4/                 classic-ESP32 bootloader+partitions
        FlipperZeroMultiBoardS3/    S3 bootloader+partitions + the shared boot_app0.bin
        FlipperZeroDevBoard/        S2 bootloader+partitions
  * Flash offsets: partitions 0x8000, boot_app0 0xE000, app 0x10000 always.
    bootloader 0x1000 on classic ESP32 / S2, 0x0 on S3 / most C-series / H2,
    and 0x2000 on the ESP32-C5 (see _BOOTLOADER_OFFSET / _bootloader_offset below).

Suicide-bundle note (flash_suicide / read_bundle_manifest): this module only FLASHES a
bundle that the Suicide-Marauder repo's provisioner already built (bundle.json + .bins). It
does NOT burn eFuses and does NOT do any T2/secure-boot provisioning or password hashing —
that all happens in the Suicide-Marauder host provisioner, never here.

----------------------------------------------------------------------------------------
FIRMWARE-PROFILE REGISTRY (additive — does NOT change the Marauder or suicide flow)
----------------------------------------------------------------------------------------
On top of the original Marauder flasher, this module now exposes an extensible registry of
FirmwareProfile objects so the same esptool plumbing can flash other ESP32 firmwares:

  * 'marauder'  — ESP32Marauder (the original behavior, byte-for-byte; supports_suicide=True).
  * 'esp32-div' — cifertech/ESP32-DIV (ESP32-S3, multi-file image; app@0x10000 + boot chain).
  * 'bruce'     — pr3y/Bruce (per-board MERGED single .bin, flashed at 0x0; auto board->chip map).
  * 'custom'    — flash ANY local .bin(s) you provide, with chip-appropriate default offsets.

The original MODULE-LEVEL functions (latest_release, variants_for_chip, default_variant,
support_files, detect_chip, flash, erase, flash_suicide, cache_dir, download_to,
read_bundle_manifest) are preserved as BACK-COMPAT wrappers that delegate to the marauder
profile, so the existing GUI/TUI keep working unchanged.

NOTE on ESP32-DIV / Bruce: these are pen-test/RF firmwares that include RF-jamming features
which are ILLEGAL to operate. This module only FLASHES the stock images byte-for-byte; it
adds NO jamming functionality and enables nothing — it is plain firmware flashing.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

LATEST_API = "https://api.github.com/repos/justcallmekoko/ESP32Marauder/releases/latest"
RAW_BRANCHES = ("master", "main")
RAW_TMPL = "https://raw.githubusercontent.com/justcallmekoko/ESP32Marauder/{branch}/FlashFiles/{path}"
_UA = {"User-Agent": "headless-marauder-gui"}

# SSRF / redirect hardening: every firmware/release fetch must be HTTPS to a host we trust.
# A release-asset URL, an API response, or an HTTP redirect could otherwise point the
# downloader at an internal/metadata endpoint (169.254.169.254, localhost, a LAN service) or
# an attacker host. We pin the scheme to https and the host to GitHub's release/raw infra.
_ALLOWED_HOSTS = frozenset((
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
))
_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"   # e.g. objects-origin.githubusercontent.com


def _host_allowed(host: Optional[str]) -> bool:
    """True if `host` is an exact allowlisted GitHub host or a *.githubusercontent.com host."""
    if not host:
        return False
    h = host.lower()
    # Strip any userinfo / port that slipped through (urlsplit.hostname already does, but be safe).
    h = h.split("@")[-1].split(":")[0]
    return h in _ALLOWED_HOSTS or h.endswith(_ALLOWED_HOST_SUFFIX)


def _require_allowed_url(url: str) -> str:
    """Validate `url` is https:// to an allowlisted host; raise ValueError otherwise.

    Returns the url unchanged on success so it can be used inline.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("refusing empty/invalid download URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"refusing non-https URL scheme {parts.scheme!r}: {url!r}")
    if not _host_allowed(parts.hostname):
        raise ValueError(f"refusing URL to non-allowlisted host {parts.hostname!r}: {url!r}")
    return url


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any HTTP redirect that points off the allowlisted host set.

    GitHub release downloads legitimately 302 from github.com to
    objects.githubusercontent.com, so redirects are allowed — but ONLY to hosts that pass
    `_host_allowed` over https. A redirect to anything else (http://, an internal IP, a foreign
    host) raises HTTPError instead of being followed, closing the SSRF-via-redirect hole.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme.lower() != "https" or not _host_allowed(parts.hostname):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing redirect to non-allowlisted location: {newurl!r}",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# A module-level opener that enforces the redirect allowlist for every fetch in this module.
_OPENER = urllib.request.build_opener(_AllowlistRedirectHandler())

Line = Callable[[str], None]

# image-model markers
IMAGE_MERGED = "merged-single-bin"      # one .bin holds bootloader+partitions+app, flash at its offset
IMAGE_MULTI = "multi-file-offsets"      # app .bin only; needs separate bootloader/partitions/boot_app0

# bootloader sits at 0x0 on S3 and the RISC-V parts, 0x1000 on classic ESP32 / S2
_BOOTLOADER_0 = {"esp32s3", "esp32c2", "esp32c3", "esp32c6", "esp32c5", "esp32h2"}

# Per-chip bootloader flash offset override. The ESP32-C5 ROM expects the second-stage
# bootloader at 0x2000 — NOT 0x0 (S3 / most RISC-V parts) and NOT 0x1000 (classic ESP32 / S2).
# Flashing a C5 bootloader at 0x0 produces a board that never boots. `_bootloader_offset`
# consults this map first, then falls back to the _BOOTLOADER_0 (0x0 vs 0x1000) rule, so the
# C5 fix lives in exactly one place and every profile's support_files() routes through it.
_BOOTLOADER_OFFSET = {"esp32c5": "0x2000"}


def _bootloader_offset(chip: str) -> str:
    """Return the second-stage bootloader flash offset for a chip family.

    Order: explicit per-chip override (C5 -> 0x2000), then the _BOOTLOADER_0 rule
    (0x0 for S3 / most C-series / H2, 0x1000 for classic ESP32 / S2)."""
    if chip in _BOOTLOADER_OFFSET:
        return _BOOTLOADER_OFFSET[chip]
    return "0x0" if chip in _BOOTLOADER_0 else "0x1000"


# FlashFiles dir that holds bootloader+partitions for each chip family
_SUPPORT_DIR = {
    "esp32": "MarauderV4",
    "esp32s2": "FlipperZeroDevBoard",
    "esp32s3": "FlipperZeroMultiBoardS3",
}
_BOOT_APP0_PATH = "FlipperZeroMultiBoardS3/boot_app0.bin"
_BOOTLOADER_NAME = "esp32_marauder.ino.bootloader.bin"
_PARTITIONS_NAME = "esp32_marauder.ino.partitions.bin"

# Friendly labels for the release app variants (suffix -> description)
_VARIANT_LABELS = {
    "old_hardware": "Generic ESP32 / original v4 hardware (ILI9341)",
    "lddb": "Generic ESP32 dev board, no display (LDDB/NodeMCU/Wemos)",
    "v6": "Official Marauder v6", "v6_1": "Official Marauder v6.1",
    "v7": "Official Marauder v7", "v8": "Official Marauder v8",
    "kit": "Marauder Kit (Huzzah32)", "mini": "Marauder Mini",
    "mini_v3": "Marauder Mini v3 (ESP32-C5)",
    "marauder_dev_board_pro": "Dev Board Pro / BFFB (serial)",
    "multiboardS3": "Flipper MultiBoard / ESP32-S3",
    "flipper": "Flipper Zero WiFi Dev Board (ESP32-S2)",
    "rev_feather": "Rev Feather (ESP32-S2)",
    "m5cardputer": "M5Cardputer (ESP32-S3)", "m5cardputer_adv": "M5Cardputer Adv (ESP32-S3)",
    "m5stickc_plus": "M5StickC Plus", "m5stickc_plus2": "M5StickC Plus 2",
    "cyd_2432S028": "CYD 2.8\"", "cyd_2432S028_2usb": "CYD 2.8\" (2-USB)",
    "cyd_2432S024_guition": "CYD 2.4\" Guition", "cyd_3_5_inch": "CYD 3.5\"",
    "esp32c5devkitc1": "ESP32-C5 DevKitC-1",
}


def _chip_of_variant(name: str) -> str:
    n = name.lower()
    if "multiboards3" in n or "m5cardputer" in n:
        return "esp32s3"
    if "_flipper" in n or "rev_feather" in n:
        return "esp32s2"
    if "mini_v3" in n or "esp32c5devkitc1" in n:
        return "esp32c5"
    return "esp32"  # everything else (old_hardware, v6/7/8, kit, mini, lddb, cyd_*, m5stick...)


def _variant_label(name: str) -> str:
    # Match the most specific (longest) suffix so e.g. "esp32c5devkitc1" doesn't match "kit",
    # and "mini_v3" doesn't match "mini".
    best = ""
    for suffix in _VARIANT_LABELS:
        if suffix in name and len(suffix) > len(best):
            best = suffix
    return _VARIANT_LABELS[best] if best else name


# --------------------------------------------------------------------------- #
# esptool plumbing  (shared by every profile)
# --------------------------------------------------------------------------- #

def esptool_argv(*args: str) -> List[str]:
    return [sys.executable, "-m", "esptool", *args]


def esptool_available() -> bool:
    try:
        return subprocess.run(esptool_argv("version"), capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def _run_stream(argv: List[str], on_line: Line) -> int:
    """Run a command, stream combined stdout/stderr line-by-line, return exit code.

    On any exception mid-stream (e.g. the UI callback raises because a dialog closed), the
    child is killed and reaped so it can't keep holding the serial port — otherwise the next
    flash fails with 'port busy'.
    """
    on_line("$ " + " ".join(argv))
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError as e:
        on_line(f"[error] {e}")
        return 127
    try:
        for line in proc.stdout:                   # type: ignore[union-attr]
            on_line(line.rstrip("\n"))
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
    on_line(f"[exit {proc.returncode}]")
    return proc.returncode


def _detect_chip(port: str, on_line: Line) -> Optional[str]:
    """Return an esptool chip id ('esp32', 'esp32s3', ...) or None. (chip detection is
    firmware-agnostic, so every profile shares this implementation.)"""
    argv = esptool_argv("--port", port, "chip_id")
    out_lines: List[str] = []

    def cap(s: str):
        out_lines.append(s)
        on_line(s)

    _run_stream(argv, cap)
    text = "\n".join(out_lines)
    for token, chip in (("ESP32-S3", "esp32s3"), ("ESP32-S2", "esp32s2"),
                        ("ESP32-C6", "esp32c6"), ("ESP32-C5", "esp32c5"),
                        ("ESP32-C3", "esp32c3"), ("ESP32-C2", "esp32c2"),
                        ("ESP32-H2", "esp32h2")):
        if token in text:
            return chip
    if re.search(r"\bESP32\b", text):
        return "esp32"
    return None


def _http_get(url: str) -> bytes:
    # SSRF guard: only https to an allowlisted GitHub host, and follow redirects ONLY to the
    # same allowlist (via _OPENER's redirect handler).
    _require_allowed_url(url)
    req = urllib.request.Request(url, headers=_UA)
    with _OPENER.open(req, timeout=30) as r:
        return r.read()


def _safe_cache_name(name: str) -> str:
    """Validate a download-target *name* is a plain in-directory basename, or raise ValueError.

    Shared with the bundle path-traversal check (`_safe_bundle_join`): a release-asset name comes
    from a remote manifest/API and is attacker-influenced, so before it is joined onto a cache
    directory and opened we require it to be a bare basename — never empty/'.'/'..', a non-basename,
    absolute, drive/UNC-prefixed, or ".."-bearing (after normalizing both / and \\). This stops a
    hostile asset name (e.g. "..\\..\\evil.bin", "/abs/evil.bin", "C:\\evil.bin", "a/b.bin") from
    being written outside the cache dir. Returns the validated basename.
    """
    if not isinstance(name, str) or name in ("", ".", ".."):
        raise ValueError(f"refusing unsafe cache file name: {name!r}")
    if os.path.basename(name) != name:
        raise ValueError(f"refusing non-basename cache file name: {name!r}")
    if os.path.isabs(name):
        raise ValueError(f"refusing absolute cache file name: {name!r}")
    drive, _ = os.path.splitdrive(name)
    if drive:
        raise ValueError(f"refusing cache file name with drive/UNC prefix: {name!r}")
    # Normalize backslashes so a Windows-style "..\\.." or "a\\b" is caught on every platform.
    norm = name.replace(chr(92), "/")
    if ".." in norm.split("/") or "/" in norm:
        raise ValueError(f"refusing cache file name with path separator/'..': {name!r}")
    return name


def download_to(url: str, cache_dir: str, name: str, on_line: Line) -> str:
    """Download `url` into `cache_dir` under the sanitized basename `name`, returning the path.

    Path-traversal sink defense: `name` is an attacker-influenced GitHub release-asset name, and
    download_to itself builds + opens the destination, so the open() target is provably inside
    `cache_dir`. `_safe_cache_name` rejects any empty/'.'/'..', non-basename, absolute, drive/UNC,
    separator-bearing, or ".."-bearing name BEFORE the join, and we then assert the realpath of the
    final dest is contained in cache_dir as belt-and-suspenders (catches symlink/OS quirks).
    """
    safe = _safe_cache_name(name)
    dest = os.path.join(cache_dir, safe)
    # Defense-in-depth: confirm the path we are about to open() stays inside cache_dir.
    real_dir = os.path.realpath(cache_dir)
    real_dest = os.path.realpath(dest)
    if real_dest != os.path.join(real_dir, safe) and not real_dest.startswith(real_dir + os.sep):
        raise ValueError(f"refusing download dest that escapes the cache dir: {dest!r}")
    on_line(f"[download] {safe}")
    data = _http_get(url)
    with open(dest, "wb") as f:
        f.write(data)
    on_line(f"[download] {len(data)} bytes -> {dest}")
    return dest


def cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "marauder_fw")
    os.makedirs(d, exist_ok=True)
    return d


def erase(port: str, chip: str, on_line: Line) -> int:
    return _run_stream(esptool_argv("--chip", chip, "--port", port, "erase_flash"), on_line)


def _github_latest(api_url: str) -> Tuple[str, List[Dict]]:
    """GET a GitHub /releases/latest API URL and return (tag, raw_assets_list)."""
    data = json.loads(_http_get(api_url).decode("utf-8"))
    tag = data.get("tag_name", "latest")
    return tag, data.get("assets", [])


# --------------------------------------------------------------------------- #
# FirmwareProfile abstraction
# --------------------------------------------------------------------------- #

class FirmwareProfile:
    """Base class for a flashable firmware.

    Subclasses describe WHERE the firmware comes from and HOW its image is laid out; the
    actual esptool invocation is shared (see `flash_assets`). An asset dict is
    {name, url, chip, label} and may additionally carry {offset, merged:bool} when a profile
    needs to pin an explicit flash offset (e.g. a merged image at 0x0, or an app-only image
    at 0x10000).

    Attributes
    ----------
    id              short stable id used by get_profile() / list_profiles()
    label           human-friendly name
    repo            "owner/name" GitHub repo, or None for local-only profiles
    supports_suicide whether the Suicide-Marauder bundle flow applies (marauder only)
    image_model     IMAGE_MERGED or IMAGE_MULTI — whether the release is a single merged bin
    """

    id: str = "base"
    label: str = "Firmware"
    repo: Optional[str] = None
    supports_suicide: bool = False
    image_model: str = IMAGE_MULTI

    # ---- release / variant discovery ----
    def latest_release(self) -> Tuple[str, List[Dict]]:
        """Return (tag, [ {name, url, chip, label[, offset, merged]} ... ])."""
        raise NotImplementedError

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return [a for a in assets if a.get("chip") == chip]

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    # ---- support files (None when the release is a merged single image) ----
    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        """Return offset->path for bootloader/partitions/boot_app0, or None when the
        firmware ships a merged single image (nothing extra to fetch)."""
        return None

    # ---- the app-image offset for this profile/chip ----
    def app_offset(self, chip: str) -> str:
        """Where the app/merged image is written. Merged images go to 0x0; app-only at
        0x10000."""
        return "0x0" if self.image_model == IMAGE_MERGED else "0x10000"

    # ---- flashing (shared esptool invocation) ----
    def flash_assets(self, port: str, chip: str, app_path: str, on_line: Line,
                     mode: str = "app", baud: int = 921600,
                     support: Optional[Dict[str, str]] = None,
                     app_offset: Optional[str] = None,
                     flash_freq: Optional[str] = None) -> int:
        """Write `support` (offset->path) plus the app image with esptool.

        mode 'app'  -> write only the application image (re-flash / update existing board)
        mode 'full' -> also write support files first (blank board); needs `support`
                       (a merged-single-bin profile never needs `support`).
        """
        files: List[str] = []
        if mode == "full":
            if support:
                for off, path in support.items():
                    files += [off, path]
            elif self.image_model != IMAGE_MERGED:
                on_line("[error] full flash needs bootloader/partitions/boot_app0 (none provided)")
                return 2
        off = app_offset or self.app_offset(chip)
        files += [off, app_path]

        # --flash_size detect: auto-detect the chip's real flash size and patch the image
        # header. Without it esptool keeps the binary's header value (often 16MB), which
        # boot-loops a 4MB board with "Detected size(4096k) smaller than ... header(16384k)."
        extra: List[str] = []
        if flash_freq:
            extra += ["--flash_freq", flash_freq]
        argv = esptool_argv("--chip", chip, "--port", port, "--baud", str(baud),
                            "--before", "default_reset", "--after", "hard_reset",
                            "write_flash", "-z", "--flash_size", "detect", *extra, *files)
        return _run_stream(argv, on_line)


# --------------------------------------------------------------------------- #
# Marauder profile  (REPRODUCES the original module behavior EXACTLY)
# --------------------------------------------------------------------------- #

class MarauderProfile(FirmwareProfile):
    id = "marauder"
    label = "ESP32 Marauder (justcallmekoko)"
    repo = "justcallmekoko/ESP32Marauder"
    supports_suicide = True
    image_model = IMAGE_MULTI

    def latest_release(self) -> Tuple[str, List[Dict]]:
        """Return (tag, [ {name, url, chip, label} ... ]) for app .bin assets."""
        tag, raw = _github_latest(LATEST_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": _chip_of_variant(name),
                "label": _variant_label(name),
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        pref = {"esp32": "old_hardware", "esp32s3": "multiboardS3",
                "esp32s2": "flipper", "esp32c5": "esp32c5devkitc1"}.get(chip)
        cands = self.variants_for_chip(assets, chip)
        if pref:
            for a in cands:
                if pref in a["name"]:
                    return a
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        """Download bootloader/partitions/boot_app0 for a full flash. Returns offset->path."""
        sdir = _SUPPORT_DIR.get(chip)
        if not sdir:
            raise RuntimeError(f"No auto support-file mapping for {chip}; use local files for a full flash.")
        boot = _fetch_flashfile(f"{sdir}/{_BOOTLOADER_NAME}", os.path.join(cache, f"{chip}_bootloader.bin"), on_line)
        part = _fetch_flashfile(f"{sdir}/{_PARTITIONS_NAME}", os.path.join(cache, f"{chip}_partitions.bin"), on_line)
        bapp = _fetch_flashfile(_BOOT_APP0_PATH, os.path.join(cache, "boot_app0.bin"), on_line)
        bl_off = _bootloader_offset(chip)
        return {bl_off: boot, "0x8000": part, "0xe000": bapp}


def _fetch_flashfile(rel_path: str, dest: str, on_line: Line) -> str:
    # `dest` is a full path built from a hardcoded (non-attacker) name; download_to now takes
    # (cache_dir, name) and re-sanitizes the name, so split the trusted dest into its parts.
    cache_dir_, name = os.path.split(dest)
    last = None
    for branch in RAW_BRANCHES:
        url = RAW_TMPL.format(branch=branch, path=rel_path)
        try:
            return download_to(url, cache_dir_, name, on_line)
        except Exception as e:
            last = e
    raise RuntimeError(f"could not fetch {rel_path}: {last}")


# --------------------------------------------------------------------------- #
# ESP32-DIV profile  (cifertech/ESP32-DIV — ESP32-S3, multi-file image)
# --------------------------------------------------------------------------- #
#
# Releases ship ONLY the app image (e.g. ESP32-DIV-v1.6.0.bin, ~1.6 MB) which goes at
# 0x10000 — NOT a merged factory bin, so image_model is multi-file-offsets. The boot chain
# (bootloader / partitions / boot_app0) is NOT attached to releases; it lives in the repo
# tree under tools/esp32s3/ and tools/esp32-div-flasher/bundled/. We fetch those raw.
#
#   ESP32-S3 (DIV v2, current): bootloader@0x0,    partitions@0x8000, boot_app0@0xE000,
#                               app@0x10000, flash_mode dio, flash_freq 80m
#   classic ESP32 (DIV v1):     bootloader@0x1000, partitions@0x8000, boot_app0@0xE000,
#                               app@0x10000, flash_mode dio, flash_freq 40m
#
# This is plain firmware flashing — no jamming functionality is added or enabled.

_DIV_API = "https://api.github.com/repos/cifertech/ESP32-DIV/releases/latest"
_DIV_RAW_TMPL = "https://raw.githubusercontent.com/cifertech/ESP32-DIV/{branch}/{path}"
_DIV_BRANCHES = ("main", "master")
# boot-chain bins live under tools/ in the repo (S3 generation = DIV v2, recommended)
_DIV_BOOTLOADER = "tools/esp32s3/ESP32-DIV.ino.bootloader.bin"
_DIV_PARTITIONS = "tools/esp32s3/ESP32-DIV.ino.partitions.bin"
_DIV_BOOT_APP0 = "tools/esp32-div-flasher/bundled/boot_app0.bin"
_DIV_FLASH_FREQ = {"esp32s3": "80m", "esp32": "40m"}


class Esp32DivProfile(FirmwareProfile):
    id = "esp32-div"
    label = "ESP32-DIV (cifertech)"
    repo = "cifertech/ESP32-DIV"
    supports_suicide = False
    image_model = IMAGE_MULTI

    def latest_release(self) -> Tuple[str, List[Dict]]:
        """Return (tag, assets). Releases bundle the app .bin plus raw Arduino source files
        as separate assets; only the .bin assets are firmware. Each is the APP image
        (-> 0x10000). DIV v2 boards are ESP32-S3."""
        tag, raw = _github_latest(_DIV_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue   # skip .ino/.cpp/.h source assets
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": "esp32s3",          # current/recommended DIV generation
                "label": f"ESP32-DIV app image ({name})",
                "offset": "0x10000",        # release bin is the app image only
                "merged": False,
            })
        return tag, assets

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        # DIV releases are S3 app images; show them for any selected chip rather than hiding
        # everything when detection comes back as classic ESP32 on an older DIV v1 board.
        same = [a for a in assets if a.get("chip") == chip]
        return same if same else list(assets)

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        boot = _fetch_div_file(_DIV_BOOTLOADER, os.path.join(cache, f"div_{chip}_bootloader.bin"), on_line)
        part = _fetch_div_file(_DIV_PARTITIONS, os.path.join(cache, f"div_{chip}_partitions.bin"), on_line)
        bapp = _fetch_div_file(_DIV_BOOT_APP0, os.path.join(cache, "div_boot_app0.bin"), on_line)
        bl_off = _bootloader_offset(chip)
        return {bl_off: boot, "0x8000": part, "0xe000": bapp}

    def app_offset(self, chip: str) -> str:
        return "0x10000"

    def flash_assets(self, port: str, chip: str, app_path: str, on_line: Line,
                     mode: str = "app", baud: int = 921600,
                     support: Optional[Dict[str, str]] = None,
                     app_offset: Optional[str] = None,
                     flash_freq: Optional[str] = None) -> int:
        # DIV uses a chip-specific flash_freq (S3 80m / classic 40m); default it here.
        freq = flash_freq or _DIV_FLASH_FREQ.get(chip)
        return super().flash_assets(port, chip, app_path, on_line, mode=mode, baud=baud,
                                    support=support, app_offset=app_offset, flash_freq=freq)


def _fetch_div_file(rel_path: str, dest: str, on_line: Line) -> str:
    # `dest` is a full path built from a hardcoded (non-attacker) name; download_to now takes
    # (cache_dir, name) and re-sanitizes the name, so split the trusted dest into its parts.
    cache_dir_, name = os.path.split(dest)
    last = None
    for branch in _DIV_BRANCHES:
        url = _DIV_RAW_TMPL.format(branch=branch, path=rel_path)
        try:
            return download_to(url, cache_dir_, name, on_line)
        except Exception as e:
            last = e
    raise RuntimeError(f"could not fetch {rel_path}: {last}")


# --------------------------------------------------------------------------- #
# Bruce profile  (pr3y/Bruce — per-board MERGED single .bin)
# --------------------------------------------------------------------------- #
#
# Bruce auto-maps cleanly: each release ships one MERGED .bin per board, strictly named
# Bruce-<env>.bin (a single esptool merge-bin image with bootloader+partitions+app baked in,
# the chip-specific bootloader offset already inside it). So the flash command is always
# `write_flash 0x0 Bruce-<env>.bin` with --chip <family> for autodetect/verify. The only
# per-board variation is the chip family, which we derive from the env name. A parallel set
# of Bruce-LAUNCHER_<board>.bin assets is a separate loader variant — surfaced as its own
# label so a board picker keeps them distinct. Unknown/new boards fall through to chip
# 'esp32' and can also be flashed via the 'custom' local-bin profile.
#
# This is plain firmware flashing — no jamming functionality is added or enabled.

_BRUCE_API = "https://api.github.com/repos/pr3y/Bruce/releases/latest"
_BRUCE_RE = re.compile(r"^Bruce-(LAUNCHER_)?(.+)\.bin$", re.IGNORECASE)

# env-name fragments -> esptool chip family (derived from the CI build matrix). Order matters:
# the most specific fragments are tried first so e.g. "esp32-s3" wins over "esp32".
_BRUCE_FAMILY_HINTS: Tuple[Tuple[str, str], ...] = (
    ("esp32-s3", "esp32s3"), ("esp32s3", "esp32s3"), ("-s3", "esp32s3"),
    ("cardputer", "esp32s3"), ("sticks3", "esp32s3"), ("cores3", "esp32s3"),
    ("dinmeter", "esp32s3"), ("smoochiee", "esp32s3"), ("reaper", "esp32s3"),
    ("xk404", "esp32s3"), ("es3c28p", "esp32s3"),
    ("t-embed", "esp32s3"), ("t-deck", "esp32s3"), ("t-watch-s3", "esp32s3"),
    ("t-hmi", "esp32s3"), ("t-lora-pager", "esp32s3"), ("t-display-s3", "esp32s3"),
    ("esp32-c5", "esp32c5"), ("esp32c5", "esp32c5"), ("nm-cyd-c5", "esp32c5"),
    ("-c5", "esp32c5"),
    ("esp32-c6", "esp32c6"), ("esp32c6", "esp32c6"), ("nesso-n1", "esp32c6"),
    ("-c6", "esp32c6"),
)


def _bruce_family(env: str) -> str:
    """Map a Bruce env/board name to an esptool chip family. Defaults to classic 'esp32'
    (the largest CI bucket: CYD boards, M5Stack core/stick, Marauder boards, etc.)."""
    e = env.lower()
    for frag, fam in _BRUCE_FAMILY_HINTS:
        if frag in e:
            return fam
    return "esp32"


class BruceProfile(FirmwareProfile):
    id = "bruce"
    label = "Bruce (pr3y)"
    repo = "pr3y/Bruce"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        """Return (tag, assets). One MERGED .bin per board, Bruce-<env>.bin (flash @0x0).
        LAUNCHER_* assets are kept as a distinct, separate firmware variant."""
        tag, raw = _github_latest(_BRUCE_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            m = _BRUCE_RE.match(name)
            if not m:
                continue
            is_launcher = bool(m.group(1))
            env = m.group(2)
            fam = _bruce_family(env)
            label = f"Bruce {env}" + (" [LAUNCHER loader]" if is_launcher else "")
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": fam,
                "label": label,
                "offset": "0x0",       # merged image always flashes at 0x0
                "merged": True,
                "launcher": is_launcher,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        # prefer a non-launcher (main app) build for this chip family
        cands = self.variants_for_chip(assets, chip)
        for a in cands:
            if not a.get("launcher"):
                return a
        return cands[0] if cands else None

    # merged single image: nothing extra to fetch
    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Custom / local profile  (flash ANY local .bin(s) — the extensibility play)
# --------------------------------------------------------------------------- #
#
# No GitHub repo: the user points at local files. Two ways to use it:
#   * flash a single merged image at 0x0 (image_model treated as merged via default offset),
#   * or pass an explicit `support` map (offset->path) for a full multi-file flash and the
#     app image at its app_offset (default 0x10000 app-only, or 0x0 for a merged blob).
# Bruce-on-a-new-board, or any other ESP32 firmware you have a .bin for, can be flashed here.

class CustomLocalProfile(FirmwareProfile):
    id = "custom"
    label = "Custom / local .bin"
    repo = None
    supports_suicide = False
    image_model = IMAGE_MERGED   # a lone local .bin is treated as a merged image @0x0 by default

    def latest_release(self) -> Tuple[str, List[Dict]]:
        # No remote release for local files.
        return ("local", [])

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return list(assets)

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        # The caller supplies its own local support files; nothing to download.
        return None

    @staticmethod
    def local_asset(path: str, chip: Optional[str] = None,
                    offset: str = "0x0", merged: bool = True) -> Dict:
        """Build an asset dict for a local .bin (no download needed; flash_local uses path)."""
        return {
            "name": os.path.basename(path),
            "url": None,
            "path": path,
            "chip": chip or "esp32",
            "label": f"Local: {os.path.basename(path)}",
            "offset": offset,
            "merged": merged,
        }

    def flash_local(self, port: str, chip: str, app_path: str, on_line: Line,
                    app_offset: str = "0x0", baud: int = 921600,
                    support: Optional[Dict[str, str]] = None,
                    flash_freq: Optional[str] = None) -> int:
        """Flash local file(s). `support` (offset->path) is optional for a full flash; the
        app image goes at `app_offset` (0x0 for a merged blob, 0x10000 for app-only)."""
        mode = "full" if support else "app"
        return self.flash_assets(port, chip, app_path, on_line, mode=mode, baud=baud,
                                 support=support, app_offset=app_offset, flash_freq=flash_freq)


# --------------------------------------------------------------------------- #
# GhostESP profile  (GhostESP-Revival/GhostESP — ESP32-S3/C5/C6, multi-file)
# --------------------------------------------------------------------------- #
#
# GhostESP releases ship per-board .bin sets (bootloader + partitions + app) as
# individual assets. The app binary naming follows: GhostESP_<board>.bin. Board
# variants map to chip families via env name fragments (same approach as Bruce).
# Flash method: esptool with separate bootloader/partitions/app at standard offsets.

_GHOSTESP_API = "https://api.github.com/repos/GhostESP-Revival/GhostESP/releases/latest"

_GHOSTESP_BOARD_CHIPS: Dict[str, str] = {
    "ESP32-S3-DevKitC-1": "esp32s3",
    "ESP32-S3-Zero": "esp32s3",
    "Cardputer": "esp32s3",
    "CYD-2432S028": "esp32",
    "ESP32-C5-DevKitC-1": "esp32c5",
    "ESP32-C6-DevKitC-1": "esp32c6",
    "XIAO_ESP32_S3": "esp32s3",
    "LilyGo-T-Display-S3": "esp32s3",
    "Waveshare-ESP32-S3-Touch-LCD-1.28": "esp32s3",
}


def _ghostesp_chip(name: str) -> str:
    n = name.lower()
    for board, chip in _GHOSTESP_BOARD_CHIPS.items():
        if board.lower() in n:
            return chip
    if "s3" in n:
        return "esp32s3"
    if "c5" in n:
        return "esp32c5"
    if "c6" in n:
        return "esp32c6"
    return "esp32"


class GhostEspProfile(FirmwareProfile):
    id = "ghostesp"
    label = "GhostESP (GhostESP-Revival)"
    repo = "GhostESP-Revival/GhostESP"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_GHOSTESP_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            if "bootloader" in name.lower() or "partitions" in name.lower() or "boot_app0" in name.lower():
                continue
            chip = _ghostesp_chip(name)
            board = name.replace(".bin", "").replace("GhostESP_", "").replace("GhostESP-", "")
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"GhostESP {board}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        for a in cands:
            if "devkitc" in a["name"].lower():
                return a
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# HaleHound-CYD profile  (JesseCHale/HaleHound-CYD — ESP32, merged single bin)
# --------------------------------------------------------------------------- #
#
# Releases ship a single merged FULL .bin (HaleHound-CYD-FULL.bin) that includes
# bootloader+partitions+app, flashed at 0x0. Targets the CYD 2.8" (ESP32-2432S028R).

_HALEHOUND_API = "https://api.github.com/repos/JesseCHale/HaleHound-CYD/releases/latest"


class HaleHoundProfile(FirmwareProfile):
    id = "halehound"
    label = "HaleHound-CYD (JesseCHale)"
    repo = "JesseCHale/HaleHound-CYD"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_HALEHOUND_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            label = "HaleHound CYD"
            if "FULL" in name.upper():
                label += " (merged full)"
            elif "OTA" in name.upper():
                label += " (OTA update)"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": "esp32",
                "label": label,
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        for a in cands:
            if "FULL" in a["name"].upper():
                return a
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Meshtastic profile  (meshtastic/firmware — many boards, merged factory bins)
# --------------------------------------------------------------------------- #
#
# Meshtastic releases ship per-board factory .bin files (merged images) plus
# app-only update .bin files. Factory bins flash at 0x0. The naming convention is:
#   firmware-<board>-<version>.factory.bin   (merged, flash at 0x0)
#   firmware-<board>-<version>.bin           (app-only update, flash at 0x10000)
# We prefer the factory .bin for simplicity. Boards include heltec-v3, t-beam,
# xiao-esp32s3, rak4631, etc.

_MESHTASTIC_API = "https://api.github.com/repos/meshtastic/firmware/releases/latest"

_MESHTASTIC_CHIP_MAP: Dict[str, str] = {
    "heltec-v3": "esp32s3", "heltec-v2": "esp32", "heltec-wsl-v3": "esp32s3",
    "t-beam": "esp32", "t-beam-s3": "esp32s3",
    "t-deck": "esp32s3", "t-watch-s3": "esp32s3",
    "t-lora-v2": "esp32", "t-lora-v2-1-1.6": "esp32",
    "station-g1": "esp32", "station-g2": "esp32s3",
    "xiao-esp32s3": "esp32s3", "xiao-esp32c3": "esp32c3",
    "rak11200": "esp32", "nano-g1": "esp32",
    "pico-v1": "esp32s3", "picomputer-s3": "esp32s3",
    "tlora-t3s3-v1": "esp32s3",
    "wio-tracker-wm1110": "nrf52840",
    "rak4631": "nrf52840",
}


def _meshtastic_chip(board: str) -> str:
    b = board.lower()
    for key, chip in _MESHTASTIC_CHIP_MAP.items():
        if key in b:
            return chip
    if "s3" in b:
        return "esp32s3"
    if "c3" in b:
        return "esp32c3"
    if "c6" in b:
        return "esp32c6"
    if "nrf" in b or "rak" in b or "wio" in b:
        return "nrf52840"
    return "esp32"


class MeshtasticProfile(FirmwareProfile):
    id = "meshtastic"
    label = "Meshtastic (meshtastic)"
    repo = "meshtastic/firmware"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_MESHTASTIC_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin") and not name.endswith(".uf2"):
                continue
            is_factory = ".factory." in name or "-factory" in name
            is_uf2 = name.endswith(".uf2")
            board = name
            for prefix in ("firmware-", "meshtastic-"):
                if board.startswith(prefix):
                    board = board[len(prefix):]
            board = re.sub(r"-[\d.]+\.(factory\.)?bin$", "", board)
            board = re.sub(r"-[\d.]+\.uf2$", "", board)
            chip = _meshtastic_chip(board)
            label = f"Meshtastic {board}"
            if is_factory:
                label += " (factory)"
            elif is_uf2:
                label += " (UF2)"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": label,
                "offset": "0x0" if is_factory else "0x10000",
                "merged": is_factory,
                "factory": is_factory,
            })
        return tag, assets

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        same = [a for a in assets if a.get("chip") == chip]
        factory_first = sorted(same, key=lambda a: (not a.get("factory", False), a["name"]))
        return factory_first

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        for a in cands:
            if a.get("factory") and "heltec-v3" in a["name"].lower():
                return a
        for a in cands:
            if a.get("factory"):
                return a
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Flock-You profile  (colonelpanichacks/flock-you — ESP32, app-only bins)
# --------------------------------------------------------------------------- #
#
# Flock-You is typically built via PlatformIO. Releases (when available) ship
# app-only .bin files for ESP32 boards. Flash at 0x10000 with standard boot chain,
# or flash a merged factory bin at 0x0 if provided.

_FLOCKYOU_API = "https://api.github.com/repos/colonelpanichacks/flock-you/releases/latest"


class FlockYouProfile(FirmwareProfile):
    id = "flock-you"
    label = "Flock-You (colonelpanichacks)"
    repo = "colonelpanichacks/flock-you"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        try:
            tag, raw = _github_latest(_FLOCKYOU_API)
        except Exception:
            return ("source-only", [])
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            chip = "esp32s3" if "s3" in name.lower() else "esp32"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"Flock-You {name.replace('.bin', '')}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# OUI-Spy profile  (colonelpanichacks/oui-spy-unified-blue — ESP32-S3)
# --------------------------------------------------------------------------- #
#
# OUI-Spy Unified Blue targets the LILYGO T-Display S3 and XIAO ESP32-S3. Built
# via PlatformIO, releases ship compiled .bin files.

_OUISPY_API = "https://api.github.com/repos/colonelpanichacks/oui-spy-unified-blue/releases/latest"


class OuiSpyProfile(FirmwareProfile):
    id = "oui-spy"
    label = "OUI-Spy Unified Blue (colonelpanichacks)"
    repo = "colonelpanichacks/oui-spy-unified-blue"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        try:
            tag, raw = _github_latest(_OUISPY_API)
        except Exception:
            return ("source-only", [])
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            chip = "esp32s3"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"OUI-Spy {name.replace('.bin', '')}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Sky-Spy profile  (colonelpanichacks/Sky-Spy — ESP32-S3/WROOM-32, drone RemoteID)
# --------------------------------------------------------------------------- #

_SKYSPY_API = "https://api.github.com/repos/colonelpanichacks/Sky-Spy/releases/latest"


class SkySpyProfile(FirmwareProfile):
    id = "sky-spy"
    label = "Sky-Spy Drone RemoteID (colonelpanichacks)"
    repo = "colonelpanichacks/Sky-Spy"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        try:
            tag, raw = _github_latest(_SKYSPY_API)
        except Exception:
            return ("source-only", [])
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            chip = "esp32s3" if "s3" in name.lower() else "esp32"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"Sky-Spy {name.replace('.bin', '')}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# BLE AirTag Scanner profile  (MatthewKuKanich/ESP32-AirTag-Scanner)
# --------------------------------------------------------------------------- #

_AIRTAG_API = "https://api.github.com/repos/MatthewKuKanich/ESP32-AirTag-Scanner/releases/latest"


class AirTagScannerProfile(FirmwareProfile):
    id = "airtag-scanner"
    label = "ESP32 AirTag Scanner (MatthewKuKanich)"
    repo = "MatthewKuKanich/ESP32-AirTag-Scanner"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        try:
            tag, raw = _github_latest(_AIRTAG_API)
        except Exception:
            return ("source-only", [])
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            chip = "esp32s3" if "s3" in name.lower() else "esp32"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"AirTag Scanner {name.replace('.bin', '')}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Chasing Your Tail NG profile  (ArgeliusLabs/Chasing-Your-Tail-NG)
# --------------------------------------------------------------------------- #

_CYTNG_API = "https://api.github.com/repos/ArgeliusLabs/Chasing-Your-Tail-NG/releases/latest"


class CytNgProfile(FirmwareProfile):
    id = "cyt-ng"
    label = "Chasing Your Tail NG (ArgeliusLabs)"
    repo = "ArgeliusLabs/Chasing-Your-Tail-NG"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        try:
            tag, raw = _github_latest(_CYTNG_API)
        except Exception:
            return ("source-only", [])
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not name.endswith(".bin"):
                continue
            chip = "esp32"
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"CYT-NG {name.replace('.bin', '')}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Momentum firmware profile  (Next-Flip/Momentum-Firmware — Flipper Zero)
# --------------------------------------------------------------------------- #
#
# Flipper Zero firmware is flashed via qFlipper or USB DFU. The release assets are
# .tgz bundles. This profile handles download and delegates to qFlipper for actual
# flashing. flash_method is 'qflipper' (external tool invocation).

_MOMENTUM_API = "https://api.github.com/repos/Next-Flip/Momentum-Firmware/releases/latest"


class MomentumProfile(FirmwareProfile):
    id = "momentum"
    label = "Flipper Momentum (Next-Flip)"
    repo = "Next-Flip/Momentum-Firmware"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_MOMENTUM_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not (name.endswith(".tgz") or name.endswith(".tar.gz") or name.endswith(".zip")):
                continue
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": "flipper",
                "label": f"Momentum {name}",
                "offset": "0x0",
                "merged": True,
                "flash_method": "qflipper",
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return list(assets)

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def flash_assets(self, port: str, chip: str, app_path: str, on_line: Line,
                     mode: str = "app", baud: int = 921600,
                     support: Optional[Dict[str, str]] = None,
                     app_offset: Optional[str] = None,
                     flash_freq: Optional[str] = None) -> int:
        on_line("[info] Flipper Zero firmware requires qFlipper for flashing.")
        on_line("[info] Attempting to launch qFlipper with the downloaded firmware package...")
        qflipper = shutil.which("qFlipper") or shutil.which("qflipper")
        if not qflipper:
            for candidate in (
                r"C:\Program Files\qFlipper\qFlipper.exe",
                r"C:\Program Files (x86)\qFlipper\qFlipper.exe",
                "/usr/bin/qFlipper",
                "/usr/local/bin/qFlipper",
                "/Applications/qFlipper.app/Contents/MacOS/qFlipper",
            ):
                if os.path.isfile(candidate):
                    qflipper = candidate
                    break
        if not qflipper:
            on_line("[error] qFlipper not found. Install from https://flipperzero.one/update")
            on_line(f"[info] Firmware downloaded to: {app_path}")
            on_line("[info] Open qFlipper manually and install from file.")
            return 1
        on_line(f"[info] Found qFlipper at: {qflipper}")
        return _run_stream([qflipper, "--install", app_path], on_line)


# --------------------------------------------------------------------------- #
# Unleashed firmware profile  (DarkFlippers/unleashed-firmware — Flipper Zero)
# --------------------------------------------------------------------------- #

_UNLEASHED_API = "https://api.github.com/repos/DarkFlippers/unleashed-firmware/releases/latest"


class UnleashedProfile(FirmwareProfile):
    id = "unleashed"
    label = "Flipper Unleashed (DarkFlippers)"
    repo = "DarkFlippers/unleashed-firmware"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_UNLEASHED_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not (name.endswith(".tgz") or name.endswith(".tar.gz") or name.endswith(".zip")):
                continue
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": "flipper",
                "label": f"Unleashed {name}",
                "offset": "0x0",
                "merged": True,
                "flash_method": "qflipper",
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return list(assets)

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def flash_assets(self, port: str, chip: str, app_path: str, on_line: Line,
                     mode: str = "app", baud: int = 921600,
                     support: Optional[Dict[str, str]] = None,
                     app_offset: Optional[str] = None,
                     flash_freq: Optional[str] = None) -> int:
        momentum = MomentumProfile()
        return momentum.flash_assets(port, chip, app_path, on_line, mode, baud, support, app_offset, flash_freq)


# --------------------------------------------------------------------------- #
# MinigotchiV3 profile  (dj1ch/minigotchi-V3 — ESP32 Pwnagotchi clone)
# --------------------------------------------------------------------------- #
#
# ESP32 implementation of Pwnagotchi with WiFi frame manipulation and deauth
# capabilities. Releases ship per-board MERGED single .bin images (flash at 0x0).
# Supports ESP32 classic and ESP32-S3 boards (Cardputer, CYD, etc.).

_MINIGOTCHI_API = "https://api.github.com/repos/dj1ch/minigotchi-V3/releases/latest"
_MINIGOTCHI_RE = re.compile(r"\.bin$", re.IGNORECASE)

_MINIGOTCHI_CHIP_MAP = {
    "cardputer": "esp32s3", "m5cardputer": "esp32s3", "s3": "esp32s3",
    "cyd": "esp32", "esp32": "esp32", "wroom": "esp32",
}


class MinigotchiV3Profile(FirmwareProfile):
    id = "minigotchi-v3"
    label = "MinigotchiV3 (dj1ch)"
    repo = "dj1ch/minigotchi-V3"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_MINIGOTCHI_API)
        assets = []
        for a in raw:
            name = a.get("name", "")
            if not _MINIGOTCHI_RE.search(name):
                continue
            chip = "esp32"
            n = name.lower()
            for frag, c in _MINIGOTCHI_CHIP_MAP.items():
                if frag in n:
                    chip = c
                    break
            assets.append({
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"MinigotchiV3 {name}",
                "offset": "0x0",
                "merged": True,
            })
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else (assets[0] if assets else None)

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        same = [a for a in assets if a.get("chip") == chip]
        return same if same else list(assets)

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# Profile registry
# --------------------------------------------------------------------------- #

_MARAUDER = MarauderProfile()

PROFILES: Dict[str, FirmwareProfile] = {
    p.id: p for p in (
        _MARAUDER,
        Esp32DivProfile(),
        BruceProfile(),
        GhostEspProfile(),
        HaleHoundProfile(),
        MeshtasticProfile(),
        FlockYouProfile(),
        OuiSpyProfile(),
        SkySpyProfile(),
        AirTagScannerProfile(),
        CytNgProfile(),
        MinigotchiV3Profile(),
        MomentumProfile(),
        UnleashedProfile(),
        CustomLocalProfile(),
    )
}


def get_profile(profile_id: str) -> FirmwareProfile:
    """Return the FirmwareProfile for an id (raises KeyError on unknown id)."""
    return PROFILES[profile_id]


def list_profiles() -> List[Tuple[str, str]]:
    """Return [(id, label) ...] for every registered profile, in registry order."""
    return [(p.id, p.label) for p in PROFILES.values()]


# --------------------------------------------------------------------------- #
# BACK-COMPAT module-level API  (delegates to the marauder profile so the
# existing GUI/TUI keep working byte-for-byte)
# --------------------------------------------------------------------------- #

def latest_release() -> Tuple[str, List[Dict]]:
    """Marauder release assets (back-compat wrapper)."""
    return _MARAUDER.latest_release()


def variants_for_chip(assets: List[Dict], chip: str) -> List[Dict]:
    return _MARAUDER.variants_for_chip(assets, chip)


def default_variant(assets: List[Dict], chip: str) -> Optional[Dict]:
    return _MARAUDER.default_variant(assets, chip)


def support_files(chip: str, cache: str, on_line: Line) -> Dict[str, str]:
    """Download Marauder bootloader/partitions/boot_app0. Returns offset->path."""
    # marauder always returns a dict (raises if unmapped); keep the original return type.
    result = _MARAUDER.support_files(chip, cache, on_line)
    assert result is not None  # marauder never returns None
    return result


def detect_chip(port: str, on_line: Line) -> Optional[str]:
    """Return an esptool chip id ('esp32', 'esp32s3', ...) or None."""
    return _detect_chip(port, on_line)


def flash(port: str, chip: str, app_path: str, on_line: Line,
          mode: str = "app", baud: int = 921600,
          support: Optional[Dict[str, str]] = None) -> int:
    """
    Flash the Marauder app (back-compat wrapper, identical behavior to the original flash()).

    mode 'app'  -> write only the application at 0x10000 (re-flash / update existing board)
    mode 'full' -> write bootloader+partitions+boot_app0+app (blank board); needs `support`
    """
    return _MARAUDER.flash_assets(port, chip, app_path, on_line,
                                  mode=mode, baud=baud, support=support)


# --------------------------------------------------------------------------- #
# suicide bundle (flash a pre-provisioned Suicide-Marauder bundle)
# --------------------------------------------------------------------------- #

def _safe_bundle_join(bundle_dir: str, name: str) -> str:
    """Resolve a manifest file `name` to an absolute path INSIDE `bundle_dir`, or raise.

    Hardening (path-traversal defense): a bundle.json is data that may have been tampered
    with, so a manifest entry's file name must be a plain basename that lands inside the
    bundle dir. We reject anything that is not a bare basename, is absolute, carries a
    drive/UNC prefix, or walks up via "..", and then defensively confirm the realpath stays
    within the bundle dir (catches symlinks / OS-specific quirks). On any violation we raise
    ValueError so the caller NEVER hands a bad path to esptool.
    """
    # Plain-basename only (shared with the download-cache sink): reject empty/'.'/'..', a
    # non-basename, an absolute path, a drive/UNC prefix, or any separator/".." component.
    # Backslashes are normalized so a Windows-style "..\\.." is caught on every platform.
    # _safe_cache_name raises ValueError on any violation; re-raise with the manifest message.
    try:
        _safe_cache_name(name)
    except ValueError as e:
        raise ValueError(f"unsafe manifest file name: {e}")
    joined = os.path.join(bundle_dir, name)
    # Defense-in-depth: confirm the resolved path is contained in the resolved bundle dir.
    real_dir = os.path.realpath(bundle_dir)
    real_join = os.path.realpath(joined)
    prefix = real_dir + os.sep
    if real_join != real_dir and not real_join.startswith(prefix):
        raise ValueError(
            f"refusing manifest file name that escapes the bundle dir: {name!r}"
        )
    return joined


def _sha256_file(path: str) -> str:
    """Return the lowercase hex SHA-256 of a file's bytes (streamed, constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bundle_manifest(bundle_dir: str) -> Dict:
    """Parse <bundle_dir>/bundle.json and return the manifest dict.

    A bundle is produced by the Suicide-Marauder repo's host/provision.py: it's a directory
    holding bundle.json plus the .bin images. The manifest must carry a "files" list whose
    entries each name a file and an offset ("offset_hex" like "0x10000", or an int "offset").
    Each entry may also carry a "sha256" hex digest of the image bytes (newer bundles), which
    flash_suicide enforces before flashing.

    Each entry's file name is validated as a plain basename that resolves inside bundle_dir
    (path-traversal hardening): a non-basename / absolute / drive-or-UNC / ".."-bearing name is
    rejected with ValueError so a tampered manifest can never point the flasher at a file outside
    the bundle.

    Raises FileNotFoundError if bundle.json is missing, ValueError if it's malformed.
    eFuse/T2 provisioning is NOT described or performed here — see the module docstring.
    """
    path = os.path.join(bundle_dir, "bundle.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no bundle.json in {bundle_dir} (expected at {path})")
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not read bundle.json: {e}")
    if not isinstance(manifest, dict):
        raise ValueError("bundle.json must contain a JSON object")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError('bundle.json is missing a non-empty "files" list')
    for i, entry in enumerate(files):
        if not isinstance(entry, dict) or not entry.get("file"):
            raise ValueError(f'bundle.json "files"[{i}] must be an object with a "file" key')
        if entry.get("offset_hex") is None and entry.get("offset") is None:
            raise ValueError(f'bundle.json "files"[{i}] is missing an "offset_hex"/"offset"')
        # Reject path-traversal in the file name HERE, before any file is opened or esptool is
        # invoked. _safe_bundle_join raises ValueError on a non-basename / absolute / drive-or-
        # UNC / ".."-bearing / dir-escaping name.
        try:
            _safe_bundle_join(bundle_dir, entry["file"])
        except ValueError as e:
            raise ValueError(f'bundle.json "files"[{i}] has an unsafe file name: {e}')
    return manifest


def _bundle_offset(entry: Dict) -> int:
    """Resolve a manifest file entry's flash offset to an int (offset_hex wins, then offset)."""
    if entry.get("offset_hex") is not None:
        return int(str(entry["offset_hex"]), 16)
    return int(entry["offset"])


# Canonical schema string a Suicide-Marauder provisioner stamps into bundle.json. When a bundle
# declares this schema (or the active firmware profile supports the suicide flow), a missing/empty
# sha256 on a PRESENT file is a HARD ERROR — no TOFU warn-and-flash for an anti-forensic build.
_SUICIDE_SCHEMA = "suicide-marauder/bundle@1"


def _is_suicide_bundle(manifest: Dict, profile: Optional["FirmwareProfile"] = None) -> bool:
    """True when integrity MUST be enforced strictly (no missing-sha256 TOFU downgrade).

    A bundle is treated as a suicide bundle when its manifest declares the suicide schema
    (`schema`/`bundle_schema` == "suicide-marauder/bundle@1") OR the active firmware profile
    advertises `supports_suicide`. flash_suicide is the Marauder suicide entrypoint, so it defaults
    to the marauder profile (supports_suicide=True) — i.e. the strict path is the default here, and
    warn-and-flash survives only for an explicitly non-suicide/custom bundle.
    """
    schema = manifest.get("schema") or manifest.get("bundle_schema")
    if isinstance(schema, str) and schema.strip() == _SUICIDE_SCHEMA:
        return True
    if profile is not None and getattr(profile, "supports_suicide", False):
        return True
    return False


def flash_suicide(port: str, chip: str, bundle_dir: str, on_line: Line,
                  baud: int = 921600, profile: Optional["FirmwareProfile"] = None) -> int:
    """Flash a pre-provisioned Suicide-Marauder bundle in ONE esptool write_flash.

    Reads bundle.json, validates every listed .bin name is a safe in-bundle basename and exists
    (lists any that don't), verifies each image's SHA-256 against the manifest, warns if the
    manifest's chip disagrees with `chip`, copies each VERIFIED image into a fresh 0700 tempdir and
    re-hashes the staged copy (TOCTOU-safe: verify is atomic with flash), then writes the staged
    offset/path pairs (sorted by offset) in a single `write_flash -z --flash_size detect`. Mirrors
    flash() for reset/size handling. The staging dir is removed afterwards.

    Integrity policy:
      * SUICIDE bundle (manifest schema == "suicide-marauder/bundle@1", or the active profile
        supports the suicide flow — the default here): a MISSING/empty sha256 on a present file is a
        HARD ERROR (abort, rc 2). An anti-forensic build is NEVER flashed un-verified.
      * non-suicide / custom bundle: a missing sha256 warns and is allowed (TOFU, older bundles).
      * Any present sha256 is ENFORCED in BOTH cases.

    A path-traversal-unsafe manifest file name raises ValueError (esptool is never invoked); a
    sha256 mismatch / missing-required-sha256 aborts with rc 2 before any esptool call.

    `profile` defaults to the marauder profile (supports_suicide=True) for back-compat, so the
    existing call `flash_suicide(port, chip, bundle_dir, on_line, baud=baud)` keeps working and
    stays on the strict path.

    This NEVER burns eFuses and does NO T2/secure-boot provisioning — the Suicide-Marauder host
    provisioner does that; here we only flash an already-provisioned bundle. Returns the rc.
    """
    manifest = read_bundle_manifest(bundle_dir)
    strict = _is_suicide_bundle(manifest, profile if profile is not None else _MARAUDER)

    man_chip = manifest.get("chip")
    if man_chip and man_chip != chip:
        on_line(f"[WARNING] bundle chip is {man_chip!r} but flashing as {chip!r} "
                f"— flash will likely fail or brick; double-check the selected chip")

    # Resolve every entry to (offset, absolute path); collect any missing files first so we can
    # report them all at once instead of failing on the first one. Every file name is run through
    # _safe_bundle_join (path-traversal hardening) — a bad name raises ValueError, which we let
    # propagate so esptool is NEVER invoked on a tampered manifest. read_bundle_manifest already
    # validated the names, but we re-validate here so flash_suicide is safe even if a caller passes
    # a manifest it built itself.
    # Each tuple: (offset, src abs path, basename, expected-sha256-or-None).
    entries: List[Tuple[int, str, str, Optional[str]]] = []
    missing: List[str] = []
    for entry in manifest["files"]:
        name = entry["file"]
        abs_path = _safe_bundle_join(bundle_dir, name)
        if not os.path.isfile(abs_path):
            missing.append(name)
            continue
        entries.append((_bundle_offset(entry), abs_path, name, entry.get("sha256")))
    if missing:
        on_line("[error] bundle is missing file(s): " + ", ".join(missing))
        return 2

    # Integrity check (defense-in-depth vs a tampered bundle): recompute each PRESENT image's
    # SHA-256 and compare to the manifest. ABORT on mismatch so we never flash an image whose bytes
    # don't match what the provisioner recorded.
    #   * SUICIDE bundle: a missing/empty sha256 is a HARD ERROR (no TOFU downgrade for an
    #     anti-forensic build) — abort rc 2.
    #   * non-suicide bundle: a missing sha256 warns and is allowed (TOFU, older bundles).
    # A present sha256 is ENFORCED in both cases. Done before any esptool call.
    integrity_failed: List[str] = []
    missing_hash: List[str] = []
    for off, abs_path, name, expected in entries:
        if not expected:
            if strict:
                on_line(f"[error] suicide bundle entry {name!r} has NO sha256 — refusing to "
                        f"flash an anti-forensic build without integrity verification")
                missing_hash.append(name)
            else:
                on_line(f"[WARNING] bundle entry {name!r} has no sha256 (older non-suicide "
                        f"bundle); flashing WITHOUT integrity verification for this file (TOFU)")
            continue
        actual = _sha256_file(abs_path)
        if actual.lower() != str(expected).lower():
            on_line(f"[error] sha256 MISMATCH for {name!r}: "
                    f"manifest {str(expected).lower()} != actual {actual}")
            integrity_failed.append(name)
    if missing_hash:
        on_line("[error] aborting flash: suicide bundle requires a sha256 for every file; "
                "missing for: " + ", ".join(missing_hash)
                + " (re-provision with the current Suicide-Marauder provisioner)")
        return 2
    if integrity_failed:
        on_line("[error] aborting flash: integrity check failed for: "
                + ", ".join(integrity_failed)
                + " (bundle may be corrupt or tampered; re-provision and try again)")
        return 2

    # TOCTOU defense: between the hash above and esptool reading the file, the on-disk bytes could
    # be swapped. Copy each verified image into a fresh private (0700) staging dir, RE-hash the
    # staged copy against the manifest, and flash from the staged copies so verify is atomic with
    # flash. Any re-hash failure aborts (rc 2) before esptool runs. The staging dir is always
    # cleaned up.
    staging = tempfile.mkdtemp(prefix="suicide_stage_")
    try:
        try:
            os.chmod(staging, 0o700)   # no-op-ish on Windows, real on POSIX
        except OSError:
            pass
        pairs: List[Tuple[int, str]] = []
        restage_failed: List[str] = []
        for off, abs_path, name, expected in entries:
            # Prefix with the offset so two entries that share a basename (different flash offsets)
            # can't clobber each other's staged copy.
            staged = os.path.join(staging, f"0x{off:x}_{os.path.basename(name)}")
            shutil.copyfile(abs_path, staged)
            if expected:
                staged_hash = _sha256_file(staged)
                if staged_hash.lower() != str(expected).lower():
                    on_line(f"[error] staged-copy sha256 MISMATCH for {name!r} "
                            f"(file changed under us?): manifest {str(expected).lower()} "
                            f"!= staged {staged_hash}")
                    restage_failed.append(name)
            pairs.append((off, staged))
        if restage_failed:
            on_line("[error] aborting flash: staged-copy integrity check failed for: "
                    + ", ".join(restage_failed)
                    + " (bundle changed during staging; re-provision and try again)")
            return 2

        pairs.sort(key=lambda p: p[0])
        files: List[str] = []
        for off, path in pairs:
            files += [f"0x{off:x}", path]

        # --flash_size detect mirrors flash(): patch the image header to the board's real size so a
        # 4MB board doesn't boot-loop on an image whose header claims 16MB.
        argv = esptool_argv("--chip", chip, "--port", port, "--baud", str(baud),
                            "--before", "default_reset", "--after", "hard_reset",
                            "write_flash", "-z", "--flash_size", "detect", *files)
        return _run_stream(argv, on_line)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
