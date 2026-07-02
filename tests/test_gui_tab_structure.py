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
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTextBrowser,
)


# (tab title, the CyberControllerWindow attribute that holds that tab's widget) — in add order.
# Source of truth: src/ui/qt/main_window.py (addTab calls). Keep this list in lockstep with the code;
# a diff here is the intended signal that the tab IA changed.
EXPECTED_TABS = [
    ("Flash", "_flash_tab"),
    ("Devices", "_device_tab"),
    ("Software OS", "_software_tab"),
    ("Health", "_health_tab"),
    # S4 regroup (2026-07-01): Operate is a grouped *surface* holding Targets/Broadcast/Macros/Wardrive as
    # sub-views — none of those four are top-level tabs anymore. See test_operate_surface_subtabs.
    ("Operate", "_operate_surface"),
    # Network is a grouped *surface* holding the Graph (NetworkTab) and Cross-Comm sub-views — Cross-Comm is
    # not a top-level tab. See test_network_surface_subtabs.
    ("Network", "_network_surface"),
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


def test_tab_count_is_8(qapp, isolated_settings):
    # 8 top-level tabs after the S4 regroup folded Targets/Broadcast/Macros/Wardrive into the Operate surface
    # and Cross-Comm into the Network surface (was 12 flat tabs originally, 11 after the Network fold).
    win = _make_window()
    assert win._tabs.count() == len(EXPECTED_TABS) == 8


def test_operate_surface_subtabs(qapp, isolated_settings):
    # The Operate action surface holds four sub-views — Targets (leads), Broadcast, Macros, Wardrive — and the
    # re-parented widgets are the SAME objects the window still exposes on its named attributes.
    win = _make_window()
    surface = win._operate_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Targets", "Broadcast", "Macros", "Wardrive"]
    assert surface.widget(0) is win._targets_tab, "Targets sub-tab must be the TargetsTab object"
    assert surface.widget(1) is win._broadcast_bar, "Broadcast sub-tab must be the BroadcastBar object"
    assert surface.widget(2) is win._macro_tab, "Macros sub-tab must be the MacroTab object"
    assert surface.widget(3) is win._wardrive_tab, "Wardrive sub-tab must be the WardriveTab object"
    # None of the four are direct top-level tabs anymore.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Targets", "Broadcast", "Macros", "Wardrive"):
        assert gone not in toplevel, f"{gone!r} should be an Operate sub-tab, not top-level"
    assert "Operate" in toplevel


def test_network_surface_subtabs(qapp, isolated_settings):
    # The Network anchor surface holds two sub-views — Graph (the NetworkTab) then Cross-Comm — and the
    # re-parented widgets are the SAME objects the window still exposes on _network_tab / _cross_comm_tab.
    win = _make_window()
    surface = win._network_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Graph", "Cross-Comm"]
    assert surface.widget(0) is win._network_tab, "Graph sub-tab must be the NetworkTab object"
    assert surface.widget(1) is win._cross_comm_tab, "Cross-Comm sub-tab must be the CrossCommTab object"
    # Cross-Comm is no longer a direct top-level tab.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert "Cross-Comm" not in toplevel and "Network" in toplevel


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


# ── Per-tab widget inventory (S4 characterization) ───────────────────
# Records the key controls each tab exposes today so the overhaul cannot silently drop one.
# Attribute names are the source of truth from src/ui/qt/*_tab.py; a diff here is the intended signal.

def test_broadcast_tab_widget_inventory(qapp, isolated_settings):
    # BroadcastBar (main_window._broadcast_bar): a compact bar whose critical control is STOP ALL.
    win = _make_window()
    bar = win._broadcast_bar
    assert isinstance(bar._stop_btn, QPushButton)
    assert "STOP" in bar._stop_btn.text().upper()
    assert isinstance(bar._status, QLabel)


def test_cross_comm_tab_widget_inventory(qapp, isolated_settings):
    # CrossCommTab: target pool table + live event stream + auto-routing rules + action history.
    t = _make_window()._cross_comm_tab
    assert isinstance(t._pool_table, QTableWidget) and t._pool_table.columnCount() == 6
    assert isinstance(t._action_table, QTableWidget)
    assert isinstance(t._rule_list, QListWidget)
    for attr in ("_stream_card", "_rules_card", "_action_card"):
        assert hasattr(t, attr), f"CrossCommTab missing {attr!r}"
    for btn in ("_refresh_pool_btn", "_clear_pool_btn", "_add_rule_btn", "_remove_rule_btn"):
        assert isinstance(getattr(t, btn), QPushButton), f"CrossCommTab.{btn} not a QPushButton"


def test_health_tab_widget_inventory(qapp, isolated_settings):
    # HealthTab: four ArcGauges (CPU/RAM/Disk/Battery) + a device-health table.
    t = _make_window()._health_tab
    for g in ("_cpu_gauge", "_ram_gauge", "_disk_gauge", "_batt_gauge"):
        assert getattr(t, g) is not None, f"HealthTab missing gauge {g!r}"
    assert isinstance(t._device_table, QTableWidget)
    assert hasattr(t, "_dev_card")


def test_macro_tab_widget_inventory(qapp, isolated_settings):
    # MacroTab: recorded-macro list + steps table + transport combos + record/stop/play/save controls
    # + the {mac}/{ssid}/{channel} substitution fields.
    t = _make_window()._macro_tab
    assert isinstance(t._macro_list, QListWidget)
    assert isinstance(t._steps_table, QTableWidget)
    assert isinstance(t._macro_name_label, QLabel)
    for combo in ("_port_combo", "_speed_combo"):
        assert isinstance(getattr(t, combo), QComboBox), f"MacroTab.{combo} not a QComboBox"
    for btn in ("_btn_record", "_btn_stop", "_btn_play", "_btn_save"):
        assert isinstance(getattr(t, btn), QPushButton), f"MacroTab.{btn} not a QPushButton"
    for var in ("_var_mac", "_var_ssid", "_var_channel"):
        assert isinstance(getattr(t, var), QLineEdit), f"MacroTab.{var} not a QLineEdit"


def test_howto_tab_widget_inventory(qapp, isolated_settings):
    # HowToTab: a single rich-text documentation browser.
    t = _make_window()._howto_tab
    assert isinstance(t._view, QTextBrowser)
