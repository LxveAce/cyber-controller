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
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTextBrowser,
    QTextEdit,
)


# (tab title, the CyberControllerWindow attribute that holds that tab's widget) — in add order.
# Source of truth: src/ui/qt/main_window.py (addTab calls). Keep this list in lockstep with the code;
# a diff here is the intended signal that the tab IA changed.
EXPECTED_TABS = [
    # S4 regroup (2026-07-01): Flash is a grouped *surface* holding Firmware (the FlashTab) + Software OS as
    # sub-views — Software OS is not a top-level tab anymore. See test_flash_surface_subtabs.
    ("Flash", "_flash_surface"),
    # Connect is a grouped *surface* holding Devices/Health as sub-views — neither is a top-level tab anymore.
    # See test_connect_surface_subtabs.
    ("Connect", "_connect_surface"),
    # Operate is a grouped *surface* holding Targets/Broadcast/Macros/Wardrive as sub-views — none of those four
    # are top-level tabs anymore. See test_operate_surface_subtabs.
    ("Operate", "_operate_surface"),
    # Network is a grouped *surface* holding the Graph (NetworkTab) and Cross-Comm sub-views — Cross-Comm is
    # not a top-level tab. See test_network_surface_subtabs.
    ("Network", "_network_surface"),
    ("Settings", "_settings_tab"),
    # How-To moved to the Help menu (CC-6) — no longer a top-level tab. See test_howto_available_via_help.
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


def test_tab_count_is_5(qapp, isolated_settings):
    # 5 top-level surfaces after the S4 regroup + CC-6 (How-To moved to the Help menu): Flash, Connect,
    # Operate, Network, Settings. (Was 12 flat tabs originally — Firmware+Software OS folded into Flash,
    # Devices+Health into Connect, Targets/Broadcast/Macros/Wardrive into Operate, Cross-Comm into Network.)
    win = _make_window()
    assert win._tabs.count() == len(EXPECTED_TABS) == 5


def test_flash_surface_subtabs(qapp, isolated_settings):
    # The Flash surface holds two sub-views — Firmware (the FlashTab, leads) then Software OS — and the
    # re-parented widgets are the SAME objects the window still exposes on _flash_tab / _software_tab.
    win = _make_window()
    surface = win._flash_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Firmware", "Software OS"]
    assert surface.widget(0) is win._flash_tab, "Firmware sub-tab must be the FlashTab object"
    assert surface.widget(1) is win._software_tab, "Software OS sub-tab must be the SoftwareTab object"
    # Software OS is no longer a direct top-level tab.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert "Software OS" not in toplevel and "Flash" in toplevel


def test_connect_surface_subtabs(qapp, isolated_settings):
    # The Connect landing surface holds three sub-views — Devices (leads), Health, then Nodes (W1.1
    # wireless-node management) — and the re-parented widgets are the SAME objects the window exposes.
    win = _make_window()
    surface = win._connect_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Devices", "Health", "Nodes"]
    assert surface.widget(0) is win._device_tab, "Devices sub-tab must be the DeviceTab object"
    assert surface.widget(1) is win._health_tab, "Health sub-tab must be the HealthTab object"
    assert surface.widget(2) is win._nodes_tab, "Nodes sub-tab must be the NodesTab object"
    # Neither is a direct top-level tab anymore.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Devices", "Health"):
        assert gone not in toplevel, f"{gone!r} should be a Connect sub-tab, not top-level"
    assert "Connect" in toplevel


def test_operate_surface_subtabs(qapp, isolated_settings):
    # The Operate action surface holds five sub-views — Targets (leads), Broadcast, Macros, Wardrive, Flock Map
    # (FL F5) — and the re-parented widgets are the SAME objects the window still exposes on named attributes.
    win = _make_window()
    surface = win._operate_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Targets", "Broadcast", "Macros", "Wardrive", "Multi-Wardrive", "Flock Map"]
    assert surface.widget(0) is win._targets_tab, "Targets sub-tab must be the TargetsTab object"
    assert surface.widget(1) is win._broadcast_bar, "Broadcast sub-tab must be the BroadcastBar object"
    assert surface.widget(2) is win._macro_tab, "Macros sub-tab must be the MacroTab object"
    assert surface.widget(3) is win._wardrive_tab, "Wardrive sub-tab must be the WardriveTab object"
    assert surface.widget(4) is win._wardrive_multi_tab, "Multi-Wardrive sub-tab must be the WardriveMultiTab object"
    assert surface.widget(5) is win._flock_heatmap, "Flock Map sub-tab must be the FlockHeatmapTab object"
    # None of the sub-views are direct top-level tabs anymore.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Targets", "Broadcast", "Macros", "Wardrive", "Multi-Wardrive", "Flock Map"):
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


def test_howto_widget_inventory(qapp, isolated_settings):
    # HowToTab: a single rich-text documentation browser. CC-6 moved it off the tab strip into a Help-menu
    # dialog (_on_howto), so it's constructed on demand rather than held as a window attribute.
    from src.ui.qt.howto_tab import HowToTab

    t = HowToTab()
    assert isinstance(t._view, QTextBrowser)


def test_howto_available_via_help_not_tabstrip(qapp, isolated_settings):
    # CC-6: How-To is reachable from the Help menu (and the command palette), not as a top-level tab.
    win = _make_window()
    assert hasattr(win, "_on_howto")            # the Help-menu action handler exists
    assert not hasattr(win, "_howto_tab")       # and it is no longer mounted as a tab widget


def test_devices_tab_widget_inventory(qapp, isolated_settings):
    # DeviceTab: device list + per-device firmware/protocol picker + connect/disconnect + serial terminal with a
    # command palette/input/send + the BlueJammer control panel whose critical control is its STOP button.
    # Characterized ahead of the S4 "Connect" surface fold so the regroup can't silently drop a control.
    t = _make_window()._device_tab
    assert isinstance(t._device_list, QListWidget)
    assert isinstance(t._firmware_combo, QComboBox)
    assert isinstance(t._terminal, QTextEdit)
    assert isinstance(t._cmd_palette, QComboBox)
    assert isinstance(t._cmd_input, QLineEdit)
    for btn in ("_btn_connect", "_btn_disconnect", "_btn_send"):
        assert isinstance(getattr(t, btn), QPushButton), f"DeviceTab.{btn} not a QPushButton"
    # BlueJammer safety control must survive the regroup.
    assert isinstance(t._bj_stop_btn, QPushButton)
    assert "STOP" in t._bj_stop_btn.text().upper()


def test_flash_tab_widget_inventory(qapp, isolated_settings):
    # FlashTab: port + firmware-profile + board/variant pickers, Browse/Flash/Backup/Erase controls, a progress
    # bar + log, the flash queue, the Dead Man's Switch enable, and the cached-firmware vault status.
    # Characterized ahead of the S4 "Flash" surface fold (Flash + Software OS).
    t = _make_window()._flash_tab
    for combo in ("_port_combo", "_profile_combo", "_variant_combo"):
        assert isinstance(getattr(t, combo), QComboBox), f"FlashTab.{combo} not a QComboBox"
    for btn in ("_btn_browse", "_btn_flash", "_btn_backup", "_btn_erase"):
        assert isinstance(getattr(t, btn), QPushButton), f"FlashTab.{btn} not a QPushButton"
    assert isinstance(t._progress, QProgressBar)
    assert isinstance(t._log_output, QTextEdit)
    assert isinstance(t._queue_list, QListWidget)
    assert isinstance(t._suicide_checkbox, QCheckBox)
    assert isinstance(t._vault_status, QLabel)
