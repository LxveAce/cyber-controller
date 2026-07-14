"""Regression (deep-audit-2, 2026-07-13, HIGH): a device attached BEFORE launch must register.

The Qt UI relies solely on HotPlugMonitor for its device registry — unlike the web/tk front-ends it
does no initial scan_ports()->add_device of its own. HotPlugMonitor.run() used to seed _known_ports
with the currently-visible ports WITHOUT add_device()-ing them, so every board already attached at
process start was treated as "already known" and never registered until a manual Scan.
list_devices() stayed empty and the bottom-left persistent-terminal Connect reported "No devices"
even with hardware plugged in. run() now REGISTERS (add_device + _fire_connected) each initially-
visible device before entering its poll loop.

Pure/threaded logic — no real serial hardware: scan_ports is monkeypatched and the stop event is set
before run() so it does the initial registration and returns without polling.
"""
from __future__ import annotations

from src.core.device_manager import DeviceManager, HotPlugMonitor
from src.models.device import Device


def _run_once(dm: DeviceManager) -> HotPlugMonitor:
    """Run only the initial-registration prologue of HotPlugMonitor.run() (stop set up front)."""
    mon = HotPlugMonitor(dm, interval=0.01)
    mon._stop_event.set()  # loop body never runs; run() does the initial registration then returns
    mon.run()
    return mon


def test_devices_present_at_start_are_registered(monkeypatch):
    dm = DeviceManager()
    present = [
        Device(port="COM_HP1", name="Marauder", firmware="marauder"),
        Device(port="COM_HP2", name="Flipper", firmware="flipper"),
    ]
    monkeypatch.setattr(dm, "scan_ports", lambda: list(present))

    fired: list[str] = []
    dm.on_device_connected(lambda d: fired.append(d.port))

    assert dm.list_devices() == []  # nothing registered before the monitor runs
    _run_once(dm)

    ports = {d.port for d in dm.list_devices()}
    assert ports == {"COM_HP1", "COM_HP2"}, "pre-launch devices must be registered, not just seeded"
    assert dm.get_device("COM_HP1") is not None
    assert sorted(fired) == ["COM_HP1", "COM_HP2"], "each initial device must fire connected"


def test_no_devices_at_start_registers_nothing(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [])
    fired: list[str] = []
    dm.on_device_connected(lambda d: fired.append(d.port))
    _run_once(dm)
    assert dm.list_devices() == []
    assert fired == []


def test_already_registered_device_is_not_double_fired(monkeypatch):
    # If another path already registered a port, the initial pass must not re-fire connected for it.
    dm = DeviceManager()
    dm.add_device(Device(port="COM_HP3", name="Marauder", firmware="marauder"))
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM_HP3", name="Marauder")])
    fired: list[str] = []
    dm.on_device_connected(lambda d: fired.append(d.port))
    _run_once(dm)
    assert fired == [], "an already-registered device must not re-fire on_device_connected"
    assert dm.get_device("COM_HP3") is not None


def test_known_ports_seeded_so_the_first_poll_sees_no_spurious_new_device(monkeypatch):
    # After the initial registration, _known_ports must equal the visible set so the very first poll
    # iteration doesn't re-report the same devices as freshly connected.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM_HP4", name="Marauder")])
    mon = _run_once(dm)
    assert mon._known_ports == {"COM_HP4"}
