"""Tk Settings 'Default Port' must actually preselect that port in the Flash tab.

Regression: serial.default_port was written on Save and restored into the Settings combobox on load, but
no code path consumed it — _refresh_ports always did _flash_port.current(0), so the chosen default port
was silently ignored. _refresh_ports now consumes _configured_port(). Built via __new__ (no Tk display).
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")


class _Widget:
    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value


class _FakeCombo:
    def __init__(self):
        self._vals = []
        self.current_index = None

    def __setitem__(self, key, val):
        if key == "values":
            self._vals = val

    def __getitem__(self, key):
        return self._vals if key == "values" else None

    def current(self, i):
        self.current_index = i


def _app():
    import src.ui.tk.app as tkapp
    return tkapp, tkapp.TkLightApp.__new__(tkapp.TkLightApp)


def _devs():
    from src.models.device import Device
    return [Device(port="COM3", name="ESP32"), Device(port="COM7", name="Marauder")]


def test_refresh_ports_preselects_configured_default_port():
    tkapp, app = _app()
    devs = _devs()
    app._dm = type("DM", (), {"scan_ports": lambda self: devs})()
    app._settings_port = _Widget("COM7")   # the user's Default Port
    app._flash_port = _FakeCombo()

    tkapp.TkLightApp._refresh_ports(app)

    assert app._flash_port.current_index == 1, "the configured Default Port (COM7) must be preselected"


def test_refresh_ports_falls_back_to_first_when_no_match():
    tkapp, app = _app()
    devs = _devs()
    app._dm = type("DM", (), {"scan_ports": lambda self: devs})()
    app._settings_port = _Widget("COM99")  # not present -> fall back to index 0
    app._flash_port = _FakeCombo()

    tkapp.TkLightApp._refresh_ports(app)

    assert app._flash_port.current_index == 0
