"""Whole-app coherence smoke: the assembled window builds, and every screen renders at launch and
across a resize (compact / regular / expanded) without raising.

Wave-3 + the Grand-Overhaul touched every screen's reflow, the shared terminal (activity_log), and
the background timers. Per-screen tests cover each in isolation; this exercises them ASSEMBLED — it
constructs the real `CyberControllerWindow` (all surfaces + the shell + the terminal), then walks
every leaf tab and renders it at three size classes, firing each tab's `resizeEvent` -> its
`_relayout_*` reflow. A screen that crashes on layout, or a null render, fails here. Pure UI,
offscreen Qt; drives no serial and authors no TX.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QTabWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _leaf_tabs(tabw):
    """Yield every leaf screen, recursing the nested QTabWidget surfaces (Flash/Connect/…)."""
    for i in range(tabw.count()):
        w = tabw.widget(i)
        if isinstance(w, QTabWidget):
            yield from _leaf_tabs(w)
        else:
            yield w


# (w, h) per size class the resolver keys off: compact folds the shell, expanded is 3-col.
_SIZE_CLASSES = ((1440, 900), (900, 700), (480, 800))


def test_app_launch_render_resize_smoke(qapp):
    from src.ui.qt.layout_profile import layout_profile
    win = _make_window()
    try:
        leaves = list(_leaf_tabs(win._tabs))
        # Sanity: the app really did assemble its full screen set (not a stub of one tab).
        assert len(leaves) >= 12, f"expected the full screen set, walked only {len(leaves)}"

        for w, h in _SIZE_CLASSES:
            # The assembled shell reflows + renders at this size class.
            win._apply_shell_layout(layout_profile(w, h, dpi=96))
            win.resize(w, h)
            qapp.processEvents()
            shell_pm = win.grab()
            assert not shell_pm.isNull(), f"assembled window rendered null at {w}x{h}"

            # Every leaf screen reflows (its resizeEvent -> _relayout_*) + renders without raising.
            for leaf in leaves:
                leaf.resize(w, max(240, h - 140))
                qapp.processEvents()
                pm = leaf.grab()
                assert not pm.isNull(), f"{type(leaf).__name__} rendered null at {w}x{h}"
    finally:
        win.close()
