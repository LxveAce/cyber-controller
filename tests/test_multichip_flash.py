"""Multi-chip firmware profiles must AUTO-DETECT the chip, not pin the first board's chip.

Regression guard for the M5Stick flashing bug: m5stick_nemo lists M5StickC Plus2 (esp32) first, then
M5Cardputer / M5Stick-S3 (esp32s3). Pinning chip to boards[0] sent `--chip esp32` to the S3 boards, so
esptool aborted and the S3 builds couldn't even be selected. The whole multi-chip class (marauder,
esp32_div, ghost_esp, ...) had the same defect.
"""

from __future__ import annotations

import types

import pytest

profile_loader = pytest.importorskip("src.core.profile_loader")
flash_engine = pytest.importorskip("src.core.flash_engine")
from src.core.flash_engine import FirmwareProfile  # noqa: E402
from src.core.resources import resource_path  # noqa: E402

_MULTI = {"boards": [{"name": "A", "chip": "esp32"}, {"name": "B", "chip": "esp32s3"}]}
_SINGLE = {"boards": [{"name": "A", "chip": "esp32s3"}, {"name": "B", "chip": "esp32s3"}]}


def test_select_chip_multichip_returns_auto():
    assert profile_loader.select_chip(_MULTI) == "auto"


def test_select_chip_singlechip_returns_that_chip():
    assert profile_loader.select_chip(_SINGLE) == "esp32s3"


def test_select_chip_requested_overrides_multichip():
    assert profile_loader.select_chip(_MULTI, requested_chip="esp32c3") == "esp32c3"


def test_select_chip_named_board_returns_its_chip():
    assert profile_loader.select_chip(_MULTI, board_name="B") == "esp32s3"


def test_m5stick_nemo_profile_autodetects():
    prof = FirmwareProfile.from_file(resource_path("src", "config", "profiles", "m5stick_nemo.json"))
    assert prof.chip == "auto", "m5stick_nemo must auto-detect (esp32 StickCPlus2 + esp32s3 Cardputer/S3)"


def test_multichip_profiles_autodetect():
    for stem in ("marauder", "ghost_esp", "airtag_scanner"):
        prof = FirmwareProfile.from_file(resource_path("src", "config", "profiles", stem + ".json"))
        assert prof.chip == "auto", f"{stem} spans multiple chips and must auto-detect"


def test_esp32div_singlechip_after_legacy_removal():
    # The classic-ESP32 "DIV v1 (legacy)" board was removed in 1.6.1: the profile's chip_map is fixed to
    # esp32s3 and the boot chain is hardcoded to tools/esp32s3/, so a real classic ESP32 could only ever
    # be flashed wrong-chip S3 firmware. With it gone, esp32_div is single-chip esp32s3 (no auto-detect).
    prof = FirmwareProfile.from_file(resource_path("src", "config", "profiles", "esp32_div.json"))
    assert prof.chip == "esp32s3"


def test_singlechip_profile_stays_pinned():
    prof = FirmwareProfile.from_file(resource_path("src", "config", "profiles", "m5gotchi.json"))
    assert prof.chip == "esp32s3", "single-chip profile must keep its chip (no needless detection)"


def test_list_variants_multichip_unions_all_board_chips(monkeypatch):
    """A multi-chip/auto profile's picker must offer every board's build, not just esp32's."""
    fe = flash_engine.FlashEngine()

    class FakeCore:
        def latest_release(self):
            return ("v1", ["assets"])

        def variants_for_chip(self, assets, chip):
            return {
                "esp32": [{"name": "img_esp32"}],
                "esp32s3": [{"name": "img_s3"}],
            }.get(chip, [])

    monkeypatch.setattr(flash_engine.flash_core, "get_profile", lambda cid: FakeCore())
    prof = types.SimpleNamespace(
        core_id="marauder",  # any id present in flash_core.PROFILES
        chip="auto",
        boards=[{"chip": "esp32"}, {"chip": "esp32s3"}],
    )
    names = {v["name"] for v in fe.list_variants(prof)}
    assert names == {"img_esp32", "img_s3"}
