"""Unit tests for chip_match — advisory firmware/board chip compatibility (F4).

Pure logic behind the green/red flash-picker hint. Advisory only: 'match' / 'mismatch'
/ 'neutral', where neutral means "can't say" and the UI leaves the item its default colour.
"""
from pathlib import Path

from src.core.flash_engine import FirmwareProfile, chip_match

_DIR = Path("src/config/profiles")


def _p(stem: str) -> FirmwareProfile:
    return FirmwareProfile.from_file(_DIR / f"{stem}.json")


def _prof(chips: list[str]) -> FirmwareProfile:
    return FirmwareProfile(boards=[{"name": f"b{i}", "chip": c} for i, c in enumerate(chips)])


def test_match_normalizes_dashes_and_case():
    assert chip_match("esp32-s3", _prof(["esp32s3"])) == "match"
    assert chip_match("ESP32_S3", _prof(["esp32s3"])) == "match"
    assert chip_match("esp32s3", _prof(["esp32-s3"])) == "match"


def test_mismatch_when_chip_not_supported():
    assert chip_match("esp32s3", _prof(["esp32"])) == "mismatch"
    assert chip_match("esp32", _prof(["esp32s3", "esp32c3"])) == "mismatch"


def test_neutral_when_chip_unknown():
    for c in (None, "", "unknown", "UNKNOWN"):
        assert chip_match(c, _prof(["esp32"])) == "neutral"


def test_neutral_when_profile_has_no_chips():
    assert chip_match("esp32", _prof([])) == "neutral"
    assert chip_match("esp32", FirmwareProfile(boards=[{"name": "x"}])) == "neutral"


def test_real_profiles():
    # airtag_scanner lists both esp32 and esp32s3 boards
    assert chip_match("esp32s3", _p("airtag_scanner")) == "match"
    assert chip_match("esp32", _p("airtag_scanner")) == "match"
    # a bare custom profile has no boards -> neutral, never a false warning
    assert chip_match("esp32", _p("custom")) == "neutral"
