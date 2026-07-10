r"""Wordlist provisioning for the offline WPA/WPA2 dictionary-crack pipeline.

The cracker in :mod:`src.core.crack_pipeline` is *dictionary-only* -- it can only recover a passphrase
that is actually in the wordlist you point it at. So the wordlist IS the tool. This module is the
"bring a wordlist" half: it lets the operator either **install a prepackaged wordlist** from a small
curated catalog, or **use their own** file (BYO). Nothing is bundled in the app; prepackaged lists are
*downloaded on explicit opt-in* from pinned upstream URLs and integrity-checked, so CC ships no large
data blobs and no attack material.

Honesty / safety invariants (load-bearing):

* **Opt-in, never automatic.** A download only happens when the operator picks a catalog entry and
  confirms. :func:`install_choices_text` is the "install prepackaged or use your own" copy the UI shows.
* **Integrity-checked, never faked.** Every catalog entry carries a real ``size_bytes``. Entries whose
  content we could verify carry a real ``sha256`` (:func:`is_pinned`); a download is rejected if the
  hash mismatches. Entries we could not pre-hash (e.g. the 140 MB rockyou) are ``sha256=""`` and are
  verified by size only, with a loud "integrity not pre-pinned -- verify manually" warning. We never
  ship a made-up hash.
* **Immutable sources.** SecLists entries are pinned to a specific commit SHA (not ``master``), so the
  pinned hash matches forever. rockyou is the canonical naive-hashcat release asset.
* **BYO is first-class.** :func:`register_byo` validates any file the operator already has; the whole
  catalog is optional convenience, not a requirement.

Structure mirrors crack_pipeline: the pure pieces (catalog data, path/size/hash helpers, install scan,
UI copy) are unit-testable with no network and no files; :func:`download_wordlist` is a thin, best-effort
stdlib-only (urllib) fetch that verifies before it commits the file into place.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .crack_pipeline import validate_wordlist  # BYO reuses the same non-empty-file guard

Line = Callable[[str], None]

# Pre-verification byte ceiling for downloads. Every catalog entry carries an expected size; we allow
# 1.25x slack (mirror drift, minor upstream churn). Size-unknown specs fall back to this absolute
# backstop so a misbehaving/oversized upstream cannot fill the disk before the hash check runs.
_DOWNLOAD_HARD_CAP_BYTES = 20 * 1024**3  # 20 GiB

# -- catalog ----------------------------------------------------------

#: SecLists pinned to an immutable commit so the recorded sha256 matches for good. Raw URLs at a commit
#: SHA are content-stable; ``master`` is not (upstream would rotate the file out from under the hash).
_SECLISTS_COMMIT = "acfed0cf1eecc1f8b412c8cd5085c3090494a1fa"


def _seclists(path: str) -> str:
    return f"https://raw.githubusercontent.com/danielmiessler/SecLists/{_SECLISTS_COMMIT}/{path}"


@dataclass(frozen=True)
class WordlistSpec:
    """One installable wordlist. ``sha256=''`` means integrity is size-only (see module docstring).
    ``lines=0`` means the line count is unknown. ``compressed`` is '' | 'gz' (fetched-then-inflated)."""

    id: str
    name: str
    description: str
    url: str
    size_bytes: int
    lines: int = 0
    sha256: str = ""
    category: str = "general"  # "wpa" | "general"
    compressed: str = ""


#: Curated, deliberately small catalog. WPA-specific lists first (this feeds a WPA cracker), then a
#: general top-10k quick pass, then the classic rockyou as the big general option. Sizes + hashes are
#: real (measured at the pinned commit); rockyou is size-pinned only (too large to pre-hash here).
CATALOG: tuple[WordlistSpec, ...] = (
    WordlistSpec(
        id="wpa-top62",
        name="Probable WPA top-62",
        description="62 most-probable WPA/WPA2 passphrases. Tiny -- a seconds-long smoke test.",
        url=_seclists("Passwords/WiFi-WPA/probable-v2-wpa-top62.txt"),
        size_bytes=573,
        lines=62,
        sha256="c5088caa6798a77ca6d37f407a89da7fa843bdc8a54f6c585ce853b3deba6baf",
        category="wpa",
    ),
    WordlistSpec(
        id="wpa-top4800",
        name="Probable WPA top-4800",
        description="4,800 most-probable WPA/WPA2 passphrases (SecLists WiFi-WPA). Fast, high-yield.",
        url=_seclists("Passwords/WiFi-WPA/probable-v2-wpa-top4800.txt"),
        size_bytes=45276,
        lines=4800,
        sha256="5dc33214cad9a11eb926de00f5ded20b1a5fc12ba3332319e054713265a35c51",
        category="wpa",
    ),
    WordlistSpec(
        id="common-10k",
        name="10k most common",
        description="10,000 most common passwords (SecLists). A quick general first pass.",
        url=_seclists("Passwords/Common-Credentials/10k-most-common.txt"),
        size_bytes=73017,
        lines=10000,
        sha256="4adb3f0afb4a10cf19ebe48d8c69a46f934bbc8d77c694c210564f9583e7f4ba",
        category="general",
    ),
    WordlistSpec(
        id="rockyou",
        name="rockyou.txt",
        description="The classic 14.3M-password leak list. Large (~134 MiB); the standard baseline.",
        url="https://github.com/brannondorsey/naive-hashcat/releases/download/data/rockyou.txt",
        size_bytes=139921497,
        lines=14344391,
        sha256="",  # not pre-pinned: verified by size, with a loud warning (never a faked hash)
        category="general",
    ),
)


def catalog() -> list[WordlistSpec]:
    """The installable wordlist catalog (WPA-specific first, then general, small-to-large)."""
    return list(CATALOG)


def spec_by_id(wid: str) -> Optional[WordlistSpec]:
    """Catalog entry with this id, or None."""
    return next((w for w in CATALOG if w.id == wid), None)


# -- paths + formatting (pure) ----------------------------------------

def default_wordlist_dir() -> str:
    """Where installed wordlists live. Honors ``CC_WORDLIST_DIR``; else ``~/.cyber-controller/wordlists``.
    A plain user-writable dir -- no admin, and outside the app bundle so it survives reinstalls."""
    env = os.environ.get("CC_WORDLIST_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".cyber-controller", "wordlists")


def filename_for(spec: WordlistSpec) -> str:
    """Local filename for a spec: the URL basename, with any ``.gz`` suffix dropped (we store inflated)."""
    base = spec.url.rstrip("/").split("/")[-1]
    if spec.compressed == "gz" and base.endswith(".gz"):
        base = base[: -len(".gz")]
    return base


def target_path(spec: WordlistSpec, directory: Optional[str] = None) -> str:
    """Absolute path this spec installs to inside *directory* (default :func:`default_wordlist_dir`)."""
    return os.path.join(directory or default_wordlist_dir(), filename_for(spec))


def format_size(n: int) -> str:
    """Human byte size (e.g. ``134.3 MiB``). Binary units; 1 decimal above KiB."""
    if n < 1024:
        return f"{n} B"
    val = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        val /= 1024.0
        if val < 1024.0:
            return f"{val:.1f} {unit}"
    return f"{val:.1f} PiB"


def is_pinned(spec: WordlistSpec) -> bool:
    """True if this entry has a real content hash to enforce (vs size-only verification)."""
    return bool(spec.sha256)


# -- integrity (pure-ish: reads a file, no network) -------------------

def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (1 MiB chunks so a 140 MB list never loads into RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_file(path: str, spec: WordlistSpec) -> tuple[bool, str]:
    """Check an on-disk file against a spec. Returns (ok, message).

    * missing file -> (False, ...).
    * hash-pinned entry -> SHA-256 must match (size is advisory).
    * size-only entry -> size must match; ok, but the message flags 'integrity not pre-pinned'."""
    if not os.path.isfile(path):
        return (False, f"not installed: {path}")
    actual_size = os.path.getsize(path)
    if is_pinned(spec):
        actual = sha256_file(path)
        if actual.lower() != spec.sha256.lower():
            return (False, f"SHA-256 mismatch (got {actual[:12]}…, expected {spec.sha256[:12]}…)")
        return (True, f"verified ({format_size(actual_size)}, SHA-256 pinned)")
    # size-only entry
    if spec.size_bytes and actual_size != spec.size_bytes:
        return (False, f"size mismatch (got {format_size(actual_size)}, "
                       f"expected {format_size(spec.size_bytes)})")
    return (True, f"present ({format_size(actual_size)}) — integrity NOT pre-pinned; verify manually")


# -- install scan (reads a dir, no network) ---------------------------

def scan_installed(directory: Optional[str] = None) -> list[dict]:
    """Every ``*.txt`` in the wordlist dir -> [{name, path, size, size_human}], sorted by name. Used to
    populate the crack UI's wordlist picker from what's actually on disk (catalog + BYO alike)."""
    directory = directory or default_wordlist_dir()
    if not os.path.isdir(directory):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        out.append({"name": name, "path": path, "size": size, "size_human": format_size(size)})
    return out


def is_installed(spec: WordlistSpec, directory: Optional[str] = None) -> bool:
    """True if this catalog entry's target file exists on disk (presence only; use verify_file for
    integrity)."""
    return os.path.isfile(target_path(spec, directory))


# -- UI copy (pure) ---------------------------------------------------

def install_choices_text() -> str:
    """The install-time 'prepackaged or your own' explainer the UI shows above the catalog."""
    return (
        "A dictionary attack needs a wordlist. You can install a prepackaged list below, or use your "
        "own file. Prepackaged lists are downloaded on demand from their pinned upstream source and "
        "integrity-checked — Cyber Controller does not bundle wordlists. Bring-your-own is always "
        "available: point the cracker at any .txt of candidate passphrases you already have."
    )


def catalog_text() -> str:
    """One line per catalog entry (name, size, category, pin status) for a text/list view."""
    rows = []
    for w in CATALOG:
        pin = "SHA-256 pinned" if is_pinned(w) else "size-only"
        rows.append(f"  [{w.category:<7}] {w.name} — {format_size(w.size_bytes)} ({pin})\n"
                    f"            {w.description}")
    return "Prepackaged wordlists (download on demand):\n" + "\n".join(rows)


def register_byo(path: str) -> str:
    """Validate a bring-your-own wordlist the operator already has. Returns the path on success; raises
    ValueError (via the crack pipeline's guard) for a missing/empty file. No copy, no network -- the
    cracker reads it in place."""
    return validate_wordlist(path)


# -- download (thin, best-effort, stdlib only) ------------------------

def download_wordlist(spec: WordlistSpec, directory: Optional[str] = None,
                      on_line: Optional[Line] = None, *,
                      timeout: float = 120.0, force: bool = False) -> str:
    """Download *spec* into *directory*, verify it, and return the installed path. Opt-in only -- the
    caller must have shown :func:`install_choices_text` and gotten a confirmation first.

    Fails closed: downloads to a temp file, inflates a ``gz`` entry, verifies (SHA-256 if pinned, else
    size), and only then moves it into place. A verification failure deletes the temp file and raises
    RuntimeError -- a bad/rotated download is never installed. Uses only the stdlib (urllib), so there
    is no new dependency and nothing is bundled."""
    log: Line = on_line or (lambda *_a: None)
    directory = directory or default_wordlist_dir()
    dest = target_path(spec, directory)
    if os.path.isfile(dest) and not force:
        ok, msg = verify_file(dest, spec)
        if ok:
            log(f"[wordlist] already installed: {os.path.basename(dest)} ({msg})")
            return dest
        log(f"[wordlist] reinstalling {os.path.basename(dest)}: {msg}")

    os.makedirs(directory, exist_ok=True)
    if not is_pinned(spec):
        log("[wordlist] NOTE: this list has no pinned hash — it will be size-verified only. "
            "Verify its integrity yourself before trusting recovered results.")
    log(f"[wordlist] downloading {spec.name} ({format_size(spec.size_bytes)}) from {spec.url}")

    tmp_dl = tempfile.NamedTemporaryFile(prefix="cc-wl-", suffix=".part", dir=directory, delete=False)
    tmp_dl.close()
    req = urllib.request.Request(spec.url, headers={"User-Agent": "cyber-controller-wordlist"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_dl.name, "wb") as out:
            _stream_capped(resp, out, spec)
    except Exception as exc:  # noqa: BLE001 -- surface any network error as an honest failure
        _rm(tmp_dl.name)
        raise RuntimeError(f"download failed: {exc}") from exc

    final_tmp = tmp_dl.name
    if spec.compressed == "gz":
        log("[wordlist] inflating .gz …")
        inflated = tmp_dl.name + ".txt"
        try:
            with gzip.open(tmp_dl.name, "rb") as gz, open(inflated, "wb") as out:
                shutil.copyfileobj(gz, out)
        except Exception as exc:  # noqa: BLE001
            _rm(tmp_dl.name)
            _rm(inflated)
            raise RuntimeError(f"could not inflate the download: {exc}") from exc
        _rm(tmp_dl.name)
        final_tmp = inflated

    ok, msg = verify_file(final_tmp, spec)
    if not ok:
        _rm(final_tmp)
        raise RuntimeError(f"integrity check failed, not installing: {msg}")

    os.replace(final_tmp, dest)
    log(f"[wordlist] installed {os.path.basename(dest)} — {msg}")
    return dest


def _stream_capped(resp, out, spec: WordlistSpec) -> None:
    """Copy the response body to ``out`` in chunks, aborting if it exceeds the expected size.

    Fails closed BEFORE a runaway/oversized upstream can fill the operator's disk. The post-download
    hash/size check in :func:`verify_file` still runs afterward — this is only the pre-verification
    byte bound. On overflow the caller's ``except`` removes the ``.part`` temp.
    """
    limit = int(spec.size_bytes * 1.25) if spec.size_bytes else _DOWNLOAD_HARD_CAP_BYTES
    written = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise RuntimeError(
                f"response exceeded size ceiling ({format_size(limit)}) — aborting to protect disk"
            )
        out.write(chunk)


def _rm(path: str) -> None:
    """Best-effort unlink; never raises (cleanup of a temp file must not mask the real error)."""
    try:
        os.remove(path)
    except OSError:
        pass
