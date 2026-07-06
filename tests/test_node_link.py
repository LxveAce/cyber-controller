"""Tests for NodeLink (W1.0) — the wireless node presented as a SerialConnection stand-in.

Covers: the seal->gateway->unseal round-trip over a mock gateway, that frames on the wire are
ciphertext (not plaintext), the outbound control-byte guard, drop-never-surface for forged/replayed/
garbage inbound, directional-key separation (a host can't decrypt its own frame -> no cross-direction
nonce reuse), the MTU guard, DeviceManager.attach_connection, and a full ride through the real
TargetIngestor + a real parser (proving the downstream stack treats a node like a serial device).
All keys are OBVIOUSLY FAKE test constants — no real secret material.
"""
from __future__ import annotations

import base64

import pytest

from src.core.device_manager import DeviceManager
from src.core.node_link import NodeLink
from src.core.serial_handler import ConnectionState
from src.models.device import BoardType, Device

KEY = bytes(32)          # obviously-fake shared per-node key
KEY2 = bytes([0x22]) * 32


class MockGateway:
    """A minimal SerialConnection-shaped transport. `write` records outbound base64 lines; `deliver`
    injects an inbound line to on_line subscribers. Two of these + `pump` model the host<->node wire."""

    def __init__(self, port: str = "gw") -> None:
        self.port = port
        self._state = ConnectionState.DISCONNECTED
        self._line_cbs: list = []
        self._state_cbs: list = []
        self.sent: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_line(self, cb):
        self._line_cbs.append(cb)

    def remove_line_callback(self, cb):
        try:
            self._line_cbs.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb):
        self._state_cbs.append(cb)

    def remove_state_callback(self, cb):
        try:
            self._state_cbs.remove(cb)
        except ValueError:
            pass

    def connect(self):
        self._state = ConnectionState.CONNECTED
        for cb in list(self._state_cbs):
            cb(self._state)

    def disconnect(self):
        self._state = ConnectionState.DISCONNECTED
        for cb in list(self._state_cbs):
            cb(self._state)

    def write(self, data: str):
        self.sent.append(data)

    def write_bytes(self, payload: bytes):
        self.sent.append(payload)

    def deliver(self, line: str):
        for cb in list(self._line_cbs):
            cb(line)


def pump(src: MockGateway, dst: MockGateway) -> None:
    """Carry everything src wrote onto dst's inbound (the wire), then clear src's outbox."""
    for line in src.sent:
        dst.deliver(line)
    src.sent.clear()


def _pair(key=KEY, node_id=1):
    """A connected host+node NodeLink pair over two mock gateways sharing one key."""
    host_gw, node_gw = MockGateway("host-gw"), MockGateway("node-gw")
    host = NodeLink(host_gw, key, node_id, role="host")
    node = NodeLink(node_gw, key, node_id, role="node")
    host.connect()
    node.connect()
    return host, host_gw, node, node_gw


# ── Duck-typing / lifecycle ──────────────────────────────────────────
def test_nodelink_quacks_like_serialconnection():
    gw = MockGateway()
    link = NodeLink(gw, KEY, 5)
    for attr in ("port", "is_connected", "state", "connect", "disconnect", "write",
                 "write_bytes", "on_line", "remove_line_callback", "on_state_change", "on_error"):
        assert hasattr(link, attr)
    assert link.port == "node:5"


def test_connect_disconnect_and_state_mirror_gateway():
    gw = MockGateway()
    link = NodeLink(gw, KEY, 1)
    seen: list = []
    link.on_state_change(seen.append)
    assert link.is_connected is False
    link.connect()
    assert link.is_connected is True and link.state == ConnectionState.CONNECTED
    assert ConnectionState.CONNECTED in seen
    # A NodeLink BORROWS its gateway: disconnect() detaches THIS link (stops decoding under its key) but
    # must NOT tear down the shared physical port — one dongle may gateway several nodes + the Devices
    # tab, so force-closing it here would silently kill every other consumer. The owner (DeviceManager
    # refcount) closes the gateway.
    link.disconnect()
    assert gw.is_connected is True, "detaching a node must not close the shared gateway"
    assert link.is_connected is True  # still mirrors the (still-open) gateway


def test_close_spares_shared_gateway_and_removes_both_callbacks():
    # Two nodes share ONE gateway (one dongle). Closing node A must fully unhook A (both the line hook —
    # so it stops decoding under a now-stale key — AND the state hook, which used to leak) while leaving
    # the gateway connected and node B fully wired.
    gw = MockGateway()
    a = NodeLink(gw, KEY, 1)
    b = NodeLink(gw, KEY2, 2)
    gw.connect()
    assert a._on_gateway_line in gw._line_cbs and a._emit_state in gw._state_cbs
    assert b._on_gateway_line in gw._line_cbs and b._emit_state in gw._state_cbs

    a.close()

    assert a._on_gateway_line not in gw._line_cbs, "close() must remove the line hook"
    assert a._emit_state not in gw._state_cbs, "close() must remove the state hook (no leak)"
    assert gw.is_connected is True, "closing one node must not tear down the shared gateway"
    assert b._on_gateway_line in gw._line_cbs and b._emit_state in gw._state_cbs, "node B stays wired"


# ── Round-trip over the mock gateway ─────────────────────────────────
def test_round_trip_host_to_node():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.write("scanap")
    pump(host_gw, node_gw)
    assert got == ["scanap"]


def test_round_trip_node_to_host_multiline():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    host.on_line(got.append)
    node.write_bytes(b"line one\nline two\n")   # node relays two firmware lines in one frame
    pump(node_gw, host_gw)
    assert got == ["line one", "line two"]


def test_frames_on_the_wire_are_ciphertext_not_plaintext():
    host, host_gw, _node, _gw = _pair()
    host.write("supersecretcommand")
    assert len(host_gw.sent) == 1
    raw = base64.b64decode(host_gw.sent[0])
    assert b"supersecretcommand" not in raw   # sealed, not sent in the clear


# ── Outbound control-byte guard (command-injection) ──────────────────
@pytest.mark.parametrize("payload", ["scan\nDEAUTH all", "a\x00b", "cmd\x07", "x\x7f"])
def test_write_rejects_embedded_control_chars(payload):
    host, _hg, _n, _ng = _pair()
    with pytest.raises(ValueError):
        host.write(payload)


def test_trailing_newline_is_allowed_and_stripped():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.write("scanap\n")   # a normal trailing terminator is fine
    pump(host_gw, node_gw)
    assert got == ["scanap"]


# ── Drop-never-surface: forged / replayed / garbage inbound ──────────
def test_forged_frame_is_dropped_not_surfaced():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    # Seal a frame under the WRONG key, deliver to the node: must be dropped, never surfaced.
    evil = NodeLink(MockGateway(), KEY2, 1, role="host")
    evil.write("rm -rf")
    forged_line = evil._gateway.sent[0]  # its base64 frame
    node_gw.deliver(forged_line)
    assert got == []


def test_tampered_frame_is_dropped():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.write("scanap")
    frame = bytearray(base64.b64decode(host_gw.sent[0]))
    frame[-1] ^= 0x01                                   # flip a ciphertext/tag byte
    node_gw.deliver(base64.b64encode(bytes(frame)).decode())
    assert got == []


def test_replayed_frame_is_dropped():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.write("scanap")
    line = host_gw.sent[0]
    node_gw.deliver(line)   # first sight -> surfaced
    node_gw.deliver(line)   # replay -> dropped
    assert got == ["scanap"]


@pytest.mark.parametrize("junk", ["", "   ", "!!!not base64!!!", "@@@@", "zzzz"])
def test_garbage_inbound_is_dropped_without_crash(junk):
    _h, _hg, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    node_gw.deliver(junk)   # must not raise, must not surface
    assert got == []


# ── Directional keys: no cross-direction nonce reuse ─────────────────
def test_host_cannot_decrypt_its_own_frame():
    """Host seals host->node and opens node->host (different HKDF sub-keys). Feeding a host frame back
    to the host must fail auth and be dropped — proving the two directions never share a (key,nonce)."""
    host, host_gw, _n, _ng = _pair()
    got: list = []
    host.on_line(got.append)
    host.write("scanap")
    pump(host_gw, host_gw)  # loop the host's own frame back to itself
    assert got == []        # can't open it -> dropped


# ── MTU guard ────────────────────────────────────────────────────────
def test_oversize_payload_is_rejected_not_truncated():
    host, _hg, _n, _ng = _pair()
    with pytest.raises(ValueError):
        host.write("x" * 500)   # > max_plaintext(ESP_NOW_MTU) == 219


# ── Key / role validation ────────────────────────────────────────────
@pytest.mark.parametrize("badkey", [b"", bytes(16), bytes(31), bytes(33)])
def test_bad_key_rejected(badkey):
    with pytest.raises(ValueError):
        NodeLink(MockGateway(), badkey, 1)


def test_bad_role_rejected():
    with pytest.raises(ValueError):
        NodeLink(MockGateway(), KEY, 1, role="sideways")


# ── DeviceManager.attach_connection seam ─────────────────────────────
def test_device_manager_attach_connection():
    dm = DeviceManager()
    gw = MockGateway()
    link = NodeLink(gw, KEY, 7)
    dev = Device(port=link.port, name="Test Node", firmware="marauder", board_type=BoardType.ESP32_S3)

    ret = dm.attach_connection(dev, link)
    assert ret is link
    assert dm.get_connection(link.port) is link
    assert dev in dm.list_devices()

    # State reconciles onto the Device just like a wired connection.
    link.connect()
    assert dm.get_device(link.port).connected is True
    # The gateway's owner closing it flips the node Device too — state mirrors through the attached link.
    # (link.disconnect() is a DETACH now, not a gateway close, so we drive the gateway directly here.)
    gw.disconnect()
    assert dm.get_device(link.port).connected is False


# ── Full stack: a node feeds the real TargetIngestor + parser ────────
def test_nodelink_feeds_target_ingestor_like_a_serial_device():
    from src.core.target_ingest import TargetIngestor
    from src.models.target import TargetType
    from src.protocols.ghost_esp import GhostESPProtocol

    class FakePool:
        def __init__(self):
            self.added = []

        def add(self, t):
            self.added.append(t)

    host, host_gw, node, node_gw = _pair()
    pool = FakePool()
    TargetIngestor(pool).attach(host, GhostESPProtocol())   # attach to the wireless link, unchanged API

    # The node relays a GhostESP AP-scan line; it must arrive as a pooled AP target on the host.
    node.write("SSID: HomeWiFi | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -50")
    pump(node_gw, host_gw)

    assert len(pool.added) == 1
    t = pool.added[0]
    assert t.target_type is TargetType.AP
    assert t.mac == "AA:BB:CC:DD:EE:FF" and t.ssid == "HomeWiFi"
    assert t.device_source == host.port   # attributed to the wireless node's port


# ── Restart safety / nonce reuse across instances (DEBUG finding 1/2) ─
def test_two_default_instances_use_different_tx_epochs():
    """Two fresh NodeLinks on the same key must NOT both start at (epoch 0, counter 0) — that would
    reuse (key, nonce). The random-epoch default gives them disjoint nonce spaces."""
    a = NodeLink(MockGateway(), KEY, 1, role="host")
    b = NodeLink(MockGateway(), KEY, 1, role="host")
    assert a.tx_epoch != b.tx_epoch


def test_persist_and_restore_sender_state_never_reuses_a_nonce():
    import struct

    from src.core.node_crypto import HEADER_LEN

    a = NodeLink(MockGateway(), KEY, 1, role="host", epoch=1000, counter=0)
    a.write("one")
    a.write("two")
    saved_epoch, saved_counter = a.tx_epoch, a.tx_counter   # next-to-use
    assert (saved_epoch, saved_counter) == (1000, 2)
    # Resume from persisted state in a new instance.
    b = NodeLink(MockGateway(), KEY, 1, role="host", epoch=saved_epoch, counter=saved_counter)
    b.write("three")
    _, _, e, c = struct.unpack(">BHIQ", base64.b64decode(b._gateway.sent[0])[:HEADER_LEN])
    assert (e, c) == (1000, 2)   # continues a's line — never reuses (1000, 0) or (1000, 1)


def test_restored_replay_window_rejects_captured_old_frame():
    """Restoring rx_epoch/rx_highest blocks a captured, still-authentic frame from replaying across a
    receiver restart (conservative window = everything <= persisted highest is treated as seen)."""
    host, host_gw, node, node_gw = _pair()
    host.write("scanap")
    old_line = host_gw.sent[0]
    node_gw.deliver(old_line)                       # node sees it once
    ep, hi = node.rx_epoch, node.rx_highest
    # Node "restarts": new NodeLink restored from persisted rx state.
    node2_gw = MockGateway()
    node2 = NodeLink(node2_gw, KEY, 1, role="node", rx_epoch=ep, rx_highest=hi)
    got: list = []
    node2.on_line(got.append)
    node2_gw.deliver(old_line)                       # replay the captured frame after restart
    assert got == []                                 # rejected -> not surfaced


# ── send_interrupt + write_bytes coverage ────────────────────────────
def test_send_interrupt_round_trips_sealed():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.send_interrupt()   # a single 0x03
    # The wire line is base64 text (printable) — the raw 0x03 control byte never hits the wire in the clear.
    assert all(0x20 <= ord(ch) <= 0x7E for ch in host_gw.sent[0])
    pump(host_gw, node_gw)
    assert got == ["\x03"]   # ...and the node recovers it after unseal


def test_write_bytes_is_sealed_and_mtu_guarded():
    host, host_gw, node, node_gw = _pair()
    got: list = []
    node.on_line(got.append)
    host.write_bytes(b"raw payload")
    assert b"raw payload" not in base64.b64decode(host_gw.sent[0])   # sealed
    pump(host_gw, node_gw)
    assert got == ["raw payload"]
    with pytest.raises(ValueError):
        host.write_bytes(b"x" * 500)   # > max_plaintext(ESP_NOW_MTU)


def test_different_node_ids_do_not_cross_decrypt():
    """node_id folded into HKDF: a frame sealed for node 1 can't be opened by a node-2 link (same key)."""
    gw1, gw2 = MockGateway(), MockGateway()
    host1 = NodeLink(gw1, KEY, 1, role="host")
    node2 = NodeLink(gw2, KEY, 2, role="node")
    got: list = []
    node2.on_line(got.append)
    host1.write("hello")
    gw2.deliver(gw1.sent[0])   # feed node-1's frame to node-2's receiver
    assert got == []           # distinct derived keys -> auth fail -> dropped


def test_close_detaches_from_gateway():
    gw = MockGateway()
    link = NodeLink(gw, KEY, 1)
    link.connect()
    link.close()
    # After close, inbound lines are no longer decoded/surfaced by this link.
    got: list = []
    link.on_line(got.append)
    other = NodeLink(MockGateway(), KEY, 1, role="node")
    other.write("x")
    gw.deliver(other._gateway.sent[0])
    assert got == []
