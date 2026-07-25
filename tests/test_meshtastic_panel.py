"""Offscreen tests for the Meshtastic UI panel (src/ui/qt/meshtastic_panel.py).

Drives the REAL MeshtasticBackend + EventBus through the panel: feed framed FromRadio bytes -> backend decodes
-> on_event -> bus -> panel refreshes. Same-thread publish is a direct signal connection, so widget updates are
synchronous and assertable. No real-radio data (frames are built from the codec's field encoders).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus  # noqa: E402
from src.protocols import meshtastic_proto as mp  # noqa: E402
from src.protocols.meshtastic_stream import MeshtasticBackend  # noqa: E402
from src.protocols.stream_framer import StreamFramer  # noqa: E402
from src.ui.qt.meshtastic_panel import MeshtasticPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Conn:
    def __init__(self, backend):
        self.mesh_backend = backend


class _Dm:
    def __init__(self, conns):
        self._c = conns

    def get_connection(self, port):
        return self._c.get(port)


def _framed_my_info(num):
    return StreamFramer.frame(mp.field_bytes(3, mp.field_varint(1, num)))


def _framed_node_info(num, long_name, short_name, hw, snr):
    user = (
        mp.field_bytes(1, ("!%08x" % num).encode())
        + mp.field_bytes(2, long_name.encode())
        + mp.field_bytes(3, short_name.encode())
        + mp.field_varint(5, hw)
    )
    ni = mp.field_varint(1, num) + mp.field_bytes(2, user) + mp.field_bytes(4, struct.pack("<f", snr))
    return StreamFramer.frame(mp.field_bytes(4, ni))


def _framed_channel(idx, name, role):
    ch = mp.field_varint(1, idx) + mp.field_bytes(2, mp.field_bytes(3, name.encode())) + mp.field_varint(3, role)
    return StreamFramer.frame(mp.field_bytes(10, ch))


def _framed_text(frm, text):
    data = mp.field_varint(1, mp.TEXT_MESSAGE_APP) + mp.field_bytes(2, text.encode())
    pkt = mp.field_fixed32(1, frm) + mp.field_bytes(4, data)
    return StreamFramer.frame(mp.field_bytes(2, pkt))


def _make(port="COM1"):
    sent: list[bytes] = []
    bus = EventBus()
    backend = MeshtasticBackend(
        sent.append,
        on_event=lambda t, d, _p=port: bus.publish("mesh." + t[len("mesh_"):], {"port": _p, **d}),
    )
    dm = _Dm({port: _Conn(backend)})
    panel = MeshtasticPanel(dm, bus)
    panel.set_port(port)
    return panel, backend, bus, sent


def test_empty_state_when_no_backend(qapp):
    panel = MeshtasticPanel(_Dm({}), EventBus())
    panel.set_port("COM1")
    assert not panel._send_btn.isEnabled()
    assert "Connect a Meshtastic" in panel._status.text()


def test_nodes_populate_from_stream(qapp):
    panel, backend, _, _ = _make()
    backend.feed_bytes(
        _framed_my_info(0x043AE298)
        + _framed_node_info(0x043AE298, "Local", "LCL", 43, 0.0)
        + _framed_node_info(0x1BA746AC, "V4 Neighbor", "46ac", 110, 6.75)
    )
    assert panel._nodes.rowCount() == 2
    assert panel._nodes.item(0, 0).text() == "!043ae298"  # local node first
    assert "(this node)" in panel._nodes.item(0, 1).text()
    hw_col = {panel._nodes.item(r, 2).text() for r in range(2)}
    assert "HELTEC_V4" in hw_col
    assert panel._send_btn.isEnabled()


def test_node_role_and_location_columns(qapp):
    # The node table surfaces the decoded role + GPS location; a node with no fix shows "—".
    panel, backend, _, _ = _make()
    user = (
        mp.field_bytes(1, b"!1ba746ac") + mp.field_bytes(2, b"Router") + mp.field_bytes(3, b"RTR")
        + mp.field_varint(5, 110) + mp.field_varint(7, 2)          # hw=110, role=2 (ROUTER)
    )
    pos = mp.field_fixed32(1, -338568000) + mp.field_fixed32(2, 1512153000) + mp.field_varint(3, 25)
    ni = mp.field_varint(1, 0x1BA746AC) + mp.field_bytes(2, user) + mp.field_bytes(3, pos)
    backend.feed_bytes(
        _framed_my_info(0x043AE298)
        + _framed_node_info(0x043AE298, "Local", "LCL", 43, 0.0)   # local node: no fix -> "—"
        + StreamFramer.frame(mp.field_bytes(4, ni))
    )
    hdr = [panel._nodes.horizontalHeaderItem(c).text() for c in range(panel._nodes.columnCount())]
    role_col, loc_col = hdr.index("Role"), hdr.index("Location")

    def _row(node_id):
        return next(r for r in range(panel._nodes.rowCount())
                    if panel._nodes.item(r, 0).text() == node_id)

    rtr = _row("!1ba746ac")
    assert panel._nodes.item(rtr, role_col).text() == "ROUTER"
    assert panel._nodes.item(rtr, loc_col).text() == "-33.85680, 151.21530"
    # the local node reported no GPS fix -> honest dash, never a fabricated 0,0
    assert panel._nodes.item(_row("!043ae298"), loc_col).text() == "—"


def test_node_hops_column(qapp):
    # The Hops column shows the RF hop distance, "☁" for a node heard over MQTT (internet), or "—".
    panel, backend, _, _ = _make()
    user_h = mp.field_bytes(1, b"!abcd0003") + mp.field_bytes(2, b"Relay") + mp.field_varint(5, 43)
    ni_hop = mp.field_varint(1, 0xABCD0003) + mp.field_bytes(2, user_h) + mp.field_varint(9, 3)
    user_m = mp.field_bytes(1, b"!abcd0002") + mp.field_bytes(2, b"Cloud") + mp.field_varint(5, 43)
    ni_mqtt = mp.field_varint(1, 0xABCD0002) + mp.field_bytes(2, user_m) + mp.field_varint(8, 1)
    backend.feed_bytes(
        _framed_my_info(0x043AE298)
        + _framed_node_info(0x043AE298, "Local", "LCL", 43, 0.0)   # local: no hops -> "—"
        + StreamFramer.frame(mp.field_bytes(4, ni_hop))
        + StreamFramer.frame(mp.field_bytes(4, ni_mqtt))
    )
    hdr = [panel._nodes.horizontalHeaderItem(c).text() for c in range(panel._nodes.columnCount())]
    hcol = hdr.index("Hops")

    def _row(node_id):
        return next(r for r in range(panel._nodes.rowCount())
                    if panel._nodes.item(r, 0).text() == node_id)

    assert panel._nodes.item(_row("!abcd0003"), hcol).text() == "3"       # relayed 3 hops
    assert panel._nodes.item(_row("!abcd0002"), hcol).text() == "☁"       # heard over MQTT
    assert panel._nodes.item(_row("!043ae298"), hcol).text() == "—"       # direct, none reported


def test_channels_populate_active_only(qapp):
    panel, backend, _, _ = _make()
    backend.feed_bytes(_framed_channel(0, "LongFast", 1) + _framed_channel(1, "", 0))
    assert panel._channel.count() == 1  # DISABLED channel is not offered
    assert panel._channel.itemData(0) == 0


def test_incoming_text_is_logged(qapp):
    panel, backend, _, _ = _make()
    backend.feed_bytes(_framed_text(0x1BA746AC, "hi from V4"))
    assert "hi from V4" in panel._log.toPlainText()
    assert "!1ba746ac" in panel._log.toPlainText()


def test_send_writes_frame_and_echoes(qapp):
    panel, backend, _, sent = _make()
    backend.feed_bytes(_framed_my_info(0x1) + _framed_channel(0, "LongFast", 1))
    panel._input.setText("hello mesh")
    panel._send()
    payload = StreamFramer().feed(sent[-1])[0]
    packet = mp.parse(mp.parse(payload)[1][0])
    assert mp.parse(packet[4][0])[2][0] == b"hello mesh"
    assert "hello mesh" in panel._log.toPlainText()
    assert panel._input.text() == ""  # cleared after send


def test_send_no_op_without_backend(qapp):
    panel = MeshtasticPanel(_Dm({}), EventBus())
    panel.set_port("NOPE")
    panel._input.setText("x")
    panel._send()  # must not raise


def test_event_for_other_port_ignored(qapp):
    panel, backend, bus, _ = _make(port="COM1")
    # An event tagged for a different port must not touch this panel's table.
    bus.publish("mesh.node", {"port": "COM9", "num": 0x2, "node_id": "!00000002",
                              "long_name": "Other", "short_name": "O", "hw_model": 43,
                              "hw_model_name": "HELTEC_V3", "snr": None, "battery": None,
                              "last_heard": None, "is_local": False})
    assert panel._nodes.rowCount() == 0
