"""DeviceManager connection ownership + hotplug-during-connect hardening (bug-hunt #8, #27).

Uses a fake SerialConnection (monkeypatched) so no real serial port is opened."""

from __future__ import annotations

import pytest


class _FakeConn:
    def __init__(self, port, baud=115200, line_ending="\n"):
        self.port = port
        self.line_ending = line_ending
        self._open = False
        self.disconnected = 0

    def connect(self):
        self._open = True

    def disconnect(self):
        self._open = False
        self.disconnected += 1

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


def test_shared_connection_survives_until_last_owner_releases(monkeypatch):
    dm = _dm(monkeypatch)
    c1 = dm.open_connection("COM7", owner="devices_tab")
    c2 = dm.open_connection("COM7", owner="pterm")
    assert c1 is c2 and c1.is_connected  # one shared connection, two owners

    dm.close_connection("COM7", owner="pterm")  # one owner releases
    assert c1.is_connected                      # still alive — devices_tab still owns it
    assert dm.get_device("COM7").connected is True

    dm.close_connection("COM7", owner="devices_tab")  # last owner releases
    assert not c1.is_connected
    assert dm.get_device("COM7").connected is False


def test_close_without_owner_force_closes(monkeypatch):
    dm = _dm(monkeypatch)
    c = dm.open_connection("COM7", owner="devices_tab")
    dm.close_connection("COM7")  # shutdown/hotplug path -> force close regardless of owners
    assert not c.is_connected and dm.get_device("COM7").connected is False


def test_open_connection_hotplug_during_connect_raises_and_cleans_up(monkeypatch):
    import src.core.device_manager as DM
    from src.models.device import Device
    dm = DM.DeviceManager()
    dm.add_device(Device(port="COM9"))

    class _RemovingConn(_FakeConn):
        def connect(self):
            super().connect()
            dm.remove_device("COM9")  # simulate a hot-unplug mid-connect (releases the registry entry)

    monkeypatch.setattr(DM, "SerialConnection", _RemovingConn)
    with pytest.raises(KeyError):
        dm.open_connection("COM9", owner="devices_tab")
    # The freshly opened orphan conn must be disconnected, not leaked, and no zombie left in the registry.
    assert dm.get_connection("COM9") is None
