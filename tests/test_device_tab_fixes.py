"""Devices-tab reliability fixes: per-device send terminator (#16) and DMS auto-auth replying to the
SOURCE device rather than the currently-selected one (#6). Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_send_uses_selected_device_terminator(qapp):
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    class _Conn:
        def __init__(self):
            self.line_ending = "\n"
            self.writes = []

        def write(self, s):
            self.writes.append((s, self.line_ending))

    conn = _Conn()
    dm = DeviceManager()
    dm.add_device(Device(port="COM9", name="Flipper", firmware="flipper", connected=True))
    tab = DeviceTab(dm)
    tab._active_port = "COM9"
    tab._active_conn = conn
    tab._cmd_input.setText("device_info")
    tab._on_send()
    # The Flipper's own firmware terminator (CR) is stamped from the DEVICE, not the shared combo (LF).
    assert conn.line_ending == "\r"
    assert conn.writes and conn.writes[-1][1] == "\r"


def test_dms_reply_goes_to_source_connection(qapp, monkeypatch):
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    writes = {"A": [], "B": []}

    class _Conn:
        def __init__(self, tag):
            self.tag = tag

        def write(self, s):
            writes[self.tag].append(s)

    conn_a, conn_b = _Conn("A"), _Conn("B")
    dm = DeviceManager()
    dm.add_device(Device(port="COMA"))
    dm.add_device(Device(port="COMB"))
    monkeypatch.setattr(dm, "get_connection", lambda p: {"COMA": conn_a, "COMB": conn_b}.get(p))

    tab = DeviceTab(dm)

    class _DMS:
        def check_line(self, line, writer):
            writer("BOOTPW")  # "detected a prompt" -> auto-reply the boot password

    tab._dms_auth = _DMS()
    tab._active_port = "COMB"
    tab._active_conn = conn_b  # device B is the SELECTED row...

    tab._on_line_received("COMA", "Enter password:")  # ...but the DMS prompt came from device A

    assert writes["A"] == ["BOOTPW"], "DMS reply must go to the device that emitted the prompt"
    assert writes["B"] == [], "must NOT write the boot password to the wrong (selected) device"
