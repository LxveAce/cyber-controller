"""Firmware vault — offline local cache for firmware binaries."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Reuse the flash core's vetted SSRF/path-traversal primitives (single source of truth).
from src.core.flash_core import IMAGE_MULTI, _require_allowed_url, _safe_cache_name
from src.core.resources import resource_path

log = logging.getLogger(__name__)

_DEFAULT_VAULT_DIR = Path.home() / ".cyber-controller" / "firmware_vault"
_INDEX_FILE = "vault_index.json"
_PROFILES_DIR = resource_path("src", "config", "profiles")
_GITHUB_API = "https://api.github.com"
_DOWNLOAD_CHUNK = 8192
_TIMEOUT = 30
_MAX_FIRMWARE_BYTES = 64 * 1024 * 1024  # 64 MB cap — abort oversized / MITM-streamed downloads
_MAX_REDIRECTS = 6


def configured_vault_dir() -> Path:
    """Resolve the firmware-cache directory from ``settings['vault']['dir']``.

    The Settings tab persists this path, so every FirmwareVault construction routes through here — a user
    who points the vault at, say, ``D:\\fw`` and saves actually caches firmware there instead of the
    hardcoded default. A blank/missing setting (or any read error) falls back to :data:`_DEFAULT_VAULT_DIR`.
    The import is lazy to avoid a settings<->vault import cycle.
    """
    raw = None
    try:
        from src.config.settings import load_settings
        raw = (load_settings().get("vault") or {}).get("dir")
    except Exception:  # noqa: BLE001 - never let a settings hiccup break vault construction
        raw = None
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return _DEFAULT_VAULT_DIR


def _safe_version_key(version: str) -> str:
    """Sanitize a version/tag string into the filesystem- and index-safe key.

    Any character outside ``[A-Za-z0-9._-]`` is replaced with ``_`` (path-traversal
    defense). The vault index is keyed by this sanitized form, so callers comparing an
    upstream GitHub tag against cached keys MUST run the tag through this same function
    first — otherwise a tag like ``2024.1+deb`` never matches its stored key ``2024.1_deb``.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(version)) or "unknown"


def _safe_streamed_download(url: str, dest_path: Path, progress_callback, filename: str) -> int:
    """Stream *url* to *dest_path*, SSRF-safe and size-capped.

    Redirects are followed MANUALLY with every hop re-validated against the GitHub
    host allowlist (so a 302 can't bounce us to 169.254.169.254/a LAN host), and the
    body is hard-capped at ``_MAX_FIRMWARE_BYTES``. Returns the byte count written.
    """
    _require_allowed_url(url)
    current = url
    for _ in range(_MAX_REDIRECTS):
        resp = requests.get(current, stream=True, timeout=_TIMEOUT, allow_redirects=False)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "")
                _require_allowed_url(loc)  # raises ValueError if off-allowlist
                current = loc
                continue
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            if total and total > _MAX_FIRMWARE_BYTES:
                raise ValueError(f"firmware exceeds size cap ({total} bytes)")
            downloaded = 0
            with dest_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > _MAX_FIRMWARE_BYTES:
                        raise ValueError("firmware exceeded size cap mid-stream")
                    if progress_callback:
                        progress_callback(downloaded, total, f"Downloading {filename}...")
            return downloaded
        finally:
            resp.close()
    raise ValueError("too many redirects")


def _safe_api_get_json(url: str) -> Any:
    """GET a GitHub *API* URL and return parsed JSON, with the SAME SSRF policy as the
    binary path (M-2).

    The release-asset download (``_safe_streamed_download``) already validates every redirect
    hop against the host allowlist; the metadata/API GETs must too, or the SSRF story is
    inconsistent — a 302 on the API host (or a future profile whose ``firmware_urls`` parses to
    an attacker-controlled owner/repo) could bounce the *metadata* request off-allowlist. We
    therefore validate the initial URL and follow redirects manually, re-validating each
    ``Location`` against ``_require_allowed_url`` before following it.
    """
    _require_allowed_url(url)
    current = url
    for _ in range(_MAX_REDIRECTS):
        resp = requests.get(current, timeout=_TIMEOUT, allow_redirects=False)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "")
                _require_allowed_url(loc)  # raises ValueError if off-allowlist
                current = loc
                continue
            resp.raise_for_status()
            return resp.json()
        finally:
            resp.close()
    raise ValueError("too many redirects")


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_DOWNLOAD_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _parse_github_release_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub releases URL."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/releases", url)
    if m:
        return m.group(1), m.group(2)
    return None


class FirmwareVault:
    """Local cache of firmware binaries for offline flashing.

    Firmware is stored under ``vault_dir/{profile_id}/{version}/{filename}``.
    A ``vault_index.json`` file tracks all cached entries with metadata.

    Thread-safe: all public methods acquire an internal lock.
    """

    def __init__(self, vault_dir: Path | None = None) -> None:
        self.vault_dir = vault_dir or _DEFAULT_VAULT_DIR
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: dict[str, Any] = self._load_index()

    # ── Index persistence ────────────────────────────────────────────

    def _index_path(self) -> Path:
        return self.vault_dir / _INDEX_FILE

    def _load_index(self) -> dict[str, Any]:
        path = self._index_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("Corrupt vault index — starting fresh")
            else:
                # Valid JSON of the wrong type (null, an array, a bare string) would otherwise sail
                # through and make self._index a non-dict, so the next vault op (list_cached/get_cached)
                # blows up on .items()/.get(). Require an object, else start fresh.
                if isinstance(raw, dict):
                    return raw
                log.warning("Vault index is not a JSON object — starting fresh")
        return {}

    def _save_index(self) -> None:
        # Atomic write: a bare write_text truncates vault_index.json at open, so a power loss mid-write
        # (the vault exists for offline/field flashing, where power blips are normal) leaves it partial;
        # _load_index then starts fresh and the ENTIRE cache index is lost while the .bin dirs linger on
        # disk (invisible to get_cached and unreachable by clear_cache()). Temp + fsync + os.replace so a
        # crash leaves either the old complete index or the new one — never a truncated file.
        path = self._index_path()
        data = json.dumps(self._index, indent=2, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── Profile loading ──────────────────────────────────────────────

    @staticmethod
    def _load_profile(profile_id: str) -> dict[str, Any] | None:
        """Load a firmware profile JSON by its id."""
        if _PROFILES_DIR.is_dir():
            for f in _PROFILES_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if data.get("id") == profile_id:
                        return data
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    @staticmethod
    def list_profiles() -> list[dict[str, str]]:
        """Return a list of available firmware profile summaries."""
        profiles = []
        if _PROFILES_DIR.is_dir():
            for f in sorted(_PROFILES_DIR.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    profiles.append({
                        "id": data.get("id", f.stem),
                        "name": data.get("name", f.stem),
                        "description": data.get("description", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        return profiles

    # ── Download ─────────────────────────────────────────────────────

    def download_firmware(
        self,
        profile_id: str,
        version: str = "latest",
        progress_callback: Any = None,
    ) -> Path | None:
        """Download firmware binary from the profile's URL into the vault.

        Args:
            profile_id: Firmware profile identifier (matches profile JSON ``id`` field).
            version: Version tag to download (default ``"latest"``).
            progress_callback: Optional ``(bytes_downloaded, total_bytes, message)`` callable.

        Returns:
            Path to the downloaded file, or None on failure.
        """
        profile = self._load_profile(profile_id)
        if not profile:
            log.error("Unknown firmware profile: %s", profile_id)
            return None

        # Offline-cache safety: the vault stores ONE bare .bin per profile and the offline-flash path
        # (flash_engine._flash_offline_fallback) writes that file as a MERGED blob at 0x0. That is only
        # correct for a merged-single-bin firmware. A 'multi-file-offsets' profile (marauder, esp32-div)
        # ships an APP-ONLY image meant to flash at 0x10000 ON TOP of a separate bootloader/partitions/
        # boot_app0 boot chain — none of which the vault can store or the offline path can apply. Caching
        # it would let an offline flash write an app-only image at the wrong offset with no boot chain and
        # brick the board (white screen / non-booting). Refuse to cache it (fail closed) rather than store
        # a brick. (Board-aware multi-file offline caching needs the boot chain persisted in the index AND
        # the offline flash path taught the offsets — see the module notes.)
        if profile.get("image_model") == IMAGE_MULTI:
            log.error(
                "Refusing to vault %s: its firmware is app-only ('%s') and needs a bootloader/partitions/"
                "boot_app0 boot chain flashed at per-file offsets. The offline vault can only store and "
                "flash a single merged image at 0x0, so caching this would brick the board on an offline "
                "flash. Flash it online (board-aware) instead.",
                profile_id, IMAGE_MULTI,
            )
            return None

        urls = profile.get("firmware_urls", {})
        url = urls.get(version) or urls.get("latest")
        if not url:
            log.error("No download URL for %s version %s", profile_id, version)
            return None

        # Resolve GitHub "latest" redirect to actual release
        resolved_version = version
        download_url = url
        assets: list[dict] = []

        info = _parse_github_release_url(url)
        if info:
            owner, repo = info
            # Honor a pinned version: a caller asking for "v1.2.0" must NOT silently receive latest.
            # Only "latest" resolves the /releases/latest redirect; a specific tag queries that tag (and
            # fails loudly if it doesn't exist) rather than resolving to a different version.
            if version and version != "latest":
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
                    log.error("Refusing unsafe version tag %r for %s", version, profile_id)
                    return None
                api_url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/tags/{version}"
            else:
                api_url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/latest"
            try:
                release = _safe_api_get_json(api_url)  # SSRF-allowlisted (M-2)
                resolved_version = release.get("tag_name", version)
                assets = release.get("assets", [])
            except (requests.RequestException, ValueError) as exc:
                log.error("GitHub API error for %s (version=%s): %s", profile_id, version, exc)
                return None

        # Find a .bin asset to download. Do NOT fall back to assets[0] — flashing an
        # arbitrary first release asset of any type is a supply-chain hazard.
        bin_asset = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".bin"):
                bin_asset = asset
                break
        if not bin_asset:
            log.error("No .bin asset in the %s release for %s — refusing to guess", resolved_version, profile_id)
            return None
        download_url = bin_asset.get("browser_download_url", "")
        if not download_url:
            log.error("No downloadable asset URL for %s", profile_id)
            return None
        try:
            filename = _safe_cache_name(bin_asset.get("name", f"{profile_id}.bin"))
        except ValueError as exc:
            log.error("Unsafe asset filename for %s: %s", profile_id, exc)
            return None

        # Sanitize the version tag for use as a directory name (path-traversal defense).
        safe_version = _safe_version_key(resolved_version)
        dest_dir = self.vault_dir / profile_id / safe_version
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        # Containment: the final path must resolve to inside the vault dir.
        try:
            dest_path.resolve().relative_to(self.vault_dir.resolve())
        except ValueError:
            log.error("Refusing firmware dest that escapes the vault: %s", dest_path)
            return None

        # Download (SSRF-safe redirect following + size cap), then verify integrity.
        try:
            log.info("Downloading %s v%s from %s", profile_id, resolved_version, download_url)
            downloaded = _safe_streamed_download(download_url, dest_path, progress_callback, filename)
            sha = _sha256_file(dest_path)
            log.info("Downloaded %s (%d bytes, sha256=%s)", dest_path.name, downloaded, sha[:16])
        except (requests.RequestException, OSError, ValueError) as exc:
            log.error("Download failed for %s: %s", profile_id, exc)
            if dest_path.exists():
                dest_path.unlink()
            return None

        # Integrity pinning: if the profile pins a sha256 for this version, ENFORCE it
        # (hard-fail + delete on mismatch). Otherwise warn — we cannot pin a moving
        # "latest" tag, so this is trust-on-first-use for unpinned firmware.
        pins = profile.get("firmware_sha256")
        expected = None
        if isinstance(pins, dict):
            expected = pins.get(resolved_version) or pins.get(version) or pins.get("latest")
        if expected:
            if sha.lower() != str(expected).strip().lower():
                log.error("SHA-256 MISMATCH for %s %s: expected %s got %s — DELETING",
                          profile_id, resolved_version, expected, sha)
                dest_path.unlink(missing_ok=True)
                return None
            log.info("SHA-256 pin verified for %s %s", profile_id, resolved_version)
        else:
            log.warning("No SHA-256 pin for %s %s — firmware stored unverified (TOFU). "
                        "Add a 'firmware_sha256' pin to the profile to enforce integrity.",
                        profile_id, resolved_version)
        resolved_version = safe_version

        # Update index
        with self._lock:
            if profile_id not in self._index:
                self._index[profile_id] = {}
            self._index[profile_id][resolved_version] = {
                "filename": filename,
                "path": str(dest_path),
                "sha256": sha,
                "size": downloaded,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_index()

        return dest_path

    # ── Cache queries ────────────────────────────────────────────────

    def get_cached(self, profile_id: str, version: str = "latest") -> Path | None:
        """Return the path to a cached firmware binary, or None if not cached.

        When version is ``"latest"``, returns the most recently downloaded
        version for the profile.
        """
        with self._lock:
            versions = self._index.get(profile_id, {})
            if not versions:
                return None

            if version == "latest":
                # Pick most recently downloaded
                best = None
                best_time = ""
                for v, info in versions.items():
                    dl_time = info.get("downloaded_at", "")
                    if dl_time > best_time:
                        best_time = dl_time
                        best = info
                if best:
                    p = Path(best["path"])
                    return p if p.exists() else None
            else:
                info = versions.get(version)
                if info:
                    p = Path(info["path"])
                    return p if p.exists() else None

        return None

    def list_cached(self) -> dict[str, list[str]]:
        """Return a dict of ``{profile_id: [versions]}`` for all cached firmware."""
        with self._lock:
            result = {}
            for pid, versions in self._index.items():
                valid_versions = []
                for v, info in versions.items():
                    if Path(info.get("path", "")).exists():
                        valid_versions.append(v)
                if valid_versions:
                    result[pid] = sorted(valid_versions)
            return result

    def get_cache_info(self, profile_id: str, version: str) -> dict[str, Any] | None:
        """Return metadata for a specific cached entry."""
        with self._lock:
            return self._index.get(profile_id, {}).get(version)

    # ── Update checking ──────────────────────────────────────────────

    def check_updates(self) -> list[dict[str, str]]:
        """Check GitHub for newer releases than what's cached.

        Returns:
            List of dicts with keys: ``profile_id``, ``cached_version``,
            ``latest_version``, ``name``.
        """
        updates: list[dict[str, str]] = []
        profiles = self.list_profiles()

        for prof in profiles:
            pid = prof["id"]
            profile_data = self._load_profile(pid)
            if not profile_data:
                continue

            urls = profile_data.get("firmware_urls", {})
            url = urls.get("latest")
            if not url:
                continue

            info = _parse_github_release_url(url)
            if not info:
                continue

            owner, repo = info
            try:
                api_url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/latest"
                release = _safe_api_get_json(api_url)  # SSRF-allowlisted (M-2)
                latest_tag = release.get("tag_name", "")
            except (requests.RequestException, ValueError):
                continue

            if not latest_tag:
                continue

            with self._lock:
                cached_entries = dict(self._index.get(pid, {}))
            cached_versions = list(cached_entries)

            # The index is keyed by the SANITIZED version (see download_firmware), so the raw
            # upstream tag must be sanitized the same way before the membership test — otherwise a
            # tag containing e.g. '+' is perpetually reported as an available update despite being cached.
            if _safe_version_key(latest_tag) not in cached_versions:
                # Report the NEWEST cached version (by download time, matching get_cached), not an
                # arbitrary dict-insertion-order key — otherwise "you have X, latest is Y" can name an
                # older cached version that happens to sort last.
                cached_str = (
                    max(cached_entries, key=lambda v: cached_entries[v].get("downloaded_at", ""))
                    if cached_entries else "none"
                )
                updates.append({
                    "profile_id": pid,
                    "name": prof["name"],
                    "cached_version": cached_str,
                    "latest_version": latest_tag,
                })

        return updates

    # ── Cache management ─────────────────────────────────────────────

    def clear_cache(self, profile_id: str | None = None) -> int:
        """Delete cached firmware binaries.

        Args:
            profile_id: If given, clear only that profile. Otherwise clear all.

        Returns:
            Number of files deleted.
        """
        deleted = 0
        with self._lock:
            if profile_id:
                profile_dir = self.vault_dir / profile_id
                if profile_dir.exists():
                    deleted = sum(1 for _ in profile_dir.rglob("*") if _.is_file())
                    shutil.rmtree(profile_dir)
                self._index.pop(profile_id, None)
            else:
                for pid in list(self._index.keys()):
                    profile_dir = self.vault_dir / pid
                    if profile_dir.exists():
                        deleted += sum(1 for _ in profile_dir.rglob("*") if _.is_file())
                        shutil.rmtree(profile_dir)
                self._index.clear()
            self._save_index()

        log.info("Vault cache cleared: %d files deleted", deleted)
        return deleted

    def vault_size_bytes(self) -> int:
        """Return total size of cached firmware in bytes."""
        total = 0
        with self._lock:
            for pid, versions in self._index.items():
                for v, info in versions.items():
                    p = Path(info.get("path", ""))
                    if p.exists():
                        total += p.stat().st_size
        return total
