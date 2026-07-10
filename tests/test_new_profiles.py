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
        "default_fragment": "mclite-v",  # prefer_fragment must pick the T-Deck (mclite-v), not cands[0]
        # longest_substring label_map must label each bin for its OWN device (never cross them):
        "labels": {
            "mclite-v0.4.1.bin": "LilyGo T-Deck Plus (SX1262 LoRa)",
            "mclite-watch-v0.4.1.bin": "LilyGo T-Watch Ultra (SX1262 LoRa)",
        },
    },
    "bit_pirate": {
        "assets": [
            {"name": "bit_pirate_16_s3devkit.bin", "browser_download_url": "https://x/f"},
            {"name": "bit_pirate_16_cardputer.bin", "browser_download_url": "https://x/g"},
            {"name": "bit_pirate_16_xiaos3.bin", "browser_download_url": "https://x/h"},
        ],
        "expect_bins": 3,
    },
    # esp8266_deauther (CC-12) — the first esp8266-chip profile: a fixed chip_map onto 'esp8266',
    # 37 board-specific merged bins @ 0x0, default variant prefers the Wemos D1 mini fragment.
    "esp8266_deauther": {
        "chip": "esp8266",
        "assets": [
            {"name": "esp8266_deauther_2.6.1_WEMOS_D1_MINI.bin", "browser_download_url": "https://x/i"},
            {"name": "esp8266_deauther_2.6.1_NODEMCU.bin", "browser_download_url": "https://x/j"},
            {"name": "esp8266_deauther_2.6.1_DSTIKE_DEAUTHER_V3.bin", "browser_download_url": "https://x/k"},
        ],
        "expect_bins": 3,
        "default_fragment": "WEMOS_D1_MINI",  # prefer_fragment must pick the D1 mini, not just cands[0]
    },
}


@pytest.mark.parametrize("pid", sorted(CASES))
def test_new_profile_resolver(pid, monkeypatch):
    c = CASES[pid]
    chip = c.get("chip", "esp32s3")
    prof = flash_core.get_profile(pid)
    assert prof.image_model == flash_core.IMAGE_MERGED, f"{pid}: expected merged single-bin"
    assert prof.app_offset(chip) == "0x0", f"{pid}: merged image flashes at 0x0"

    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v", [dict(a) for a in c["assets"]]))
    _tag, assets = prof.latest_release()

    assert len(assets) == c["expect_bins"], f"{pid}: non-.bin assets must be excluded"
    assert all(a["chip"] == chip for a in assets), f"{pid}: all targets {chip}"
    assert all(a.get("offset") == "0x0" and a.get("merged") is True for a in assets), \
        f"{pid}: merged @ 0x0"
    default = prof.default_variant(assets, chip)
    assert default in assets, f"{pid}: default_variant picks a real asset"
    if c.get("default_fragment"):
        assert c["default_fragment"] in default["name"], \
            f"{pid}: default_variant must honor the prefer_fragment order"
    if c.get("labels"):
        by_name = {a["name"]: a for a in assets}
        for name, want in c["labels"].items():
            assert by_name[name].get("label") == want, \
                f"{pid}: label_map must label {name!r} as {want!r} (never cross devices)"
