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


def test_scan_autoselects_first_device_so_connect_works(qapp, monkeypatch):
    """The bottom-left Connect/Disconnect buttons act on _active_port, only set when a device row is
    selected. QListWidget.addItem never auto-selects, so after a Scan the list showed a device but
    nothing was current, _active_port stayed "", and the buttons hit the `if not port: return` guard
    and silently no-opped — the "buttons don't work" report. _refresh_devices must auto-select the
    first device so the buttons target it with no manual click."""
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    class _Conn:
        is_connected = True

        def on_line(self, *_a, **_k):
            pass

        def write(self, *_a, **_k):
            pass

    dm = DeviceManager()
    dm.add_device(Device(port="COM7", name="Marauder"))
    tab = DeviceTab(dm)
    tab._refresh_devices()  # what Scan Ports / Refresh does

    # Root cause: without an auto-select, _active_port stays "" and the buttons are dead.
    assert tab._active_port == "COM7", "first device must be auto-selected"
    assert tab._btn_connect.isEnabled(), "Connect must be enabled for the auto-selected device"

    # And Connect now actually opens the link instead of returning early on an empty port.
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        dm, "open_connection",
        lambda port, baud=115200, owner=None: captured.update(port=port) or _Conn(),
    )
    tab._on_connect()
    assert captured.get("port") == "COM7", "Connect must open the auto-selected port, not no-op"


def test_refresh_keeps_user_selection_not_forcing_first(qapp):
    """Auto-select only fills an EMPTY _active_port — a later refresh must not yank the user's
    chosen row back to the first device (multi-device selection stability)."""
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(Device(port="COM1", name="A"))
    dm.add_device(Device(port="COM2", name="B"))
    tab = DeviceTab(dm)
    tab._refresh_devices()
    assert tab._active_port == "COM1"  # auto-selected the first

    tab._active_port = "COM2"          # user picks the second device
    tab._refresh_devices()             # a periodic refresh must respect that choice
    assert tab._active_port == "COM2", "refresh must not force the selection back to the first"


def test_terminal_line_html_escaped(qapp):
    # Untrusted device serial lines are appended to a QTextEdit, which renders rich text when the line
    # begins with markup. They must be escaped so a board can't spoof the Serial Terminal with
    # <b>/<img>/<span> markup (command-echo/output injection on a security tool).
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    tab = DeviceTab(DeviceManager())  # _dms_auth defaults to None -> straight append path
    payload = '<img src="file:///C:/Windows/win.ini"><b>fake echo</b>'
    tab._on_line_received("COM9", payload)
    text = tab._terminal.toPlainText()
    # HTML-parsed rich text would strip the angle-bracket markup from plain text; escaped text keeps it.
    assert payload in text, "device serial line must be shown verbatim, not rendered as HTML"
