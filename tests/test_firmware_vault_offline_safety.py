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


# ── corrupt-but-valid-JSON index resilience ───────────────────────────────
import json as _json


@pytest.mark.parametrize("payload", ["null", "[1, 2, 3]", '"a string"', "42"])
def test_non_dict_index_starts_fresh(tmp_path, payload):
    """A valid-JSON-but-non-dict index (e.g. the literal `null` from a bad external write) must not
    make self._index a non-dict — otherwise the next op (list_cached/get_cached) crashes with
    AttributeError and the whole offline-cache feature is dead until the file is hand-repaired."""
    (tmp_path / fwv._INDEX_FILE).write_text(payload, encoding="utf-8")
    v = fwv.FirmwareVault(vault_dir=tmp_path)
    assert v._index == {}
    # The consumers that previously blew up now degrade cleanly.
    assert v.list_cached() == {}
    assert v.get_cached("marauder") is None


def test_save_index_is_atomic_a_failed_commit_keeps_the_old_index(tmp_path, monkeypatch):
    """A power loss while persisting vault_index.json must NOT discard the whole cache catalog.
    _save_index now writes a temp file + fsync + os.replace, so a failure at the atomic rename leaves
    the previous COMPLETE index on disk. The old bare write_text truncated the file at open, so the
    same crash left a partial/empty index and _load_index started fresh — silently losing every cached
    entry while the .bin dirs lingered (invisible to get_cached, unreachable by a catalog-wide
    clear_cache)."""
    import os

    v = fwv.FirmwareVault(vault_dir=tmp_path)
    v._index = {"marauder": {"version": "v1", "path": "x.bin"}}
    v._save_index()   # first, good write lands atomically

    v._index = {"marauder": {"version": "v1", "path": "x.bin"},
                "ghostesp": {"version": "v2", "path": "y.bin"}}

    def boom(*_a, **_k):
        raise OSError("simulated power loss at commit")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        v._save_index()
    monkeypatch.undo()

    reloaded = fwv.FirmwareVault(vault_dir=tmp_path)
    assert reloaded._index == {"marauder": {"version": "v1", "path": "x.bin"}}  # old index intact, not lost
