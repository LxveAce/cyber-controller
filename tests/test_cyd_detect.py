"""Tests for ``src.core.cyd_detect`` — CYD board/panel detection report parsing.

Covered (no serial, no esptool, no device): ``parse_report`` maps real probe serial output to the
right Marauder variant, and the bundled probe image exists. The report strings below are captured
verbatim from the probe running on real hardware (2-USB ST7789 CYD and a bare ESP32).
"""

from __future__ import annotations

import pytest

cyd_detect = pytest.importorskip("src.core.cyd_detect")
from src.core.cyd_detect import PROBE_BIN, parse_report  # noqa: E402


def _block(cyd, conf, ctrl, touch, variant, alive, ldr):
    return (
        "\n=====CYD_PROBE=====\n"
        f"CYD={cyd} CONF={conf} CONTROLLER={ctrl} TOUCH={touch}\n"
        f"VARIANT={variant}\n"
        f"D3=0x00000000 04=0x00000000 09=0x00610000 alive={alive} cap_i2c=0x00 LDR={ldr}\n"
        "=====END=====\n"
    )


def test_parse_2usb_st7789_cyd():
    raw = _block("yes", "high", "ST7789", "resistive", "cyd_2432S028_2usb", 1, 2368) * 2
    r = parse_report(raw)
    assert r.is_cyd
    assert r.confidence == "high"
    assert r.controller == "ST7789"
    assert r.variant == "cyd_2432S028_2usb"
    assert "ST7789" in r.label and "2-USB" in r.label
    assert r.ldr == 2368


def test_parse_bare_esp32_is_not_a_cyd():
    raw = _block("no", "none", "none", "resistive", "none", 0, 82)
    r = parse_report(raw)
    assert not r.is_cyd
    assert r.variant == ""  # 'none' normalizes to empty so nothing gets pre-selected
    assert "No CYD" in r.summary


def test_parse_ili9341_and_st7796():
    r1 = parse_report(_block("yes", "high", "ILI9341", "resistive", "cyd_2432S028", 1, 2100))
    assert r1.variant == "cyd_2432S028" and "ILI9341" in r1.label
    r2 = parse_report(_block("yes", "high", "ST7796", "resistive", "cyd_3_5_inch", 1, 1900))
    assert r2.variant == "cyd_3_5_inch" and "3.5" in r2.label


def test_parse_uses_last_complete_block():
    # A late-connecting reader may capture a partial first block then full ones; take the last full one.
    raw = "garbageCYD=partial\n" + _block("yes", "medium", "ST7789", "capacitive", "cyd_2432S024_guition", 1, 2600)
    r = parse_report(raw)
    assert r.variant == "cyd_2432S024_guition"
    assert r.touch == "capacitive"


def test_parse_empty_is_safe():
    r = parse_report("")
    assert not r.is_cyd and r.variant == "" and r.confidence == "none"


def test_probe_binary_is_bundled():
    assert PROBE_BIN.is_file(), f"probe image missing at {PROBE_BIN}"
    assert PROBE_BIN.stat().st_size > 100_000  # merged esp32 image, ~340 KB
