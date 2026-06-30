"""Per-firmware command terminator (firmware-comms fix #1): most firmwares read a line on LF, but the Flipper
Zero CLI shell only submits on CR. The connection's terminator follows the selected firmware's protocol."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


class _FakeSerial:
    is_open = True

    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    def flush(self):
        pass


def test_serial_write_uses_line_ending():
    from src.core.serial_handler import SerialConnection
    cr = SerialConnection("COMX", line_ending="\r")
    cr._serial = _FakeSerial()
    cr.write("device_info")
    assert cr._serial.buf == b"device_info\r"           # CR for Flipper-style

    lf = SerialConnection("COMY")                        # default
    lf._serial = _FakeSerial()
    lf.write("scanap")
    assert lf._serial.buf == b"scanap\n"                 # LF by default


def test_protocol_line_endings():
    from src.protocols import get_protocol
    assert get_protocol("flipper").line_ending == "\r"   # the decisive Flipper fix
    assert get_protocol("marauder").line_ending == "\n"  # everyone else keeps LF


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_apply_line_ending_follows_selected_firmware(qapp):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    class _Conn:
        line_ending = "\n"

    tab = DeviceTab(DeviceManager())
    tab._active_conn = _Conn()
    idx = next((i for i in range(tab._firmware_combo.count())
                if "flipper" in tab._firmware_combo.itemText(i).lower()), -1)
    assert idx >= 0, "Flipper should be a firmware choice"
    tab._firmware_combo.setCurrentIndex(idx)             # fires _update_bj_panel -> _apply_line_ending
    assert tab._active_conn.line_ending == "\r"
