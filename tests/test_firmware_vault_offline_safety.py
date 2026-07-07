"""Offline-cache safety tests for src/core/firmware_vault.py.

The vault stores ONE bare .bin per profile, and the offline-flash path
(flash_engine._flash_offline_fallback) writes that file as a MERGED blob at 0x0. That is only
correct for a merged-single-bin firmware. An app-only ('multi-file-offsets') profile (marauder,
esp32-div) ships an image meant to flash at 0x10000 on top of a separate bootloader/partitions/
boot_app0 boot chain the vault can neither store nor apply — so caching it would let an offline
flash write an app-only image at the wrong offset with no boot chain and BRICK the board.
download_firmware must refuse to cache such a profile (fail closed).

Pure logic: the GitHub API + profile loading are monkeypatched, so no network is touched.
"""
from __future__ import annotations

import pytest

fwv = pytest.importorskip("src.core.firmware_vault")

_GH = "https://github.com/o/r/releases/latest"


@pytest.fixture
def vault(tmp_path):
    return fwv.FirmwareVault(vault_dir=tmp_path)


def test_download_firmware_refuses_multi_file_profile(vault, monkeypatch):
    """A 'multi-file-offsets' (app-only) profile is refused BEFORE any network, and nothing is
    cached — because an offline flash of the bare app .bin at 0x0 with no boot chain bricks the board."""
    called = {"api": 0}

    def fake_api(url):
        called["api"] += 1
        return {"tag_name": "v1", "assets": [
            {"name": "esp32_marauder_old_hardware.bin",
             "browser_download_url": "https://github.com/o/r/releases/download/v1/x.bin"}]}

    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {
        "firmware_urls": {"latest": _GH}, "image_model": "multi-file-offsets"})

    assert vault.download_firmware("marauder") is None
    assert called["api"] == 0                       # refused before any GitHub call
    assert vault.get_cached("marauder") is None      # nothing was stored in the vault
    assert vault.list_cached() == {}


def test_multi_file_marker_matches_flash_core(monkeypatch):
    """Guard uses flash_core's IMAGE_MULTI constant, so it tracks the real profile marker
    ('multi-file-offsets' — the value marauder.json / esp32-div carry)."""
    assert fwv.IMAGE_MULTI == "multi-file-offsets"


def test_download_firmware_allows_merged_profile_past_guard(vault, monkeypatch):
    """A merged-single-bin profile is NOT refused: the guard lets it reach the release/API path
    (proving the refusal is specific to app-only firmware, not a blanket block)."""
    called = {"api": 0}

    def fake_api(url):
        called["api"] += 1
        return {"tag_name": "v1", "assets": []}  # no .bin -> returns None, but only AFTER the API call

    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {
        "firmware_urls": {"latest": _GH}, "image_model": "merged-single-bin"})

    vault.download_firmware("bruce")
    assert called["api"] == 1  # merged profile proceeded past the guard to the release query
