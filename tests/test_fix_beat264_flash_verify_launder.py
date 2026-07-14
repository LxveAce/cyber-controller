"""Beat 264 — flash_engine pinned-SHA verify-launder (cc-deep-audit-10 [3] MED).

`_flash_esptool` ran the pinned-firmware integrity gate `flash_core.verify_sha256(app_path,
variant["sha256"], ...)` INSIDE the same try/except that wraps the download, and the broad
`except Exception` fell through to `_flash_offline_fallback(...)`. So a hash mismatch — a TAMPER
signal — was treated exactly like a network failure and silently flashed the cached offline-vault
copy, which `_flash_offline_fallback` never re-verifies against the pin (get_cached stores it
TOFU/unverified). The gate that exists to reject a tampered/MITM'd image was bypassed instead of
aborting. The sibling paths (`_resolve_binary`, `_flash_qflipper`, `_flash_rtl8720`) already
hard-fail on the same exception; only `_flash_esptool` laundered it. 44 real profiles carry a
variant `sha256` pin and flash through `_flash_esptool`, so once any is cached the bypass is real.

Fix: verify runs OUTSIDE the download try/except; a mismatch hard-fails (returns False, no
fallback), while a genuine download failure still falls back to the offline vault (its purpose).

Discriminating: test_pinned_sha_mismatch_hard_fails_no_offline_fallback fails on HEAD (the tamper
failure calls the fallback and returns its True) and passes on the fix. The download-failure guard
passes on both. Network + the destructive write are mocked.
"""
from __future__ import annotations

import pytest

from src.core import flash_core
from src.core.flash_engine import FirmwareProfile, FlashEngine

_PIN = "a" * 64
_VARIANT = {"name": "app.bin", "url": "https://example/app.bin", "chip": "esp32",
            "sha256": _PIN, "offset": "0x10000"}


class _FakeCore:
    def latest_release(self):
        return ("v1", [_VARIANT])

    def variants_for_chip(self, assets, chip):
        return [_VARIANT]

    def default_variant(self, assets, chip):
        return _VARIANT


def _profile(fallback_path):
    # core_id must be a real flash_core.PROFILES key so _flash_esptool doesn't bail early;
    # get_profile is monkeypatched to _FakeCore, so which real key we pick doesn't matter.
    return FirmwareProfile(chip="esp32", core_id="bluejammer-esp32",
                           offline_fallback_path=str(fallback_path))


def _engine_with_spied_fallback(monkeypatch):
    engine = FlashEngine()
    calls: list = []

    def spy(*a, **k):
        calls.append((a, k))
        return True  # pretend the offline-vault flash "succeeded" (HEAD's tamper-failure behavior)

    monkeypatch.setattr(engine, "_flash_offline_fallback", spy)
    return engine, calls


def test_pinned_sha_mismatch_hard_fails_no_offline_fallback(monkeypatch, tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"downloaded-but-tampered")
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: _FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(flash_core, "download_to", lambda u, c, n, cap: str(app))

    def tampered_verify(path, expected, on_line):
        raise ValueError("SHA-256 mismatch (pinned firmware integrity check failed)")

    monkeypatch.setattr(flash_core, "verify_sha256", tampered_verify)
    engine, fallback_calls = _engine_with_spied_fallback(monkeypatch)

    result = engine._flash_esptool("COM5", _profile(app), None)

    assert result is False, "a pinned-hash mismatch must HARD-FAIL, not report success"
    assert fallback_calls == [], "a tamper failure must NOT be laundered into an offline flash"


def test_download_failure_still_uses_offline_fallback(monkeypatch, tmp_path):
    """No-regression: a genuine DOWNLOAD failure still falls back to the offline vault.
    Passes on HEAD and on the fix — guards that pulling verify out of the try/except kept the
    legitimate offline-fallback-on-download-failure path."""
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: _FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))

    def broken_download(u, c, n, cap):
        raise OSError("network down")

    monkeypatch.setattr(flash_core, "download_to", broken_download)
    engine, fallback_calls = _engine_with_spied_fallback(monkeypatch)

    result = engine._flash_esptool("COM5", _profile(tmp_path / "cached.bin"), None)

    assert result is True, "a download failure with a cached copy should flash the offline vault"
    assert len(fallback_calls) == 1, "the offline fallback must run on a download failure"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
