"""Driver seam (comms rework, S3-a): dispatch a node's outbound command by driver_type. text-cli preserves
the old send path exactly; stream/controlmap are honest no-ops (no text command channel). Pins selection +
per-driver behavior + the hub dispatching through it.
"""
from __future__ import annotations

import pytest

from src.core.drivers import ControlMapDriver, StreamDriver, TextCliDriver, driver_for
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.models.device import Device


class _FakeConn:
    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.line_ending = "\n"
        self.written: list[str] = []

    def write(self, command: str) -> None:
        self.written.append(command)


# ── selection ──────────────────────────────────────────────────────────

def test_driver_for_selects_by_driver_type():
    assert isinstance(driver_for(Device(port="C1", firmware="marauder")), TextCliDriver)
    assert isinstance(driver_for(Device(port="C2", firmware="meshtastic")), StreamDriver)
    assert isinstance(driver_for(Device(port="C3", firmware="bluejammer")), ControlMapDriver)


def test_driver_for_defaults_to_text_cli():
    assert isinstance(driver_for(None), TextCliDriver)               # no device
    assert isinstance(driver_for(Device(port="C4")), TextCliDriver)  # unknown firmware


# ── per-driver behavior ────────────────────────────────────────────────

def test_text_cli_driver_writes_with_firmware_terminator():
    conn = _FakeConn()
    dev = Device(port="C", firmware="flipper")
    assert TextCliDriver().deliver_text(conn, dev, "scanap") is True
    assert conn.written == ["scanap"]
    assert conn.line_ending == "\r"  # Flipper submits on CR


def test_stream_driver_is_an_honest_no_op():
    conn = _FakeConn()
    dev = Device(port="C", firmware="meshtastic")
    assert StreamDriver().deliver_text(conn, dev, "nodes") is False
    assert conn.written == []  # plain text is never written to a protobuf stream


def test_controlmap_driver_is_an_honest_no_op():
    conn = _FakeConn()
    dev = Device(port="C", firmware="bluejammer")
    assert ControlMapDriver().deliver_text(conn, dev, "stop") is False
    assert conn.written == []  # no serial command channel to write to


def test_stream_raw_path_raises_without_binary_write():
    # A connection with no binary write path (bare _FakeConn) still can't send a frame — honest boundary.
    with pytest.raises(NotImplementedError):
        StreamDriver().deliver_raw(_FakeConn(), b"\x94\xc3\x00\x01")


class _RawConn(_FakeConn):
    """A connection that exposes the binary write path (SerialConnection.write_bytes)."""

    def __init__(self) -> None:
        super().__init__()
        self.raw: list[bytes] = []

    def write_bytes(self, payload: bytes) -> None:
        self.raw.append(bytes(payload))


def test_stream_raw_path_sends_framed_bytes_when_binary_write_available():
    # With a binary write path present, deliver_raw frames the payload (StreamFramer) and puts it on the wire.
    from src.protocols.stream_framer import StreamFramer
    conn = _RawConn()
    payload = b"\x08\x01\x12\x03abc"
    assert StreamDriver().deliver_raw(conn, payload) is True
    assert conn.raw == [StreamFramer.frame(payload)]
    assert conn.written == []  # binary path, not the text channel


# ── hub dispatches through the seam ────────────────────────────────────

def _dm_with_conn(port: str, firmware: str) -> tuple[DeviceManager, _FakeConn]:
    dm = DeviceManager()
    dm.add_device(Device(port=port, firmware=firmware))
    conn = _FakeConn()
    dm._connections[port] = conn
    return dm, conn


def test_hub_dispatches_text_cli_write():
    dm, conn = _dm_with_conn("C_M", "marauder")
    CrossCommHub(dm).send_to_port("C_M", "scanap")
    assert conn.written == ["scanap"]  # text-cli path unchanged


def test_hub_no_ops_a_stream_device():
    # A routed command to a Meshtastic (stream) node must NOT write raw text to the port.
    dm, conn = _dm_with_conn("C_S", "meshtastic")
    CrossCommHub(dm).send_to_port("C_S", "nodes")
    assert conn.written == []


def test_hub_no_ops_a_controlmap_device():
    dm, conn = _dm_with_conn("C_C", "bluejammer")
    CrossCommHub(dm).send_to_port("C_C", "stop")
    assert conn.written == []
