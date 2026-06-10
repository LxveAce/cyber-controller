"""Tests for ``src.core.flash_core`` — pure-stdlib firmware flash core.

Covered (no network, no esptool, no device):
    * ``_bootloader_offset`` per-chip offsets (incl. the ESP32-C5 0x2000 gotcha);
    * the profile registry size and ``get_profile`` lookups;
    * the ``_safe_cache_name`` path-traversal sink defense;
    * the ``_require_allowed_url`` SSRF allowlist (scheme + host).

``flash_core`` imports only the standard library, so it loads without any of the
heavy optional deps; the ``importorskip`` is belt-and-suspenders.
"""

from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


# ── _bootloader_offset ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "chip, expected",
    [
        # classic ESP32 + S2 -> 0x1000
        ("esp32", "0x1000"),
        ("esp32s2", "0x1000"),
        # S3 + most RISC-V parts -> 0x0
        ("esp32s3", "0x0"),
        ("esp32c3", "0x0"),
        ("esp32c6", "0x0"),
        # the C5 special case -> 0x2000 (flashing at 0x0 yields a board that never boots)
        ("esp32c5", "0x2000"),
    ],
)
def test_bootloader_offset(chip: str, expected: str) -> None:
    assert flash_core._bootloader_offset(chip) == expected


# ── Profile registry ─────────────────────────────────────────────────

def test_profiles_registry_size() -> None:
    assert len(flash_core.PROFILES) >= 15


def test_get_profile_marauder() -> None:
    prof = flash_core.get_profile("marauder")
    assert prof.id == "marauder"
    # marauder is the only profile that drives the suicide bundle flow.
    assert prof.supports_suicide is True


def test_get_profile_bruce() -> None:
    prof = flash_core.get_profile("bruce")
    assert prof.id == "bruce"
    # Bruce ships a merged single .bin flashed at 0x0.
    assert prof.image_model == flash_core.IMAGE_MERGED


def test_get_profile_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        flash_core.get_profile("does-not-exist")


# ── _safe_cache_name (path-traversal sink defense) ───────────────────

@pytest.mark.parametrize(
    "bad_name",
    [
        "..",                 # parent-dir token
        "../evil.bin",        # posix traversal
        "..\\evil.bin",       # windows traversal
        "/abs/evil.bin",      # absolute posix
        "a/b",                # nested (separator)
        "a\\b",               # nested (windows separator)
        "C:\\x",              # drive prefix
        "",                   # empty
        ".",                  # current-dir token
    ],
)
def test_safe_cache_name_rejects(bad_name: str) -> None:
    with pytest.raises(ValueError):
        flash_core._safe_cache_name(bad_name)


def test_safe_cache_name_accepts_plain_basename() -> None:
    assert flash_core._safe_cache_name("fw.bin") == "fw.bin"


# ── _require_allowed_url (SSRF allowlist) ────────────────────────────

def test_require_allowed_url_accepts_https_github() -> None:
    url = "https://github.com/justcallmekoko/ESP32Marauder/releases/latest"
    assert flash_core._require_allowed_url(url) == url


def test_require_allowed_url_accepts_objects_githubusercontent() -> None:
    # GitHub release downloads 302 to objects.githubusercontent.com — must be allowed.
    url = "https://objects.githubusercontent.com/some/asset.bin"
    assert flash_core._require_allowed_url(url) == url


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://github.com/foo/bar",                 # non-https scheme
        "https://evil.com/payload.bin",              # off-allowlist host
        "https://169.254.169.254/latest/meta-data",  # cloud metadata SSRF target
        "ftp://github.com/x",                         # non-https scheme
        "https://github.com.evil.com/x",              # suffix-spoof host
        "",                                           # empty
    ],
)
def test_require_allowed_url_rejects(bad_url: str) -> None:
    with pytest.raises(ValueError):
        flash_core._require_allowed_url(bad_url)
