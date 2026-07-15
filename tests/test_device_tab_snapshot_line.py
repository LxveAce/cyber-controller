"""Devices-tab airspace-occupancy tile — the LxveOS ``airspace`` command reports a ``snapshot``
event (AP + open/WPS splits, BLE + known-tracker counts, client + alert tallies); the tab surfaces
the latest one as a muted-blue line below the alert line. Latest-wins, no counter. Offscreen Qt.

Builds on the snapshot data path (Device.apply_snapshot / ingestor snapshot route); this is the
Devices-tab surface that makes the current airspace picture visible where a board is connected.
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

def test_snapshot_line_blank_when_empty():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._snapshot_line({}) == ""      # nothing reported
    assert DeviceTab._snapshot_line(None) == ""    # non-dict -> blank, never raises
    # a dict whose only fields are absent/blank renders nothing (not "airspace: ")
    assert DeviceTab._snapshot_line({"unrelated": ""}) == ""


def test_snapshot_line_formats_present_fields_in_order():
    from src.ui.qt.device_tab import DeviceTab

    line = DeviceTab._snapshot_line(
        {"aps": 14, "open": 3, "wps": 2, "bles": 8, "trackers": 1, "stas": 40, "alerts": 5}
    )
    assert line.startswith("airspace: ")
    for token in ("APs 14", "open 3", "WPS 2", "BLE 8", "trackers 1", "clients 40", "alerts 5"):
        assert token in line
    # field order is preserved (APs before BLE before alerts)
    assert line.index("APs 14") < line.index("BLE 8") < line.index("alerts 5")


def test_snapshot_line_shows_only_present_fields():
    from src.ui.qt.device_tab import DeviceTab

    # a partial snapshot (only some counts carried) still formats — absent fields don't render
    line = DeviceTab._snapshot_line({"aps": 2, "bles": 1, "trackers": 1})
    assert line == "airspace: APs 2  ·  BLE 1  ·  trackers 1"
    assert "open" not in line and "WPS" not in line and "clients" not in line


def test_snapshot_line_renders_zero_counts():
    from src.ui.qt.device_tab import DeviceTab

    # 0 is a meaningful count (0 open APs is good news), so it renders — only None/"" are dropped.
    line = DeviceTab._snapshot_line({"aps": 5, "open": 0, "wps": 0})
    assert "open 0" in line and "WPS 0" in line


# ── live surface on the tab ──────────────────────────────────────────

def _tab_with_device(dev):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(dev)
    tab = DeviceTab(dm)
    tab._active_port = dev.port
    return tab


def test_snapshot_line_reflects_device_snapshot(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.last_snapshot = {"aps": 12, "open": 1, "bles": 4}
    tab = _tab_with_device(dev)
    tab._update_snapshot_line()
    text = tab._snapshot_label.text()
    assert text.startswith("airspace: ") and "APs 12" in text and "BLE 4" in text


def test_incoming_snapshot_line_refreshes(qapp):
    # The live-refresh wire: the ingestor applies the snapshot on the serial thread; then
    # _on_line_received runs on the Qt thread and must repaint the snapshot line from the Device.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_snapshot_line()
    assert tab._snapshot_label.text() == ""  # nothing reported yet

    dev.apply_snapshot({"aps": 2, "open": 1, "wps": 0, "bles": 1, "trackers": 1})
    tab._on_line_received("COM23", "LXVEOS/1 snapshot aps=2 open=1 wps=0 bles=1 trackers=1")
    text = tab._snapshot_label.text()
    assert "APs 2" in text and "trackers 1" in text


def test_snapshot_line_from_other_port_does_not_repaint_active(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_snapshot_line()
    before = tab._snapshot_label.text()
    dev.last_snapshot = {"aps": 9}  # mutate COM23, but a DIFFERENT port speaks
    tab._on_line_received("COM99", "noise from another device")
    assert tab._snapshot_label.text() == before  # unchanged — refresh is gated on the active port
