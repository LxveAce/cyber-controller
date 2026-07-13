"""Devices-tab telemetry line — a connected device's live device_info telemetry (LxveOS ops/heap/
identity, already on Device.telemetry from beat 226) renders as a read-only line under the caps
chips, refreshed the same way (per incoming line, self-guarded). Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

_STATUS = (
    "LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 "
    "panel=none caps=0x007 ops=12/3/6 heap=184988"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _status_telemetry() -> dict:
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos")
    from src.protocols.lxveos import LxveOSProtocol
    dev.apply_device_info(LxveOSProtocol().parse_line(_STATUS).data)
    return dev.telemetry


# ── pure formatter ───────────────────────────────────────────────────

def test_telemetry_line_renders_full_status_fields():
    from src.ui.qt.device_tab import DeviceTab

    line = DeviceTab._telemetry_line(_status_telemetry())
    assert "bare_esp32_headless/esp32" in line
    assert "fw 0.1.0-m0" in line
    assert "ui headless" in line
    assert "ops 12/3/6 (ready/planned/unavailable)" in line
    assert "heap 180 KB" in line  # 184988 // 1024


def test_telemetry_line_info_block_subset_no_ops_heap():
    from src.ui.qt.device_tab import DeviceTab

    line = DeviceTab._telemetry_line(
        {"fw": "0.1.0-m0", "board": "bare_esp32_headless", "chip": "esp32"})
    assert "bare_esp32_headless/esp32" in line and "fw 0.1.0-m0" in line
    assert "ops" not in line and "heap" not in line  # neither was reported


def test_telemetry_line_empty_is_blank():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._telemetry_line({}) == ""


# ── live surface on the tab ──────────────────────────────────────────

def _tab_with_device(dev):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(dev)
    tab = DeviceTab(dm)
    tab._active_port = dev.port
    return tab


def test_telemetry_label_shows_connected_device_telemetry(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.telemetry = dict(_status_telemetry())
    tab = _tab_with_device(dev)
    tab._update_telemetry()
    txt = tab._telemetry_label.text()
    assert "heap 180 KB" in txt and "ops 12/3/6 (ready/planned/unavailable)" in txt


def test_incoming_line_refreshes_telemetry_from_device(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_telemetry()
    assert tab._telemetry_label.text() == ""  # nothing reported yet

    dev.telemetry = dict(_status_telemetry())  # ingestor applied a status line
    tab._on_line_received("COM23", _STATUS)
    assert "heap 180 KB" in tab._telemetry_label.text()


def test_incoming_line_from_other_port_does_not_repaint_telemetry(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.telemetry = {"heap": 100000}
    tab = _tab_with_device(dev)
    tab._update_telemetry()
    before = tab._telemetry_label.text()
    dev.telemetry = {"heap": 999999}
    tab._on_line_received("COM99", "noise from another device")
    assert tab._telemetry_label.text() == before  # gated on the active port
