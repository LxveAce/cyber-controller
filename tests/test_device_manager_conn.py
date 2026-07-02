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
        self._state_cbs = []

    def on_state_change(self, cb):
        self._state_cbs.append(cb)

    def _fire(self):
        for cb in list(self._state_cbs):
            cb(self._open)

    def connect(self):
        self._open = True
        self._fire()

    def disconnect(self):
        self._open = False
        self.disconnected += 1
        self._fire()

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


def test_concurrent_open_same_port_builds_one_connection(monkeypatch):
    """Two threads opening the SAME port at once must share ONE connection — not both build one and
    have the second overwrite (leak) the first. A per-port build lock serializes build+connect."""
    import threading
    import time

    import src.core.device_manager as DM
    from src.models.device import Device

    built: list[object] = []
    gate = threading.Event()

    class _SlowConn(_FakeConn):
        def __init__(self, port, baud=115200, line_ending="\n"):
            super().__init__(port, baud, line_ending)
            built.append(self)

        def connect(self):
            gate.wait(2.0)  # hold the in-flight build so a second concurrent opener overlaps it
            super().connect()

    monkeypatch.setattr(DM, "SerialConnection", _SlowConn)
    dm = DM.DeviceManager()
    dm.add_device(Device(port="COM7"))

    results: dict[str, object] = {}

    def opener(name):
        results[name] = dm.open_connection("COM7", owner=name)

    t1 = threading.Thread(target=opener, args=("a",))
    t2 = threading.Thread(target=opener, args=("b",))
    t1.start()
    t2.start()
    time.sleep(0.15)  # both threads are now past the fast top-check; one holds build_lock, one waits
    gate.set()        # let the single in-flight connect() complete
    t1.join(3.0)
    t2.join(3.0)

    assert results["a"] is results["b"]              # one shared connection
    assert len(built) == 1                           # only ONE SerialConnection ever built (no leak)
    assert results["a"].disconnected == 0            # the single conn was never orphaned/disconnected
    assert dm._conn_owners.get("COM7") == {"a", "b"}  # both owners registered on the shared conn


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


def test_error_state_reflects_in_device_connected(monkeypatch):
    """A mid-session serial ERROR (not a full unplug) must flip Device.connected False, so the UI stops
    showing 'connected' and the AutoRouter stops silently dropping routed commands to a dead port."""
    import src.core.device_manager as DM
    from src.models.device import Device
    from src.core.serial_handler import ConnectionState

    class _StatefulFake:
        def __init__(self, port, baud=115200, line_ending="\n"):
            self.port = port
            self.line_ending = line_ending
            self._state = ConnectionState.DISCONNECTED
            self._cbs = []

        def on_state_change(self, cb):
            self._cbs.append(cb)

        def _set(self, s):
            self._state = s
            for cb in list(self._cbs):
                cb(s)

        def connect(self):
            self._set(ConnectionState.CONNECTED)

        def disconnect(self):
            self._set(ConnectionState.DISCONNECTED)

        def fail(self):
            self._set(ConnectionState.ERROR)  # transient glitch / reboot in the reader loop

        @property
        def is_connected(self):
            return self._state == ConnectionState.CONNECTED

    monkeypatch.setattr(DM, "SerialConnection", _StatefulFake)
    dm = DM.DeviceManager()
    dm.add_device(Device(port="COM7"))
    conn = dm.open_connection("COM7", owner="devices_tab")
    assert dm.get_device("COM7").connected is True
    conn.fail()
    assert conn.is_connected is False
    assert dm.get_device("COM7").connected is False  # indicator no longer lies
