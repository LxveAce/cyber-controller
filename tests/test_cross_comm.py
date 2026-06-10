"""Tests for ``src.core.cross_comm`` rendering/sanitisation helpers.

Covered (pure, no device, no heavy deps):
    * ``_safe_render`` substitutes ONLY {mac}/{ssid}/{channel}, leaves unknown
      placeholders untouched, and strips control chars from a newline-bearing SSID
      (command-injection defense — 'a\\nreboot' must not produce a newline);
    * ``_sanitize_value`` removes control characters and caps the length.

``cross_comm`` imports only the standard library plus the pure ``Target`` model,
so no optional dep is required; the ``importorskip`` is belt-and-suspenders.
"""

from __future__ import annotations

import pytest

cross_comm = pytest.importorskip("src.core.cross_comm")

_MAX = cross_comm._MAX_VALUE_LEN


# ── _safe_render ─────────────────────────────────────────────────────

def test_safe_render_substitutes_all_placeholders() -> None:
    out = cross_comm._safe_render(
        "deauth {mac} ssid={ssid} ch={channel}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="CoffeeShop",
        channel=6,
    )
    assert out == "deauth AA:BB:CC:DD:EE:FF ssid=CoffeeShop ch=6"


def test_safe_render_ignores_unknown_placeholders() -> None:
    # Only {mac}/{ssid}/{channel} are recognised; anything else is left verbatim
    # (NOT passed through str.format, which would enable attribute traversal).
    out = cross_comm._safe_render(
        "{mac} {unknown} {ssid.__class__}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="net",
        channel=1,
    )
    assert "{unknown}" in out
    assert "{ssid.__class__}" in out
    assert out.startswith("AA:BB:CC:DD:EE:FF ")


def test_safe_render_strips_newline_bearing_ssid() -> None:
    # A crafted SSID 'a\nreboot' must not inject a second serial command.
    out = cross_comm._safe_render(
        "attack {ssid}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="a\nreboot",
        channel=1,
    )
    assert "\n" not in out
    assert "\r" not in out
    # The control char is removed; the surrounding text remains joined.
    assert out == "attack areboot"


def test_safe_render_non_numeric_channel_blanks() -> None:
    out = cross_comm._safe_render("ch={channel}", mac="", ssid="", channel="not-a-number")
    assert out == "ch="


# ── _sanitize_value ──────────────────────────────────────────────────

def test_sanitize_value_removes_control_chars() -> None:
    assert cross_comm._sanitize_value("a\nb\tc\r\x00d") == "abcd"


def test_sanitize_value_caps_length() -> None:
    long = "x" * (_MAX + 50)
    out = cross_comm._sanitize_value(long)
    assert len(out) == _MAX


def test_sanitize_value_coerces_non_str() -> None:
    assert cross_comm._sanitize_value(12345) == "12345"
