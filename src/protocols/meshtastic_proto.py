"""Dependency-free minimal protobuf codec for the Meshtastic StreamAPI (comms rework, Wave 8).

Meshtastic's serial link carries length-delimited **protobuf** ToRadio/FromRadio messages (framed by
:class:`~src.protocols.stream_framer.StreamFramer`). To read node/channel/text state and send a text message,
CC has to decode/encode those protobufs.

**Why hand-rolled, not the `protobuf` runtime + vendored `*_pb2.py`.** The vendored-pb2 path adds the
`protobuf` dependency (plus its `upb` C extension) and is, per the Wave-8 research, the *main* PyInstaller
frozen-build pitfall — the global descriptor pool throws "duplicate file name" when a `*_pb2` module is
importable under two paths, and the upb backend is stricter still. CC's transport layer is already
hand-rolled + fully tested (``StreamFramer``); this is its natural companion. Wave 8 needs only ~6 message
types decoded (FromRadio, MeshPacket, Data, NodeInfo, User, Channel, MyNodeInfo) and 2 encoded (ToRadio
variants). Protobuf's wire format is small and stable, and **field numbers are fixed by contract** (renumbering
would break every existing Meshtastic client), so a minimal codec is safe, dependency-free, and — validated
here against a real Heltec node's captured stream — honest. Every unrecognised field is skipped generically.

Field numbers pinned from upstream ``meshtastic/protobufs`` (mesh.proto, portnums.proto, channel.proto).
This module is PURE: no serial, no Qt, no I/O — trivially testable and validated against golden real-radio bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ── Wire types (protobuf) ────────────────────────────────────────────────────
WT_VARINT = 0
WT_I64 = 1
WT_LEN = 2
WT_I32 = 5

# Meshtastic constants (upstream: meshtastic/__init__.py, portnums.proto)
BROADCAST_NUM = 0xFFFFFFFF
TEXT_MESSAGE_APP = 1  # portnums.proto PortNum.TEXT_MESSAGE_APP
NODELESS_WANT_CONFIG_ID = 69420  # sending this id tells the node to skip other nodes' NodeInfos

# A few common hardware models (mesh.proto HardwareModel) — surfaced for display only; unknown -> "hw#N".
_HW_MODELS = {
    43: "HELTEC_V3",
    110: "HELTEC_V4",
    9: "HELTEC_V2_1",
    4: "HELTEC_V1",
    31: "HELTEC_WSL_V3",
    39: "HELTEC_WIRELESS_TRACKER",
    71: "RAK4631",
    77: "T_DECK",
}


def hw_model_name(n: int | None) -> str:
    if n is None:
        return ""
    return _HW_MODELS.get(n, f"hw#{n}")


# ── Low-level wire reader ────────────────────────────────────────────────────


class _Reader:
    """Cursor over a protobuf byte buffer. Tolerant: raises ``EOFError`` on truncation so callers can stop
    cleanly rather than crash on a partial/garbage frame."""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.end = len(data)

    def eof(self) -> bool:
        return self.pos >= self.end

    def read_varint(self) -> int:
        result = 0
        shift = 0
        while True:
            if self.pos >= self.end:
                raise EOFError("truncated varint")
            b = self.data[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7
            if shift > 63:  # guard against a malformed 10+ byte varint
                raise EOFError("varint too long")

    def read_bytes(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.end:
            raise EOFError("truncated length-delimited field")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def read_i32(self) -> bytes:
        return self.read_bytes(4)

    def read_i64(self) -> bytes:
        return self.read_bytes(8)


def iter_fields(data: bytes):
    """Yield ``(field_number, wire_type, value)`` for each field in *data*.

    ``value`` is an ``int`` for varint fields, and raw ``bytes`` for I32/I64/length-delimited fields
    (the caller interprets those by the field's declared type). Stops cleanly at the first truncation
    (a partial trailing field is ignored rather than raising) so a slightly-short frame still yields its
    good fields.
    """
    r = _Reader(data)
    while not r.eof():
        try:
            key = r.read_varint()
            field_no = key >> 3
            wt = key & 0x07
            if wt == WT_VARINT:
                yield field_no, wt, r.read_varint()
            elif wt == WT_LEN:
                length = r.read_varint()
                yield field_no, wt, r.read_bytes(length)
            elif wt == WT_I32:
                yield field_no, wt, r.read_i32()
            elif wt == WT_I64:
                yield field_no, wt, r.read_i64()
            else:  # unknown/deprecated group wire types (3,4) — cannot skip safely, stop.
                return
        except EOFError:
            return


def parse(data: bytes) -> dict[int, list]:
    """Generic parse: ``field_number -> [values]`` (a list, since protobuf allows repeated fields).
    varint -> int; I32/I64/LEN -> bytes. Order within a field number is preserved."""
    out: dict[int, list] = {}
    for field_no, _, value in iter_fields(data):
        out.setdefault(field_no, []).append(value)
    return out


def _first(fields: dict[int, list], field_no: int, default=None):
    vals = fields.get(field_no)
    return vals[0] if vals else default


def as_str(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


def as_float(b) -> float | None:
    """Interpret an I32 field's 4 raw bytes as a little-endian IEEE-754 float (protobuf float encoding)."""
    if isinstance(b, (bytes, bytearray)) and len(b) == 4:
        return struct.unpack("<f", bytes(b))[0]
    if isinstance(b, int):  # a varint stored where a float was expected — surface as-is
        return float(b)
    return None


def as_u32(b) -> int | None:
    """Interpret a value as an unsigned int (varint int passthrough, or a 4-byte fixed32 -> LE uint)."""
    if isinstance(b, int):
        return b
    if isinstance(b, (bytes, bytearray)) and len(b) == 4:
        return struct.unpack("<I", bytes(b))[0]
    return None


def as_i32(b) -> int | None:
    """Interpret a varint as a signed int32. A negative protobuf int32 is sign-extended on the wire to a
    full 64-bit varint, so ``read_varint`` returns its large unsigned magnitude; mask to 32 bits and apply
    two's-complement so e.g. an RSSI of -95 dBm decodes as -95, not 18446744073709551521."""
    if isinstance(b, int):
        v = b & 0xFFFFFFFF
        return v - 0x100000000 if v >= 0x80000000 else v
    return None


# ── Low-level wire writer ────────────────────────────────────────────────────


def encode_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1  # two's-complement for negative int32/int64 (protobuf semantics)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_no: int, wt: int) -> bytes:
    return encode_varint((field_no << 3) | wt)


def field_varint(field_no: int, value: int) -> bytes:
    return _tag(field_no, WT_VARINT) + encode_varint(value)


def field_bytes(field_no: int, value: bytes) -> bytes:
    return _tag(field_no, WT_LEN) + encode_varint(len(value)) + bytes(value)


def field_fixed32(field_no: int, value: int) -> bytes:
    return _tag(field_no, WT_I32) + struct.pack("<I", value & 0xFFFFFFFF)


# ── Typed decode outputs ─────────────────────────────────────────────────────


@dataclass
class MeshNode:
    """A node from a NodeInfo (or the local node from MyNodeInfo)."""

    num: int
    node_id: str = ""            # "!043ae298"
    long_name: str = ""
    short_name: str = ""
    hw_model: int | None = None
    snr: float | None = None
    last_heard: int | None = None
    battery: int | None = None
    is_local: bool = False

    @property
    def hw_model_name(self) -> str:
        return hw_model_name(self.hw_model)


@dataclass
class MeshChannel:
    index: int
    name: str = ""
    role: int = 0  # 0 DISABLED, 1 PRIMARY, 2 SECONDARY

    @property
    def role_name(self) -> str:
        return {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(self.role, str(self.role))


@dataclass
class MeshText:
    from_num: int
    to_num: int
    channel: int
    text: str
    rx_snr: float | None = None
    rx_rssi: int | None = None
    packet_id: int | None = None


@dataclass
class FromRadioResult:
    """Tagged result of decoding one FromRadio frame. ``kind`` is one of:
    'my_info', 'node_info', 'channel', 'text', 'config_complete', 'packet', 'other'."""

    kind: str
    node: MeshNode | None = None
    channel: MeshChannel | None = None
    text: MeshText | None = None
    my_node_num: int | None = None
    config_complete_id: int | None = None
    portnum: int | None = None
    raw_fields: dict = field(default_factory=dict)


def node_id_str(num: int) -> str:
    """Meshtastic renders a node number as ``!`` + 8 lowercase hex digits."""
    return "!%08x" % (num & 0xFFFFFFFF)


# ── FromRadio decode ─────────────────────────────────────────────────────────
# FromRadio field numbers (mesh.proto): id=1, packet=2, my_info=3, node_info=4, config=5,
# log_record=6, config_complete_id=7, rebooted=8, moduleConfig=9, channel=10, queueStatus=11,
# xmodemPacket=12, metadata=13.


def decode_fromradio(payload: bytes) -> FromRadioResult:
    f = parse(payload)
    if 2 in f:  # packet (MeshPacket) — the common case once running
        return _decode_packet(_first(f, 2))
    if 4 in f:  # node_info (NodeInfo)
        return FromRadioResult("node_info", node=_decode_nodeinfo(_first(f, 4)))
    if 3 in f:  # my_info (MyNodeInfo)
        raw = _first(f, 3)
        my = parse(raw) if isinstance(raw, bytes) else {}
        num = as_u32(_first(my, 1))
        return FromRadioResult("my_info", my_node_num=num)
    if 10 in f:  # channel (Channel)
        return FromRadioResult("channel", channel=_decode_channel(_first(f, 10)))
    if 7 in f:  # config_complete_id (uint32)
        return FromRadioResult("config_complete", config_complete_id=as_u32(_first(f, 7)))
    return FromRadioResult("other", raw_fields=f)


def _decode_nodeinfo(data) -> MeshNode:
    # NodeInfo: num=1(uint32), user=2(User), position=3, snr=4(float), last_heard=5(uint32),
    # device_metrics=6(DeviceMetrics), channel=7, ...
    if not isinstance(data, bytes):
        return MeshNode(num=0)
    f = parse(data)
    num = as_u32(_first(f, 1)) or 0
    node = MeshNode(num=num, node_id=node_id_str(num))
    user = _first(f, 2)
    if isinstance(user, bytes):
        uf = parse(user)
        # User: id=1(string), long_name=2(string), short_name=3(string), macaddr=4(bytes),
        # hw_model=5(enum), ...
        node.node_id = as_str(_first(uf, 1)) or node.node_id
        node.long_name = as_str(_first(uf, 2))
        node.short_name = as_str(_first(uf, 3))
        node.hw_model = _first(uf, 5)
    node.snr = as_float(_first(f, 4))
    node.last_heard = as_u32(_first(f, 5))
    metrics = _first(f, 6)
    if isinstance(metrics, bytes):
        mf = parse(metrics)
        # DeviceMetrics: battery_level=1(uint32), voltage=2(float), ...
        node.battery = as_u32(_first(mf, 1))
    return node


def _decode_channel(data) -> MeshChannel:
    # Channel: index=1(int32), settings=2(ChannelSettings), role=3(enum)
    if not isinstance(data, bytes):
        return MeshChannel(index=-1)
    f = parse(data)
    index = _first(f, 1) or 0
    role = _first(f, 3) or 0
    name = ""
    settings = _first(f, 2)
    if isinstance(settings, bytes):
        sf = parse(settings)
        # ChannelSettings: psk=2(bytes), name=3(string), id=4(fixed32), ...
        name = as_str(_first(sf, 3))
    return MeshChannel(index=int(index), name=name, role=int(role))


def _decode_packet(data) -> FromRadioResult:
    # MeshPacket: from=1(fixed32), to=2(fixed32), channel=3(uint32), decoded=4(Data), encrypted=5(bytes),
    # id=6(fixed32), rx_time=7(fixed32), rx_snr=8(float), hop_limit=9, want_ack=10, priority=11,
    # rx_rssi=12(int32), ...
    if not isinstance(data, bytes):
        return FromRadioResult("other")
    f = parse(data)
    from_num = as_u32(_first(f, 1)) or 0
    to_num = as_u32(_first(f, 2)) or 0
    channel = _first(f, 3) or 0
    packet_id = as_u32(_first(f, 6))
    rx_snr = as_float(_first(f, 8))
    rx_rssi = as_i32(_first(f, 12))  # int32 dBm — sign-corrected (negative on the wire is 64-bit extended)
    decoded = _first(f, 4)
    if isinstance(decoded, bytes):
        df = parse(decoded)
        # Data: portnum=1(enum), payload=2(bytes), ...
        portnum = _first(df, 1) or 0
        payload = _first(df, 2)
        if portnum == TEXT_MESSAGE_APP and isinstance(payload, bytes):
            return FromRadioResult(
                "text",
                portnum=portnum,
                text=MeshText(
                    from_num=from_num,
                    to_num=to_num,
                    channel=int(channel),
                    text=payload.decode("utf-8", "replace"),
                    rx_snr=rx_snr,
                    rx_rssi=rx_rssi,
                    packet_id=packet_id,
                ),
            )
        return FromRadioResult("packet", portnum=int(portnum), raw_fields=f)
    return FromRadioResult("packet", portnum=None, raw_fields=f)


# ── ToRadio encode (the send path) ───────────────────────────────────────────
# ToRadio field numbers (mesh.proto): packet=1, want_config_id=3, disconnect=4, heartbeat=7.


def encode_want_config(config_id: int) -> bytes:
    """A ToRadio{want_config_id} — the on-connect handshake request."""
    if config_id == NODELESS_WANT_CONFIG_ID:
        config_id += 1
    return field_varint(3, config_id & 0xFFFFFFFF)


def encode_disconnect() -> bytes:
    """A ToRadio{disconnect=true} — clean link close."""
    return field_varint(4, 1)


def encode_heartbeat() -> bytes:
    """A ToRadio{heartbeat={}} — keep-alive (empty Heartbeat sub-message)."""
    return field_bytes(7, b"")


def encode_text_message(text: str, channel: int = 0, dest: int = BROADCAST_NUM) -> bytes:
    """A ToRadio{packet: MeshPacket{to, channel, decoded: Data{portnum=TEXT_MESSAGE_APP, payload}}}.

    ``dest`` defaults to the broadcast address (0xFFFFFFFF). ``channel`` is the channel *index*. The local
    node fills in ``from`` and encrypts on-air, so CC supplies neither a source nor a key (it is inside the
    node's trust boundary over USB/serial)."""
    payload = text.encode("utf-8")
    # Data { portnum=1 (varint), payload=2 (bytes) }
    data = field_varint(1, TEXT_MESSAGE_APP) + field_bytes(2, payload)
    # MeshPacket { to=2 (fixed32), channel=3 (varint), decoded=4 (Data) }
    packet = field_fixed32(2, dest) + field_varint(3, channel) + field_bytes(4, data)
    # ToRadio { packet=1 (MeshPacket) }
    return field_bytes(1, packet)
