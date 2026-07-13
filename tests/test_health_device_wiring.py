"""Device-health wiring — the HealthMonitor <-> DeviceManager cross-module link.

Regression guard for the bug where ``HealthMonitor.register_device`` was never called
anywhere, so ``get_all_device_health()`` always returned ``{}`` and the Health tab's
Device Health table was permanently empty even with a board connected.

The fix is ``HealthMonitor.attach_device_manager(dm)``: it registers already-known
devices and subscribes register/unregister to the manager's device connect/disconnect
events, and the poll body re-resolves each port's live connection so a serial link
opened after detection flips the device's status to "connected".
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core.health_monitor import HealthMonitor


class _FakeDM:
    """Minimal DeviceManager stand-in: records connect/disconnect callbacks and lets a
    test fire them, mirroring what HotPlugMonitor does on a real plug/unplug."""

    def __init__(self) -> None:
        self._connected: list = []
        self._disconnected: list = []
        self.devices: list = []
        self.conns: dict = {}

    def on_device_connected(self, cb) -> None:
        self._connected.append(cb)

    def on_device_disconnected(self, cb) -> None:
        self._disconnected.append(cb)

    def list_devices(self) -> list:
        return list(self.devices)

    def get_connection(self, port: str):
        return self.conns.get(port)

    # test helpers ----------------------------------------------------
    def fire_connected(self, dev) -> None:
        for cb in self._connected:
            cb(dev)

    def fire_disconnected(self, dev) -> None:
        for cb in self._disconnected:
            cb(dev)


def test_device_health_empty_until_attached():
    # Baseline: with nothing wired, the table source is empty (the pre-fix state).
    hm = HealthMonitor()
    assert hm.get_all_device_health() == {}


def test_connect_event_registers_device():
    hm = HealthMonitor()
    dm = _FakeDM()
    hm.attach_device_manager(dm)
    assert hm.get_all_device_health() == {}, "nothing known yet -> still empty"

    # A board is detected (hotplug fires on_device_connected).
    dm.fire_connected(SimpleNamespace(port="COM7"))

    health = hm.get_all_device_health()
    assert "COM7" in health, "connect event must register the device for health tracking"
    assert health["COM7"]["port"] == "COM7"


def test_disconnect_event_unregisters_device():
    hm = HealthMonitor()
    dm = _FakeDM()
    hm.attach_device_manager(dm)

    dev = SimpleNamespace(port="COM7")
    dm.fire_connected(dev)
    assert "COM7" in hm.get_all_device_health()

    dm.fire_disconnected(dev)
    assert "COM7" not in hm.get_all_device_health(), "disconnect must drop the device"


def test_attach_backfills_already_known_devices():
    # A device already in the registry at attach time is registered immediately.
    hm = HealthMonitor()
    dm = _FakeDM()
    dm.devices = [SimpleNamespace(port="COM3")]
    hm.attach_device_manager(dm)
    assert "COM3" in hm.get_all_device_health()


def test_poll_refresh_reflects_link_opened_after_detection():
    # A device is detected before any serial port is open (get_connection -> None),
    # then a link is opened later. The poll body must re-resolve the live connection
    # so the device's status flips from "registered" to "connected".
    hm = HealthMonitor()
    dm = _FakeDM()
    hm.attach_device_manager(dm)

    dm.fire_connected(SimpleNamespace(port="COM7"))
    hm._refresh_device_health()
    assert hm.get_all_device_health()["COM7"]["status"] == "registered", (
        "no live link yet -> not connected"
    )

    # Operator opens the serial link on the Devices tab -> DeviceManager now has a
    # live connection for the port.
    dm.conns["COM7"] = SimpleNamespace(is_connected=True)
    hm._refresh_device_health()
    info = hm.get_all_device_health()["COM7"]
    assert info["status"] == "connected", "poll must pick up the link opened after detection"
    assert info["last_seen"], "last_seen must be refreshed once the device is live"


def test_closed_link_flips_connected_to_disconnected():
    # Regression: a device that was connected then has its serial link CLOSED (get_connection -> None,
    # e.g. the Devices-tab Disconnect pops it) must stop reading "connected". The old code skipped the
    # conn-None case entirely, so the status stayed frozen at "connected" forever while the board stayed
    # physically plugged.
    hm = HealthMonitor()
    dm = _FakeDM()
    hm.attach_device_manager(dm)
    dm.fire_connected(SimpleNamespace(port="COM7"))
    dm.conns["COM7"] = SimpleNamespace(is_connected=True)
    hm._refresh_device_health()
    assert hm.get_all_device_health()["COM7"]["status"] == "connected"

    del dm.conns["COM7"]                       # operator disconnects; no live connection for the port
    hm._refresh_device_health()
    assert hm.get_all_device_health()["COM7"]["status"] == "disconnected", (
        "a closed-but-plugged device must not keep reading connected"
    )


class _FakeDMWithDevices(_FakeDM):
    """_FakeDM that also answers get_device(port) -> the registered Device, mirroring
    DeviceManager.get_device so health can read the real firmware/probe-health."""

    def __init__(self) -> None:
        super().__init__()
        self.by_port: dict = {}

    def get_device(self, port: str):
        return self.by_port.get(port)


def test_health_surfaces_firmware_and_flags_silent_board():
    from src.models.device import Device

    hm = HealthMonitor()
    dm = _FakeDMWithDevices()
    hm.attach_device_manager(dm)

    # An alive Marauder on an open link: the real firmware surfaces (not "unknown"),
    # status is connected, and last_seen refreshes.
    alive = Device(port="COM7", name="M", firmware="marauder")
    alive.health = "alive"
    dm.by_port["COM7"] = alive
    dm.conns["COM7"] = SimpleNamespace(is_connected=True)
    dm.fire_connected(alive)
    hm._refresh_device_health()
    info = hm.get_all_device_health()["COM7"]
    assert info["firmware_version"] == "marauder", "firmware must come from the handshake, not stay 'unknown'"
    assert info["status"] == "connected"
    assert info["last_seen"]

    # A hung / mis-flashed board keeps its CDC link open but never replies ("no-reply"):
    # it must NOT read as a green "connected", and last_seen must FREEZE (stop ticking).
    dead = Device(port="COM9", name="D")
    dead.health = "no-reply"
    dm.by_port["COM9"] = dead
    dm.conns["COM9"] = SimpleNamespace(is_connected=True)
    dm.fire_connected(dead)
    hm._refresh_device_health()
    first = hm.get_all_device_health()["COM9"]
    assert first["status"] == "no-reply", "silent firmware must not read as connected"
    frozen = first["last_seen"]
    hm._refresh_device_health()
    assert hm.get_all_device_health()["COM9"]["last_seen"] == frozen, "last_seen must freeze while silent"


# ── GUI integration: the real window must wire the monitor to its DeviceManager ──

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.models.device import Device  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    """SoftwareTab.__init__ shells out to PowerShell for SD detection on Windows; stub it
    to an instant empty result so building a window can't hang (test isolation only)."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


class _FakeConn:
    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected

    def disconnect(self) -> None:  # called by DeviceManager.shutdown on window close
        self.is_connected = False


def test_window_wires_health_monitor_to_device_manager(qapp, isolated_settings):
    from PyQt5.QtCore import QTimer

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    dm = DeviceManager()
    win = CyberControllerWindow(dm, FlashEngine(), bus, TargetPool(bus))
    try:
        # Quiesce background activity so no thread/timer leaks into later tests.
        try:
            win._health.stop()
        except Exception:
            pass
        for t in win.findChildren(QTimer):
            t.stop()

        # Table source starts empty...
        assert win._health.get_all_device_health() == {}

        # ...and a device becoming available fires on_device_connected, which the window
        # must have wired into the monitor. attach_connection is the public path that
        # fires that event (same event HotPlugMonitor fires on a physical plug).
        dev = Device(port="COM_TEST", name="Test Board")
        dm.attach_connection(dev, _FakeConn(connected=True), owner="test")

        health = win._health.get_all_device_health()
        assert "COM_TEST" in health, (
            "the window must wire HealthMonitor to the DeviceManager so a connected "
            "device populates the Device Health table"
        )
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
