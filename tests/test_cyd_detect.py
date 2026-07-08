"""Tests for ``src.core.cyd_detect`` — CYD board/panel detection report parsing.

Covered (no serial, no esptool, no device): ``parse_report`` maps real probe serial output to the
right Marauder variant, and the bundled probe image exists. The report strings below are captured
verbatim from the probe running on real hardware (2-USB ST7789 CYD and a bare ESP32).
"""

from __future__ import annotations

import pytest

cyd_detect = pytest.importorskip("src.core.cyd_detect")
from src.core.cyd_detect import PROBE_BIN, parse_report  # noqa: E402


def _block(cyd, conf, ctrl, touch, variant, alive, ldr, r04="0x00000000", cap="0x00"):
    return (
        "\n=====CYD_PROBE=====\n"
        f"CYD={cyd} CONF={conf} CONTROLLER={ctrl} TOUCH={touch}\n"
        f"VARIANT={variant}\n"
        f"D3=0x00000000 04={r04} 09=0x00610000 alive={alive} cap_i2c={cap} LDR={ldr}\n"
        "=====END=====\n"
    )


def test_parse_2usb_st7789_cyd():
    # A real 2-USB board carries the ST7789 register signature (04 high byte 0x85), so it stays a
    # confident, unambiguous identification.
    raw = _block("yes", "high", "ST7789", "resistive", "cyd_2432S028_2usb", 1, 2368, r04="0x85000000") * 2
    r = parse_report(raw)
    assert r.is_cyd
    assert r.confidence == "high"
    assert not r.ambiguous
    assert r.controller == "ST7789"
    assert r.variant == "cyd_2432S028_2usb"
    assert "ST7789" in r.label and "2-USB" in r.label
    assert r.ldr == 2368


def test_unsupported_st7789_guess_is_ambiguous():
    # The owner's bug: a 2.8" ILI9341 whose 0xD3 read-ID fails to latch falls into the probe's ST7789
    # fallback with NO 04 register signature and NO capacitive touch, so the probe DEFAULTS to 2-USB and
    # (from liveness+LDR alone) still stamps CONF=high. Flashing that 2-USB build blanks the ILI9341 panel.
    # The host must re-derive an honest LOW confidence + ambiguous flag so the UI warns instead of silently
    # pre-selecting the wrong variant.
    raw = _block("yes", "high", "ST7789", "resistive", "cyd_2432S028_2usb", 1, 2100, r04="0x00000000", cap="0x00")
    r = parse_report(raw)
    assert r.is_cyd
    assert r.ambiguous is True
    assert r.confidence == "low", "an unsupported ST7789 guess must not keep the probe's CONF=high"
    assert "verify" in r.summary.lower() or "not positively" in r.summary.lower()


def test_parse_bare_esp32_is_not_a_cyd():
    raw = _block("no", "none", "none", "resistive", "none", 0, 82)
    r = parse_report(raw)
    assert not r.is_cyd
    assert r.responded  # the probe DID answer — this is a confirmed bare-ESP32 verdict
    assert r.variant == ""  # 'none' normalizes to empty so nothing gets pre-selected
    assert "No CYD" in r.summary


def test_parse_ili9341_and_st7796():
    # Positive read-ID matches (0x9341 / 0x7796) stay confident and unambiguous — no downgrade.
    r1 = parse_report(_block("yes", "high", "ILI9341", "resistive", "cyd_2432S028", 1, 2100))
    assert r1.variant == "cyd_2432S028" and "ILI9341" in r1.label
    assert r1.confidence == "high" and not r1.ambiguous
    r2 = parse_report(_block("yes", "high", "ST7796", "resistive", "cyd_3_5_inch", 1, 1900))
    assert r2.variant == "cyd_3_5_inch" and "3.5" in r2.label
    assert r2.confidence == "high" and not r2.ambiguous


def test_parse_uses_last_complete_block():
    # A late-connecting reader may capture a partial first block then full ones; take the last full one.
    raw = "garbageCYD=partial\n" + _block("yes", "medium", "ST7789", "capacitive", "cyd_2432S024_guition", 1, 2600)
    r = parse_report(raw)
    assert r.variant == "cyd_2432S024_guition"
    assert r.touch == "capacitive"


def test_parse_empty_is_safe():
    r = parse_report("")
    assert not r.is_cyd and r.variant == "" and r.confidence == "none"


def test_no_report_is_not_reported_as_bare_esp32():
    # A read that captured NO probe report (timeout, wrong firmware, missed reset window) must not be
    # dressed up as a confident "bare ESP32" verdict — that false negative would let the user flash the
    # wrong CYD build (blank/mirrored screen), defeating the whole safeguard. It must be a distinct
    # "no response" outcome so the caller/UI can say "detection failed, retry" instead.
    for raw in ("", "boot noise\r\nrst:0x1 (POWERON)\r\nno probe here\r\n"):
        r = parse_report(raw)
        assert not r.responded, f"expected no-response for {raw!r}"
        assert not r.is_cyd  # still not a CYD, but...
        assert "No CYD" not in r.summary  # ...must NOT claim a confirmed bare ESP32
        assert "No response" in r.summary


def test_real_report_marks_responded():
    # Sanity: a genuine probe report (CYD or not) flips responded True, so the "no response" branch
    # only fires when there truly was no report.
    r = parse_report(_block("yes", "high", "ILI9341", "resistive", "cyd_2432S028", 1, 2100))
    assert r.responded and r.is_cyd


def test_probe_binary_is_bundled():
    assert PROBE_BIN.is_file(), f"probe image missing at {PROBE_BIN}"
    assert PROBE_BIN.stat().st_size > 100_000  # merged esp32 image, ~340 KB
