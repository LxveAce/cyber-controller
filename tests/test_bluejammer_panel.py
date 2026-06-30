"""BlueJammer control/STOP panel in the Devices tab (src/ui/qt/device_tab.py).

When a BlueJammer is the active firmware, a prominent control/stop panel appears and the (inert) serial
send affordances are disabled — the stock firmware has no serial command channel, so the real control is
its web UI. Uses isHidden() (the widget's own visibility request) since the tab isn't shown. Offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _tab():
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab
    return DeviceTab(DeviceManager())


def _combo_index(combo, needle):
    for i in range(combo.count()):
        if needle in combo.itemText(i).lower():
            return i
    return -1


def test_panel_hidden_by_default(qapp):
    tab = _tab()
    assert tab._bj_panel.isHidden()


def test_panel_shows_and_disables_send_for_bluejammer(qapp):
    tab = _tab()
    idx = _combo_index(tab._firmware_combo, "jammer")
    assert idx >= 0, "BlueJammer should be a firmware choice"
    tab._firmware_combo.setCurrentIndex(idx)  # fires _update_bj_panel
    assert not tab._bj_panel.isHidden()
    assert not tab._btn_send.isEnabled()      # no serial command channel
    assert not tab._cmd_input.isEnabled()
    assert not tab._cmd_palette.isEnabled()


def test_panel_hides_for_other_firmware(qapp):
    tab = _tab()
    bj = _combo_index(tab._firmware_combo, "jammer")
    tab._firmware_combo.setCurrentIndex(bj)
    assert not tab._bj_panel.isHidden()
    mar = _combo_index(tab._firmware_combo, "marauder")
    assert mar >= 0
    tab._firmware_combo.setCurrentIndex(mar)
    assert tab._bj_panel.isHidden()
    assert tab._cmd_input.isEnabled()
    assert tab._cmd_palette.isEnabled()


def test_open_webui_does_not_raise(qapp, monkeypatch):
    tab = _tab()
    called = {}
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: called.setdefault("url", url))
    tab._open_bj_webui()
    assert called.get("url") == "http://192.168.1.1"
