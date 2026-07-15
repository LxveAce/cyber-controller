"""The generic-ILI9341 Auto-flash confirm must RECOMMEND running Detect, not merely mention it.

CYD auto-detect Phase A, defect (2). A Marauder "Auto" flash on a chip we can't uniquely ID over USB
falls to the generic-ILI9341 build, which blanks CYD panels. 'Detect board (CYD)' auto-identifies
the panel, so it is the fix for exactly the "not sure which CYD this is" case — the confirm should
lead with it as the recommended step, not as a shortcut for users who already know the board.

The message builder is a pure staticmethod, so this asserts the copy without a modal dialog.
"""
from __future__ import annotations

import pytest

flash_tab = pytest.importorskip("src.ui.qt.flash_tab")
_msg = flash_tab.FlashTab._generic_ili9341_message


def test_picker_visible_recommends_detect_first():
    """With the picker/Detect on-screen (Pro mode), Detect is offered as the recommended action."""
    title, body = _msg("marauder", 1, True)
    assert "per-chip default build" in title
    assert "Recommended:" in body
    assert "Detect board (CYD)" in body
    assert "identify the panel automatically" in body
    # The blank-screen risk is still spelled out, and the escape hatch remains.
    assert "blank" in body
    assert "per-chip default" in body


def test_undersell_phrasing_is_gone():
    """Regression: the old copy framed Detect as useful only 'if you know your panel' — the exact
    inversion of what Detect is for. That phrasing must not come back."""
    _, body = _msg("marauder", 1, True)
    assert "if you know your panel" not in body.lower()


def test_simple_mode_points_at_pro_but_still_names_detect():
    """When the picker + Detect are hidden (Simple mode), the nudge points at Pro mode AND still
    names Detect as the thing to run there — not just 'pick your board'."""
    _, body = _msg("marauder", 1, False)
    assert "Pro mode" in body
    assert "Detect board (CYD)" in body
    assert "blank" in body


def test_batch_count_phrasing():
    """A queued batch says how many Marauder jobs are affected; a single flash names the profile."""
    _, one = _msg("marauder", 1, True)
    assert "'marauder'" in one
    _, many = _msg("marauder", 4, True)
    assert "4 queued Marauder job(s)" in many
