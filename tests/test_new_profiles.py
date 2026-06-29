"""Smoke tests for newly-added drop-in JSON firmware profiles that have NO hardcoded
oracle class (T-REX, MCLite, ESP32 Bit Pirate — added 2026-06-29 via the hybrid model).

Validates the resolver config (assets route to the right chip/offset/merged, non-.bin
assets excluded, image_model/app_offset correct). Network mocked. Real on-device flash
correctness remains the Stage-5 hardware gate.
"""

from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

CASES = {
    "trex": {
        "assets": [
            {"name": "T-Rex-v1.0-TDeck.bin", "browser_download_url": "https://x/a"},
            {"name": "T-Rex-v1.0-TDeck-Plus.bin", "browser_download_url": "https://x/b"},
        ],
        "expect_bins": 2,
    },
    "mclite": {
        "assets": [
            {"name": "mclite-v0.4.1.bin", "browser_download_url": "https://x/c"},
            {"name": "mclite-watch-v0.4.1.bin", "browser_download_url": "https://x/d"},
            {"name": "mclite_config_tool.html", "browser_download_url": "https://x/e"},
        ],
        "expect_bins": 2,  # the .html must be excluded
    },
    "bit_pirate": {
        "assets": [
            {"name": "bit_pirate_16_s3devkit.bin", "browser_download_url": "https://x/f"},
            {"name": "bit_pirate_16_cardputer.bin", "browser_download_url": "https://x/g"},
            {"name": "bit_pirate_16_xiaos3.bin", "browser_download_url": "https://x/h"},
        ],
        "expect_bins": 3,
    },
}


@pytest.mark.parametrize("pid", sorted(CASES))
def test_new_profile_resolver(pid, monkeypatch):
    c = CASES[pid]
    prof = flash_core.get_profile(pid)
    assert prof.image_model == flash_core.IMAGE_MERGED, f"{pid}: expected merged single-bin"
    assert prof.app_offset("esp32s3") == "0x0", f"{pid}: merged image flashes at 0x0"

    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v", [dict(a) for a in c["assets"]]))
    _tag, assets = prof.latest_release()

    assert len(assets) == c["expect_bins"], f"{pid}: non-.bin assets must be excluded"
    assert all(a["chip"] == "esp32s3" for a in assets), f"{pid}: all targets esp32s3"
    assert all(a.get("offset") == "0x0" and a.get("merged") is True for a in assets), \
        f"{pid}: merged @ 0x0"
    assert prof.default_variant(assets, "esp32s3") in assets, f"{pid}: default_variant picks a real asset"
