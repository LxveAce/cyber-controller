"""Software-OS flashing catalog: write bootable PC/USB operating systems to a USB stick.

This generalizes the single-purpose Tails flow (:mod:`src.core.tails`) into a *catalog* of operating
systems (Tails, Kali, Arch, ...). Each catalog entry knows two things the firmware flasher does not:

  1. **How to RESOLVE its latest version** so the tool never ships a stale OS:
       * Tails  -> the installer feed / version redirector on tails.net
       * Kali   -> parse ``cdimage.kali.org/current/SHA256SUMS`` (the ``current`` path is always latest)
       * Arch   -> the machine-readable feed ``archlinux.org/releng/releases/json/``
     If the network is unavailable the **pinned** (bundled) version in ``os_catalog.json`` is used, so
     flashing still works fully offline.

  2. **How to VERIFY it** (two upstream models):
       * ``image_sig``     (Tails, Arch): a detached OpenPGP ``.sig`` over the IMAGE itself.
       * ``checksums_sig`` (Kali): an OpenPGP ``.gpg`` over a ``SHA256SUMS`` file that lists the image
         hash. Verify the file's signature, then confirm the image's SHA-256 appears in it.

The destructive device write is NOT reimplemented here — it reuses the hardened removable-only writer
in :mod:`src.core.backends.sd_backend` (``confirmed=True`` required; whole drive erased), exactly like
``tails.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from shutil import which
from typing import Any, Callable, Dict, List, Optional

import requests

from src.core import tails as _tails
from src.core.backends import sd_backend as sd
from src.core.resources import resource_path

log = logging.getLogger(__name__)
Line = Callable[[str], None]
Progress = Optional[Callable[[float], None]]

_CATALOG_PARTS = ("src", "config", "os_catalog.json")

# SSRF allowlist for OS metadata + image downloads. Mirrors the tails.py pattern.
_OS_HOSTS = frozenset((
    "tails.net", "download.tails.net", "tails.boum.org", "dl.amnesia.boum.org",
    "cdimage.kali.org", "kali.download",
    "archlinux.org", "www.archlinux.org", "geo.mirror.pkgbuild.com",
    "deb.parrot.sh",
))
_OS_HOST_SUFFIXES = (".tails.net", ".boum.org", ".kali.org", ".archlinux.org",
                     ".mirror.pkgbuild.com", ".parrot.sh")


def _host_allowed(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.lower().split("@")[-1].split(":")[0]
    return h in _OS_HOSTS or any(h.endswith(s) for s in _OS_HOST_SUFFIXES)


def _require_os_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"refusing non-https OS URL: {url!r}")
    if not _host_allowed(parts.hostname):
        raise ValueError(f"refusing OS URL to non-allowlisted host {parts.hostname!r}")
    return url


# ── catalog model ────────────────────────────────────────────────────

@dataclass
class OSImage:
    id: str
    name: str
    category: str
    description: str
    homepage: str
    image_type: str            # "img" or "iso" (both raw-written to the device)
    resolver: str              # "tails" | "kali" | "arch"
    verify_model: str          # "image_sig" | "checksums_sig"
    gpg_fingerprint: Optional[str]
    pinned: Dict[str, Any]
    extra: Dict[str, Any]      # resolver-specific keys (kali_variant, arch_feed_url, ...)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OSImage":
        known = {"id", "name", "category", "description", "homepage", "image_type",
                 "resolver", "verify_model", "gpg_fingerprint", "pinned"}
        return cls(
            id=d["id"], name=d["name"], category=d.get("category", ""),
            description=d.get("description", ""), homepage=d.get("homepage", ""),
            image_type=d.get("image_type", "img"), resolver=d["resolver"],
            verify_model=d.get("verify_model", "image_sig"),
            gpg_fingerprint=d.get("gpg_fingerprint"), pinned=d.get("pinned", {}),
            extra={k: v for k, v in d.items() if k not in known},
        )


@dataclass
class Resolved:
    """A concrete, flashable release for one catalog entry."""
    image_id: str
    version: str
    image_url: str
    image_type: str
    verify_model: str
    sig_url: Optional[str] = None
    checksums_url: Optional[str] = None
    checksums_sig_url: Optional[str] = None
    sha256: Optional[str] = None
    gpg_fingerprint: Optional[str] = None
    source: str = "online"     # "online" or "pinned"


def load_catalog(path: Optional[str] = None) -> List[OSImage]:
    p = path or str(resource_path(*_CATALOG_PARTS))
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return [OSImage.from_dict(d) for d in data.get("images", [])]


def get_image(image_id: str, path: Optional[str] = None) -> OSImage:
    for img in load_catalog(path):
        if img.id == image_id:
            return img
    raise KeyError(f"no such OS image in catalog: {image_id!r}")


def list_images(path: Optional[str] = None) -> List[Dict[str, str]]:
    return [{"id": i.id, "name": i.name, "category": i.category,
             "description": i.description, "image_type": i.image_type}
            for i in load_catalog(path)]


# ── HTTP helpers (allowlisted; monkeypatched in tests) ───────────────

def _http_get_parsed(url: str, timeout: int, extract: Callable[[Any], Any]) -> Any:
    """GET an allowlisted OS-metadata URL and return ``extract(resp)``, following redirects
    MANUALLY and re-validating every hop with ``_require_os_url`` (mirrors ``download()`` and
    firmware_vault._safe_api_get_json). requests' default redirect following is NOT used: a 302
    on an allowlisted host must not bounce a metadata fetch off-allowlist (SSRF) — otherwise the
    SHA256SUMS/feed these resolvers trust could be served by an attacker-chosen endpoint."""
    _require_os_url(url)
    current = url
    for _ in range(8):
        resp = requests.get(current, timeout=timeout, allow_redirects=False)
        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                current = _require_os_url(resp.headers.get("Location", ""))
                continue
            resp.raise_for_status()
            return extract(resp)
        finally:
            resp.close()
    raise ValueError("too many redirects fetching the OS metadata")


def _http_get_text(url: str, timeout: int = 30) -> str:
    return _http_get_parsed(url, timeout, lambda r: r.text)


def _http_get_json(url: str, timeout: int = 30) -> Any:
    return _http_get_parsed(url, timeout, lambda r: r.json())


# ── resolvers ────────────────────────────────────────────────────────

def parse_sha256sums(text: str, filename: str) -> Optional[str]:
    """Return the SHA-256 listed for *filename* in a ``SHA256SUMS`` body, or None."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([0-9a-fA-F]{64})[ \t*]+(.+)$", line)
        if m and os.path.basename(m.group(2).strip()) == filename:
            return m.group(1).lower()
    return None


def _resolve_tails(entry: OSImage, on_line: Line) -> Resolved:
    info = _tails.try_fetch_latest(on_line)  # {version, url, sha256} or None
    if not info or not info.get("url"):
        raise RuntimeError("tails feed unavailable")
    img_url = _require_os_url(info["url"])
    base = os.path.basename(urllib.parse.urlsplit(img_url).path)  # tails-amd64-<v>.img
    sig_url = _require_os_url(f"https://tails.net/torrents/files/{base}.sig")
    version = str(info.get("version") or "").strip()
    if not version or version.lower() == "none":
        m = re.search(r"tails-amd64-([0-9][0-9.]*)\.img", base)
        version = m.group(1) if m else "?"
    return Resolved(image_id=entry.id, version=version,
                    image_url=img_url, image_type=entry.image_type, verify_model="image_sig",
                    sig_url=sig_url, sha256=info.get("sha256"),
                    gpg_fingerprint=entry.gpg_fingerprint)


def _resolve_kali(entry: OSImage, on_line: Line) -> Resolved:
    sums_url = entry.pinned["checksums_url"]
    text = _http_get_text(sums_url)
    variant = entry.extra.get("kali_variant", "live-amd64")
    fname = ver = sha = None
    for line in text.splitlines():
        m = re.match(r"([0-9a-fA-F]{64})[ \t*]+(.+)$", line.strip())
        if not m:
            continue
        name = os.path.basename(m.group(2).strip())
        vm = re.match(rf"kali-linux-(.+?)-{re.escape(variant)}\.iso$", name)
        if vm:
            fname, ver, sha = name, vm.group(1), m.group(1).lower()
            break
    if not fname:
        raise RuntimeError(f"no kali {variant} image found in SHA256SUMS")
    base = sums_url.rsplit("/", 1)[0] + "/"
    img_url = _require_os_url(base + fname)
    return Resolved(image_id=entry.id, version=ver, image_url=img_url,
                    image_type=entry.image_type, verify_model="checksums_sig",
                    checksums_url=sums_url, checksums_sig_url=entry.pinned.get("checksums_sig_url"),
                    sha256=sha, gpg_fingerprint=entry.gpg_fingerprint)


def _resolve_arch(entry: OSImage, on_line: Line) -> Resolved:
    feed = entry.extra.get("arch_feed_url", "https://archlinux.org/releng/releases/json/")
    mirror = entry.extra.get("arch_mirror_base", "https://geo.mirror.pkgbuild.com").rstrip("/")
    data = _http_get_json(feed)
    releases = data.get("releases", []) if isinstance(data, dict) else []
    avail = [r for r in releases if r.get("available") and r.get("iso_url") and r.get("sha256_sum")]
    if not avail:
        raise RuntimeError("no available arch release in feed")
    latest_ver = data.get("latest_version")
    rel = next((r for r in avail if r.get("version") == latest_ver), None) or \
        sorted(avail, key=lambda r: str(r.get("release_date") or ""), reverse=True)[0]
    iso_path = rel["iso_url"]
    img_url = _require_os_url(mirror + iso_path if iso_path.startswith("/") else mirror + "/" + iso_path)
    return Resolved(image_id=entry.id, version=str(rel.get("version") or "?"),
                    image_url=img_url, image_type=entry.image_type, verify_model="image_sig",
                    sig_url=_require_os_url(img_url + ".sig"), sha256=str(rel.get("sha256_sum")).lower(),
                    gpg_fingerprint=rel.get("pgp_fingerprint") or entry.gpg_fingerprint)


def _resolve_parrot(entry: OSImage, on_line: Line) -> Resolved:
    """Resolve Parrot's latest release by scanning the versioned ISO directory index.

    Parrot has no stable ``current``/``latest`` ISO path, so we GET the parent index
    (``deb.parrot.sh/parrot/iso/``), pick the highest-semver version subdir, and build the ISO URL.
    Integrity is the SHA-256 published in that version's ``signed-hashes.txt`` — a single PGP
    *inline-clearsigned* document (md5/sha256/sha512 sections; NO detached per-ISO ``.sig``). The
    existing detached-sig verifier can't check a clearsigned file, so we resolve as ``image_sig``
    with the SHA-256 (from the signed file) as the anchor and no ``sig_url``; on any failure we fall
    back to the pinned release. See the entry's ``verify_note`` in os_catalog.json for the full
    manual PGP-verification path against the pinned fingerprint.
    """
    index_url = entry.extra.get("parrot_index_url", "https://deb.parrot.sh/parrot/iso/")
    edition = entry.extra.get("parrot_edition", "security")
    index = _http_get_text(index_url)
    vers = re.findall(r'href="(\d+\.\d+(?:\.\d+)?)/"', index)
    if not vers:
        raise RuntimeError("no Parrot version directories found in index")
    latest = max(vers, key=lambda v: tuple(int(x) for x in v.split(".")))
    base = index_url.rstrip("/") + "/" + latest + "/"
    fname = f"Parrot-{edition}-{latest}_amd64.iso"
    img_url = _require_os_url(base + fname)
    sums_url = _require_os_url(base + "signed-hashes.txt")
    sha = parse_sha256sums(_http_get_text(sums_url), fname)
    if not sha:
        raise RuntimeError(f"no sha256 for {fname} in Parrot signed-hashes.txt")
    return Resolved(image_id=entry.id, version=latest, image_url=img_url,
                    image_type=entry.image_type, verify_model="image_sig",
                    checksums_url=sums_url, sha256=sha, gpg_fingerprint=entry.gpg_fingerprint)


_RESOLVERS: Dict[str, Callable[[OSImage, Line], Resolved]] = {
    "tails": _resolve_tails, "kali": _resolve_kali, "arch": _resolve_arch,
    "parrot": _resolve_parrot,
}


def _pinned(entry: OSImage) -> Resolved:
    p = entry.pinned
    return Resolved(image_id=entry.id, version=str(p.get("version") or "?"),
                    image_url=p["image_url"], image_type=entry.image_type,
                    verify_model=entry.verify_model, sig_url=p.get("sig_url"),
                    checksums_url=p.get("checksums_url"), checksums_sig_url=p.get("checksums_sig_url"),
                    sha256=(p.get("sha256") or None), gpg_fingerprint=entry.gpg_fingerprint,
                    source="pinned")


def resolve(entry: OSImage, on_line: Line, online: bool = True) -> Resolved:
    """Resolve the live latest release; fall back to the pinned (offline) one on any failure."""
    if online:
        fn = _RESOLVERS.get(entry.resolver)
        if fn is not None:
            try:
                r = fn(entry, on_line)
                on_line(f"[os] {entry.name}: latest is {r.version}")
                return r
            except Exception as exc:  # noqa: BLE001 - any failure -> offline fallback
                on_line(f"[os] {entry.name}: could not resolve latest ({exc}); using bundled "
                        f"version {entry.pinned.get('version','?')} (offline).")
    return _pinned(entry)


# ── verification ─────────────────────────────────────────────────────

def _gpg() -> Optional[str]:
    for cand in ("gpg", "gpg2"):
        if which(cand):
            return cand
    return None


def verify_gpg_detached(target_path: str, sig_path: str, fingerprint: Optional[str],
                        on_line: Line) -> Optional[bool]:
    """Verify a detached OpenPGP *sig_path* over *target_path* against *fingerprint*.

    Returns True (good sig from the pinned key), False (bad/foreign sig), or None if gpg is not
    available (caller falls back to SHA-256). Assumes the signing key is already in the keyring.
    """
    gpg = _gpg()
    if not gpg:
        on_line("[os] gpg not found — skipping signature check (SHA-256 will be used instead).")
        return None
    try:
        proc = subprocess.run([gpg, "--status-fd", "1", "--verify", sig_path, target_path],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        on_line(f"[os] gpg verify error: {exc}")
        return None
    status = proc.stdout + proc.stderr
    flat = status.replace(" ", "")
    have_good = ("VALIDSIG" in status or "GOODSIG" in status)
    if not fingerprint:
        # A good signature from an UNPINNED key proves nothing about authenticity — ANY key in the local
        # keyring (including one an attacker planted) satisfies VALIDSIG. Don't rubber-stamp it as trusted:
        # report "can't establish" (None) so the caller falls through to the SHA-256 anchor instead of
        # writing an image on the strength of a meaningless signature.
        on_line("[os] no pinned key fingerprint for this image — a GPG signature alone can't prove "
                "authenticity; deferring to the SHA-256 check.")
        return None
    if not have_good:
        # No good signature came back. Tell apart "can't establish authenticity" from "the signature is
        # bad". The normal case for a fresh box is that the pinned signing key simply isn't in the keyring
        # (we never auto-import one), so gpg emits NO_PUBKEY/ERRSIG with no VALIDSIG. That is NOT a forged
        # signature — treat it like a missing gpg and defer to the SHA-256 anchor (None) so a genuine image
        # still flashes. Only a real bad/forged signature (BADSIG, no missing-key marker) hard-refuses.
        if "NO_PUBKEY" in flat or "ERRSIG" in flat:
            on_line("[os] the pinned signing key isn't in your keyring — can't verify the GPG signature; "
                    "deferring to the SHA-256 check (import the pinned key for full PGP assurance).")
            return None
        on_line("[os] GPG signature NOT valid for the expected key")
        return False
    good = fingerprint.replace(" ", "") in flat
    on_line("[os] GPG signature " + ("VALID" if good else "NOT valid for the expected key"))
    return good


def verify_gpg_clearsigned(clearsigned_path: str, fingerprint: Optional[str],
                           on_line: Line) -> Optional[bool]:
    """Verify an INLINE-clearsigned OpenPGP document (e.g. Parrot's ``signed-hashes.txt``) against
    *fingerprint*.

    Returns True (good clearsign from the pinned key), False (bad/foreign), or None if it can't be
    established (gpg missing, or no pinned fingerprint — an unpinned clearsign proves nothing). Parrot
    ships one clearsigned hashes file instead of a detached per-ISO sig, so this is the ONLY way its
    PGP assurance actually gets checked before the SHA-256 it carries is trusted.
    """
    gpg = _gpg()
    if not gpg:
        on_line("[os] gpg not found — cannot verify the clearsigned hashes (SHA-256 alone will be used).")
        return None
    if not fingerprint:
        on_line("[os] no pinned key fingerprint — a clearsigned hashes file can't establish authenticity.")
        return None
    try:
        proc = subprocess.run([gpg, "--status-fd", "1", "--verify", clearsigned_path],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        on_line(f"[os] gpg clearsign verify error: {exc}")
        return None
    status = proc.stdout + proc.stderr
    flat = status.replace(" ", "")
    have_good = ("VALIDSIG" in status or "GOODSIG" in status)
    if not have_good:
        # Same trichotomy as verify_gpg_detached: a missing pinned key (NO_PUBKEY/ERRSIG) can't establish
        # authenticity and defers to SHA-256 (None); a genuinely bad clearsign hard-refuses (False).
        if "NO_PUBKEY" in flat or "ERRSIG" in flat:
            on_line("[os] the pinned signing key isn't in your keyring — can't verify the clearsigned "
                    "hashes; deferring to the SHA-256 check (import the pinned key for full assurance).")
            return None
        on_line("[os] clearsigned hashes NOT valid for the expected key")
        return False
    good = fingerprint.replace(" ", "") in flat
    on_line("[os] clearsigned hashes " + ("VALID" if good else "NOT valid for the expected key"))
    return good


# expose the shared sha256 check (identical semantics to tails.verify_sha256)
verify_sha256 = _tails.verify_sha256


# ── download (allowlisted, redirect-following) ───────────────────────

def download(url: str, dest_dir: str, on_line: Line, on_progress: Progress = None) -> str:
    """Download an OS image/sig/checksums file from an allowlisted host (redirects re-validated)."""
    _require_os_url(url)
    os.makedirs(dest_dir, exist_ok=True)
    name = sd._safe_filename(url.rsplit("/", 1)[-1].split("?")[0]) or "download.bin"
    dest = os.path.join(dest_dir, name)
    current = url
    for _ in range(8):
        resp = requests.get(current, stream=True, timeout=120, allow_redirects=False)
        # try/finally: the streamed socket is released deterministically even when the
        # redirect-allowlist check or raise_for_status raises (mirrors firmware_vault
        # ._safe_streamed_download) — never leak the connection to GC finalization.
        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                current = _require_os_url(resp.headers.get("Location", ""))
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
            on_line(f"[os] downloaded {written} bytes -> {dest}")
            return dest
        finally:
            resp.close()
    raise ValueError("too many redirects fetching the OS file")


# ── flash pipeline ───────────────────────────────────────────────────

def flash_os_image(entry: OSImage, resolved: Resolved, image_path: str, device: str, on_line: Line,
                   on_progress: Progress = None, sig_path: Optional[str] = None,
                   checksums_path: Optional[str] = None, checksums_sig_path: Optional[str] = None,
                   confirmed: bool = False) -> int:
    """Verify (per the entry's model) then write a local OS image to a removable *device*.

    Returns 0 on success. The write goes through ``sd_backend.write_image`` (removable-only,
    ``confirmed=True`` required — the whole drive is erased)."""
    if not confirmed:
        raise ValueError("flash requires confirmed=True — the entire target USB will be erased")
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"OS image not found: {image_path}")

    fpr = resolved.gpg_fingerprint or entry.gpg_fingerprint
    verified = False

    if resolved.verify_model == "checksums_sig":
        # Kali: the .gpg signs the SHA256SUMS file; the image hash must appear in that file.
        sums_ok: Optional[bool] = None
        if checksums_path and checksums_sig_path:
            sums_ok = verify_gpg_detached(checksums_path, checksums_sig_path, fpr, on_line)
            if sums_ok is False:
                raise ValueError("SHA256SUMS signature is NOT valid for the expected key — refusing.")
        # The enforced hash MUST come from the bytes the signature authenticated. resolved.sha256
        # was parsed during a SEPARATE, unsigned resolve-time GET of SHA256SUMS, so a mirror could
        # sign one list yet feed the resolver a swapped image's hash — the sig would validate a
        # file we never enforce. So when the sig verified, re-parse the hash from the signed
        # checksums_path and enforce THAT; fall back to resolved.sha256 only as an UNAUTHENTICATED
        # integrity anchor when no signature could be established (disclosed below).
        signed_expected = None
        if checksums_path and os.path.isfile(checksums_path):
            with open(checksums_path, "r", encoding="utf-8", errors="replace") as fh:
                signed_expected = parse_sha256sums(fh.read(), os.path.basename(image_path))
        if sums_ok is True:
            if not signed_expected:
                raise ValueError("SHA256SUMS signature verified but lists no hash for this image "
                                 "— refusing to write.")
            expected = signed_expected
        else:
            expected = resolved.sha256 or signed_expected
        if expected:
            if not verify_sha256(image_path, expected, on_line, on_progress):
                raise ValueError("SHA-256 does not match SHA256SUMS — refusing to write.")
            verified = True
        if verified and sums_ok is not True:
            on_line("[os] NOTE: checksum matched but the SHA256SUMS GPG signature was not verified "
                    "(gpg missing or signature file absent). Verify the signature for full assurance.")
    else:
        # image_sig: a detached sig over the image (Tails, Arch), OR an inline-clearsigned hashes file
        # whose SHA-256 is only trustworthy if that clearsign checks out (Parrot).
        if sig_path:
            result = verify_gpg_detached(image_path, sig_path, fpr, on_line)
            if result is True:
                verified = True
            elif result is False:
                raise ValueError("GPG signature is NOT valid for the expected key — refusing to write.")
        # Parrot-style: the SHA-256 came out of a CLEARSIGNED hashes file. That hash is worthless against a
        # MITM unless the clearsign itself is verified — a swapped image + swapped hashes would match. So
        # verify the clearsign, and only then treat the SHA as authenticated.
        if not verified and checksums_path and os.path.isfile(checksums_path):
            clearsig_ok = verify_gpg_clearsigned(checksums_path, fpr, on_line)
            if clearsig_ok is False:
                raise ValueError("Clearsigned hashes are NOT valid for the expected key — refusing to write.")
            if clearsig_ok is True:
                # Enforce the hash from the CLEARSIGN-VERIFIED file, not resolved.sha256 (which was
                # parsed from a separate unsigned resolve-time GET of the same file — a mirror could
                # sign one copy and feed the resolver another).
                with open(checksums_path, "r", encoding="utf-8", errors="replace") as fh:
                    signed_sha = parse_sha256sums(fh.read(), os.path.basename(image_path))
                if not signed_sha:
                    raise ValueError("Clearsigned hashes verified but list no hash for this image "
                                     "— refusing to write.")
                if not verify_sha256(image_path, signed_sha, on_line, on_progress):
                    raise ValueError("SHA-256 does not match the PGP-signed hashes — refusing to write.")
                verified = True
        if not verified and resolved.sha256:
            if not verify_sha256(image_path, resolved.sha256, on_line, on_progress):
                raise ValueError("SHA-256 does not match — refusing to write an unverified image.")
            verified = True
            # DISCLOSE unconditionally: a bare SHA-256 match is an INTEGRITY check, NOT
            # cryptographic authentication — no GPG signature over this image was verified (gpg
            # missing, or the detached .sig / clearsigned hashes were absent). The old
            # `if checksums_path:` gate stayed SILENT for the detached-sig profiles (Tails/Arch)
            # that carry NO hashes file, so a match against a NETWORK-fetched sha (Tails' hash rides
            # the same fetch as the image) authenticated the write with no note and no warning — a
            # MITM serving a matched image+hash pair passed unseen (verify-never-fake). Always tell
            # the operator the real trust level they got.
            on_line("[os] NOTE: SHA-256 matched but the image's GPG signature was not verified "
                    "(gpg missing, or the signature/hashes file was absent) — this is an integrity "
                    "check only, not cryptographic authentication. Verify the signature against "
                    "the pinned key before trusting it.")

    if not verified:
        on_line(f"[os] WARNING: {entry.name} image is UNVERIFIED (no valid signature/checksum). "
                "Strongly verify against the official source before writing.")

    rc = sd.write_image(image_path, device, on_line, on_progress, confirmed=True)
    if rc != 0:
        on_line(f"[os] write FAILED (exit {rc})")
        return rc
    on_line("[os] verifying write (read-back)...")
    if sd.verify_write(image_path, device, on_line, on_progress):
        on_line(f"[os] done — {entry.name} USB is ready. Boot the target machine from this USB.")
        return 0
    on_line("[os] read-back verification FAILED — the USB may be bad; re-flash.")
    return 1


# ── CLI surfaces ─────────────────────────────────────────────────────

def list_catalog_cli() -> int:
    print("=== Cyber Controller — Software OS catalog (flash to USB) ===")
    for i in load_catalog():
        print(f"  {i.id:<8} {i.name:<22} [{i.category}] ({i.image_type})")
        print(f"           {i.description}")
    print("\nFlash with:  cyber-controller --flash-os <id> [--os-image <local.iso/.img>] "
          "[--os-target <device>] [--offline] [--yes]")
    return 0


def run_os_flash_cli(image_id: str, target: Optional[str] = None, image: Optional[str] = None,
                     sig: Optional[str] = None, assume_yes: bool = False, offline: bool = False) -> int:
    """Interactive CLI for ``cyber-controller --flash-os <id>``. Destructive — erases the target USB."""
    import sys
    import tempfile

    def on(s: str) -> None:
        print(s)

    try:
        entry = get_image(image_id)
    except KeyError:
        avail = ", ".join(i["id"] for i in list_images())
        print(f"Unknown OS id {image_id!r}. Available: {avail}", file=sys.stderr)
        return 2

    print(f"=== Cyber Controller — flash {entry.name} to USB ===")
    print("Writes a verified bootable OS image to a removable USB. The ENTIRE target USB is erased.\n")

    resolved = resolve(entry, on, online=not offline)
    img = image
    sig_path = sig
    checksums_path = checksums_sig_path = None
    cache = os.path.join(tempfile.gettempdir(), f"cc_os_{entry.id}")

    if not img:
        try:
            img = download(resolved.image_url, cache, on)
            if resolved.verify_model == "image_sig" and resolved.sig_url and not sig_path:
                try:
                    sig_path = download(resolved.sig_url, cache, on)
                except (requests.RequestException, ValueError, OSError) as exc:
                    on(f"[os] could not fetch signature ({exc}); will fall back to SHA-256.")
            # Parrot-style image_sig entries anchor integrity on a CLEARSIGNED hashes file (no detached
            # per-ISO .sig). Fetch it so flash_os_image can verify the clearsign before trusting its SHA.
            if resolved.verify_model == "image_sig" and resolved.checksums_url and not checksums_path:
                try:
                    checksums_path = download(resolved.checksums_url, cache, on)
                except (requests.RequestException, ValueError, OSError) as exc:
                    on(f"[os] could not fetch the signed hashes file ({exc}).")
            if resolved.verify_model == "checksums_sig":
                if resolved.checksums_url:
                    checksums_path = download(resolved.checksums_url, cache, on)
                if resolved.checksums_sig_url:
                    try:
                        checksums_sig_path = download(resolved.checksums_sig_url, cache, on)
                    except (requests.RequestException, ValueError, OSError) as exc:
                        on(f"[os] could not fetch SHA256SUMS signature ({exc}).")
        except (requests.RequestException, ValueError, OSError) as exc:
            print(f"Download failed: {exc}\nDownload {entry.name} manually from {entry.homepage} "
                  f"(verify it!) and pass --os-image <path>.", file=sys.stderr)
            return 1

    cards = sd.detect_sd_cards(on)
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
        print(f"\n*** This will ERASE EVERYTHING on {dev} and write {entry.name} {resolved.version}. ***")
        if input(f"  Type the device to confirm ({dev}): ").strip() != dev:
            print("Confirmation mismatch — aborted.", file=sys.stderr)
            return 2

    try:
        return flash_os_image(entry, resolved, img, dev, on, sig_path=sig_path,
                              checksums_path=checksums_path, checksums_sig_path=checksums_sig_path,
                              confirmed=True)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Flash aborted: {exc}", file=sys.stderr)
        return 1
