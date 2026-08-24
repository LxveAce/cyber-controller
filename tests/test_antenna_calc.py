"""Antenna length calculator tests. The 433 MHz case is pinned to the worked example that seeded the
feature (lambda = 69.2 cm, quarter-wave = 17.3 cm / 6.8 in) so the physics can't silently drift."""
from __future__ import annotations

import math

import pytest

from src.core.antenna_calc import (
    C,
    band_label,
    compute,
    format_report,
    freq_label,
    normalize_freq,
)


def test_speed_of_light_is_exact():
    assert C == 299_792_458


def test_433mhz_matches_the_worked_example():
    r = compute("433", "mhz")
    assert r["freq_hz"] == pytest.approx(433e6)
    assert r["freq_label"] == "433 MHz"
    assert "433" in r["band"]
    # lambda = c / f = 0.6924 m = 69.2 cm
    assert r["wavelength"]["cm"] == pytest.approx(69.24, abs=0.05)
    q = r["elements"]["quarter"]
    assert q["cm"] == pytest.approx(17.31, abs=0.05)   # 17.3 cm in the video
    assert q["in"] == pytest.approx(6.81, abs=0.05)    # 6.8 in in the video


def test_24ghz_wifi():
    r = compute(2.4, "ghz")
    assert r["freq_hz"] == pytest.approx(2.4e9)
    assert "2.4 GHz" in r["band"]
    assert r["wavelength"]["cm"] == pytest.approx(12.49, abs=0.05)
    assert r["elements"]["quarter"]["cm"] == pytest.approx(3.12, abs=0.05)


@pytest.mark.parametrize("text,hz", [
    ("2.4GHz", 2.4e9),
    ("2.4 ghz", 2.4e9),
    ("433MHz", 433e6),
    ("433mhz", 433e6),
    ("915", 915e6),          # bare number uses the default unit (mhz)
    ("125khz", 125e3),
    ("1090 MHz", 1090e6),
])
def test_string_parsing(text, hz):
    assert normalize_freq(text) == pytest.approx(hz)


def test_numeric_with_unit():
    assert normalize_freq(915, "mhz") == pytest.approx(915e6)
    assert normalize_freq(2.4, "ghz") == pytest.approx(2.4e9)


def test_velocity_factor_scales_length():
    # values are rounded to 0.1 mm for display, so compare within a sub-mm tolerance
    full = compute("433", "mhz")["wavelength"]["m"]
    vf = compute("433", "mhz", velocity_factor=0.95)["wavelength"]["m"]
    assert vf == pytest.approx(full * 0.95, abs=1e-3)


def test_all_element_fractions_present_and_ordered():
    els = compute("433", "mhz")["elements"]
    assert set(els) == {"full", "five_eighths", "half", "quarter", "eighth"}
    # the fractions hold (within display rounding of 0.1 mm)
    assert els["quarter"]["m"] == pytest.approx(els["half"]["m"] / 2, abs=1e-3)
    assert els["half"]["m"] == pytest.approx(els["full"]["m"] / 2, abs=1e-3)
    assert els["five_eighths"]["m"] == pytest.approx(els["full"]["m"] * 0.625, abs=1e-3)


def test_band_label_unknown_is_empty_not_fabricated():
    assert band_label(700e6) == ""          # not in our tables
    assert "915" in band_label(915e6)
    assert freq_label(125e3) == "125 kHz"


@pytest.mark.parametrize("bad", [0, -5, "abc", "12jz"])
def test_bad_frequency_raises(bad):
    with pytest.raises(ValueError):
        normalize_freq(bad)


@pytest.mark.parametrize("value", ["433", 433])  # a bare number (string or numeric)...
def test_bad_unit_raises_for_both_input_types(value):
    # ...with an unknown fallback unit must error, not silently default to MHz (string branch used to)
    with pytest.raises(ValueError):
        normalize_freq(value, unit="lightyears")


@pytest.mark.parametrize("vf", [0, -0.1, 1.5, 2])
def test_bad_velocity_factor_raises(vf):
    with pytest.raises(ValueError):
        compute("433", "mhz", velocity_factor=vf)


def test_report_is_readable_text():
    txt = format_report(compute("433", "mhz"))
    assert "433 MHz" in txt
    assert "Quarter-wave" in txt
    assert "cm" in txt and "in" in txt
    assert not math.isnan(compute("433", "mhz")["wavelength"]["m"])
