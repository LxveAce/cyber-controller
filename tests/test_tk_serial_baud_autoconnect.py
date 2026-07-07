"""Tk Settings 'Baud Rate' and 'Auto-connect on device detection' controls must actually drive serial.

Regression: ``_on_dev_connect`` opened the link with ``dm.open_connection(port)`` passing NO baud, so the
configured Baud Rate was ignored and every connection ran at the hardcoded 115200 — a device that only
talks at another speed (e.g. a 9600 UART/GPS module) got a wrong-speed link with nothing surfaced. And
the 'Auto-connect on device detection' toggle was set/get only inside the Settings handlers and consumed
nowhere, so it did nothing.

Both are now wired: connect passes the configured baud, and the periodic refresh auto-opens each
newly-detected, not-yet-connected device (once) when the toggle is on. Built via ``__new__`` so no Tk
display / widget tree is needed (same pattern as test_macro_recording_wiring's Tk case).
"""
from __future__ import annotations

import pytest

pytest.importorskip("tkinter")


class _Conn:
    is_connected = True

    def __init__(self):
        self.writes: list[str] = []

    def on_line(self, _cb):
        pass

    def write(self, s):
        self.writes.append(s)


class _Widget:
    """Minimal stand-in for a combobox / bool-var / button (.get / .configure)."""

    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def configure(self, *_a, **_k):
        pass


def _app():
    import src.ui.tk.app as tkapp
    return tkapp, tkapp.TkLightApp.__new__(tkapp.TkLightApp)


def test_connect_passes_configured_baud():
    tkapp, app = _app()
    calls: list = []

    class _DM:
        def open_connection(self, port, baud=115200, owner=None):
            calls.append((port, baud))
            return _Conn()

    app._dm = _DM()
    app._active_port = "COM3"
    app._settings_baud = _Widget("9600")
    app._btn_dev_connect = _Widget()
    app._btn_dev_disconnect = _Widget()
    app._btn_send = _Widget()
    app._serial_append_sys = lambda *a, **k: None
    app._refresh_device_list = lambda: None
    app._root = _Widget()  # only referenced inside an uninvoked on_line lambda

    tkapp.TkLightApp._on_dev_connect(app)
    assert calls == [("COM3", 9600)], "connect must open at the Settings baud, not the hardcoded 115200"


def test_autoconnect_opens_detected_device_once_when_enabled():
    from src.models.device import Device

    tkapp, app = _app()
    opened: list = []
    dev = Device(port="COM4", name="ESP32", connected=False)

    class _DM:
        def list_devices(self):
            return [dev]

        def open_connection(self, port, baud=115200, owner=None):
            opened.append((port, baud, owner))
            dev.connected = True
            return _Conn()

    app._dm = _DM()
    app._settings_autoconnect = _Widget(True)
    app._settings_baud = _Widget("9600")
    app._autoconn_seen = set()

    tkapp.TkLightApp._maybe_autoconnect(app)
    assert opened == [("COM4", 9600, "tk_autoconnect")], (
        "an enabled auto-connect must open the detected device at the configured baud"
    )

    # A later tick must NOT re-open it (even if the user manually disconnected) — auto-open is once per
    # detection, so the toggle never fights a deliberate disconnect.
    dev.connected = False
    tkapp.TkLightApp._maybe_autoconnect(app)
    assert opened == [("COM4", 9600, "tk_autoconnect")], "must auto-open only once per detection"


def test_autoconnect_noop_when_disabled():
    from src.models.device import Device

    tkapp, app = _app()
    opened: list = []
    dev = Device(port="COM4", name="ESP32", connected=False)

    class _DM:
        def list_devices(self):
            return [dev]

        def open_connection(self, port, baud=115200, owner=None):
            opened.append(port)
            return _Conn()

    app._dm = _DM()
    app._settings_autoconnect = _Widget(False)
    app._settings_baud = _Widget("115200")
    app._autoconn_seen = set()

    tkapp.TkLightApp._maybe_autoconnect(app)
    assert opened == [], "auto-connect off -> must not open anything"
