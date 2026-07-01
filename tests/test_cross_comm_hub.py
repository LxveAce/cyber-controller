"""CrossCommHub (comms rework, S2): the six cross-comm parts assemble in ONE core object instead of being
hand-wired inside the Qt window. Behavior-preserving — same parts, same wiring, just on the spine. These
tests pin the assembly + the routed-command send sink so the UI can stay a thin consumer.
"""
from __future__ import annotations

from src.core.cross_comm import EventBus, RoutingRule, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.models.device import Device
from src.models.target import Target, TargetType


class _FakeConn:
    """Minimal stand-in for a live SerialConnection: records writes + the terminator set on it."""

    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.line_ending = "\n"
        self.written: list[str] = []

    def write(self, command: str) -> None:
        self.written.append(command)


def _dm_with_conn(port: str, firmware: str, connected: bool = True) -> tuple[DeviceManager, _FakeConn]:
    dm = DeviceManager()
    dm.add_device(Device(port=port, firmware=firmware))
    conn = _FakeConn(connected=connected)
    dm._connections[port] = conn  # inject a live connection without real hardware (mirrors open_connection)
    return dm, conn


def test_hub_assembles_all_parts():
    hub = CrossCommHub(DeviceManager())
    assert isinstance(hub.bus, EventBus)
    assert isinstance(hub.pool, TargetPool)
    assert hub.ingestor is not None
    assert hub.router is not None
    assert hub.broadcast is not None
    # action_resolver is optional but importable in this tree, so it should be present here.
    assert hub.action_resolver is not None


def test_hub_reuses_supplied_bus_and_pool():
    bus = EventBus()
    pool = TargetPool(bus)
    hub = CrossCommHub(DeviceManager(), bus=bus, pool=pool)
    assert hub.bus is bus
    assert hub.pool is pool


def test_pool_publishes_on_the_hub_bus():
    # The pool must share the hub's bus, or the router (subscribed to that bus) never sees discoveries.
    hub = CrossCommHub(DeviceManager())
    assert hub.pool.bus is hub.bus


def test_send_to_port_writes_with_firmware_terminator():
    dm, conn = _dm_with_conn("COM_T", "flipper")
    hub = CrossCommHub(dm)
    hub.send_to_port("COM_T", "scanap")
    assert conn.written == ["scanap"]
    assert conn.line_ending == "\r"  # Flipper CLI submits on CR, not LF


def test_send_to_port_no_connection_is_safe():
    # No live connection on the port -> a logged warning, never an exception.
    hub = CrossCommHub(DeviceManager())
    hub.send_to_port("COM_NOPE", "reboot")  # must not raise


def test_send_to_port_skips_a_dead_connection():
    dm, conn = _dm_with_conn("COM_D", "marauder", connected=False)
    hub = CrossCommHub(dm)
    hub.send_to_port("COM_D", "stop")
    assert conn.written == []  # a disconnected port is not written to


def test_router_dispatches_through_the_hub_send_sink():
    # The end-to-end spine: a discovered AP on the bus -> AutoRouter rule -> hub.send_to_port -> serial write.
    dm, conn = _dm_with_conn("COM_B", "marauder")
    hub = CrossCommHub(dm)
    hub.router.add_rule(RoutingRule(
        name="ap-to-B",
        target_type=TargetType.AP,
        ssid_pattern="lab",
        min_rssi=-90,
        command_template="channel {channel}",
        device_port="COM_B",
        cooldown=0.0,
        enabled=True,
    ))
    # A scan on "device A" lands an AP in the shared pool; that publishes target.added on the hub bus.
    hub.pool.add(Target(target_type=TargetType.AP, mac="AA:BB:CC:DD:EE:FF", ssid="lab-ap", channel=6))
    assert conn.written == ["channel 6"]
