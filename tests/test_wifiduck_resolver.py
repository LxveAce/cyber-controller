"""WiFiDuck (SpacehuhnTech/WiFiDuck) must flash ONLY its two ESP8266 images, never a wrong-chip asset.

The v1.1.0 release ships five assets across THREE different MCUs: two esp8266 ``.bin`` (dstike + malduino_w),
an ATSAMD21 (ARM Cortex-M0) ``.bin`` + ``.uf2``, and an ATmega32u4 ``.hex``. The profile's esptool/esp8266
path must select exactly the two esp8266 ``.bin`` and drop the rest — an atsamd21 ``.bin`` slipping through
would be cold-flashed to an esp8266 via esptool (WRONG chip). ``exclude_substrings`` is case-sensitive, so the
profile lists the real (lowercase) chip tokens; these lock the actual asset names so an upstream rename or a
profile edit that breaks the filter is caught here rather than on a device. Network mocked; on-device
correctness stays the Stage-5 hardware gate. Mirrors tests/test_halehound_ota.py.
"""
from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

# The actual SpacehuhnTech/WiFiDuck v1.1.0 release assets, verified against the GitHub API.
_ASSETS = [
    {"name": "dstike_wifi_duck_atmega32u4_v1.1.0.hex", "browser_download_url": "https://x/atmega-hex"},
    {"name": "dstike_wifi_duck_esp8266_v1.1.0.bin", "browser_download_url": "https://x/dstike-esp"},
    {"name": "malduino_w_atsamd21_v1.1.0.bin", "browser_download_url": "https://x/samd-bin"},
    {"name": "malduino_w_atsamd21_v1.1.0.uf2", "browser_download_url": "https://x/samd-uf2"},
    {"name": "malduino_w_esp8266_v1.1.0.bin", "browser_download_url": "https://x/malduino-esp"},
]


def _resolve(monkeypatch, assets):
    prof = flash_core.get_profile("wifi_duck")
    assert prof.image_model == flash_core.IMAGE_MERGED
    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v1.1.0", [dict(a) for a in assets]))
    _tag, out = prof.latest_release()
    return out


def test_wifiduck_selects_only_the_two_esp8266_bins(monkeypatch):
    out = _resolve(monkeypatch, _ASSETS)
    names = sorted(a["name"] for a in out)
    assert names == [
        "dstike_wifi_duck_esp8266_v1.1.0.bin",
        "malduino_w_esp8266_v1.1.0.bin",
    ], names
    # every selected asset is an esp8266 merged image flashed at 0x0 — no wrong-chip target
    assert all(a.get("chip") == "esp8266" for a in out), out
    assert all(a.get("offset") == "0x0" and a.get("merged") is True for a in out), out


def test_wifiduck_never_offers_a_non_esp8266_asset(monkeypatch):
    # the ATSAMD21 image (ARM Cortex-M0, NOT esp8266) must never be offered — esptool would flash the wrong
    # chip. The .hex/.uf2 are dropped by suffix; the atsamd21 .bin by exclude_substrings.
    out = _resolve(monkeypatch, _ASSETS)
    assert not any("atsamd21" in a["name"].lower() for a in out), out
    assert not any("atmega" in a["name"].lower() for a in out), out
    assert not any(a["name"].endswith((".hex", ".uf2")) for a in out), out
