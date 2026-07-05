"""Unit tests for supported_boards_text — the firmware supported-boards tooltip (F3).

Pure helper, no Qt: builds the hover text listing which boards a firmware profile
supports, so the flash picker can show it without touching the item's label.
"""
import glob
from pathlib import Path

from src.core.flash_engine import FirmwareProfile, supported_boards_text

_PROFILE_DIR = Path("src/config/profiles")


def _profile(stem: str) -> FirmwareProfile:
    return FirmwareProfile.from_file(_PROFILE_DIR / f"{stem}.json")


def test_lists_real_boards_with_chip():
    text = supported_boards_text(_profile("airtag_scanner"))
    assert text.startswith("Supported boards:")
    assert "ESP32 Generic (esp32)" in text
    assert "ESP32-S3 Generic (esp32s3)" in text
    assert text.count("•") >= 2  # one bullet per board


def test_empty_boards_returns_empty_string():
    # a bare 'custom' profile lists no boards -> caller skips the tooltip
    assert supported_boards_text(_profile("custom")) == ""


def test_dedupes_and_handles_missing_fields():
    p = FirmwareProfile(boards=[
        {"name": "CYD", "chip": "esp32"},
        {"name": "CYD", "chip": "esp32"},   # duplicate -> collapsed
        {"name": "NoChip"},                  # name only
        {"chip": "esp32s3"},                 # chip only
        {},                                   # nothing usable -> skipped
        "not-a-dict",                         # wrong type -> skipped
    ])
    text = supported_boards_text(p)
    assert text.count("CYD (esp32)") == 1
    assert "NoChip" in text
    assert "esp32s3" in text


def test_every_shipped_profile_is_safe():
    # never raises, always returns a str, for every real profile on disk
    for fp in glob.glob(str(_PROFILE_DIR / "*.json")):
        out = supported_boards_text(FirmwareProfile.from_file(fp))
        assert isinstance(out, str)
