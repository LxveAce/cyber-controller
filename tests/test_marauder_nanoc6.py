"""Regression: Marauder upstream v1.12.3 (2026-06-22) added the **M5NanoC6** board — an **ESP32-C6**.

The profile's chip_map had no rule for it, so it fell to the classic-`esp32` default. That is the wrong chip.
The load-bearing effect is the esptool **`--chip`** argument the resolved chip feeds: `--chip esp32` against a
real ESP32-C6 is refused by esptool's chip-magic check *before any write* (a failed flash), while `--chip
esp32c6` flashes cleanly. (The C6's second-stage bootloader also sits at 0x0 — it's in `_BOOTLOADER_0` — vs
classic ESP32's 0x1000, which matters on the full/blank-board path.) Either way the classic-esp32 default was
wrong; this locks `m5nanoc6 → esp32c6`, and asserts every other live v1.12.3 variant is unchanged.

Asset names are the real ones from the live justcallmekoko/ESP32Marauder v1.12.3 release. Network mocked.
"""
from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

# Every .bin in the live v1.12.3 release, with its EXPECTED chip family. Only m5nanoc6 changed with this fix;
# the rest are the profile's prior classifications (guards against a future rule accidentally reclassifying one).
_EXPECTED_CHIP = {
    "cyd_2432S024_guition": "esp32", "cyd_2432S028": "esp32", "cyd_2432S028_2usb": "esp32",
    "cyd_3_5_inch": "esp32", "esp32c5devkitc1": "esp32c5", "esp32_lddb": "esp32", "flipper": "esp32s2",
    "kit": "esp32", "m5cardputer": "esp32s3", "m5cardputer_adv": "esp32s3", "m5nanoc6": "esp32c6",
    "m5stickc_plus": "esp32", "m5stickc_plus2": "esp32", "marauder_dev_board_pro": "esp32",
    "marauder_v7": "esp32", "mini": "esp32", "mini_v3": "esp32c5", "multiboardS3": "esp32s3",
    "old_hardware": "esp32", "rev_feather": "esp32s2", "v6": "esp32", "v6_1": "esp32", "v8": "esp32",
}


def _name(stub):
    return f"esp32_marauder_v1_12_3_20260622_{stub}.bin"


def _resolved(monkeypatch):
    assets = [{"name": _name(s), "browser_download_url": f"https://x/{s}"} for s in _EXPECTED_CHIP]
    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v1.12.3", assets))
    _tag, out = flash_core.get_profile("marauder").latest_release()
    return {a["name"]: a for a in out}


def test_m5nanoc6_classifies_as_esp32c6(monkeypatch):
    nano = _resolved(monkeypatch)[_name("m5nanoc6")]
    assert nano["chip"] == "esp32c6", "M5NanoC6 must resolve to esp32c6, not the classic-esp32 default"
    assert nano["label"] == "M5NanoC6 (ESP32-C6)", "M5NanoC6 must get its real label, not the raw filename"


def test_all_v1_12_3_variants_classify_as_expected(monkeypatch):
    # exactly one board changed (m5nanoc6→esp32c6); every other real asset keeps its prior chip.
    by = _resolved(monkeypatch)
    for stub, chip in _EXPECTED_CHIP.items():
        assert by[_name(stub)]["chip"] == chip, f"{stub}: expected {chip}, got {by[_name(stub)]['chip']}"
    c6 = [s for s, c in _EXPECTED_CHIP.items() if by[_name(s)]["chip"] == "esp32c6"]
    assert c6 == ["m5nanoc6"], f"exactly m5nanoc6 should be esp32c6, got {c6}"


def test_c6_is_a_first_class_chip_at_0x0():
    # C6 bootloader @ 0x0 (in _BOOTLOADER_0), not classic 0x1000 / C5 0x2000 — matters on the full-flash path.
    assert "esp32c6" in flash_core._BOOTLOADER_0
    assert flash_core._bootloader_offset("esp32c6") == "0x0"
