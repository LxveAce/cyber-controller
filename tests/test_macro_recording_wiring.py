"""Macro recording is actually wired to a producer.

Regression for the "recording captures nothing" bug: ``MacroRecorder.record_command`` is the only hook
that appends a step to an in-progress recording, but no command-send path called it, so
``start_recording`` -> send commands -> ``stop_recording`` always returned an empty macro.

These tests drive the real terminal send handlers (Qt ``DeviceTab._on_send`` and Tk
``TkLightApp._on_send_cmd``) with a live ``MacroRecorder`` recording and assert every sent command is
captured as a macro step. Both FAIL before the wiring (empty ``macro.steps``) and PASS after.
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


class _Conn:
    """Minimal serial-connection stand-in that records what was written."""

    def __init__(self):
        self.line_ending = "\n"
        self.sent = []

    def write(self, s):
        self.sent.append(s)


def test_qt_devicetab_send_captures_into_recording(qapp, tmp_path, monkeypatch):
    """The Devices-tab terminal send feeds each sent command into the active macro recording."""
    from src.core import safety
    from src.core.device_manager import DeviceManager
    from src.core.macro_recorder import MacroRecorder
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    # Isolate from the danger-confirm modal (covered by its own tests) so nothing blocks headlessly.
    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)

    dm = DeviceManager()
    dm.add_device(Device(port="COM9", name="Marauder", firmware="marauder", connected=True))
    recorder = MacroRecorder(macros_dir=tmp_path)
    tab = DeviceTab(dm, recorder=recorder)
    tab._active_port = "COM9"
    tab._active_conn = _Conn()

    recorder.start_recording("COM9", protocol="marauder")
    for cmd in ("scanap", "stopscan", "sysinfo"):
        tab._cmd_input.setText(cmd)
        tab._on_send()
    macro = recorder.stop_recording(name="rec")

    assert tab._active_conn.sent == ["scanap", "stopscan", "sysinfo"], "commands must still reach the wire"
    assert [s.command for s in macro.steps] == ["scanap", "stopscan", "sysinfo"], (
        "each command sent from the Devices tab must be captured as a macro step"
    )


def test_qt_send_without_recorder_is_safe(qapp, tmp_path, monkeypatch):
    """A DeviceTab built without a recorder (legacy call sites) still sends fine — no wiring crash."""
    from src.core import safety
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)

    dm = DeviceManager()
    dm.add_device(Device(port="COM9", name="Marauder", firmware="marauder", connected=True))
    tab = DeviceTab(dm)  # no recorder -> record path must be a guarded no-op
    tab._active_port = "COM9"
    tab._active_conn = _Conn()
    tab._cmd_input.setText("scanap")
    tab._on_send()

    assert tab._active_conn.sent == ["scanap"]


def test_tk_terminal_send_captures_into_recording(tmp_path):
    """The Tk terminal send handler likewise feeds the recorder, or a macro recorded there is empty."""
    pytest.importorskip("tkinter")
    import src.ui.tk.app as tkapp
    from src.core.macro_recorder import MacroRecorder

    # Build the app object without running the Tk UI constructor (no display / widget tree needed).
    app = tkapp.TkLightApp.__new__(tkapp.TkLightApp)
    recorder = MacroRecorder(macros_dir=tmp_path)
    app._macro_recorder = recorder

    class _Entry:
        def __init__(self, text):
            self._text = text

        def get(self):
            return self._text

        def delete(self, *_a):
            self._text = ""

    app._active_conn = _Conn()
    app._append_serial = lambda *_a, **_k: None
    app._serial_append_sys = lambda *_a, **_k: None

    recorder.start_recording("COM1")
    for cmd in ("scanap", "stopscan"):
        app._cmd_entry = _Entry(cmd)
        tkapp.TkLightApp._on_send_cmd(app)
    macro = recorder.stop_recording(name="rec")

    assert app._active_conn.sent == ["scanap", "stopscan"], "commands must still reach the wire"
    assert [s.command for s in macro.steps] == ["scanap", "stopscan"], (
        "each command sent from the Tk terminal must be captured as a macro step"
    )


def test_tk_device_view_send_captures_into_recording(tmp_path):
    """The Tk Device View / Remote send bridge must also feed the recorder — dropping those commands was
    silent data loss: a macro recorded while tapping Device View / Remote buttons replayed incomplete."""
    pytest.importorskip("tkinter")
    import src.ui.tk.app as tkapp
    from src.core.macro_recorder import MacroRecorder

    app = tkapp.TkLightApp.__new__(tkapp.TkLightApp)
    recorder = MacroRecorder(macros_dir=tmp_path)
    app._macro_recorder = recorder
    app._active_conn = _Conn()
    app._append_serial = lambda *_a, **_k: None

    recorder.start_recording("COM1")
    for cmd in ("scanap", "stopscan"):
        tkapp.TkLightApp._device_view_send(app, cmd)
    macro = recorder.stop_recording(name="rec")

    assert app._active_conn.sent == ["scanap", "stopscan"], "commands must still reach the wire"
    assert [s.command for s in macro.steps] == ["scanap", "stopscan"], (
        "each command sent from the Device View / Remote tab must be captured as a macro step"
    )
