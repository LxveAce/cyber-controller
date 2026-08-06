"""S0 characterization — locks the current main-window tab structure.

This is a *characterization* test (a safety net), not a behavior change: it captures the tab set,
their titles, order, and the widget identity behind each tab exactly as they are today. The S4 GUI
overhaul will regroup these tabs — when it does, this test fails loudly and forces an intentional,
reviewed update of the expected structure rather than a silent drift. Pairs with the tab-grouping
inventory + IA proposal in the internal GUI-overhaul notes.

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


# (tab title, the CyberControllerWindow attribute that holds that tab's widget) — in nav_model order.
# Source of truth: src/core/nav_model.py (the verb IA) + main_window._build_tabs (the addTab calls). Keep
# this list in lockstep; a diff here is the intended signal that the tab IA changed. Spade v2 verb IA (P2.5):
# the rail is driven by nav_model.visible_nav() — RIG · HUNT · OPERATE · CRACK · MAP + the pinned Settings.
EXPECTED_TABS = [
    # DEVICE — the reform front door: Dashboard (re-homes Devices+Health) / Firmware / Software OS /
    # Mesh. (_rig_surface attr kept; rail relabeled RIG->DEVICE.) See test_device_surface_subtabs.
    ("DEVICE", "_rig_surface"),
    # HUNT — passive discovery: Wi-Fi/BLE analyzers/Targets/Graph. See test_hunt_surface_subtabs.
    ("HUNT", "_hunt_surface"),
    # OPERATE — the ONE action surface (the double-Operate died): Home launcher/merged Control/Macros.
    # Operate Home is now the launcher SUB-view here, not a peer top-level tab. See test_operate_surface_subtabs.
    ("OPERATE", "_operate_surface"),
    # CRACK — the offline Crack Lab. See test_crack_surface_subtabs.
    ("CRACK", "_crack_surface"),
    # MAP — one canvas: Wardrive/Multi-Wardrive/Flock Map. See test_map_surface_subtabs.
    ("MAP", "_map_surface"),
    # Settings — the pinned utility surface.
    # TERMINAL — reform P3: the persistent terminal hub, moved out of the docked bottom pane into a
    # pinned rail surface (above Settings). Its widget is the term_frame itself.
    ("Terminal", "_term_frame"),
    ("Settings", "_settings_tab"),
    # How-To is in the Help menu (CC-6); Operate Home is retired (P2, OPERATE opens on the Console) — neither is top-level.
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


def test_tab_count_is_7(qapp, isolated_settings):
    # 7 top-level surfaces: the 5 job-verbs (DEVICE/HUNT/OPERATE/CRACK/MAP) + the pinned TERMINAL (reform
    # P3: the persistent terminal moved out of the docked bottom pane into a rail surface) + pinned Settings.
    win = _make_window()
    assert win._tabs.count() == len(EXPECTED_TABS) == 7


def test_lands_on_device_dashboard(qapp, isolated_settings):
    # Reform front door: launch opens on DEVICE ▸ Dashboard (the re-homed landing), not the old
    # Operate-Home bounce-pad (DEVICE selected + its Dashboard sub-view current).
    win = _make_window()
    assert win._tabs.currentWidget() is win._rig_surface
    assert win._rig_surface.currentWidget() is win._device_dashboard


def test_device_surface_subtabs(qapp, isolated_settings):
    # Reform: DEVICE = Dashboard (leads, the landing that RE-HOMES the Devices + Health widgets) +
    # Firmware + Software OS + Mesh. Devices/Health are no longer their own sub-tabs (their leaves live
    # in the Dashboard); Nodes re-homes to Mesh (follow-up). The re-parented widgets are the SAME objects.
    win = _make_window()
    surface = win._rig_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Dashboard", "Firmware", "Software OS", "Mesh"]
    assert surface.widget(0) is win._device_dashboard, "Dashboard sub-tab must be the DeviceDashboard"
    assert surface.widget(1) is win._flash_tab, "Firmware sub-tab must be the FlashTab object"
    assert surface.widget(2) is win._software_tab, "Software OS sub-tab must be the SoftwareTab object"
    assert surface.widget(3) is win._cross_comm_tab, "Mesh sub-tab must be the CrossCommTab object"
    # The Dashboard still holds the SAME device_tab/health_tab instances (re-homed, refs survive).
    assert win._device_dashboard._device_tab is win._device_tab
    assert win._device_dashboard._health_tab is win._health_tab
    # None of these are direct top-level tabs (Devices/Health/Nodes are subsumed / re-homed).
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Devices", "Health", "Nodes", "Software OS", "Firmware", "Flash", "Connect", "Cross-Comm"):
        assert gone not in toplevel, f"{gone!r} should be a DEVICE sub-tab, not top-level"
    assert "DEVICE" in toplevel


def test_operate_surface_subtabs(qapp, isolated_settings):
    # P2.5 kills the double-Operate: OPERATE = Home launcher (leads) + Control + Macros. Targets re-homes to
    # HUNT. Control is the QA-1 (decision #9) merged screen — the fan-out Broadcast + single-device Console in
    # ONE vertical splitter, preserved verbatim. The widgets stay the SAME objects the window exposes.
    win = _make_window()
    surface = win._operate_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    # Reform (P2): OPERATE opens on the single dense Console (the merged Operate splitter), matching the
    # mockup — the old tile Operate-Home is retired from the surface (kept constructed + hidden to prime
    # the console). Sub-tabs are now Console | Macros; Home no longer leads.
    assert titles == ["Console", "Macros"]
    assert surface.widget(0) is win._operate_action, "Console must be the merged Operate splitter (leads OPERATE)"
    assert surface.widget(1) is win._macro_tab, "Macros sub-tab must be the MacroTab object"
    assert surface.indexOf(win._operate_home) < 0, "Operate-Home is retired from the OPERATE surface"
    # The merged Control screen still holds BOTH the fan-out bar and the single-device console.
    merged = {win._operate_action.widget(i) for i in range(win._operate_action.count())}
    assert win._broadcast_bar in merged, "fan-out BroadcastBar must live in the merged Control screen"
    assert win._operate_console in merged, "single-device console must live in the merged Control screen"
    # Targets moved to HUNT; none of the sub-views are direct top-level tabs.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Targets", "Control", "Macros", "Home", "Operate Home"):
        assert gone not in toplevel, f"{gone!r} should be inside OPERATE, not top-level"
    assert "OPERATE" in toplevel


def test_map_surface_subtabs(qapp, isolated_settings):
    # P2.5: MAP is the one map canvas (was Survey) — Wardrive (leads), Multi-Wardrive, Flock Map.
    win = _make_window()
    surface = win._map_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Wardrive", "Multi-Wardrive", "Flock Map"]
    assert surface.widget(0) is win._wardrive_tab, "Wardrive sub-tab must be the WardriveTab object"
    assert surface.widget(1) is win._wardrive_multi_tab, "Multi-Wardrive must be the WardriveMultiTab object"
    assert surface.widget(2) is win._flock_heatmap, "Flock Map must be the FlockHeatmapTab object"
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Wardrive", "Multi-Wardrive", "Flock Map", "Survey"):
        assert gone not in toplevel, f"{gone!r} should be a MAP sub-tab, not top-level"
    assert "MAP" in toplevel


def test_ble_analyzer_fed_by_ingestor_events(qapp, isolated_settings):
    # End-to-end wiring: a BLE advert line on the window's ingestor -> parse -> route -> the event
    # observer -> the marshalling signal -> the analyzer tab's model. The signal is emitted on the
    # test (GUI) thread, so it's a direct connection and the model updates synchronously.
    from src.protocols import get_protocol

    win = _make_window()

    class _Conn:
        port = "COM4"

        def __init__(self) -> None:
            self._cbs = []

        def on_line(self, cb) -> None:
            self._cbs.append(cb)

        def feed(self, line: str) -> None:
            for cb in list(self._cbs):
                cb(line)

    conn = _Conn()
    win._ingestor.attach(conn, get_protocol("marauder"))
    conn.feed("BLE: 12:34:56:78:9a:bc Name: Fitbit RSSI: -44")

    dev = win._ble_analyzer.model.get("12:34:56:78:9a:bc")
    assert dev is not None, "the ingestor's ble_found event never reached the analyzer model"
    assert dev.rssi == -44 and dev.name == "Fitbit"


def test_hunt_surface_subtabs(qapp, isolated_settings):
    # P2.5 dissolves the Analyze bundle into verbs. HUNT holds the passive-awareness views: the Wi-Fi + BLE
    # analyzers (re-homed from Analyze), Targets (re-homed from Operate), and the node Graph (from Analyze).
    # Optional analyzers that are None are simply absent (never shown as an empty tab). Same widget objects.
    win = _make_window()
    surface = win._hunt_surface
    members = {surface.widget(i) for i in range(surface.count())}
    titles = [surface.tabText(i) for i in range(surface.count())]
    expected = []
    if win._wifi_analyzer is not None:
        expected.append("Wi-Fi")
        assert win._wifi_analyzer in members, "Wi-Fi analyzer must live in HUNT"
    if win._ble_analyzer is not None:
        expected.append("BLE")
        assert win._ble_analyzer in members, "BLE analyzer must live in HUNT"
    expected += ["Targets", "Graph"]
    assert titles == expected
    assert win._targets_tab in members, "Targets sub-tab must be the TargetsTab object, in HUNT"
    assert win._network_tab in members, "Graph sub-tab must be the NetworkTab object, in HUNT"
    # These are sub-views now, not top-level; the old "Analyze"/"Network" labels are gone.
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    for gone in ("Graph", "Targets", "Analyze", "Network"):
        assert gone not in toplevel
    assert "HUNT" in toplevel


def test_crack_surface_subtabs(qapp, isolated_settings):
    # P2.5: Crack Lab is its own CRACK verb (re-homed from the dissolved Analyze bundle); Cross-Comm (Mesh)
    # re-homed to RIG, not CRACK.
    win = _make_window()
    surface = win._crack_surface
    titles = [surface.tabText(i) for i in range(surface.count())]
    assert titles == ["Crack Lab"]
    assert surface.widget(0) is win._crack_lab_tab, "Crack Lab sub-tab must be the CrackLabTab object"
    rig_members = {win._rig_surface.widget(i) for i in range(win._rig_surface.count())}
    assert win._cross_comm_tab in rig_members, "Cross-Comm (Mesh) re-homes to RIG, not CRACK"
    toplevel = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert "Crack Lab" not in toplevel and "CRACK" in toplevel


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


def test_verb_order_is_the_mission_arc(qapp, isolated_settings):
    # P2.5: the rail reads left-to-right as the mission arc RIG -> HUNT -> OPERATE -> CRACK -> MAP, with the
    # pinned Settings last — mirroring nav_model.visible_nav() order (the single source that drives the rail).
    win = _make_window()
    titles = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    assert titles == ["DEVICE", "HUNT", "OPERATE", "CRACK", "MAP", "Terminal", "Settings"]


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


def test_terms_available_via_palette(qapp, isolated_settings):
    # Reform: the menu bar was removed; Terms of Service & Use is reachable from the command palette
    # (not a tab, not a Help menu).
    win = _make_window()
    assert hasattr(win, "_on_terms")
    labels = {c.label for c in win._palette._commands}
    assert any("Terms of Service" in lbl for lbl in labels), labels


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
