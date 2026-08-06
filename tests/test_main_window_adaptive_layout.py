"""The app-shell (main_window) responds to the form factor: the sidebar folds to an icon rail and
the bottom terminal undocks whenever the nav chrome isn't a full sidebar. The decision is the pure
`layout_profile` nav_mode (Spade v2: form-factor AND density, not size alone, so the 7" touch deck
collapses even at "regular" width); this exercises the widget wiring. Offscreen, so visibility is
checked with `isHidden()` (isVisible() is always False without a shown top-level).

`_apply_shell_layout` is tested with constructed profiles rather than `resize()` because the window
carries a minimum size (`adaptive_minimum_size`) that clamps a resize back up on a desktop screen —
compact is only physically reachable on a small deck panel, but the apply logic is the same."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def test_apply_shell_layout_collapses_the_rail_on_compact(qapp):
    from src.ui.qt.layout_profile import layout_profile
    win = _make_window()
    try:
        # Reform P3: the terminal moved out of the docked bottom splitter pane into a rail surface, so
        # there is no bottom terminal to hide — the shell just folds the nav rail on a compact canvas.
        assert win._main_splitter.count() == 1, "shell-only splitter (terminal is a rail surface now)"

        win._apply_shell_layout(layout_profile(1440, 900, dpi=96))   # expanded
        assert not win._app_shell.collapsed

        win._apply_shell_layout(layout_profile(480, 800, dpi=96))    # compact
        assert win._app_shell.collapsed, "sidebar should fold to an icon rail on a compact canvas"

        win._apply_shell_layout(layout_profile(900, 700, dpi=96))    # regular — room again
        assert not win._app_shell.collapsed
    finally:
        win.close()


def test_relayout_debounces_on_nav_mode(qapp):
    # Within one nav-chrome mode the resolver must NOT re-apply, so resize jitter can't fight the
    # user's manual collapse (mirrors flash_tab's debounce). Spade v2: keys on nav_mode, not size.
    win = _make_window()
    try:
        win._relayout_shell()                 # establish the current nav mode + apply once
        first = win._last_nav_key
        assert first in ("sidebar", "rail", "bottombar")

        win._app_shell.set_collapsed(True)    # user manually collapses within the same mode
        win._relayout_shell()                 # same nav mode -> debounced -> must not un-collapse
        assert win._last_nav_key == first
        assert win._app_shell.collapsed, "debounce: same-mode relayout must not fight the user"
    finally:
        win.close()


def test_touch_deck_collapses_to_rail(qapp):
    # THE Spade P1 deck fix: at 800x480 the size is "regular" (< 1024), so the old is_compact-only
    # gate left the 7" TOUCH deck with a full desktop sidebar. Driving off nav_mode fixes it — touch
    # collapses to a rail; the same geometry with a POINTER (a small desktop window) keeps the full
    # sidebar. nav_mode, not size, is the driver. (Reform P3: the terminal is a rail surface now, not a
    # docked pane, so there is no bottom terminal to undock — the rail collapse is the whole behavior.)
    from src.ui.qt.layout_profile import layout_profile
    win = _make_window()
    try:
        win._apply_shell_layout(layout_profile(800, 480, touch=True))    # the deck
        assert win._app_shell.nav_mode == "rail", "the 800x480 touch deck renders the icon rail"
        assert win._app_shell.collapsed, "the 800x480 touch deck must fold to an icon rail"

        win._apply_shell_layout(layout_profile(800, 480, touch=False))   # same size, pointer
        assert win._app_shell.nav_mode == "sidebar", "a pointer window keeps the full sidebar"
        assert not win._app_shell.collapsed
    finally:
        win.close()

# (The "resize before the shell exists" guard is exercised by construction itself: __init__ calls
# self.resize(...) before _build_main_layout wires _app_shell, firing resizeEvent -> _relayout_shell
# -> the None-guard. If it were broken, _make_window() above would crash.)


def test_operate_home_external_tiles_route_to_their_real_tabs(qapp):
    # The Operate-Home Tools / Settings tiles have no in-place screen — a tap must open the real surface
    # (Crack Lab inside CRACK; Settings the pinned tab), NOT a "coming soon" placeholder for a shipped
    # feature. Drive the same navigate_requested signal the tile emits.
    win = _make_window()
    try:
        win._operate_home.navigate_requested.emit("tools")
        assert win._tabs.currentWidget() is win._crack_surface
        assert win._crack_surface.currentWidget() is win._crack_lab_tab

        win._operate_home.navigate_requested.emit("settings")
        assert win._tabs.currentWidget() is win._settings_tab
    finally:
        win.close()
