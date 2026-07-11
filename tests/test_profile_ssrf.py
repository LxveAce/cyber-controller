"""SSRF safety for the hybrid (data-driven) profile engine.

Profile JSON declares URLs (resolver api_url / pinned url_sources). A malicious or third-party
profile must NOT be able to make the app fetch from an arbitrary host. Verified at BOTH layers:
load-time validation (build_generic_profile) and the runtime fetch chokepoint (_require_allowed_url).
"""

from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


def _gh(api: str) -> dict:
    return {
        "id": "evil", "core_id": "evil", "image_model": "merged-single-bin", "app_offset": "0x0",
        "support_files": None, "resolver": "github_release", "backend": "esptool",
        "resolver_params": {
            "api_url": api,
            "asset_match": {"include_suffixes": [".bin"]},
            "chip_map": {"strategy": "fixed", "chip": "esp32"},
            "emit": {"offset": "0x0", "merged": True},
        },
    }


def test_loadtime_rejects_nonallowlisted_api_url():
    with pytest.raises(ValueError):
        flash_core.build_generic_profile(_gh("https://evil.example.com/releases/latest"))


def test_loadtime_rejects_nonhttps_api_url():
    with pytest.raises(ValueError):
        flash_core.build_generic_profile(_gh("http://api.github.com/x"))


def test_loadtime_rejects_pinned_evil_url_source():
    cfg = {
        "id": "evilp", "core_id": "evilp", "image_model": "merged-single-bin", "app_offset": "0x0",
        "support_files": None, "resolver": "pinned_release", "backend": "esptool",
        "resolver_params": {
            "tag": "v1",
            "url_sources": {"release": "https://evil.example.com/d",
                            "raw": "https://raw.githubusercontent.com/x/y/main"},
            "assets": [{"name": "a.bin", "source": "release", "chip": "esp32", "offset": "0x0", "merged": True}],
        },
    }
    with pytest.raises(ValueError):
        flash_core.build_generic_profile(cfg)


def test_runtime_chokepoint_blocks_evil_api():
    # Bypass load-time validation by constructing directly — the runtime fetch must STILL refuse.
    gp = flash_core.GenericProfile(_gh("https://evil.example.com/releases/latest"))
    with pytest.raises(ValueError):
        gp.latest_release()


def test_bundled_profiles_all_pass_url_validation():
    # Every shipped profile uses allowlisted hosts -> none rejected at load; registry intact.
    # 38 = 18 original + trex/mclite/bit_pirate + hydra32 + flipper_roguemaster + m5stick_nemo +
    # esp8266_deauther + m5gotchi/porkchop + esp32_wifi_pentest + wifi_duck + nrf_bluenullifier2 +
    # bluestress + esp_at + meshcore + drone_mesh_mapper + nautilus + rnode + esp32_wardriver + ble_collector
    # + rnode_nrf + whad_butterfly (round-2 nrf_dfu/uf2 profiles bundled into 1.7.0).
    # (all drop-in JSON; github/raw hosts, or the local resolver with no URLs). ble_collector is pinned_release.
    assert len(flash_core.PROFILES) == 44
