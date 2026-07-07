"""Tails OS (amnesiac live OS) USB flashing.

Same dead-man / amnesiac theme as the controller's anti-forensic line, different application: write
the official Tails USB image to a removable USB stick. This module owns the Tails-specific
VERIFICATION chain and reuses the hardened raw-image writer in
:mod:`src.core.backends.sd_backend` for the actual (destructive) device write — it does NOT
reimplement the dangerous write path.

Verification chain (https://tails.net/install/expert, /doc/about/openpgp_keys):
  * Since Tails 5.0 the fresh-install download is a USB IMAGE with a ``.img`` extension. An ``.iso``
    is the WRONG file (ISO = DVD/VM/upgrade only) — we refuse it.
  * Strongest: a detached OpenPGP signature verified against the Tails signing key
    (fingerprint A490 D0F4 D311 A415 3E2B B7CA DBB8 02B2 58AC D84F). Best-effort if ``gpg`` is present.
  * Always available: SHA-256 comparison against the official published checksum.
  * NEVER write an unverified image without an explicit warning.

Safety: the write goes through ``sd_backend.write_image`` which re-validates the target is REMOVABLE
and requires ``confirmed=True`` (the whole drive is erased).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Callable, Optional

import requests

from src.core.backends import sd_backend as sd

log = logging.getLogger(__name__)
Line = Callable[[str], None]

# Pin the Tails signing key fingerprint (re-verify cross-signatures out-of-band; keys can rotate).
TAILS_SIGNING_KEY_FINGERPRINT = "A490D0F4D311A4153E2BB7CADBB802B258ACD84F"

# Official Tails download hosts (SSRF allowlist for the best-effort metadata/image fetch).
_TAILS_HOSTS = frozenset(("tails.net", "download.tails.net", "tails.boum.org", "dl.amnesia.boum.org"))
_TAILS_HOST_SUFFIXES = (".tails.net", ".boum.org")
# Machine-readable "latest stable" feed used by the Tails installer (signed JSON).
_LATEST_FEED = "https://tails.net/install/v2/Tails/amd64/stable/latest.json"


def _host_allowed(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.lower().split("@")[-1].split(":")[0]
    return h in _TAILS_HOSTS or any(h.endswith(s) for s in _TAILS_HOST_SUFFIXES)


def _require_tails_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"refusing non-https Tails URL: {url!r}")
    if not _host_allowed(parts.hostname):
        raise ValueError(f"refusing Tails URL to non-allowlisted host {parts.hostname!r}")
    return url


# ── verification ─────────────────────────────────────────────────────

def verify_sha256(img_path: str, expected_sha256: str, on_line: Line,
                  on_progress: Optional[Callable[[float], None]] = None) -> bool:
    """True if *img_path*'s SHA-256 equals *expected_sha256* (case/space-insensitive)."""
    want = (expected_sha256 or "").strip().lower().replace(" ", "")
    if len(want) != 64 or not re.fullmatch(r"[0-9a-f]{64}", want):
        on_line(f"[tails] invalid expected SHA-256: {expected_sha256!r}")
        return False
    actual = sd.sha256_file(img_path, on_line, on_progress).lower()
    ok = hmac.compare_digest(actual, want)
    on_line("[tails] SHA-256 " + ("MATCH" if ok else f"MISMATCH (expected {want})"))
    return ok


def verify_gpg(img_path: str, sig_path: str, on_line: Line) -> Optional[bool]:
    """Verify a detached OpenPGP *sig_path* over *img_path* against the Tails signing key.

    Returns True (good sig from the pinned key), False (bad/foreign sig), or None if gpg is not
    available (caller should fall back to SHA-256). Best-effort: assumes the Tails key is already in
    the user's keyring (import + cross-signature trust is an out-of-band, documented step).
    """
    gpg = None
    for cand in ("gpg", "gpg2"):
        from shutil import which
        if which(cand):
            gpg = cand
            break
    if not gpg:
        on_line("[tails] gpg not found — skipping signature check (SHA-256 will be used instead).")
        return None
    try:
        proc = subprocess.run(
            [gpg, "--status-fd", "1", "--verify", sig_path, img_path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        on_line(f"[tails] gpg verify error: {exc}")
        return None
    status = proc.stdout + proc.stderr
    fpr = TAILS_SIGNING_KEY_FINGERPRINT
    good = ("VALIDSIG" in status or "GOODSIG" in status) and fpr in status.replace(" ", "")
    on_line("[tails] GPG signature " + ("VALID (Tails signing key)" if good else "NOT valid for the Tails key"))
    return good


# ── targets ──────────────────────────────────────────────────────────

def list_targets(on_line: Line) -> list:
    """Removable USB targets (reuses the hardened sd_backend detector)."""
    return sd.detect_sd_cards(on_line)


# ── best-effort metadata fetch ───────────────────────────────────────

def _fetch_feed_json(url: str, timeout: int = 30):
    """GET an allowlisted Tails metadata URL as JSON, following redirects MANUALLY and
    re-validating every hop with ``_require_tails_url`` (mirrors ``download_image``).

    requests' default redirect following is NOT used: a 302 on the feed host must not bounce the
    metadata fetch off-allowlist (SSRF), nor let the ``sha256`` it yields — the SOLE integrity
    anchor when gpg is unavailable — come from an attacker-chosen endpoint."""
    _require_tails_url(url)
    current = url
    for _ in range(8):
        resp = requests.get(current, timeout=timeout, allow_redirects=False)
        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                current = _require_tails_url(resp.headers.get("Location", ""))
                continue
            resp.raise_for_status()
            return resp.json()
        finally:
            resp.close()
    raise ValueError("too many redirects fetching the Tails feed")


def try_fetch_latest(on_line: Line) -> Optional[dict]:
    """Best-effort: fetch the official 'latest stable' feed → {version, url, sha256, size}.

    Returns None on any failure (network/schema). Callers should then fall back to a locally
    downloaded image (``--tails-image``). Parsed defensively (no brittle key paths): finds the first
    object carrying a ``.img`` url plus a sha256.
    """
    try:
        data = _fetch_feed_json(_LATEST_FEED)
    except (requests.RequestException, ValueError) as exc:
        on_line(f"[tails] could not fetch the latest-version feed ({exc}); use --tails-image with a "
                "manually downloaded + verified image.")
        return None

    found: dict = {}

    def walk(node):
        if isinstance(node, dict):
            url = node.get("url") or node.get("href")
            sha = node.get("sha256") or node.get("sha-256") or node.get("hash")
            if isinstance(url, str) and url.lower().endswith(".img") and isinstance(sha, str):
                found.setdefault("url", url)
                found.setdefault("sha256", sha)
                if node.get("size"):
                    found.setdefault("size", node.get("size"))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    ver = data.get("version") if isinstance(data, dict) else None
    if ver:
        found["version"] = ver
    if "url" in found and "sha256" in found:
        try:
            _require_tails_url(found["url"])
        except ValueError as exc:
            on_line(f"[tails] feed image URL rejected by allowlist ({exc}); use --tails-image.")
            return None
        on_line(f"[tails] latest: {found.get('version','?')}  sha256={found['sha256'][:16]}...")
        return found
    on_line("[tails] feed did not contain a recognizable .img + sha256; use --tails-image.")
    return None


def download_image(url: str, dest_dir: str, on_line: Line,
                   on_progress: Optional[Callable[[float], None]] = None) -> str:
    """Download a Tails .img from an allowlisted Tails host (redirects re-validated)."""
    _require_tails_url(url)
    os.makedirs(dest_dir, exist_ok=True)
    name = sd._safe_filename(url.rsplit("/", 1)[-1].split("?")[0])
    if not name.lower().endswith(".img"):
        raise ValueError("Tails fresh-install download must be a .img (an .iso is the wrong file).")
    dest = os.path.join(dest_dir, name)
    current = url
    for _ in range(8):
        resp = requests.get(current, stream=True, timeout=60, allow_redirects=False)
        # try/finally: the streamed socket is released deterministically even when the
        # redirect-allowlist check or raise_for_status raises (mirrors firmware_vault
        # ._safe_streamed_download) — never leak the connection to GC finalization.
        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                current = _require_tails_url(resp.headers.get("Location", ""))
                continue
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress and total:
                        on_progress(min(written / total, 1.0))
            on_line(f"[tails] downloaded {written} bytes -> {dest}")
            return dest
        finally:
            resp.close()
    raise ValueError("too many redirects fetching the Tails image")


# ── flash pipeline ───────────────────────────────────────────────────

def flash_local_image(img_path: str, device: str, on_line: Line,
                      on_progress: Optional[Callable[[float], None]] = None,
                      expected_sha256: Optional[str] = None, sig_path: Optional[str] = None,
                      confirmed: bool = False) -> int:
    """Verify then write a local Tails *img_path* to a removable *device*. Returns 0 on success.

    Verification precedence: a detached signature (if ``sig_path`` + gpg) OR the published SHA-256
    (``expected_sha256``). If neither is available the image is written only after an explicit
    UNVERIFIED warning. The write itself goes through ``sd_backend.write_image`` (removable-only,
    ``confirmed=True`` required — the whole drive is erased)."""
    if not confirmed:
        raise ValueError("flash requires confirmed=True — the entire target USB will be erased")
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"Tails image not found: {img_path}")
    if not img_path.lower().endswith(".img"):
        raise ValueError("Tails fresh-install image must be a .img — an .iso is the WRONG file "
                         "(ISO is for DVD/VM/upgrades). Download the USB image (.img) from tails.net.")

    verified = False
    if sig_path:
        result = verify_gpg(img_path, sig_path, on_line)
        if result is True:
            verified = True
        elif result is False:
            raise ValueError("GPG signature is NOT valid for the Tails signing key — refusing to write.")
        # result None -> gpg unavailable; fall through to SHA-256
    if not verified and expected_sha256:
        if not verify_sha256(img_path, expected_sha256, on_line, on_progress):
            raise ValueError("SHA-256 does not match the expected checksum — refusing to write an "
                             "unverified Tails image.")
        verified = True
    if not verified:
        on_line("[tails] WARNING: image is UNVERIFIED (no valid signature or --tails-sha256 given). "
                "Strongly verify against the official Tails checksum/signature before writing.")

    rc = sd.write_image(img_path, device, on_line, on_progress, confirmed=True)
    if rc != 0:
        on_line(f"[tails] write FAILED (exit {rc})")
        return rc
    on_line("[tails] verifying write (read-back)...")
    if sd.verify_write(img_path, device, on_line, on_progress):
        on_line("[tails] done — Tails USB is ready. Boot the target machine from this USB.")
        return 0
    on_line("[tails] read-back verification FAILED — the USB may be bad; re-flash.")
    return 1


def run_flash_cli(target=None, image=None, sha256=None, sig=None, assume_yes=False) -> int:
    """Interactive CLI for ``cyber-controller --flash-tails``. Destructive — erases the target USB."""
    import tempfile

    def on(s):
        print(s)

    print("=== Cyber Controller — flash Tails OS (amnesiac live USB) ===")
    print("Writes the official Tails USB image to a removable USB. The ENTIRE target USB is erased.\n")

    img = image
    expected = sha256
    if not img:
        info = try_fetch_latest(on)
        if not info:
            print("No --tails-image given and the latest-version feed is unavailable.\n"
                  "Download the Tails USB image (.img) from https://tails.net/install (verify it!),\n"
                  "then: cyber-controller --flash-tails --tails-image <path.img> [--tails-sha256 <hex>]",
                  file=sys.stderr)
            return 1
        cache = os.path.join(tempfile.gettempdir(), "cc_tails")
        try:
            img = download_image(info["url"], cache, on)
        except (requests.RequestException, ValueError, OSError) as exc:
            print(f"Download failed: {exc}\nDownload manually from tails.net and use --tails-image.",
                  file=sys.stderr)
            return 1
        expected = expected or info.get("sha256")

    cards = list_targets(on)
    if not cards:
        print("No removable USB drives detected. Insert a USB stick and retry.", file=sys.stderr)
        return 1
    dev = target
    if not dev:
        print("\n  Removable drives:")
        for i, c in enumerate(cards, 1):
            gb = (c.get("size") or 0) / (1 << 30)
            print(f"    {i}) {c['device']}  {c.get('name','')}  {gb:.1f} GB")
        raw = input("  Pick a drive number (or device path): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(cards):
            dev = cards[int(raw) - 1]["device"]
        elif raw:
            dev = raw
        else:
            print("No drive chosen — aborted.", file=sys.stderr)
            return 2

    if not assume_yes:
        print(f"\n*** This will ERASE EVERYTHING on {dev} and write Tails. ***")
        if input(f"  Type the device to confirm ({dev}): ").strip() != dev:
            print("Confirmation mismatch — aborted.", file=sys.stderr)
            return 2

    try:
        return flash_local_image(img, dev, on, expected_sha256=expected, sig_path=sig, confirmed=True)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Flash aborted: {exc}", file=sys.stderr)
        return 1
