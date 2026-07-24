"""Tests for the dependency-free Meshtastic protobuf codec (src/protocols/meshtastic_proto.py).

No real-radio node data is committed here (no-PII): decode tests build their frames from the codec's own
documented field encoders (round-trip = internal consistency), and the one hardware-proven vector is the
``want_config`` frame a real Heltec V3 accepted + answered (the id 0x12345678 is arbitrary, not device data).
The wire-compatibility proof against a live node is recorded in the Wave-8 build log, with real hardware as the
arbiter.
"""

from __future__ import annotations

import struct

from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer

# ── varint ────────────────────────────────────────────────────────────────────


def _decode_single_varint(data: bytes) -> int:
    r = mp._Reader(data)
    return r.read_varint()


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 16384, 0x12345678, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        enc = mp.encode_varint(n)
        assert _decode_single_varint(enc) == n, f"varint {n:#x} failed"


def test_varint_negative_twos_complement():
    # protobuf encodes a negative int as its 64-bit two's-complement.
    enc = mp.encode_varint(-1)
    assert _decode_single_varint(enc) == 0xFFFFFFFFFFFFFFFF


# ── want_config (hardware-proven wire vector) ─────────────────────────────────


def test_encode_want_config_wire_vector():
    # A real Heltec V3 accepted this exact frame and streamed its full config back (Wave-8 HIL capture).
    payload = mp.encode_want_config(0x12345678)
    assert payload == bytes.fromhex("18f8acd19101")
    framed = StreamFramer.frame(payload)
    assert framed == bytes.fromhex("94c3000618f8acd19101")


def test_encode_want_config_nodeless_bump():
    # Sending 69420 verbatim tells the node to skip other NodeInfos, so the codec bumps it.
    payload = mp.encode_want_config(mp.NODELESS_WANT_CONFIG_ID)
    fields = mp.parse(payload)
    assert fields[3][0] == mp.NODELESS_WANT_CONFIG_ID + 1


def test_encode_disconnect_and_heartbeat():
    assert mp.parse(mp.encode_disconnect())[4][0] == 1  # disconnect=true (field 4)
    assert mp.parse(mp.encode_heartbeat())[7][0] == b""  # heartbeat={} (field 7, empty sub-message)


# ── text message encode (send path) ──────────────────────────────────────────


def test_encode_text_message_structure():
    to_radio = mp.encode_text_message("hello mesh", channel=2)
    top = mp.parse(to_radio)
    assert set(top) == {1}  # ToRadio.packet only
    packet = mp.parse(top[1][0])
    assert mp.as_u32(packet[2][0]) == mp.BROADCAST_NUM  # to = broadcast (fixed32)
    assert packet[3][0] == 2  # channel index (varint)
    data = mp.parse(packet[4][0])  # decoded Data
    assert data[1][0] == mp.TEXT_MESSAGE_APP  # portnum
    assert data[2][0] == b"hello mesh"  # payload


def test_encode_text_message_explicit_dest():
    to_radio = mp.encode_text_message("dm", channel=0, dest=0x1BA746AC)
    packet = mp.parse(mp.parse(to_radio)[1][0])
    assert mp.as_u32(packet[2][0]) == 0x1BA746AC


# ── FromRadio decode (build fake frames, no real node data) ───────────────────


def _fake_user(node_id: str, long_name: str, short_name: str, hw_model: int) -> bytes:
    return (
        mp.field_bytes(1, node_id.encode())
        + mp.field_bytes(2, long_name.encode())
        + mp.field_bytes(3, short_name.encode())
        + mp.field_varint(5, hw_model)
    )


def _fake_nodeinfo(num: int, user: bytes, snr: float, battery: int) -> bytes:
    return (
        mp.field_varint(1, num)
        + mp.field_bytes(2, user)
        + mp.field_bytes(4, struct.pack("<f", snr))  # snr = float (I32 wire)
        + mp.field_bytes(6, mp.field_varint(1, battery))  # DeviceMetrics{battery_level=1}
    )


def test_decode_node_info():
    user = _fake_user("!deadbeef", "Test Node", "TN", 43)
    node_info = _fake_nodeinfo(0xDEADBEEF, user, 6.75, 88)
    frame = mp.field_bytes(4, node_info)  # FromRadio.node_info = field 4
    res = mp.decode_fromradio(frame)
    assert res.kind == "node_info"
    n = res.node
    assert n.num == 0xDEADBEEF
    assert n.node_id == "!deadbeef"
    assert n.long_name == "Test Node"
    assert n.short_name == "TN"
    assert n.hw_model == 43
    assert n.hw_model_name == "HELTEC_V3"
    assert abs(n.snr - 6.75) < 1e-6
    assert n.battery == 88


def test_decode_node_info_v4_hw_model():
    user = _fake_user("!1ba746ac", "V4 node", "46ac", 110)
    frame = mp.field_bytes(4, _fake_nodeinfo(0x1BA746AC, user, 5.0, 0))
    res = mp.decode_fromradio(frame)
    assert res.node.hw_model == 110
    assert res.node.hw_model_name == "HELTEC_V4"


def test_decode_channel():
    settings = mp.field_bytes(3, b"LongFast")  # ChannelSettings.name = field 3
    channel = mp.field_varint(1, 0) + mp.field_bytes(2, settings) + mp.field_varint(3, 1)  # role PRIMARY
    frame = mp.field_bytes(10, channel)  # FromRadio.channel = field 10
    res = mp.decode_fromradio(frame)
    assert res.kind == "channel"
    assert res.channel.index == 0
    assert res.channel.name == "LongFast"
    assert res.channel.role == 1
    assert res.channel.role_name == "PRIMARY"


def test_decode_my_info():
    my = mp.field_varint(1, 0x043AE298)  # MyNodeInfo.my_node_num = field 1
    frame = mp.field_bytes(3, my)  # FromRadio.my_info = field 3
    res = mp.decode_fromradio(frame)
    assert res.kind == "my_info"
    assert res.my_node_num == 0x043AE298


def test_decode_config_complete():
    frame = mp.field_varint(7, 0x12345678)  # FromRadio.config_complete_id = field 7 (varint)
    res = mp.decode_fromradio(frame)
    assert res.kind == "config_complete"
    assert res.config_complete_id == 0x12345678


def test_decode_text_packet():
    data = mp.field_varint(1, mp.TEXT_MESSAGE_APP) + mp.field_bytes(2, b"hi there")
    packet = (
        mp.field_fixed32(1, 0x1BA746AC)  # from
        + mp.field_fixed32(2, mp.BROADCAST_NUM)  # to
        + mp.field_varint(3, 0)  # channel
        + mp.field_bytes(4, data)  # decoded
        + mp.field_bytes(8, struct.pack("<f", 6.75))  # rx_snr (float)
        + mp.field_varint(12, (-42) & 0xFFFFFFFF)  # rx_rssi int32 as raw varint
    )
    frame = mp.field_bytes(2, packet)  # FromRadio.packet = field 2
    res = mp.decode_fromradio(frame)
    assert res.kind == "text"
    t = res.text
    assert t.text == "hi there"
    assert t.from_num == 0x1BA746AC
    assert t.to_num == mp.BROADCAST_NUM
    assert t.channel == 0
    assert abs(t.rx_snr - 6.75) < 1e-6


def test_decode_text_packet_negative_rssi():
    # A real RSSI is negative dBm; on the wire an int32 -95 is a 64-bit sign-extended varint. It must
    # decode as -95, not the huge unsigned magnitude.
    data = mp.field_varint(1, mp.TEXT_MESSAGE_APP) + mp.field_bytes(2, b"x")
    packet = mp.field_fixed32(1, 0x1) + mp.field_bytes(4, data) + mp.field_varint(12, -95)
    res = mp.decode_fromradio(mp.field_bytes(2, packet))
    assert res.kind == "text"
    assert res.text.rx_rssi == -95


def test_as_i32():
    assert mp.as_i32(0xFFFFFFFFFFFFFFA1) == -95  # 64-bit sign-extended form
    assert mp.as_i32(0xFFFFFFA1) == -95          # already-32-bit form
    assert mp.as_i32(5) == 5
    assert mp.as_i32(0x7FFFFFFF) == 0x7FFFFFFF   # max positive int32
    assert mp.as_i32(None) is None


def test_decode_non_text_packet_is_not_text():
    # A POSITION_APP (portnum 3) packet must NOT be surfaced as text.
    data = mp.field_varint(1, 3) + mp.field_bytes(2, b"\x08\x01")
    packet = mp.field_fixed32(1, 0x1) + mp.field_bytes(4, data)
    res = mp.decode_fromradio(mp.field_bytes(2, packet))
    assert res.kind == "packet"
    assert res.portnum == 3
    assert res.text is None


# ── robustness ────────────────────────────────────────────────────────────────


def test_decode_truncated_frame_is_graceful():
    # A NodeInfo whose sub-message length runs off the end must not raise — the good fields survive,
    # the truncated one is dropped.
    good = mp.field_varint(1, 0xABCD)
    truncated = mp.field_bytes(2, b"") [:-1] + bytes([0x7F])  # claims 127 bytes, has none
    frame = mp.field_bytes(4, good + truncated)
    res = mp.decode_fromradio(frame)  # must not raise
    assert res.kind == "node_info"
    assert res.node.num == 0xABCD


def test_unknown_fields_are_skipped():
    # A NodeInfo with extra unknown fields of every wire type still decodes its known fields.
    extra = (
        mp.field_varint(99, 123)  # unknown varint
        + mp.field_bytes(98, b"junk")  # unknown length-delim
        + mp.field_fixed32(97, 5)  # unknown i32
        + mp._tag(96, mp.WT_I64) + b"\x00" * 8  # unknown i64
    )
    node_info = mp.field_varint(1, 0x1234) + extra + mp.field_bytes(2, _fake_user("!x", "L", "S", 43))
    res = mp.decode_fromradio(mp.field_bytes(4, node_info))
    assert res.node.num == 0x1234
    assert res.node.long_name == "L"


def test_empty_payload_is_other():
    assert mp.decode_fromradio(b"").kind == "other"


def test_node_id_str():
    assert mp.node_id_str(0x043AE298) == "!043ae298"
    assert mp.node_id_str(0x00000001) == "!00000001"


def test_as_float_and_as_u32():
    assert abs(mp.as_float(struct.pack("<f", 3.5)) - 3.5) < 1e-6
    assert mp.as_u32(struct.pack("<I", 0xCAFEBABE)) == 0xCAFEBABE
    assert mp.as_u32(42) == 42
    assert mp.as_float(None) is None
