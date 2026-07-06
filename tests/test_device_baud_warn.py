"""Reusing a shared serial connection at a DIFFERENT baud must warn — an open port can't be re-bauded,
so the second caller silently runs at the first opener's speed (e.g. a GPS opened at 115200 in the Devices
tab then reused at 9600 by a wardrive → garbage NMEA → a silent 'No Fix' with no GPS tags in the CSV).

Fake SerialConnection (monkeypatched) so no real port is opened.
"""

from __future__ import annotations

import logging

import pytest


class _FakeConn:
    def __init__(self, port, baud=115200, line_ending="\n"):
        self.port = port
        self.baud = baud
        self._open = False
        self._cbs = []

    def on_state_change(self, cb):
        self._cbs.append(cb)

    def connect(self):
        self._open = True
        for cb in list(self._cbs):
            cb(True)

    def disconnect(self):
        self._open = False
        for cb in list(self._cbs):
            cb(False)

    @property
    def is_connected(self):
        return self._open


def _dm(monkeypatch):
    import src.core.device_manager as DM
    from src.models.device import Device

    monkeypatch.setattr(DM, "SerialConnection", _FakeConn)
    dm = DM.DeviceManager()
    dm.add_device(Device(port="COM7"))
    return dm


def test_reuse_same_baud_is_silent(monkeypatch, caplog):
    dm = _dm(monkeypatch)
    dm.open_connection("COM7", baud=115200, owner="a")
    with caplog.at_level(logging.WARNING):
        dm.open_connection("COM7", baud=115200, owner="b")
    assert "already open at" not in caplog.text


def test_reuse_different_baud_warns(monkeypatch, caplog):
    dm = _dm(monkeypatch)
    dm.open_connection("COM7", baud=115200, owner="devices_tab")
    with caplog.at_level(logging.WARNING):
        dm.open_connection("COM7", baud=9600, owner="wardrive")
    assert "already open at 115200" in caplog.text
    assert "9600" in caplog.text
