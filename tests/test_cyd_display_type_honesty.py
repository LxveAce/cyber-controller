"""Drift-lock on the CYD ``display_type`` metadata across every firmware profile.

``display_type`` is descriptive metadata today (no code selects a flash variant from it — verified:
it appears in no ``.py``), but a wrong value is a factual lie about the panel and a trap for a
future data-driven variant resolver. The physical ground truth is fixed by the panel silicon:

  * a 3.5" CYD (ESP32-2432S035 / QDtech E32R35T) drives an **ST7796** controller — NOT ILI9341;
  * a Guition CYD (2.4"/2-USB class) drives an **ST7789** — NOT ILI9341;
  * the classic single-USB 2.8" CYD (ESP32-2432S028) is ILI9341.

This is the same ground truth the on-device probe encodes (``tools/cyd_probe`` maps read-ID
``0x7796`` → cyd_3_5_inch). Locking it here stops a new profile from reintroducing the ili9341
mislabel that made a 3.5"/Guition board look like a 2.8" (wrong flash → blank/garbled screen).
"""
from __future__ import annotations

import json
from pathlib import Path

_PROFILE_DIR = Path(__file__).resolve().parent.parent / "src" / "config" / "profiles"
_PROFILES = sorted(_PROFILE_DIR.glob("*.json"))


def _boards_with_display():
    """Yield (profile, board_name, display_type) for each board that declares a display_type."""
    for path in _PROFILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for board in data.get("boards", []):
            if isinstance(board, dict) and "display_type" in board:
                yield path.name, str(board.get("name", "")), board["display_type"]


def test_profiles_exist():
    """Guard: the glob found the profile JSONs (else the drift-lock silently passes on nothing)."""
    assert _PROFILES, "no profile JSONs found — path resolution is wrong"
    assert list(_boards_with_display()), "no boards declare display_type — check the schema key"


def test_three_five_inch_cyd_is_st7796_not_ili9341():
    """Any 3.5" CYD board must be labelled st7796 (its real controller), never the ili9341 default
    that would flash it as a 2.8" and blank the screen."""
    offenders = [
        (prof, name, dt)
        for prof, name, dt in _boards_with_display()
        if "3.5" in name and dt != "st7796"
    ]
    assert not offenders, f"3.5\" CYD rows mislabelled (must be st7796): {offenders}"


def test_guition_cyd_is_st7789_not_ili9341():
    """Any Guition CYD board must be labelled st7789 (its real controller), never ili9341."""
    offenders = [
        (prof, name, dt)
        for prof, name, dt in _boards_with_display()
        if "guition" in name.lower() and dt != "st7789"
    ]
    assert not offenders, f"Guition CYD rows mislabelled (must be st7789): {offenders}"


def test_display_type_values_are_known_controllers():
    """Every declared display_type is a real controller token — catches a typo before it reaches a
    future data-driven resolver."""
    known = {"ili9341", "st7789", "st7796", "st7735", "ili9488", "gc9a01"}
    bad = [(prof, name, dt) for prof, name, dt in _boards_with_display() if dt not in known]
    assert not bad, f"unknown display_type token(s): {bad}"
