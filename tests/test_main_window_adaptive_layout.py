"""Wave-3 Batch A — the app-shell (main_window) responds to window size: the sidebar folds to an
icon rail and the bottom terminal hides on a compact canvas, restoring when there's room. The
decision is the pure `layout_profile` size class; this exercises the widget wiring. Offscreen, so
visibility is checked with `isHidden()` (isVisible() is always False without a shown top-level).

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


def test_apply_shell_layout_collapses_and_hides_on_compact(qapp):
    from src.ui.qt.layout_profile import layout_profile
    win = _make_window()
    try:
        terminal = win._main_splitter.widget(1)

        win._apply_shell_layout(layout_profile(1440, 900, dpi=96))   # expanded
        assert not win._app_shell.collapsed
        assert not terminal.isHidden()

        win._apply_shell_layout(layout_profile(480, 800, dpi=96))    # compact
        assert win._app_shell.collapsed, "sidebar should fold to an icon rail on a compact canvas"
        assert terminal.isHidden(), "the bottom terminal should hide on a compact canvas"

        win._apply_shell_layout(layout_profile(900, 700, dpi=96))    # regular — room again
        assert not win._app_shell.collapsed
        assert not terminal.isHidden()
    finally:
        win.close()


def test_relayout_debounces_on_size_class(qapp):
    # Within one size class the resolver must NOT re-apply, so resize jitter can't fight the user's
    # manual ≡ collapse (mirrors flash_tab's debounce).
    win = _make_window()
    try:
        win._relayout_shell()                 # establish the current size class + apply once
        first = win._last_shell_size
        assert first in ("compact", "regular", "expanded")

        win._app_shell.set_collapsed(True)    # user manually collapses within the same class
        win._relayout_shell()                 # same size class -> debounced -> must not un-collapse
        assert win._last_shell_size == first
        assert win._app_shell.collapsed, "debounce: same-class relayout must not fight the user"
    finally:
        win.close()

# (The "resize before the shell exists" guard is exercised by construction itself: __init__ calls
# self.resize(...) before _build_main_layout wires _app_shell, firing resizeEvent -> _relayout_shell
# -> the None-guard. If it were broken, _make_window() above would crash.)
