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
  * 'bruce'     — BruceDevices/firmware (per-board MERGED single .bin, flashed at 0x0; auto board->chip map).
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
import threading
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


# ── flash-size header sanity (FLASH-MERGED-4MB) ───────────────────────────────
# A merged-single-bin carries its own second-stage bootloader; that bootloader's ESP image
# header (magic 0xE9; byte 3 high-nibble = flash-size code) tells the ROM how large the SPI
# flash is. `write_flash --flash_size detect` only patches the header at the WRITE offset, NOT
# a bootloader sitting DEEPER inside a merged blob (0x1000 on classic ESP32), so a merged image
# built for a 16MB board writes+verifies fine yet BOOT-LOOPS a 4MB board with "Detected
# size(4096k) smaller than the size in the binary image header(16384k)". These helpers detect
# that mismatch so the flow can WARN honestly instead of reporting a clean "Flash complete".
# (Auto-patching the header to the real size is a separate, owner-gated change.)
_FLASH_SIZE_CODE_MB = {0x0: 1, 0x1: 2, 0x2: 4, 0x3: 8, 0x4: 16, 0x5: 32, 0x6: 64, 0x7: 128}
_DETECTED_FLASH_RE = re.compile(r"detected flash size:\s*(\d+)\s*MB", re.I)


def declared_flash_size_mb(image: bytes, chip: str) -> Optional[int]:
    """Flash size (MB) declared in a MERGED image's bootloader header, or None if not found.

    The bootloader sits at `_bootloader_offset(chip)` WITHIN the merged blob (0x1000 on classic
    ESP32/S2, 0x0 on S3 / most RISC-V parts, 0x2000 on the C5). A valid ESP image header there
    starts with magic byte 0xE9; the flash-size code is the high nibble of byte 3."""
    off = int(_bootloader_offset(chip), 16)
    if len(image) < off + 4 or image[off] != 0xE9:
        return None
    return _FLASH_SIZE_CODE_MB.get(image[off + 3] >> 4)


def parse_detected_flash_mb(line: str) -> Optional[int]:
    """Pull the board's real flash size (MB) from an esptool 'Detected flash size: 4MB' line."""
    m = _DETECTED_FLASH_RE.search(line or "")
    return int(m.group(1)) if m else None


def flash_size_mismatch_warning(declared_mb: Optional[int], detected_mb: Optional[int]) -> Optional[str]:
    """A warning when a merged image needs MORE flash than the board has (so it won't boot), else None."""
    if declared_mb and detected_mb and declared_mb > detected_mb:
        return (f"[warning] FLASH-SIZE MISMATCH: this firmware image was built for a {declared_mb}MB flash "
                f"but this board has only {detected_mb}MB. The write succeeds and verifies, but the board "
                f"will very likely NOT BOOT - it bootloops on 'Detected size({detected_mb * 1024}k) smaller "
                f"than the size in the binary image header({declared_mb * 1024}k)'. Reflash a build that "
                f"matches your board's {detected_mb}MB flash.")
    return None


# Engine-owned write_flash flags a profile's `extra_args` must NOT be able to override. `--flash_size`
# is safety-critical: the engine forces `--flash_size detect` to patch a merged image's header to the
# board's real size (the FLASH-MERGED-4MB safeguard), so a profile that slipped a fixed `--flash_size`
# into extra_args would silently re-open the wrong-size bootloop (esptool takes the LAST flag). Strip it.
_RESERVED_EXTRA_FLAGS = frozenset({"--flash_size", "--flash-size"})


def strip_reserved_extra_args(extra_args: List[str], on_line: Line) -> List[str]:
    """Drop engine-owned flags (currently --flash_size and its value) from a profile's extra_args,
    warning when one is present, so the engine's `--flash_size detect` safety patch always wins.
    Handles both the space form (`--flash_size 16MB`) and the equals form (`--flash_size=16MB`)."""
    out: List[str] = []
    skip_value = False
    for tok in extra_args:
        if skip_value:
            skip_value = False
            continue
        flag = str(tok).split("=", 1)[0].lower()
        if flag in _RESERVED_EXTRA_FLAGS:
            on_line(f"[warning] ignoring reserved flag {tok!r} in profile extra_args — the flasher "
                    f"controls --flash_size (detect) to prevent a wrong-size bootloop.")
            if "=" not in str(tok):
                skip_value = True  # also drop the following value token in the space form
            continue
        out.append(tok)
    return out


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
    # In a PyInstaller build sys.executable is CyberController.exe, NOT python — so `-m esptool` would
    # re-launch the GUI, which the single-instance mutex then aborts (GetLastError 183 -> exit 0). That
    # looked like a SILENT successful flash while no board was ever written. Route esptool through the
    # in-app dispatcher (see src/app.py main()'s `--_run-esptool` branch) so the BUNDLED esptool runs
    # in-process. In a normal (source) run sys.frozen is unset and `-m esptool` works as before.
    if getattr(sys, "frozen", False):
        return [sys.executable, "--_run-esptool", *args]
    return [sys.executable, "-m", "esptool", *args]


def esptool_available() -> bool:
    try:
        return subprocess.run(esptool_argv("version"), capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


# Supported esptool range. esptool 6 removed the underscore command/option aliases
# (write_flash, --flash_size, chip_id, flash_id) that the shared flash argv relies on; <4.7 predates
# chips we target (e.g. C5). The pyproject pin enforces this for managed installs, but a user's global
# env can still carry an out-of-range esptool — so we detect it and warn clearly instead of letting
# esptool fail with a cryptic argparse error mid-flash.
_SUPPORTED_ESPTOOL = "esptool>=4.7,<6"


def esptool_version() -> Optional[str]:
    """Installed esptool version string (e.g. '5.3.0'), or None if it can't be determined."""
    try:
        import importlib.metadata as _md
        return _md.version("esptool")
    except Exception:
        return None


def esptool_unsupported_reason() -> Optional[str]:
    """A human-readable reason if the installed esptool is outside the supported range, else None.

    Returns None when the version is unknown/unparseable — we don't block on something we can't read.
    """
    v = esptool_version()
    if not v:
        return None
    try:
        parts = v.split("+")[0].split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None
    if major >= 6:
        return (f"esptool {v} is unsupported — v6 removed the write_flash/--flash_size/chip_id aliases "
                f"this tool uses. Install a supported version:  pip install '{_SUPPORTED_ESPTOOL}'")
    # <4.7 predates chips we target (e.g. C5); the pin is >=4.7, so flag anything below it — not just <4.
    if (major, minor) < (4, 7):
        return (f"esptool {v} is too old for the chips this tool targets (needs >=4.7). Upgrade:  "
                f"pip install '{_SUPPORTED_ESPTOOL}'")
    return None


_ESPTOOL_VERSION_WARNED = False


def _warn_esptool_version_once(on_line: Line) -> None:
    """Emit the esptool-out-of-range warning at most once per process, on the flash log."""
    global _ESPTOOL_VERSION_WARNED
    if _ESPTOOL_VERSION_WARNED:
        return
    _ESPTOOL_VERSION_WARNED = True
    reason = esptool_unsupported_reason()
    if reason:
        on_line(f"[warn] {reason}")


# esptool's signatures for "the chip never entered download/bootloader mode". Seeing any of these on a
# failed run means it's almost always a reset-into-bootloader problem, not a bad binary — so we surface a
# concrete hold-BOOT hint (many CP210x/CH340 boards don't auto-reset into the ROM bootloader).
_DOWNLOAD_MODE_FAIL = (
    "failed to connect", "wrong boot mode detected", "no serial data received",
    "invalid head of packet",
)


def _run_stream(argv: List[str], on_line: Line) -> int:
    """Run a command, stream combined stdout/stderr line-by-line, return exit code.

    On any exception mid-stream (e.g. the UI callback raises because a dialog closed), the
    child is killed and reaped so it can't keep holding the serial port — otherwise the next
    flash fails with 'port busy'.

    On a failed esptool run whose output shows the chip never entered download mode, appends a
    concrete "hold BOOT" recovery hint — the single most common flashing snag on non-auto-reset boards.
    """
    # If this is an esptool invocation and the installed esptool is out of the supported range, say so
    # clearly up front (once) so a v6 argparse failure isn't a mystery. No-op for non-esptool argv.
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "esptool":
        _warn_esptool_version_once(on_line)
    on_line("$ " + " ".join(argv))
    saw_download_mode_fail = False
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError as e:
        on_line(f"[error] {e}")
        return 127
    try:
        for line in proc.stdout:                   # type: ignore[union-attr]
            text = line.rstrip("\n")
            if not saw_download_mode_fail:
                low = text.lower()
                if any(sig in low for sig in _DOWNLOAD_MODE_FAIL):
                    saw_download_mode_fail = True
            on_line(text)
        proc.wait()
    except Exception as e:
        on_line(f"[error] {e}")
        return -1
    finally:
        # Guarantee the child is killed+reaped (and stdout closed) on ANY exit path — including
        # KeyboardInterrupt / SystemExit (BaseException), which the `except Exception` above deliberately
        # does NOT catch. A still-running esptool child would otherwise keep holding the serial port and
        # the next flash fails with 'port busy'. No-op when the process already exited normally.
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
    on_line(f"[exit {proc.returncode}]")
    if proc.returncode not in (0, None) and saw_download_mode_fail:
        on_line("[hint] The board never entered download/bootloader mode. Boards with a CP210x or CH340 "
                "USB chip often don't auto-reset into the bootloader: HOLD the BOOT (a.k.a. IO0 / FLASH) "
                "button, tap EN/RST once, keep holding BOOT until 'Connecting…' succeeds, then release. "
                "If it still fails, lower the flash baud (Settings ▸ Flash) and use a data-capable USB "
                "cable — charge-only cables can't flash.")
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
                        ("ESP32-H2", "esp32h2"), ("ESP8266", "esp8266")):
        if token in text:
            return chip
    if re.search(r"\bESP32\b", text):
        return "esp32"
    return None


def _http_get(url: str) -> bytes:
    # SSRF guard: only https to an allowlisted GitHub host, and follow redirects ONLY to the
    # same allowlist (via _OPENER's redirect handler).
    _require_allowed_url(url)
    headers = dict(_UA)
    # Optional GitHub auth. Unauthenticated callers get 60 req/hr, so resolving /releases/latest for
    # several boards in a row (or several firmwares) can hit "HTTP 403: rate limit exceeded" and a flash
    # fails before it starts. When a token is present in the environment, attach it — authenticated calls
    # get 5000/hr. Sent ONLY to the api.github.com host (the API returns JSON with no cross-host redirect),
    # never to a redirected asset host, and never logged.
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
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


# --------------------------------------------------------------------------- #
# Concurrent-cache safety
# --------------------------------------------------------------------------- #
# cache_dir() is ONE process-wide directory and download_to/download_and_extract write
# DETERMINISTIC filenames, so two concurrent flashes of the SAME firmware resolve the identical
# dest path. FlashEngine permits parallel flashes on DIFFERENT ports and BatchFlasher.flash_parallel
# spawns one worker thread per job, so a naive `open(dest,"wb")` in thread B would TRUNCATE that
# file to 0 bytes while thread A's esptool child is mid-read of the same path — flashing a corrupt/
# empty image (for a full flash the shared bootloader/partitions get clobbered and the board is
# bricked) while esptool still exits 0 and the UI reports "Flash complete". We serialize per
# destination path and download ONCE per session: the first request downloads to a UNIQUE temp file
# and os.replace()s it atomically into place (no reader can exist yet, so the replace never targets
# an open handle), and every later request for that path REUSES the completed file instead of
# re-truncating a path another in-flight flash may currently be reading.
_cache_locks_guard = threading.Lock()
_cache_path_locks: Dict[str, threading.Lock] = {}
_downloaded_paths: Dict[str, str] = {}  # dest path -> the url it was downloaded from this session


def _path_lock(path: str) -> threading.Lock:
    """Return a stable per-path lock so concurrent downloads of the same dest serialize."""
    with _cache_locks_guard:
        lk = _cache_path_locks.get(path)
        if lk is None:
            lk = threading.Lock()
            _cache_path_locks[path] = lk
        return lk


def _atomic_write(cache_dir: str, dest: str, data: bytes) -> None:
    """Write `data` to `dest` atomically: to a unique temp file in the same dir, then os.replace().

    os.replace is atomic within a filesystem, so a reader either sees the old file or the complete
    new one — never a truncated/partial image. The temp file lives in `cache_dir` (same filesystem
    as `dest`) so the replace is a rename, not a cross-device copy.
    """
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".dl-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
    # Serialize on this dest so concurrent flashes of the same firmware never race on the shared
    # cache file, and reuse an already-downloaded copy instead of re-truncating a path another
    # in-flight flash may be reading. See the concurrent-cache note above.
    with _path_lock(dest):
        # Reuse the cached file only when THIS url produced it. The cache dir is flat and dest is the
        # sanitized asset BASENAME, so two releases that ship an identically-named asset (a new version
        # of the same firmware, or a different firmware's `firmware.bin`) map to the SAME dest. A
        # basename-only reuse would then serve the first download's bytes for the second — the wrong
        # firmware, with no sha256 gate on github_release profiles to catch it. Keying reuse on the url
        # re-downloads (overwrites) on a mismatch instead of flashing stale/wrong bytes.
        if _downloaded_paths.get(dest) == url and os.path.isfile(dest):
            on_line(f"[cache] reusing {safe} ({os.path.getsize(dest)} bytes)")
            return dest
        on_line(f"[download] {safe}")
        data = _http_get(url)
        _atomic_write(cache_dir, dest, data)
        _downloaded_paths[dest] = url
        on_line(f"[download] {len(data)} bytes -> {dest}")
        return dest


def download_and_extract(url: str, cache_dir: str, asset_name: str, member: str, on_line: Line) -> str:
    """Download a `.zip` release asset into `cache_dir` and extract `member`
    (e.g. "merged.bin"), returning the path to the extracted file.

    For firmwares that ship per-board ZIP bundles instead of a bare .bin (e.g.
    GhostESP). Path-traversal sink defense: both the saved zip name and the output
    name go through `_safe_cache_name`, and the wanted member is matched inside the
    zip by BASENAME (so a maliciously-nested zip entry can neither be selected by a
    traversal path nor written outside `cache_dir`).
    """
    import zipfile

    safe_zip = _safe_cache_name(asset_name)
    zip_path = os.path.join(cache_dir, safe_zip)
    real_dir = os.path.realpath(cache_dir)
    if (os.path.realpath(zip_path) != os.path.join(real_dir, safe_zip)
            and not os.path.realpath(zip_path).startswith(real_dir + os.sep)):
        raise ValueError(f"refusing zip dest that escapes the cache dir: {zip_path!r}")
    want = os.path.basename(member)
    out_path = os.path.join(cache_dir, _safe_cache_name(f"{os.path.splitext(safe_zip)[0]}_{want}"))
    # Serialize on the shared archive path so concurrent flashes never truncate the zip another
    # thread is reading, and reuse an already-extracted member instead of re-truncating out_path
    # (which a concurrent flash's esptool may be mid-read of). See the concurrent-cache note above.
    with _path_lock(zip_path):
        if _downloaded_paths.get(out_path) == url and os.path.isfile(out_path):
            on_line(f"[cache] reusing {os.path.basename(out_path)} "
                    f"({os.path.getsize(out_path)} bytes)")
            return out_path
        # Cache reuse: a chip-wide bundle (e.g. Meshtastic's 128 MB firmware-esp32s3-*.zip) holds
        # many boards' images — don't re-download it per board/member. But validate the cached file
        # is a REAL zip before trusting it: a download truncated mid-transfer leaves a nonzero file
        # that isn't a valid archive, and size>0 alone would reuse it and fail every extract until
        # the user clears the cache. zipfile.is_zipfile() rejects a missing/truncated EOCD.
        # Reuse a cached zip only if it's a valid archive AND — when it was downloaded THIS session —
        # it came from this url (so a same-name zip from a different release re-downloads instead of
        # being extracted as if it were the one requested). A zip cached in a prior session isn't
        # tracked here, so `.get(zip_path, url)` defaults to url and the persisted-cache reuse stands.
        if (os.path.isfile(zip_path) and os.path.getsize(zip_path) > 0
                and zipfile.is_zipfile(zip_path)
                and _downloaded_paths.get(zip_path, url) == url):
            on_line(f"[cache] reusing {safe_zip} ({os.path.getsize(zip_path)} bytes)")
        else:
            if os.path.isfile(zip_path):
                on_line(f"[cache] {safe_zip} is empty/corrupt — re-downloading")
            else:
                on_line(f"[download] {safe_zip}")
            data = _http_get(url)
            _atomic_write(cache_dir, zip_path, data)
            _downloaded_paths[zip_path] = url
            on_line(f"[download] {len(data)} bytes -> {zip_path}")

        with zipfile.ZipFile(zip_path) as z:
            target = next((n for n in z.namelist() if os.path.basename(n) == want), None)
            if target is None:
                raise ValueError(
                    f"zip {asset_name!r} has no member named {want!r} "
                    f"(has: {', '.join(z.namelist()[:8])})"
                )
            with z.open(target) as src:
                blob = src.read()
        _atomic_write(cache_dir, out_path, blob)
        _downloaded_paths[out_path] = url
        on_line(f"[extract] {want} ({os.path.getsize(out_path)} bytes) -> {out_path}")
        return out_path


def verify_sha256(path: str, expected: str, on_line: Line) -> None:
    """Verify the SHA-256 of `path` equals `expected` (hex). Raises ValueError on mismatch.

    Used to gate PINNED firmware (e.g. the BW16/RTL8720 bundle, whose third-party source has no
    upstream signature) so a tampered/changed image is rejected BEFORE it reaches the flasher.
    """
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got.lower() != (expected or "").lower():
        raise ValueError(
            f"SHA-256 mismatch for {os.path.basename(path)}: expected {expected}, got {got} "
            "(pinned firmware integrity check failed — refusing to flash)")
    on_line(f"[verify] {os.path.basename(path)} sha256 OK")


def cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "marauder_fw")
    os.makedirs(d, exist_ok=True)
    return d


def erase(port: str, chip: str, on_line: Line) -> int:
    return _run_stream(esptool_argv("--chip", chip, "--port", port, "erase_flash"), on_line)


def _github_latest(api_url: str) -> Tuple[str, List[Dict]]:
    """GET a GitHub /releases/latest API URL and return (tag, raw_assets_list)."""
    data = json.loads(_http_get(api_url).decode("utf-8"))
    if not isinstance(data, dict):
        # /releases (plural) returns a JSON list; a single release must be an object. Fail
        # honestly instead of crashing later with an opaque 'list' has no attribute 'get'.
        raise ValueError(
            f"expected a single release object but {api_url} returned a "
            f"{type(data).__name__}; the api_url likely points at /releases (a list) "
            "instead of /releases/latest or /releases/tags/<tag>"
        )
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
    #: Risk class for the firmware itself (not a per-command label). "" = normal;
    #: "lab-only" = RF transmit that must stay in an authorized lab; "illegal-tx" = a
    #: transmitter that is illegal to OPERATE (e.g. a jammer, FCC 47 U.S.C. 333). The flash
    #: UI surfaces this; CC still flashes it (label-never-block doctrine) but makes the
    #: legality unmissable. Most profiles leave it "".
    danger: str = ""

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
                     flash_freq: Optional[str] = None,
                     extra_args: Optional[List[str]] = None) -> int:
        """Write `support` (offset->path) plus the app image with esptool.

        mode 'app'  -> write only the application image (re-flash / update existing board)
        mode 'full' -> also write support files first (blank board); needs `support`
                       (a merged-single-bin profile never needs `support`).

        `extra_args` are extra esptool write_flash options a profile supplies (e.g.
        ``["--flash_mode", "dio"]``); they are appended to the write_flash argv.
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
        if extra_args:
            # A profile must not be able to override the engine's --flash_size detect safeguard.
            extra += strip_reserved_extra_args(list(extra_args), on_line)
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
    supports_suicide = True
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
                     flash_freq: Optional[str] = None,
                     extra_args: Optional[List[str]] = None) -> int:
        # DIV uses a chip-specific flash_freq (S3 80m / classic 40m); default it here.
        freq = flash_freq or _DIV_FLASH_FREQ.get(chip)
        return super().flash_assets(port, chip, app_path, on_line, mode=mode, baud=baud,
                                    support=support, app_offset=app_offset, flash_freq=freq,
                                    extra_args=extra_args)


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
# Bruce profile  (BruceDevices/firmware — per-board MERGED single .bin)
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

# Canonical repo is now BruceDevices/firmware (the old pr3y/Bruce was renamed; GitHub still
# 301-redirects it, but point at the live name directly — verified the Bruce-<env>.bin /
# Bruce-LAUNCHER_<env>.bin asset naming is identical, tag now "1.15"). Stays on api.github.com,
# so the SSRF allowlist + redirect handler are unaffected.
_BRUCE_API = "https://api.github.com/repos/BruceDevices/firmware/releases/latest"
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
    label = "Bruce"
    repo = "BruceDevices/firmware"
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
                    flash_freq: Optional[str] = None,
                    extra_args: Optional[List[str]] = None) -> int:
        """Flash local file(s). `support` (offset->path) is optional for a full flash; the
        app image goes at `app_offset` (0x0 for a merged blob, 0x10000 for app-only).
        `extra_args` are extra esptool write_flash options (forwarded to flash_assets)."""
        mode = "full" if support else "app"
        return self.flash_assets(port, chip, app_path, on_line, mode=mode, baud=baud,
                                 support=support, app_offset=app_offset, flash_freq=flash_freq,
                                 extra_args=extra_args)


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
            low = name.lower()
            # GhostESP ships per-board .zip bundles, each containing a flashable
            # merged.bin (bootloader+partitions+app at 0x0) alongside the split
            # bin/elf. Accept those; also accept a bare .bin if a release ever has one.
            is_zip = low.endswith(".zip")
            is_bin = low.endswith(".bin")
            if not (is_zip or is_bin):
                continue
            if is_bin and ("bootloader" in low or "partitions" in low or "boot_app0" in low):
                continue
            chip = _ghostesp_chip(name)
            base = name.rsplit(".", 1)[0].replace("GhostESP_", "").replace("GhostESP-", "")
            entry: Dict = {
                "name": name,
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": f"GhostESP {base}",
                "offset": "0x0",
                "merged": True,
            }
            if is_zip:
                # The engine downloads the zip and flashes the contained merged image.
                entry["zip_member"] = "merged.bin"
            assets.append(entry)
        return tag, assets

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        # Prefer a chip-generic build, then a devkit, then whatever is first. (A
        # board-specific build like LilyGo-TDisplayS3-Touch can be picked explicitly.)
        for a in cands:
            if "generic" in a["name"].lower():
                return a
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
            # The OTA update is an app-only image (it belongs to the running firmware's OTA path); it
            # must NOT be cold-flashed at 0x0 like the FULL merged image. Mirror halehound.json's
            # asset_match exclude_substrings — case-sensitive, both casings — so this oracle and the
            # GenericProfile stay equivalent (see tests/test_generic_equiv.py).
            if "OTA" in name or "ota" in name:
                continue
            label = "HaleHound CYD"
            if "FULL" in name.upper():
                label += " (merged full)"
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
# RTL8720DN / BW16 (AmebaD) — flashed by the 'rtl8720' backend, NOT esptool
# --------------------------------------------------------------------------- #

_RTL8720_BUNDLE_BASE = "https://raw.githubusercontent.com/vampel/vampel.github.io/main"
#: Realtek's AmebaD ImageTool flashes these three images at fixed offsets, plus the SRAM
#: loader. The Vampire Deauther serves them as raw files; we fetch them as one bundle and
#: hand the directory to rtl8720_backend.flash_ambd().
_RTL8720_BUNDLE_FILES = (
    "km0_boot_all.bin",
    "km4_boot_all.bin",
    "km0_km4_image2.bin",
    "imgtool_flashloader_amebad.bin",
)

#: SHA-256 of the EXACT bundle validated end-to-end on real BW16 hardware (2026-06). The firmware
#: comes from a third-party repo (vampel/vampel.github.io@main) with no upstream signature, and the
#: AmebaD ImageTool would happily checksum-verify + flash whatever bytes it is given — so we pin the
#: hashes here and reject (before flashing) any bundle that differs (repo compromise / MITM / an
#: upstream change we have not re-validated). If the upstream bundle is intentionally updated,
#: re-validate on hardware and update these hashes.
_RTL8720_BUNDLE_SHA256 = {
    "km0_boot_all.bin": "453c880307fc389009aa8a39c63da3d715b207ad797206c896cc691426daaa64",
    "km4_boot_all.bin": "05fbf808d43113eaf7c11b75091c986b817135f0c52eb9fa94bb9fe9f34b062c",
    "km0_km4_image2.bin": "6f6b12511d3f16e2dff3b136dcc6d6f5a6d48051d848ea5d3bbffe97c19e2e13",
    "imgtool_flashloader_amebad.bin": "9307121385cb390dfd2da64da2c6c515f17b5a9556b3d04021487c9b9f220b55",
}


class RtlAmeba8720Profile(FirmwareProfile):
    """BW16 / RTL8720DN (Realtek AmebaD), dual-band 2.4/5 GHz WiFi + BLE.

    NOT an Espressif chip — flashed by the ``rtl8720`` backend (Realtek's AmebaD ImageTool /
    rtltool), never esptool. Its "release" is a fixed AmebaD bundle (km0_boot + km4_boot +
    app image2) plus the SRAM loader, served as raw files by the Vampire Deauther repo. The
    engine's ``_flash_rtl8720`` downloads the whole bundle and drives the ImageTool.
    """

    id = "rtl8720"
    label = "BW16 RTL8720DN — Vampire Deauther (AmebaD)"
    repo = "vampel/vampel.github.io"
    supports_suicide = False
    image_model = IMAGE_MERGED  # nominal; the backend handles the real 3-file layout

    def latest_release(self) -> Tuple[str, List[Dict]]:
        assets = [{
            "name": n,
            "url": f"{_RTL8720_BUNDLE_BASE}/{n}",
            "chip": "rtl8720",
            "label": "BW16 Vampire Deauther bundle (dual-band 2.4/5 GHz)",
            "bundle": True,
            "sha256": _RTL8720_BUNDLE_SHA256[n],
        } for n in _RTL8720_BUNDLE_FILES]
        return ("vampire", assets)

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return [a for a in assets if a.get("chip") == "rtl8720"]

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        return None

    def app_offset(self, chip: str) -> str:
        return "0x0"


# --------------------------------------------------------------------------- #
# BlueJammer-V2  (EmenstaNougat/BlueJammer-V2) — a TWO-BOARD RF-research rig
# --------------------------------------------------------------------------- #
#
# *** LAB-ONLY / ILLEGAL TO OPERATE ***  BlueJammer-V2 is a 2.4 GHz jammer (Bluetooth / BLE /
# WiFi / RC). Cyber Controller adds NO jamming capability of its own: it FLASHES the precompiled
# image and reads the device's own telemetry for study. Operate/mode control is exposed only via a
# SEPARATE consent-gated surface (the Qt BlueJammer panel driving the device's web UI: arm/mode
# behind an RF-shielded-enclosure attestation + confirm, STOP ungated, and fail-safe — nothing is
# sent until a user-captured control map is loaded). OPERATING any mode transmits interference and
# is illegal under FCC / 47 U.S.C. §333 (and equivalent law elsewhere). Retained + flashable and
# labelled danger="illegal-tx" per the project's "label, never block" doctrine.
#
# Two boards (mirrors the esp32_div + rtl8720 patterns already in this module):
#   (1) ESP32-WROOM-32U "jamming engine" — esptool multi-image. Release ships the app + bootloader
#       + partitions, and NO boot_app0 (confirmed from the repo's RUN_THIS.bat):
#         bootloader@0x1000, partitions@0x8000, app@0x10000.
#   (2) BW16 / RTL8720DN "web controller / UART master" — the SAME AmebaD 3-file layout the
#       rtl8720 backend already flashes (km0_boot_all / km4_boot_all / km0_km4_image2), plus the
#       SRAM loader REUSED from the rtl8720 bundle (BlueJammer's release doesn't ship it).
#
# Firmware is CLOSED-SOURCE / precompiled (no redistribution granted) — so we fetch the pinned
# bins from the GitHub Release at flash time and NEVER vendor them into this repo.

_BLUEJAMMER_TAG = "v0.2"
_BLUEJAMMER_REL = f"https://github.com/EmenstaNougat/BlueJammer-V2/releases/download/{_BLUEJAMMER_TAG}"
_BJ_ESP_APP = "BlueJammer-V2.ino.bin"
_BJ_ESP_BOOTLOADER = "BlueJammer-V2.ino.bootloader.bin"
_BJ_ESP_PARTITIONS = "BlueJammer-V2.ino.partitions.bin"

#: SHA-256 of the v0.2 release bins (downloaded + recorded 2026-06). Closed-source firmware has no
#: upstream signature, so pinning is the only integrity guard — reject any byte that differs
#: (repo compromise / MITM / an upstream change we have not re-validated). NOTE: km0_boot_all /
#: km4_boot_all are byte-identical to the rtl8720 (Vampire) bundle's — standard AmebaD boot images;
#: only km0_km4_image2 (the app) differs.
_BLUEJAMMER_SHA256 = {
    _BJ_ESP_APP:        "6c77188ceb44a8a66126b87d51947403492d064f26e5596e21196626fd600a5b",
    _BJ_ESP_BOOTLOADER: "644de0067047e22380034b8989c39e5d2882f7538c698788866ca5130427322e",
    _BJ_ESP_PARTITIONS: "148b959cbff1c38aa8e1d5c0ba9d612c54997b945e56a63f41223eef650653a1",
    "km0_boot_all.bin":   "453c880307fc389009aa8a39c63da3d715b207ad797206c896cc691426daaa64",
    "km4_boot_all.bin":   "05fbf808d43113eaf7c11b75091c986b817135f0c52eb9fa94bb9fe9f34b062c",
    "km0_km4_image2.bin": "615c382d48b89e1faceeba0ac586538894a21d4eeb4dc0c64027fcb271a84ef9",
}


class BlueJammerEsp32Profile(FirmwareProfile):
    """BlueJammer-V2 ESP32 jamming engine. *** LAB-ONLY / ILLEGAL TO OPERATE (FCC §333). ***

    esptool multi-image: app@0x10000 + bootloader@0x1000 + partitions@0x8000, and deliberately
    NO boot_app0 @0xE000 (the upstream flasher omits it). Each bin is SHA-256-pinned. CC flashes
    and reads telemetry; any arm/mode control is a separate consent-gated surface (see the
    section header above), never an unguarded transmit.
    """

    id = "bluejammer-esp32"
    label = "BlueJammer-V2 — ESP32 engine [LAB-ONLY / illegal to operate]"
    repo = "EmenstaNougat/BlueJammer-V2"
    supports_suicide = False
    image_model = IMAGE_MULTI
    danger = "illegal-tx"

    def latest_release(self) -> Tuple[str, List[Dict]]:
        # Closed-source/precompiled: fetch the pinned release bins directly by name.
        return (_BLUEJAMMER_TAG, [{
            "name": _BJ_ESP_APP,
            "url": f"{_BLUEJAMMER_REL}/{_BJ_ESP_APP}",
            "chip": "esp32",
            "label": "BlueJammer-V2 ESP32 app image [LAB-ONLY / illegal to operate]",
            "offset": "0x10000",
            "merged": False,
            "sha256": _BLUEJAMMER_SHA256[_BJ_ESP_APP],
        }])

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        # Classic-ESP32-only firmware; surface it regardless of the detected chip.
        return list(assets)

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        boot = self._fetch_pinned(_BJ_ESP_BOOTLOADER, cache, on_line)
        part = self._fetch_pinned(_BJ_ESP_PARTITIONS, cache, on_line)
        # bootloader@0x1000, partitions@0x8000 — and intentionally NO boot_app0 @0xE000.
        return {"0x1000": boot, "0x8000": part}

    def app_offset(self, chip: str) -> str:
        return "0x10000"

    @staticmethod
    def _fetch_pinned(name: str, cache: str, on_line: Line) -> str:
        p = download_to(f"{_BLUEJAMMER_REL}/{name}", cache, name, on_line)
        verify_sha256(p, _BLUEJAMMER_SHA256[name], on_line)  # pinned integrity gate
        return p


class BlueJammerBw16Profile(FirmwareProfile):
    """BlueJammer-V2 BW16/RTL8720DN web controller + UART master.
    *** LAB-ONLY / ILLEGAL TO OPERATE (FCC §333). ***

    Same AmebaD 3-file layout as the rtl8720 (Vampire) profile — flashed by the ``rtl8720``
    backend. km0/km4/image2 come from the BlueJammer v0.2 release; the SRAM loader is reused from
    the rtl8720 bundle (BlueJammer doesn't ship it). Every file SHA-256-pinned. CC has no
    operate/transmit control over this board — control is its self-hosted web UI.
    """

    id = "bluejammer-bw16"
    label = "BlueJammer-V2 — BW16 controller [LAB-ONLY / illegal to operate]"
    repo = "EmenstaNougat/BlueJammer-V2"
    supports_suicide = False
    image_model = IMAGE_MERGED  # nominal; the rtl8720 backend handles the real 3-file layout
    danger = "illegal-tx"

    def latest_release(self) -> Tuple[str, List[Dict]]:
        assets = [{
            "name": n,
            "url": f"{_BLUEJAMMER_REL}/{n}",
            "chip": "rtl8720",
            "label": "BlueJammer-V2 BW16 bundle [LAB-ONLY / illegal to operate]",
            "bundle": True,
            "sha256": _BLUEJAMMER_SHA256[n],
        } for n in ("km0_boot_all.bin", "km4_boot_all.bin", "km0_km4_image2.bin")]
        # SRAM flash-loader: BlueJammer doesn't ship it — reuse the pinned rtl8720 one.
        loader = "imgtool_flashloader_amebad.bin"
        assets.append({
            "name": loader,
            "url": f"{_RTL8720_BUNDLE_BASE}/{loader}",
            "chip": "rtl8720",
            "label": "AmebaD SRAM loader (shared with rtl8720)",
            "bundle": True,
            "sha256": _RTL8720_BUNDLE_SHA256[loader],
        })
        return (_BLUEJAMMER_TAG, assets)

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return [a for a in assets if a.get("chip") == "rtl8720"]

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        return assets[0] if assets else None

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
    "heltec-v3": "esp32s3", "heltec-v4": "esp32s3", "heltec-v2": "esp32", "heltec-wsl-v3": "esp32s3",
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


# Meshtastic now ships per-CHIP zip bundles (firmware-<chip>-<ver>.zip), each containing every
# board's MERGED factory image (firmware-<board>-<ver>.factory.bin — flashed at 0x0) AND a separate
# app-only update image (firmware-<board>-<ver>.bin — flashed at 0x10000, NOT bootable at 0x0) plus
# bleota and per-board littlefs. We surface a curated board list per chip and extract the chosen
# board's .factory.bin from the chip zip. Minimal install = factory bin at 0x0 (Meshtastic formats its
# littlefs on first boot; bleota/littlefs are optional OTA/FS extras). The big chip zip is cached
# and reused across boards (see download_and_extract).
_MESHTASTIC_CHIP_ZIP = re.compile(r"^firmware-(esp32|esp32s2|esp32s3|esp32c3|esp32c6)-(.+)\.zip$")

# Common Meshtastic boards per ESP32 chip (pioEnv names). The esp32s3 list is verified against a
# real 2.7.15 bundle; covers the owned fleet (Heltec V3) + popular boards. Not exhaustive.
_MESHTASTIC_BOARDS: Dict[str, List[str]] = {
    "esp32s3": [
        "heltec-v3", "heltec-v4", "heltec-wireless-tracker", "heltec-wireless-tracker-v2",
        "heltec-wsl-v3", "tbeam-s3-core", "t-deck", "t-deck-pro", "t-watch-s3", "t-eth-elite",
        "seeed-xiao-s3", "station-g2", "station-g3", "tlora-t3s3-v1", "tlora-pager",
        "m5stack-cores3", "picomputer-s3", "unphone",
    ],
    # tbeam0_7 / heltec-v1 / heltec-v2_0 / heltec-v2_1 pruned 2026-07-10: verified ABSENT from the
    # 2.7.26 manifest, so they were advertised-but-unflashable (download_and_extract "no member").
    "esp32": [
        "tbeam", "tlora-v2-1-1_6", "tlora-v2-1-1_8", "rak11200", "station-g1", "nano-g1",
        "m5stack-coreink", "t-lora-v1",
    ],
    # esp32-c3-devkitm-1 / heltec-ht-ct62 were stale; the 2.7.26 manifest ships these two.
    "esp32c3": ["heltec-hru-3601", "heltec-ht62-esp32c3-sx1262"],
    # heltec-mesh-node-t114 is an nRF52840 (SX1262) board, NOT an ESP32-C6 — its Meshtastic image ships
    # as a .uf2 in the nrf52840 bundle, never as a .bin in an esp32c6 zip. Listing it here made it an
    # advertised-but-unflashable target (download_and_extract raised "zip has no member ..."). Flash
    # nRF52840 targets via the UF2/DFU path, not the esptool chip-zip expander.
    # Two REAL esp32c6 boards, verified as .bin members of firmware-esp32c6-*.zip.
    "esp32c6": ["m5stack-unitc6l", "tlora-c6"],
    "esp32s2": [],
}


class MeshtasticProfile(FirmwareProfile):
    id = "meshtastic"
    label = "Meshtastic (meshtastic)"
    repo = "meshtastic/firmware"
    supports_suicide = False
    image_model = IMAGE_MERGED

    def latest_release(self) -> Tuple[str, List[Dict]]:
        tag, raw = _github_latest(_MESHTASTIC_API)
        chip_zip: Dict[str, Tuple[str, str]] = {}  # chip -> (url, version token)
        for a in raw:
            m = _MESHTASTIC_CHIP_ZIP.match(a.get("name", ""))
            if m:
                chip_zip[m.group(1)] = (a.get("browser_download_url"), m.group(2))
        assets: List[Dict] = []
        for chip, (url, ver) in chip_zip.items():
            for board in _MESHTASTIC_BOARDS.get(chip, []):
                assets.append({
                    "name": f"meshtastic-{board}-{ver}",
                    "url": url,
                    "chip": chip,
                    "label": f"Meshtastic {board}",
                    "offset": "0x0",
                    "merged": True,
                    "zip_name": f"firmware-{chip}-{ver}.zip",   # shared chip-zip cache filename
                    "zip_member": f"firmware-{board}-{ver}.factory.bin",  # MERGED factory image (0x0); the app-only .bin would brick at 0x0
                })
        return tag, assets

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        return [a for a in assets if a.get("chip") == chip]

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        for a in cands:
            if "heltec-v3" in a["name"]:
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
# Sky-Spy profile  (colonelpanichacks/Sky-Spy — ESP32-S3 / ESP32-C5 (XIAO C5), drone RemoteID)
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
            low = name.lower()
            chip = "esp32s3" if "s3" in low else ("esp32c5" if "c5" in low else "esp32")
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
                     flash_freq: Optional[str] = None,
                     extra_args: Optional[List[str]] = None) -> int:
        # extra_args are esptool-only; qFlipper installs the whole package and ignores them.
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
                     flash_freq: Optional[str] = None,
                     extra_args: Optional[List[str]] = None) -> int:
        momentum = MomentumProfile()
        return momentum.flash_assets(port, chip, app_path, on_line, mode, baud, support,
                                     app_offset, flash_freq, extra_args)


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
# Generic JSON-driven profile  (Stage 1 hybrid model)
# --------------------------------------------------------------------------- #
# Reproduces the hardcoded FirmwareProfile subclasses above from a JSON config +
# a small named-resolver registry (modeled on os_catalog._RESOLVERS), so adding a
# firmware/board becomes "drop a JSON". The golden-locked base flash_assets() is
# REUSED UNCHANGED. See the internal flasher stage-1 design notes. The old
# subclasses remain the equivalence ORACLE (tests/test_generic_equiv.py) until the
# registry swap — GenericProfile is additive and not yet wired into PROFILES.

def _regex_flags(names: Optional[List[str]]) -> int:
    flags = 0
    for n in (names or []):
        flags |= int(getattr(re, n, 0))
    return flags


def _chip_from_spec(spec: Dict, name: str, match: Optional["re.Match"]) -> str:
    """Map an asset to a chip family from a chip_map spec (fixed | ordered fragments)."""
    if spec.get("strategy", "fixed") == "fixed":
        return spec["chip"]
    source = spec.get("source", "name")
    if source.startswith("regex_group:") and match is not None:
        hay = match.group(int(source.split(":", 1)[1])) or ""
    else:
        hay = name
    low = hay.lower()
    for frag, fam in spec.get("rules", []):
        if frag in low:
            return fam
    return spec.get("default", "esp32")


def _emit_label(emit: Dict, name: str, match: Optional["re.Match"]) -> str:
    # longest-substring lookup table (marauder _variant_label): pick the label of the LONGEST
    # label_map key that is a substring of the asset name, else the raw name.
    if emit.get("label_strategy") == "longest_substring":
        best_frag = None
        best_label = name
        for frag, lbl in (emit.get("label_map") or {}).items():
            if frag in name and (best_frag is None or len(frag) > len(best_frag)):
                best_frag, best_label = frag, lbl
        return best_label
    tmpl = emit.get("label_template")
    if not tmpl:
        return name
    env = ""
    grp = emit.get("label_group")
    if grp is not None and match is not None:
        env = match.group(grp) or ""
    label = tmpl.format(name=name, env=env)
    # first matching suffix rule (halehound: FULL -> " (merged full)", OTA -> " (OTA update)").
    for frag, suffix in emit.get("label_suffix_rules", []):
        if frag.upper() in name.upper():
            label += suffix
            break
    lg = emit.get("launcher_from_group")
    if lg is not None and match is not None and match.group(lg):
        label += emit.get("launcher_label_suffix", "")
    return label


def _resolve_github(cfg: Dict) -> Tuple[str, List[Dict]]:
    """github_release resolver — fetch the latest release and emit per-chip assets."""
    p = cfg["resolver_params"]
    am = p.get("asset_match", {})
    if not isinstance(am, dict):
        # A malformed profile (e.g. asset_match as a bare template string) must fail loudly,
        # not crash with an opaque 'str' has no attribute 'get' deeper in this function.
        raise ValueError(
            f"resolver_params.asset_match must be an object "
            f"(include_regex/include_suffixes), got {type(am).__name__}: {am!r}"
        )
    try:
        tag, raw = _github_latest(p["api_url"])
    except Exception:
        if p.get("on_error") == "source_only_empty":
            return ("source-only", [])
        raise
    if am.get("expand") == "chip_zip_boards":
        assets = _expand_chip_zip_boards(p, raw)
        # A profile can carry a SIBLING uf2 board family (Meshtastic nRF52840/RP2040/RP2350 flash
        # by drag-drop, not esptool). Emit those variants too, each tagged flash_method="uf2" so the
        # engine routes them to the uf2 backend instead of the profile's default esptool.
        if p.get("chip_uf2_boards"):
            assets = assets + _expand_chip_uf2_boards(p, raw)
        return tag, assets
    if am.get("expand") == "per_board_zip":
        return tag, _expand_per_board_zip(p, raw)
    inc_re = re.compile(am["include_regex"], _regex_flags(am.get("regex_flags"))) if am.get("include_regex") else None
    suffixes = am.get("include_suffixes", [".bin"])
    excludes = am.get("exclude_substrings", [])
    chip_map = p.get("chip_map", {"strategy": "fixed", "chip": "esp32"})
    emit = p.get("emit", {})
    assets: List[Dict] = []
    for a in raw:
        name = a.get("name", "")
        match = None
        if inc_re is not None:
            match = inc_re.match(name)
            if not match:
                continue
        elif not any(name.endswith(s) for s in suffixes):
            continue
        if any(x in name for x in excludes):
            continue
        asset: Dict = {
            "name": name,
            "url": a.get("browser_download_url"),
            "chip": _chip_from_spec(chip_map, name, match),
            "label": _emit_label(emit, name, match),
        }
        if emit.get("offset") is not None:
            asset["offset"] = emit["offset"]
        if "merged" in emit:   # only emit the key when the profile sets it (marauder omits it)
            asset["merged"] = bool(emit["merged"])
        if emit.get("zip_member") and name.endswith(".zip"):   # per-asset only (must-fix #5)
            asset["zip_member"] = emit["zip_member"]
        if emit.get("launcher_from_group") is not None:
            asset["launcher"] = bool(match.group(emit["launcher_from_group"])) if match else False
        if emit.get("flash_method"):
            asset["flash_method"] = emit["flash_method"]
        assets.append(asset)
    return tag, assets


def _expand_chip_zip_boards(p: Dict, raw: List[Dict]) -> List[Dict]:
    """Meshtastic-style: match chip-zip assets, cross-join with a curated board list."""
    czb = p["chip_zip_boards"]
    zip_re = re.compile(czb["zip_regex"])
    boards_by_chip = czb["boards_by_chip"]
    out: List[Dict] = []
    for a in raw:
        name = a.get("name", "")
        m = zip_re.match(name)
        if not m:
            continue
        chip = m.group(czb["chip_group"])
        version = m.group(czb["version_group"])
        for board in boards_by_chip.get(chip, []):
            out.append({
                "name": czb["asset_name_template"].format(board=board, chip=chip, version=version),
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": czb.get("label_template", "{board}").format(board=board, chip=chip, version=version),
                "offset": "0x0",
                "merged": True,
                "zip_name": czb["zip_name_template"].format(chip=chip, version=version),
                "zip_member": czb["member_template"].format(board=board, chip=chip, version=version),
            })
    return out


def _expand_chip_uf2_boards(p: Dict, raw: List[Dict]) -> List[Dict]:
    """UF2 sibling of _expand_chip_zip_boards (Meshtastic nRF52840 / RP2040 / RP2350).

    Those boards flash by dragging a ``.uf2`` onto the BOOT mass-storage volume, not via esptool.
    Cross-join the per-chip board lists with the matching ``firmware-{chip}-{ver}.zip`` and emit the
    ``.uf2`` member (NOT the ``.hex`` / ``-ota.zip`` DFU package the same zip also carries). No
    offset — UF2 self-addresses. Each asset is tagged ``flash_method="uf2"`` so the flash engine
    routes it to the uf2 backend regardless of the profile's default (esptool)."""
    cub = p["chip_uf2_boards"]
    zip_re = re.compile(cub["zip_regex"])
    boards_by_chip = cub["boards_by_chip"]
    member_tmpl = cub.get("member_template", "firmware-{board}-{version}.uf2")
    name_tmpl = cub.get("asset_name_template", "meshtastic-{board}-{version}")
    label_tmpl = cub.get("label_template", "{board}")
    zipname_tmpl = cub.get("zip_name_template", "firmware-{chip}-{version}.zip")
    out: List[Dict] = []
    for a in raw:
        name = a.get("name", "")
        m = zip_re.match(name)
        if not m:
            continue
        chip = m.group(cub.get("chip_group", 1))
        version = m.group(cub.get("version_group", 2))
        for board in boards_by_chip.get(chip, []):
            out.append({
                "name": name_tmpl.format(board=board, chip=chip, version=version),
                "url": a.get("browser_download_url"),
                "chip": chip,
                "label": label_tmpl.format(board=board, chip=chip, version=version),
                "zip_name": zipname_tmpl.format(chip=chip, version=version),
                "zip_member": member_tmpl.format(board=board, chip=chip, version=version),
                "flash_method": "uf2",
            })
    return out


def _expand_per_board_zip(p: Dict, raw: List[Dict]) -> List[Dict]:
    """RNode-style: each board ships as ONE .zip whose members flash to DISTINCT offsets.

    Unlike _expand_chip_zip_boards (one merged member @0x0 per board), here a single per-board
    zip carries the whole boot chain — bootloader / partitions / boot_app0 / app / an optional
    SPIFFS console_image — each at its own offset. Emit one variant per configured board that is
    actually present in the release; the app rides in `zip_member` (offset in `offset`) and the
    rest ride in `support_members` ([{member, offset, optional?}]) which the flash orchestration
    extracts from the SAME cached zip (see flash_engine). The bootloader offset is chip-dependent
    (`_bootloader_offset`: 0x0 S3/C-series, 0x1000 classic ESP32, 0x2000 C5)."""
    pbz = p["per_board_zip"]
    by_name = {a.get("name", ""): a for a in raw}
    offs = pbz.get("offsets", {})
    part_off = offs.get("partitions", "0x8000")
    boot_app0_off = offs.get("boot_app0", "0xe000")
    app_off = offs.get("app", "0x10000")
    sm_tmpl = pbz["support_member_templates"]
    console = pbz.get("console_image")
    out: List[Dict] = []
    for b in pbz["boards"]:
        board, chip = b["board"], b["chip"]
        zip_name = pbz["zip_template"].format(board=board)
        asset = by_name.get(zip_name)
        if asset is None:
            continue  # this board's zip isn't in the release — skip (defensive)
        support_members = [
            {"member": sm_tmpl["bootloader"].format(board=board), "offset": _bootloader_offset(chip)},
            {"member": sm_tmpl["partitions"].format(board=board), "offset": part_off},
            {"member": sm_tmpl["boot_app0"].format(board=board), "offset": boot_app0_off},
        ]
        if console:
            support_members.append(
                {"member": console["member"], "offset": console["offset"], "optional": True})
        out.append({
            "name": zip_name,
            "url": asset.get("browser_download_url"),
            "chip": chip,
            "label": b.get("name", board),
            "zip_member": pbz["app_member_template"].format(board=board),
            "offset": app_off,
            "support_members": support_members,
        })
    return out


def _pinned_url(cfg: Dict, source: str, name: str) -> str:
    base = cfg["resolver_params"]["url_sources"][source]
    url = f"{base.rstrip('/')}/{name}"
    # A pinned profile that still carries an unresolved ref placeholder (e.g. "<commit>" /
    # "<pinned-sha>") is STAGED, not finalized — its pinned commit/SHA were never filled in, so the
    # URL 404s and a flash would emit a bogus command (a "verify:" offset, a placeholder path). Fail
    # EARLY with a clear reason instead of a confusing mid-download 404. Real refs carry no "<...>"
    # markers, so this only trips on a genuinely-unfinalized pin. (Found by the pinned_release
    # staleness sweep: bluestress is parked to its own lane; nrf802154_sniffer is deferred to HW.)
    if "<" in url and ">" in url:
        raise ValueError(
            f"firmware profile {cfg.get('id', '?')!r} is STAGED — its pinned reference is not "
            f"finalized (unresolved placeholder in {url!r}); it is not flashable yet. The pinned "
            "commit/SHA are pending finalization.")
    return url


def _resolve_pinned(cfg: Dict) -> Tuple[str, List[Dict]]:
    """pinned_release resolver — no network discovery; assets come from config."""
    p = cfg["resolver_params"]
    tag = p.get("tag", "pinned")
    assets: List[Dict] = []
    for a in p.get("assets", []):
        asset: Dict = {
            "name": a["name"],
            "url": _pinned_url(cfg, a["source"], a["name"]),
            "chip": a["chip"],
            "label": a.get("label", a["name"]),
        }
        if a.get("offset") is not None:
            asset["offset"] = a["offset"]
        if "merged" in a:   # bundle assets (rtl8720 / bluejammer-bw16) omit the merged key
            asset["merged"] = bool(a["merged"])
        if a.get("bundle"):
            asset["bundle"] = True
        if a.get("sha256"):
            asset["sha256"] = a["sha256"]
        assets.append(asset)
    return tag, assets


def _resolve_local(cfg: Dict) -> Tuple[str, List[Dict]]:
    """local resolver — user supplies files via the bespoke flash_local surface."""
    return ("local", [])


def _fetch_tree(sf: Dict, rel_path: str, cache: str, dest_name: str, on_line: Line) -> str:
    """Fetch a support file from a repo raw tree, trying configured branches in order."""
    last_exc: Optional[Exception] = None
    for branch in sf.get("branches", ["main", "master"]):
        url = sf["raw_base"].format(branch=branch).rstrip("/") + "/" + rel_path.lstrip("/")
        try:
            return download_to(url, cache, dest_name, on_line)
        except Exception as exc:  # noqa: BLE001 — try the next branch
            last_exc = exc
            continue
    raise last_exc if last_exc else RuntimeError("no branches configured for support files")


_RESOLVERS = {
    "github_release": _resolve_github,
    "pinned_release": _resolve_pinned,
    "local": _resolve_local,
}


class GenericProfile(FirmwareProfile):
    """A FirmwareProfile built entirely from a JSON config (Stage 1 hybrid model).

    Reproduces latest_release / variants_for_chip / default_variant / support_files /
    app_offset from JSON + a named resolver. The base flash_assets() (golden-locked)
    is reused unchanged.
    """

    def __init__(self, cfg: Dict) -> None:
        self.cfg = cfg
        # canonical flash-core id (e.g. esp32_div -> esp32-div); falls back to the JSON id.
        self.id = cfg.get("core_id") or cfg["id"]
        self.label = cfg.get("label") or cfg.get("name") or cfg["id"]
        self.repo = cfg.get("repo")
        self.supports_suicide = bool(cfg.get("supports_suicide"))
        self.image_model = cfg.get("image_model", IMAGE_MULTI)
        self.danger = cfg.get("danger", "")
        self.backend = cfg.get("backend", "esptool")

    def latest_release(self) -> Tuple[str, List[Dict]]:
        return _RESOLVERS[self.cfg["resolver"]](self.cfg)

    def variants_for_chip(self, assets: List[Dict], chip: str) -> List[Dict]:
        mode = self.cfg.get("variants_for_chip", "by_chip")
        if mode == "all":
            return list(assets)
        same = [a for a in assets if a.get("chip") == chip]
        if same or mode == "by_chip":
            return same
        return list(assets)  # by_chip_else_all

    def default_variant(self, assets: List[Dict], chip: str) -> Optional[Dict]:
        cands = self.variants_for_chip(assets, chip)
        dv = self.cfg.get("default_variant", {"strategy": "first"})
        strat = dv.get("strategy", "first")
        if strat == "prefer_non_launcher":
            for a in cands:
                if not a.get("launcher"):
                    return a
        elif strat == "prefer_fragment":
            toks: List[str] = []
            by_chip = dv.get("by_chip", {})
            if by_chip.get(chip):
                toks.append(by_chip[chip])
            toks += dv.get("prefer", [])
            for t in toks:
                for a in cands:
                    if t.lower() in a.get("name", "").lower():
                        return a
        return cands[0] if cands else None

    def app_offset(self, chip: str) -> str:
        by_chip = self.cfg.get("app_offset_by_chip") or {}
        if by_chip.get(chip):
            return by_chip[chip]
        return self.cfg.get("app_offset") or ("0x0" if self.image_model == IMAGE_MERGED else "0x10000")

    def support_files(self, chip: str, cache: str, on_line: Line) -> Optional[Dict[str, str]]:
        sf = self.cfg.get("support_files")
        if not sf:
            return None
        if sf["source"] == "repo_tree":
            dirmap = sf.get("support_dir_by_chip")
            if dirmap is not None:
                d = dirmap.get(chip)
                if not d:
                    raise RuntimeError(
                        f"No auto support-file mapping for {chip}; use local files for a full flash.")
            else:
                d = ""
            out: Dict[str, str] = {}
            out[_bootloader_offset(chip)] = _fetch_tree(
                sf, sf["bootloader_path"].format(dir=d), cache, f"{self.id}_{chip}_bootloader.bin", on_line)
            out[sf["partitions_offset"]] = _fetch_tree(
                sf, sf["partitions_path"].format(dir=d), cache, f"{self.id}_{chip}_partitions.bin", on_line)
            if sf.get("include_boot_app0") and sf.get("boot_app0_path"):
                out[sf["boot_app0_offset"]] = _fetch_tree(
                    sf, sf["boot_app0_path"].format(dir=d), cache, f"{self.id}_boot_app0.bin", on_line)
            return out
        if sf["source"] == "pinned":
            out = {}
            for nm, meta in sf["pinned_files"].items():
                # Namespace the cache dest by profile id (as the repo_tree branch above already does) so two
                # profiles that pin a support file with the SAME basename (e.g. "bootloader.bin") don't
                # collide in the one shared cache dir. Without this, the second profile's download_to
                # reused the first's cached bytes — its sha256 then mismatched and the flash aborted with a
                # misleading "pinned firmware integrity check failed" tamper error for the rest of the
                # process lifetime. download_to keeps its deterministic same-name -> same-dest reuse; we
                # just make the name unique per profile.
                path = download_to(_pinned_url(self.cfg, meta["source"], nm), cache, f"{self.id}_{nm}", on_line)
                if sf.get("verify_sha256"):
                    verify_sha256(path, meta["sha256"], on_line)
                out[meta["offset"]] = path
            return out
        return None

    def flash_assets(self, port: str, chip: str, app_path: str, on_line: Line,
                     mode: str = "app", baud: int = 921600,
                     support: Optional[Dict[str, str]] = None,
                     app_offset: Optional[str] = None,
                     flash_freq: Optional[str] = None,
                     extra_args: Optional[List[str]] = None) -> int:
        # qFlipper-backed firmwares (momentum/unleashed) never use esptool.
        if self.backend == "qflipper":
            return self._flash_qflipper(app_path, on_line)
        # per-chip flash_freq (esp32-div: S3 80m / classic 40m) — default it here if unset.
        freq = flash_freq
        if freq is None:
            freq = (self.cfg.get("flash_freq_by_chip") or {}).get(chip)
        return super().flash_assets(port, chip, app_path, on_line, mode=mode, baud=baud,
                                    support=support, app_offset=app_offset, flash_freq=freq,
                                    extra_args=extra_args)

    def _flash_qflipper(self, app_path: str, on_line: Line) -> int:
        """Hand the downloaded Flipper package to a locally installed qFlipper (mirrors
        MomentumProfile/UnleashedProfile exactly)."""
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

    def flash_local(self, *args, **kwargs):
        """Local-file flash for the 'custom' profile — delegates to CustomLocalProfile."""
        return CustomLocalProfile().flash_local(*args, **kwargs)


def _validate_profile_urls(cfg: Dict) -> None:
    """SSRF defense-in-depth: every URL a profile JSON DECLARES (resolver api_url + pinned
    url_sources) must be an allowlisted https host, validated at LOAD so a malicious/third-party
    profile is rejected early. The runtime fetch chokepoint (_require_allowed_url inside _http_get/
    download_to) is the second, authoritative layer."""
    rp = cfg.get("resolver_params") or {}
    urls: List[str] = []
    if isinstance(rp.get("api_url"), str):
        urls.append(rp["api_url"])
    src = rp.get("url_sources")
    if isinstance(src, dict):
        urls.extend(v for v in src.values() if isinstance(v, str))
    for u in urls:
        _require_allowed_url(u)   # raises ValueError on non-https / non-allowlisted host


def build_generic_profile(cfg: Dict) -> "GenericProfile":
    """Construct a GenericProfile from a parsed profile JSON dict (validates its declared URLs)."""
    _validate_profile_urls(cfg)
    return GenericProfile(cfg)


# --------------------------------------------------------------------------- #
# Profile registry
# --------------------------------------------------------------------------- #

# _MARAUDER is retained for the legacy module-level marauder/suicide API + suicide-bundle
# reference below; the hardcoded classes also remain as the equivalence ORACLE
# (tests/test_generic_equiv.py).
_MARAUDER = MarauderProfile()

# Stage 1 SWAP: PROFILES are built from the hybrid JSON profiles (GenericProfile), not the
# hardcoded classes. Every GenericProfile was proven argv/release/variant-identical to its
# oracle class before this swap. Only the flash-core-backed profiles (everything in
# _PROFILE_FILES below — len(_PROFILE_FILES) of them) are built here; the sd/adb-only
# kali_arm/pwnagotchi/raspyjack/rayhunter carry no engine block and are excluded. They are
# keyed by canonical core id (cfg.core_id or cfg.id).
_PROFILE_FILES = (
    "marauder.json", "esp32_div.json", "bruce.json", "ghost_esp.json", "halehound.json",
    "rtl8720.json", "bluejammer_esp32.json", "bluejammer_bw16.json", "meshtastic.json",
    # nRF BlueNullifier 2 (wirebits, GPL-3.0) — 2.4 GHz nRF24L01 jammer, LAB-ONLY/illegal to operate,
    # flash-and-study only (no serial control surface). Prebuilt bins are committed in the repo tree
    # (no Release), so fetched pinned to commit 1dfc4d3 via raw.githubusercontent.com + SHA-256-verified
    # (bl 18992 / part 3072 / app 1127184 B). Real-hardware flash pending the Stage-5 gate.
    "nrf_bluenullifier2.json",
    "flock_you.json", "oui_spy.json", "sky_spy.json", "airtag_scanner.json", "cyt_ng.json",
    "minigotchi.json", "flipper_momentum.json", "flipper_unleashed.json", "custom.json",
    # RogueMaster — third Flipper CFW; identical qFlipper path to Momentum/Unleashed. Release assets
    # (.tgz/.zip) verified live against RogueMaster/flipperzero-firmware-wPlugins (2026-06-30, v0.420.0).
    "flipper_roguemaster.json",
    # m5stick-nemo — M5Stack multi-tool (esptool merged-single-bin @0x0). Assets verified live against
    # n0xa/m5stick-nemo v3.2.1 (README documents `write_flash -z 0x0`); mixed chips (StickCPlus2=esp32,
    # Cardputer/StickS3=esp32s3) mapped by the fragments chip_map.
    "m5stick_nemo.json",
    # New (2026-06-29 discovery): added via the hybrid model — drop-a-JSON, no code. Lawful, verified
    # releases; merged single-bin @ 0x0 web-flasher class; flash offset pending the Stage-5 hardware gate.
    "trex.json", "mclite.json", "bit_pirate.json",
    # MeshCore MAINLINE (meshcore-dev/MeshCore, MIT) — distinct from mclite (the laserir/MCLite fork CC
    # previously labeled "MeshCore"). esptool merged-single-bin @0x0, github_release pinned to the
    # companion role tag (companion-v1.16.0); multi-role/ble-usb selection is a deferred resolver
    # enhancement. Lawful LoRa mesh (danger ""). Offset 0x0 verify: until the Phase-F hardware gate.
    "meshcore.json",
    # WiFi Drone Remote-ID Detector (colonelpanichacks/drone-mesh-mapper, README-MIT) — passive ASTM-F3411
    # Remote-ID receiver, RX-only/danger "" (blue-team counter-surveillance). Prebuilt bins are IN-TREE
    # (firmware/, 0 releases) so pinned_release at commit efe96b6 via raw.githubusercontent.com. Ships the 4
    # pure-RX detector images (Xiao C3/S3, base + Meshtastic node-relay); DELIBERATELY EXCLUDES the repo's
    # esp32s3-dual-rid-apple-maps.bin (Apple-Find-My variant) pending an RX-only confirmation. Only merged app
    # bins exist (no separate bootloader/partitions) -> 0x0/merged; SHA verify: until the Phase-F hardware gate.
    "drone_mesh_mapper.json",
    # Nautilus (n0xa/Nautilus-Firmware, GPL-3.0) — sub-GHz CC1101 (300-928 MHz RX/TX) firmware for the LilyGo
    # T-Embed CC1101 (ESP32-S3). General-purpose dual-use RF tool, danger "" (same posture as same-author
    # m5stick_nemo and Bruce on this board — no jammer/deauth; operate-time risk is per-command, protocol:null
    # means no CC operate surface). github_release tracks latest; merged single-bin @0x0 (include_regex picks
    # nautilus.bin, not the 3-part set). v1.0.0 asset verified live. Offset 0x0 verify: until the Phase-F HW gate.
    "nautilus.json",
    # RNode (markqvist/RNode_Firmware, GPL-3.0) — Reticulum LoRa radio interface. Introduces the NEW
    # per_board_zip resolver mode: each board ships ONE .zip whose members flash to DISTINCT offsets
    # (bootloader chip-dependent / partitions 0x8000 / boot_app0 0xe000 / app 0x10000 / console_image
    # SPIFFS 0x210000 optional). The boot chain rides in the variant's support_members and is extracted
    # from the same cached zip by flash_engine (no support_files ABC change). Members verified live @ 1.86;
    # nRF boards excluded. danger "" (legit LoRa transport). Offsets verify: until the Phase-F HW gate.
    "rnode.json",
    # ESP32 Dual-Band Wardriver (justcallmekoko/ESP32DualBandWardriver, MIT) — passive 2.4+5 GHz WiGLE
    # logger on the ESP32-C5, danger "" (RX-only, no TX/deauth). pinned_release @ v2.3.0, multi-file:
    # bootloader@0x2000 (C5 gotcha) / partitions@0x8000 / app@0x10000, single-app (no boot_app0). SHA-256
    # digests are REAL (computed from the v2.3.0 assets) so verify_sha256 passes; offsets verify: until HW.
    "esp32_wardriver.json",
    # ESP32 BLE Collector (tobozo/ESP32-BLECollector, MIT) — passive BLE advert logger to SD, danger "".
    # APP-ONLY: upstream ships only compiled app images (esp-idf v4.4.4, verified via image-info), NO boot
    # chain -> flash_mode "app" writes app@0x10000 over the M5Stack factory bootloader (full-flash refuses,
    # no support files). REAL SHA-256 for all 5 board bins; needs a large-app partition scheme (caveat in note).
    "ble_collector.json",
    # Hydra32 / ESP32-Deauther — pinned 'Hydra32' release, multi-file ESP32 offsets verified from the
    # repo partitions.csv + SHA-256-pinned (authorized testing only; deauth gated by the safety layer).
    "hydra32.json",
    # esp8266_deauther (CC-12) — unlocks the esp8266 board class. 37 board-specific single merged .bin
    # images flashed at 0x0 via esptool --chip esp8266; assets verified live against
    # SpacehuhnTech/esp8266_deauther v2.6.1 (2026-07-02). Real-hardware flash pending the Stage-5 gate.
    "esp8266_deauther.json",
    # M5 pwn/pentest firmwares (Discord community asks). Both esptool merged-single-bin @0x0 (ESP32-S3).
    # Assets verified live 2026-07-03: Devsur11/M5Gotchi v0.7 (cardputer.bin + m5stick.bin, both S3) and
    # 0ct0sec/M5PORKCHOP v0.1.8b-PSTH (the *_m5burner.bin merged image). Flash offset pending Stage-5 HW gate.
    "m5gotchi.json", "porkchop.json",
    # ESP32 WiFi Penetration Tool (risinek) — classic-ESP32 WiFi attack/recon toolkit (deauth, PMKID + WPA
    # handshake capture over a web UI). Multi-file ESP-IDF release; offsets taken from the repo README's own
    # flash command (2026-07-08, v1.0): bootloader@0x1000, partition-table@0x8000, app@0x10000; SHA-256 pinned
    # from the downloaded assets (24016/3072/723248 B). Deauth gated/labelled by the safety layer, never operated.
    "esp32_wifi_pentest.json",
    # WiFiDuck (SpacehuhnTech) — Wi-Fi BadUSB / keystroke injection. Flashes the ESP8266 'Wi-Fi backpack' half
    # (web UI + Ducky-Script CLI) as a single merged .bin @0x0 via esptool --chip esp8266, same path as
    # esp8266_deauther. asset_match takes the two esp8266 board bins (dstike / malduino) and excludes the
    # atsamd21/atmega HID-companion assets; verified live against SpacehuhnTech/WiFiDuck v1.1.0 (2026-07-08).
    # Keystroke injection labelled/gated by the safety layer, never operated. Real-hw flash pending Stage-5 gate.
    "wifi_duck.json",
    # BlueStress (LxveLabs, in-house) — ESP32 + 1-2x nRF24L01 2.4 GHz/BLE RF-disruption device, LAB-ONLY /
    # illegal to operate on air (FCC 47 U.S.C. 333). Unlike the fire-on-boot upstream jammers it BOOTS IDLE
    # and exposes a real gated serial CLI, so CC flashes + drives a GATED Flood/Off surface (never a no-op).
    # Multi-file ESP32 offsets bl@0x1000 / part@0x8000 / app@0x10000, NO boot_app0; pinned_release +
    # SHA-256-verified. STAGED: LxveAce/BlueStress repo/build not yet published — placeholder commit + SHA-256
    # in the profile MUST be replaced with real build digests before any flash (verify_sha256 refuses a mismatch).
    "bluestress.json",
    # ESP-AT (Espressif) — official AT-command Wi-Fi/BT modem firmware; SAFE (no offensive TX).
    # Espressif ships the prebuilt bins as per-module ZIPs on download.espressif.com (NOT as GitHub
    # release assets), outside this module's GitHub-only SSRF allowlist — so the profile uses the
    # LOCAL resolver (same model as 'custom'): CC flashes the user-extracted factory image
    # (factory/factory_WROOM-32.bin), a merged single bin @0x0. Bench: ESP32-WROOM-32 on v2.4.0.
    "esp_at.json",
    # Round-2 firmware expansion (bundled into 1.7.0; offsets/SHAs verify: until real-HW confirm).
    # RNode Firmware (nRF52840) - markqvist/RNode_Firmware for RAK4631/T-Echo/Heltec-T114. Reticulum
    # LoRa transport (danger "" like the ESP32 rnode sibling; licensed-TX caveat in description).
    # FIRST shipped consumer of the built-but-unused nrf_dfu backend: per-board .zip = a whole
    # Nordic legacy-DFU 0.5 package fed to adafruit-nrfutil. nrf_dfu bench-gated -> HW gate matters.
    "rnode_nrf.json",
    # WHAD ButteRFly (nRF52840) - whad-team/butterfly multi-PHY (BLE/Zigbee/ESB/Unifying/Mosart).
    # TWO flash paths on two built-but-unused backends: Nordic PCA10059 via nrf_dfu (butterfly-
    # fwupgrade.zip), Makerdiary MDK via uf2 drag-drop (butterfly-mdk-fwupgrade.uf2). Inject/hijack
    # = active TX -> danger lab-only (labelled, never blocked). SHAs known but verify:.
    "whad_butterfly.json",
    # nRF Sniffer for 802.15.4 (nordicsemi, PCA10059 dongle) - passive Zigbee/Thread capture into
    # Wireshark (extcap). EXTENDS nrf_dfu: firmware is a RAW .hex (not a ready zip), so nrf_dfu now
    # runs `nrfutil pkg generate --hw-version 52 --sd-req 0x00 --application <hex>` first. Firmware
    # is in-tree (empty release assets) -> pinned_release + raw.githubusercontent at a pinned SHA.
    # RX-only, danger "". License NOASSERTION -> fetch-from-origin only. SHAs/commit verify:.
    "nrf802154_sniffer.json",
    # Sniffle (nccgroup, GPL-3.0) - BLE 4.x/5.x link-layer sniffer for TI CC13xx/CC26xx. FIRST
    # consumer of the NEW cc2538_bsl backend (cc2538-bsl over the TI ROM bootloader; Sonoff CC2652P
    # dongle primary). Intel-HEX (self-addressed) -> app_offset/baud verify:; danger lab-only
    # (active scan + connection-follow + relay/MITM). Per-asset SHA in the API but verify: until HW.
    "sniffle.json",
    # Z-Stack coordinator (Koenkk) - Zigbee 3.x coordinator/router for CC2652/
    # CC1352 dongles (Sonoff ZBDongle-E primary). 2nd cc2538_bsl consumer; per-board .zip wraps ONE
    # .hex (resolver unzips). danger "" (legit-protocol TX radio). app_offset/baud verify: until HW.
    "zstack_coordinator.json",
    # CatSniffer V3 (ElectronicCats, AGPL-3.0) - passive 802.15.4/Zigbee/Thread/BLE/sub-GHz sniffer.
    # Two chips: CC1352P7 radio (cc2538_bsl) + RP2040 bridge (uf2). asset_match = sniffer_fw;
    # exclude_regex drops the bundled airtag_spoofer (active TX). danger "". SHAs verify: until HW.
    "catsniffer.json",
    # PortaPack Mayhem (portapack-mayhem) — flagship HackRF+PortaPack SDR firmware. FIRST
    # consumer of the NEW hackrf_spiflash backend (hackrf_spiflash -R -w; .bin is a ZIP member
    # -> resolver extracts). danger illegal-tx (bundles EPIRB/SAME/P25/POCSAG TX apps; large
    # legit RX side) -- CC only FLASHES, authors no TX. Whole-flash @0x0. SHA in the zip but
    # verify: until a real HackRF flash.
    "mayhem.json",
    # LxveOS (LxveAce/lxveos, PRIVATE) — ESP-IDF security-panel OS with the LXVEOS/1 serial
    # bridge (protocol=lxveos parser already registered). Per-board <board_id>-merged.bin @0x0
    # from the rolling ci-latest PRERELEASE (github_release -> /releases/tags/ci-latest). esp32 +
    # esp32s3. Private repo => asset download needs an auth token until public. danger "" (arm-gated
    # offensive ops are per-command lab-only, no emitter). HW flash owner-gated until Stage-5.
    "lxveos.json",
)


def _load_profiles() -> Dict[str, FirmwareProfile]:
    import logging

    from src.core.resources import resource_path
    pdir = resource_path("src", "config", "profiles")
    out: Dict[str, FirmwareProfile] = {}
    for fname in _PROFILE_FILES:
        try:
            cfg = json.loads((pdir / fname).read_text(encoding="utf-8"))
            gp = build_generic_profile(cfg)   # validates declared URLs (SSRF) + JSON shape
        except Exception as exc:  # noqa: BLE001 — a bad/malicious profile must not brick the app
            logging.getLogger(__name__).warning("skipping invalid profile %s: %s", fname, exc)
            continue
        out[gp.id] = gp
    return out


PROFILES: Dict[str, FirmwareProfile] = _load_profiles()


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
        # The offset must not just be PRESENT but PARSE to an int: an unparseable "offset_hex"
        # (e.g. "0xZZ") or non-numeric "offset" would otherwise slip past this validator and only
        # blow up later in _bundle_offset at flash time (flash_suicide's uncaught ValueError).
        # Validate it here so the documented malformed-manifest ValueError contract holds and
        # _bundle_offset can never raise on a manifest this function returned. Twin of the
        # universal-flasher fc799d7 fix.
        try:
            _bundle_offset(entry)
        except (ValueError, TypeError) as e:
            raise ValueError(f'bundle.json "files"[{i}] has an unparseable offset: {e}')
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
