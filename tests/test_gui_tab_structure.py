"""S0 characterization — locks the current main-window tab structure.

This is a *characterization* test (a safety net), not a behavior change: it captures the tab set,
their titles, order, and the widget identity behind each tab exactly as they are today. The S4 GUI
overhaul will regroup these tabs — when it does, this test fails loudly and forces an intentional,
reviewed update of the expected structure rather than a silent drift. Pairs with the tab-grouping
inventory + IA proposal in command-center/projects/cc-GUI-OVERHAUL-PROGRAM.md.

Construction mirrors tests/test_dual_depth_ui.py::_make_window (offscreen Qt, real core objects).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


# (tab title, the CyberControllerWindow attribute that holds that tab's widget) — in add order.
# Source of truth: src/ui/qt/main_window.py (addTab calls). Keep this list in lockstep with the code;
# a diff here is the intended signal that the tab IA changed.
EXPECTED_TABS = [
    ("Flash", "_flash_tab"),
    ("Devices", "_device_tab"),
    ("Software OS", "_software_tab"),
    ("Health", "_health_tab"),
    ("Macros", "_macro_tab"),
    ("Targets", "_targets_tab"),
    ("Wardrive", "_wardrive_tab"),
    ("Broadcast", "_broadcast_bar"),
    ("Cross-Comm", "_cross_comm_tab"),
    ("Network", "_network_tab"),
    ("Settings", "_settings_tab"),
    ("How-To", "_howto_tab"),
]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def test_tab_count_is_12(qapp, isolated_settings):
    win = _make_window()
    assert win._tabs.count() == len(EXPECTED_TABS) == 12


def test_tab_titles_and_order(qapp, isolated_settings):
    win = _make_window()
    titles = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert titles == [t for t, _ in EXPECTED_TABS]


def test_each_tab_widget_identity(qapp, isolated_settings):
    # The widget mounted at each tab index is the same object the window keeps on its named attribute.
    win = _make_window()
    for i, (title, attr) in enumerate(EXPECTED_TABS):
        assert hasattr(win, attr), f"window is missing attribute {attr!r} for tab {title!r}"
        assert win._tabs.widget(i) is getattr(win, attr), (
            f"tab #{i} {title!r} is not the widget held by {attr!r}"
        )


def test_network_tab_precedes_settings(qapp, isolated_settings):
    # The Network tab is the S4 anchor (becomes the central node view); characterize its position now.
    win = _make_window()
    titles = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert titles.index("Network") < titles.index("Settings")
