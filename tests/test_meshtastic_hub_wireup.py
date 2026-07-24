"""CrossCommHub wires a MeshtasticBackend onto a stream device's raw byte path (Wave 8).

A stream device (Meshtastic) gets the protobuf backend instead of the text line ingestor: raw mode on, the
handshake sent, and decoded node/channel/text state fanned onto the bus under ``mesh.*`` topics. A text-CLI
device (Marauder) is untouched.
"""

from __future__ import annotations

from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer


class _FakeConn:
    def __init__(self) -> None:
        self.raw = False
        self.is_connected = True
        self._byte_cbs: list = []
        self._line_cbs: list = []
        self._state_cbs: list = []
        self.written: list[bytes] = []

    def on_bytes(self, cb):
        self._byte_cbs.append(cb)

    def remove_byte_callback(self, cb):
        if cb in self._byte_cbs:
            self._byte_cbs.remove(cb)

    def on_line(self, cb):
        self._line_cbs.append(cb)

    def remove_line_callback(self, cb):
        if cb in self._line_cbs:
            self._line_cbs.remove(cb)

    def on_state_change(self, cb):
        self._state_cbs.append(cb)

    def fire_state(self, state):  # test helper: simulate a connection state transition
        for cb in list(self._state_cbs):
            cb(state)

    def write_bytes(self, data):
        self.written.append(bytes(data))

    def feed(self, data):  # test helper: drive the byte callbacks like the reader thread would
        for cb in list(self._byte_cbs):
            cb(data)


class _FakeDevice:
    def __init__(self, firmware: str, port: str = "COM-TEST") -> None:
        self.firmware = firmware
        self.name = firmware
        self.port = port


def _hub_with_fw(firmware, monkeypatch):
    dm = DeviceManager()
    hub = CrossCommHub(dm)
    monkeypatch.setattr(dm, "get_device", lambda port, _fw=firmware: _FakeDevice(_fw))
    return hub


def test_meshtastic_device_gets_stream_backend(monkeypatch):
    hub = _hub_with_fw("meshtastic", monkeypatch)
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)
    assert conn.raw is True
    backend = hub.mesh_backend("COM-TEST")
    assert backend is not None
    assert len(conn.written) == 1  # want_config sent on attach
    payload = StreamFramer().feed(conn.written[0])[0]
    assert 3 in mp.parse(payload)  # ToRadio.want_config_id


def test_text_cli_device_gets_no_stream_backend(monkeypatch):
    hub = _hub_with_fw("marauder", monkeypatch)
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)
    assert conn.raw is False
    assert hub.mesh_backend("COM-TEST") is None
    assert conn.written == []


def test_decoded_node_events_publish_on_bus(monkeypatch):
    hub = _hub_with_fw("meshtastic", monkeypatch)
    got: list[dict] = []
    hub.bus.subscribe("mesh.node", lambda _t, d: got.append(d))
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)

    user = mp.field_bytes(1, b"!deadbeef") + mp.field_bytes(2, b"Node") + mp.field_varint(5, 43)
    node_info = mp.field_varint(1, 0xDEADBEEF) + mp.field_bytes(2, user)
    conn.feed(StreamFramer.frame(mp.field_bytes(4, node_info)))

    assert len(got) == 1
    assert got[0]["port"] == "COM-TEST"
    assert got[0]["node_id"] == "!deadbeef"
    assert got[0]["hw_model_name"] == "HELTEC_V3"


def test_incoming_text_publishes_on_bus(monkeypatch):
    hub = _hub_with_fw("meshtastic", monkeypatch)
    got: list[dict] = []
    hub.bus.subscribe("mesh.text", lambda _t, d: got.append(d))
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)

    data = mp.field_varint(1, mp.TEXT_MESSAGE_APP) + mp.field_bytes(2, b"ping")
    packet = mp.field_fixed32(1, 0x1BA746AC) + mp.field_bytes(4, data)
    conn.feed(StreamFramer.frame(mp.field_bytes(2, packet)))

    assert len(got) == 1
    assert got[0]["text"] == "ping"
    assert got[0]["port"] == "COM-TEST"


def test_reattach_same_conn_is_idempotent(monkeypatch):
    hub = _hub_with_fw("meshtastic", monkeypatch)
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)
    first = hub.mesh_backend("COM-TEST")
    hub._attach_ingestor("COM-TEST", conn)  # same live conn again
    assert hub.mesh_backend("COM-TEST") is first  # not replaced
    assert len(conn._byte_cbs) == 1  # not double-wired -> no double-feed


def test_stream_backend_attaches_on_firmware_change(monkeypatch):
    # First Connect resolves firmware AFTER open (the Devices tab persists it post-open, or auto-detect
    # finds it later). The hub must attach the backend when the firmware becomes a stream device, not only
    # at open time — otherwise the panel is inert on first Connect.
    dm = DeviceManager()
    hub = CrossCommHub(dm)
    conn = _FakeConn()
    dev = _FakeDevice("marauder")
    monkeypatch.setattr(dm, "get_device", lambda port: dev)
    monkeypatch.setattr(dm, "get_connection", lambda port: conn)

    hub._attach_ingestor("COM-TEST", conn)  # opened as a text-CLI device
    assert hub.mesh_backend("COM-TEST") is None
    assert conn.raw is False

    dev.firmware = "meshtastic"  # firmware resolves after connect
    hub._on_device_changed(dev)
    assert conn.raw is True
    assert hub.mesh_backend("COM-TEST") is not None


def test_stream_backend_detaches_on_disconnect(monkeypatch):
    from src.core.serial_handler import ConnectionState

    hub = _hub_with_fw("meshtastic", monkeypatch)
    conn = _FakeConn()
    hub._attach_ingestor("COM-TEST", conn)
    backend = hub.mesh_backend("COM-TEST")
    assert backend is not None and conn.raw is True

    conn.fire_state(ConnectionState.DISCONNECTED)  # the connection drops
    assert hub.mesh_backend("COM-TEST") is None  # backend removed from tracking
    assert conn.raw is False  # line mode restored for the next firmware
    assert getattr(conn, "mesh_backend", None) is None
    assert conn._byte_cbs == []  # byte callback unhooked (no leak / double-feed on reconnect)
