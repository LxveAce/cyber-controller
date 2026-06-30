"""Cyberdeck screen-scaling helpers (src/ui/qt/screen.py).

Pure-function tests (no Qt/display): the adaptive window minimum that lets the app fit a small deck panel,
the clamped launch size, and the auto Simple/Pro pick by screen size.
"""

from __future__ import annotations

from src.ui.qt.screen import (
    adaptive_launch_size,
    adaptive_minimum_size,
    recommended_ui_mode,
)


def test_min_size_desktop_keeps_900x600():
    assert adaptive_minimum_size(1920, 1080) == (900, 600)


def test_min_size_shrinks_for_small_deck():
    # 800x480 panel can't hold a 900-wide window -> clamp to avail-margin.
    assert adaptive_minimum_size(800, 480) == (760, 400)


def test_min_size_1024x600_deck():
    # width fits the 900 ideal; height clamps to 520.
    assert adaptive_minimum_size(1024, 600) == (900, 520)


def test_min_size_never_below_floor():
    w, h = adaptive_minimum_size(500, 360)
    assert w >= 480 and h >= 320


def test_launch_size_clamped_to_small_screen():
    assert adaptive_launch_size(800, 480) == (780, 440)


def test_launch_size_desktop_full():
    assert adaptive_launch_size(1920, 1080) == (1280, 800)


def test_ui_mode_explicit_wins_even_on_small_screen():
    assert recommended_ui_mode(480, "pro") == "pro"
    assert recommended_ui_mode(1080, "simple") == "simple"


def test_ui_mode_auto_simple_on_small_screen():
    assert recommended_ui_mode(480, None) == "simple"
    assert recommended_ui_mode(600, None) == "simple"  # boundary is inclusive


def test_ui_mode_auto_pro_on_desktop():
    assert recommended_ui_mode(1080, None) == "pro"


def test_ui_mode_touch_forces_simple():
    assert recommended_ui_mode(1080, None, touch=True) == "simple"


def test_ui_mode_blank_explicit_falls_back_to_auto():
    assert recommended_ui_mode(1080, "") == "pro"
    assert recommended_ui_mode(480, "") == "simple"
