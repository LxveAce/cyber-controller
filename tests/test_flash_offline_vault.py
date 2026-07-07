"""The FirmwareVault "offline cache" contract, wired end-to-end.

A firmware the user pre-downloaded into the vault must ACTUALLY be flashed when the live
download can't run (no network) — instead of the flash failing despite the binary sitting in
the "offline cache". Before this was wired, ``FirmwareVault.get_cached()`` had zero callers:
the vault was write-only from the app's perspective and an offline flash failed with the
firmware present. The fix hands the cached path to the engine via
``FirmwareProfile.offline_fallback_path``; the engine flashes it ONLY when the release fetch or
download fails, so the online (board-aware variant) path is untouched.
"""

from __future__ import annotations

import pytest

flash_engine = pytest.importorskip("src.core.flash_engine")
from src.core.flash_engine import FirmwareProfile, FlashEngine  # noqa: E402


class _OfflineCore:
    """A core profile whose release fetch fails — simulates being offline."""

    def latest_release(self):
        raise RuntimeError("offline: getaddrinfo failed")


class _DownloadFailCore:
    """Release metadata resolves, but the binary download fails (offline mid-transfer / 404)."""

    def latest_release(self):
        return ("v1", [{"name": "esp32_marauder.bin", "url": "https://x/y.bin", "chip": "esp32"}])

    def variants_for_chip(self, assets, chip):
        return list(assets)

    def default_variant(self, assets, chip):
        return assets[0] if assets else None

    def app_offset(self, chip):
        return "0x0"

    def support_files(self, chip, cache, on_line):
        return None


class _RecordingCustom:
    """Stands in for flash_core's 'custom' local profile; records the flashed path."""

    def __init__(self):
        self.flashed_path = None
        self.flashed_chip = None

    def flash_local(self, port, chip, app_path, on_line, app_offset="0x0",
                    baud=921600, support=None, flash_freq=None):
        self.flashed_path = app_path
        self.flashed_chip = chip
        on_line(f"[custom] flashing {app_path}")
        return 0  # esptool-style success


def _patch_cores(monkeypatch, marauder_core, custom):
    def _get_profile(cid):
        return custom if cid == "custom" else marauder_core
    monkeypatch.setattr(flash_engine.flash_core, "get_profile", _get_profile)


def test_offline_flash_uses_vaulted_binary(monkeypatch, tmp_path):
    """Release fetch fails (offline) -> the vaulted binary is flashed and the flash SUCCEEDS."""
    cached = tmp_path / "marauder_cyd.bin"
    cached.write_bytes(b"\x00firmware-image\xff")

    custom = _RecordingCustom()
    _patch_cores(monkeypatch, _OfflineCore(), custom)

    eng = FlashEngine()
    prof = FirmwareProfile(
        name="Marauder",
        id="marauder",
        backend="esptool",
        chip="esp32",          # explicit -> no serial chip detection
        core_id="marauder",    # present in flash_core.PROFILES
        offline_fallback_path=str(cached),
    )
    msgs: list[str] = []
    ok = eng._flash_esptool("COM5", prof, lambda pct, msg: msgs.append(msg))

    assert ok is True                              # offline flash succeeds via the vault
    assert custom.flashed_path == str(cached)      # the vaulted binary is what got flashed
    assert custom.flashed_chip == "esp32"
    assert any("offline vault" in m.lower() for m in msgs)


def test_download_failure_falls_back_to_vaulted_binary(monkeypatch, tmp_path):
    """Metadata resolves but the binary download fails -> still flash the cached binary."""
    cached = tmp_path / "marauder.bin"
    cached.write_bytes(b"cached")

    custom = _RecordingCustom()
    _patch_cores(monkeypatch, _DownloadFailCore(), custom)
    monkeypatch.setattr(flash_engine.flash_core, "cache_dir", lambda: str(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("offline: connection reset")
    monkeypatch.setattr(flash_engine.flash_core, "download_to", _boom)

    eng = FlashEngine()
    prof = FirmwareProfile(
        name="Marauder", id="marauder", backend="esptool", chip="esp32",
        core_id="marauder", offline_fallback_path=str(cached),
    )
    ok = eng._flash_esptool("COM5", prof, None)

    assert ok is True
    assert custom.flashed_path == str(cached)


def test_offline_flash_without_vault_still_fails(monkeypatch, tmp_path):
    """Negative guard: no cached binary => the offline flash fails (no fake success)."""
    custom = _RecordingCustom()
    _patch_cores(monkeypatch, _OfflineCore(), custom)

    eng = FlashEngine()
    prof = FirmwareProfile(
        name="Marauder", id="marauder", backend="esptool", chip="esp32",
        core_id="marauder",  # no offline_fallback_path
    )
    ok = eng._flash_esptool("COM5", prof, None)

    assert ok is False
    assert custom.flashed_path is None  # nothing flashed


def test_missing_cached_file_is_not_flashed(monkeypatch, tmp_path):
    """A stale index entry pointing at a deleted file must NOT be treated as flashable."""
    custom = _RecordingCustom()
    _patch_cores(monkeypatch, _OfflineCore(), custom)

    eng = FlashEngine()
    prof = FirmwareProfile(
        name="Marauder", id="marauder", backend="esptool", chip="esp32",
        core_id="marauder",
        offline_fallback_path=str(tmp_path / "does_not_exist.bin"),
    )
    ok = eng._flash_esptool("COM5", prof, None)

    assert ok is False
    assert custom.flashed_path is None
