"""Devices-tab detector/watchlist alert line — a LxveOS detector firing (deauth attack, evil twin,
BLE tracker, watchlist hit, ...) reports an ``alert`` event; the tab surfaces the latest one plus a
session count as a one-line amber warning, so it isn't lost in the terminal scroll. Offscreen Qt.

Builds on the alert data path (Device.apply_alert / ingestor alert route); this is the Devices-tab
surface that makes a live detection visible where a board is connected.
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


# ── pure formatter (no widget needed) ────────────────────────────────

def test_alert_line_blank_until_something_fires():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._alert_line(0, {}) == ""
    assert DeviceTab._alert_line(0, {"kind": "deauth"}) == ""   # count 0 -> nothing has fired
    assert DeviceTab._alert_line(3, {}) == ""                    # no alert data -> blank


def test_alert_line_formats_kind_count_and_present_fields():
    from src.ui.qt.device_tab import DeviceTab

    line = DeviceTab._alert_line(1, {"kind": "deauth", "bssid": "de:ad:be:ef:00:01", "count": 27})
    assert line.startswith("⚠ alert #1: deauth")
    assert "bssid=de:ad:be:ef:00:01" in line and "count=27" in line
    # a watchlist hit
    hit = {"kind": "watch", "mac": "11:22:33:44:55:66", "band": "ble", "rssi": -70}
    line = DeviceTab._alert_line(4, hit)
    assert line.startswith("⚠ alert #4: watch")
    assert "band=ble" in line and "mac=11:22:33:44:55:66" in line


def test_alert_line_shows_only_present_fields_and_caps_at_four():
    from src.ui.qt.device_tab import DeviceTab

    # absent fields don't render; at most four fields are shown to keep the line compact
    busy = {"kind": "eviltwin", "ssid": "MyNet", "bssid": "aa:bb:cc:00:11:22", "grade": 0,
            "count": 2, "rssi": -33, "band": "wifi"}
    line = DeviceTab._alert_line(2, busy)
    assert line.startswith("⚠ alert #2: eviltwin")
    assert line.count("=") == 4  # exactly four k=v pairs


# ── live surface on the tab ──────────────────────────────────────────

def _tab_with_device(dev):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(dev)
    tab = DeviceTab(dm)
    tab._active_port = dev.port
    return tab


def test_alert_line_reflects_device_alert(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.alert_count = 1
    dev.last_alert = {"kind": "tracker", "vendor": "AirTag", "rssi": -40}
    tab = _tab_with_device(dev)
    tab._update_alert_line()
    assert "tracker" in tab._alert_label.text() and "AirTag" in tab._alert_label.text()


def test_incoming_alert_line_refreshes(qapp):
    # The live-refresh wire: the ingestor bumps Device alert state on the serial thread; then
    # _on_line_received runs on the Qt thread and must repaint the alert line from the Device.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_alert_line()
    assert tab._alert_label.text() == ""  # nothing fired yet

    dev.alert_count = 1
    dev.last_alert = {"kind": "deauth", "count": 27}  # ingestor applied an alert line
    tab._on_line_received("COM23", "LXVEOS/1 alert kind=deauth count=27")
    assert "deauth" in tab._alert_label.text()


def test_alert_line_from_other_port_does_not_repaint_active(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_alert_line()
    before = tab._alert_label.text()
    dev.alert_count = 1
    dev.last_alert = {"kind": "deauth"}  # mutate COM23, but a DIFFERENT port speaks
    tab._on_line_received("COM99", "noise from another device")
    assert tab._alert_label.text() == before  # unchanged — refresh is gated on the active port
