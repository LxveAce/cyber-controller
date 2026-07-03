"""Track B UX #3 — fill-from-target: a Target selected in the Targets tab populates the Macro tab's
variable fields (MAC / SSID / channel) instead of being retyped.

Covers the three surfaces of the wiring: (1) TargetsTab emits fill_macro_requested with the pooled
target for both the toolbar button and the right-click menu item; (2) MacroTab.fill_target_variables
writes the fields; (3) the real main_window connection populates the Macro tab and handles the
no-selection case gracefully. Offscreen Qt, mirroring tests/test_targets_tab_index.py."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


def _targets_tab_with_one(pool_extra=None):
    from src.core.cross_comm import EventBus, TargetPool
    from src.models.target import Target, TargetType
    from src.ui.qt.targets_tab import TargetsTab

    bus = EventBus()
    pool = TargetPool(bus)
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Net",
                    channel=6, rssi=-40, device_source="COM8", extra=pool_extra or {}))
    tab = TargetsTab(pool, bus)
    tab._refresh()
    return tab


# ── MacroTab.fill_target_variables ────────────────────────────────────

def test_macro_fill_target_variables_populates_fields(qapp):
    from src.core.device_manager import DeviceManager
    from src.core.macro_recorder import MacroRecorder
    from src.ui.qt.macro_tab import MacroTab

    macro = MacroTab(MacroRecorder(), DeviceManager())
    macro.fill_target_variables(mac="AA:BB:CC:DD:EE:FF", ssid="Net", channel="6")

    assert macro._var_mac.text() == "AA:BB:CC:DD:EE:FF"
    assert macro._var_ssid.text() == "Net"
    assert macro._var_channel.text() == "6"


# ── TargetsTab emits the target (toolbar + context menu) ──────────────

def test_toolbar_button_emits_selected_target(qapp):
    tab = _targets_tab_with_one()
    captured = []
    tab.fill_macro_requested.connect(captured.append)

    tab._table.selectRow(0)
    tab._on_use_as_macro_target()

    assert len(captured) == 1
    assert captured[0].mac == "AA:BB:CC:DD:EE:FF"
    assert captured[0].ssid == "Net"
    assert captured[0].channel == 6


def test_toolbar_button_no_selection_is_graceful(qapp):
    tab = _targets_tab_with_one()
    captured = []
    tab.fill_macro_requested.connect(captured.append)

    tab._table.clearSelection()
    tab._table.setCurrentCell(-1, -1)  # no selected row
    tab._on_use_as_macro_target()  # must not raise, must not emit

    assert captured == []


def test_context_menu_item_emits_pooled_target(qapp, monkeypatch):
    # The menu resolves against the POOLED target (carries extra['index']).
    tab = _targets_tab_with_one(pool_extra={"index": 3})
    captured = []
    tab.fill_macro_requested.connect(captured.append)

    mac_item = tab._table.item(0, 2)
    monkeypatch.setattr(tab._table, "itemAt", lambda pos: mac_item)

    triggered = {}

    def _fake_exec(self, *a, **k):
        # Fire the "Use as macro target" action to simulate the user clicking it.
        for act in self.actions():
            if act.text() == "Use as macro target":
                act.trigger()
        triggered["ran"] = True
        return None

    monkeypatch.setattr(QMenu, "exec_", _fake_exec)
    tab._on_context_menu(QPoint(0, 0))

    assert triggered.get("ran")
    assert len(captured) == 1
    assert captured[0].mac == "AA:BB:CC:DD:EE:FF"
    assert captured[0].extra.get("index") == 3  # resolved against the pooled object


# ── Real main_window wiring (Targets → Macros) ────────────────────────

def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def test_window_fills_macro_and_surfaces_it(qapp, isolated_settings):
    from src.models.target import Target, TargetType

    win = _make_window()
    win._pool.add(Target(mac="11:22:33:44:55:66", target_type=TargetType.AP, ssid="Lab",
                         channel=11, rssi=-50, device_source="COM3"))
    win._targets_tab._refresh()
    win._targets_tab._table.selectRow(0)

    win._targets_tab._on_use_as_macro_target()

    # Macro variable fields now carry the target's values (no retyping).
    assert win._macro_tab._var_mac.text() == "11:22:33:44:55:66"
    assert win._macro_tab._var_ssid.text() == "Lab"
    assert win._macro_tab._var_channel.text() == "11"
    # And the Macros sub-view is surfaced so the user sees it happen.
    assert win._operate_surface.currentWidget() is win._macro_tab


def test_window_wiring_channel_zero_left_blank(qapp, isolated_settings):
    from src.models.target import Target, TargetType

    win = _make_window()
    # channel 0 == unknown -> leave the field blank rather than writing "0".
    win._pool.add(Target(mac="77:88:99:AA:BB:CC", target_type=TargetType.CLIENT, ssid="",
                         channel=0, rssi=-70, device_source="COM4"))
    win._targets_tab._refresh()
    win._targets_tab._table.selectRow(0)
    win._targets_tab._on_use_as_macro_target()

    assert win._macro_tab._var_mac.text() == "77:88:99:AA:BB:CC"
    assert win._macro_tab._var_channel.text() == ""
