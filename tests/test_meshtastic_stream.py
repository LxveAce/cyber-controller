"""Tests for the Meshtastic StreamAPI backend (src/protocols/meshtastic_stream.py).

Frames are built from the codec's own documented field encoders (no real-radio node data committed), fed
through the real StreamFramer, and the backend's decoded state + emitted events are asserted. This exercises
the same path a live raw-mode SerialConnection drives; real hardware is the arbiter (Wave-8 build log).
"""

from __future__ import annotations

import struct

from src.protocols import meshtastic_proto as mp
from src.protocols.meshtastic_stream import MeshtasticBackend
from src.protocols.stream_framer import StreamFramer

# ── frame builders (fake data) ────────────────────────────────────────────────


def _framed_my_info(num: int) -> bytes:
    return StreamFramer.frame(mp.field_bytes(3, mp.field_varint(1, num)))


def _framed_node_info(num: int, long_name: str, short_name: str, hw: int, snr: float) -> bytes:
    user = (
        mp.field_bytes(1, ("!%08x" % num).encode())
        + mp.field_bytes(2, long_name.encode())
        + mp.field_bytes(3, short_name.encode())
        + mp.field_varint(5, hw)
    )
    node_info = mp.field_varint(1, num) + mp.field_bytes(2, user) + mp.field_bytes(4, struct.pack("<f", snr))
    return StreamFramer.frame(mp.field_bytes(4, node_info))


def _framed_channel(index: int, name: str, role: int) -> bytes:
    settings = mp.field_bytes(3, name.encode())
    channel = mp.field_varint(1, index) + mp.field_bytes(2, settings) + mp.field_varint(3, role)
    return StreamFramer.frame(mp.field_bytes(10, channel))


def _framed_text(from_num: int, text: str, channel: int = 0) -> bytes:
    data = mp.field_varint(1, mp.TEXT_MESSAGE_APP) + mp.field_bytes(2, text.encode())
    packet = (
        mp.field_fixed32(1, from_num)
        + mp.field_fixed32(2, mp.BROADCAST_NUM)
        + mp.field_varint(3, channel)
        + mp.field_bytes(4, data)
    )
    return StreamFramer.frame(mp.field_bytes(2, packet))


def _framed_config_complete(cid: int) -> bytes:
    return StreamFramer.frame(mp.field_varint(7, cid))


def _make_backend(**kw):
    frames: list[bytes] = []
    events: list[tuple[str, dict]] = []
    texts: list[str] = []
    backend = MeshtasticBackend(
        frames.append,
        on_event=lambda t, d: events.append((t, d)),
        on_text=texts.append,
        **kw,
    )
    return backend, frames, events, texts


# ── handshake / TX ────────────────────────────────────────────────────────────


def test_start_sends_want_config():
    backend, frames, _, _ = _make_backend(config_id=0x12345678)
    backend.start()
    assert len(frames) == 1
    payload = StreamFramer().feed(frames[0])[0]
    assert mp.parse(payload)[3][0] == 0x12345678  # ToRadio.want_config_id


def test_nodeless_config_id_is_bumped():
    backend, _, _, _ = _make_backend(config_id=mp.NODELESS_WANT_CONFIG_ID)
    assert backend.config_id == mp.NODELESS_WANT_CONFIG_ID + 1


def test_send_text_frames_and_writes():
    backend, frames, _, _ = _make_backend()
    backend.send_text("hello mesh", channel=1)
    payload = StreamFramer().feed(frames[0])[0]
    packet = mp.parse(mp.parse(payload)[1][0])  # ToRadio.packet -> MeshPacket
    assert packet[3][0] == 1  # channel
    data = mp.parse(packet[4][0])
    assert data[1][0] == mp.TEXT_MESSAGE_APP
    assert data[2][0] == b"hello mesh"


def test_send_heartbeat():
    backend, frames, _, _ = _make_backend()
    backend.send_heartbeat()
    payload = StreamFramer().feed(frames[0])[0]
    assert 7 in mp.parse(payload)  # ToRadio.heartbeat


# ── RX decode + events + state ────────────────────────────────────────────────


def test_full_config_stream_populates_state_and_events():
    backend, _, events, _ = _make_backend()
    stream = (
        _framed_my_info(0x043AE298)
        + _framed_node_info(0x043AE298, "Local", "LCL", 43, 0.0)
        + _framed_node_info(0x1BA746AC, "V4 Neighbor", "46ac", 110, 6.75)
        + _framed_channel(0, "LongFast", 1)
        + _framed_channel(1, "", 0)
        + _framed_config_complete(0x12345678)
    )
    backend.feed_bytes(stream)

    assert backend.my_node_num == 0x043AE298
    assert set(backend.nodes) == {0x043AE298, 0x1BA746AC}
    assert backend.nodes[0x043AE298].is_local is True
    assert backend.nodes[0x1BA746AC].is_local is False
    assert backend.nodes[0x1BA746AC].hw_model_name == "HELTEC_V4"
    assert abs(backend.nodes[0x1BA746AC].snr - 6.75) < 1e-6
    assert backend.config_complete is True

    kinds = [t for t, _ in events]
    assert kinds.count("mesh_node") == 2
    assert "mesh_my_info" in kinds
    assert "mesh_channel" in kinds
    assert "mesh_config_complete" in kinds
    cc = next(d for t, d in events if t == "mesh_config_complete")
    assert cc["node_count"] == 2


def test_incoming_text_message_event():
    backend, _, events, _ = _make_backend()
    backend.feed_bytes(_framed_text(0x1BA746AC, "hi from the V4", channel=0))
    text_events = [d for t, d in events if t == "mesh_text"]
    assert len(text_events) == 1
    assert text_events[0]["text"] == "hi from the V4"
    assert text_events[0]["from_id"] == "!1ba746ac"


def test_primary_and_active_channels():
    backend, _, _, _ = _make_backend()
    backend.feed_bytes(
        _framed_channel(0, "LongFast", 1) + _framed_channel(1, "Secondary", 2) + _framed_channel(2, "", 0)
    )
    assert backend.primary_channel().name == "LongFast"
    assert [c.index for c in backend.active_channels()] == [0, 1]


def test_frame_split_across_feeds_reassembles():
    backend, _, events, _ = _make_backend()
    whole = _framed_node_info(0xABCDEF01, "Split", "SP", 43, 1.0)
    mid = len(whole) // 2
    backend.feed_bytes(whole[:mid])
    assert not backend.nodes  # not complete yet
    backend.feed_bytes(whole[mid:])
    assert 0xABCDEF01 in backend.nodes


def test_garbage_before_frame_does_not_crash():
    backend, _, events, _ = _make_backend()
    backend.feed_bytes(b"random boot noise\x00\x01" + _framed_my_info(0x1))
    assert backend.my_node_num == 0x1


def test_debug_text_lines_surface():
    backend, _, _, texts = _make_backend()
    # Non-frame ASCII (radio debug log) interleaved with a real frame; complete lines surface.
    backend.feed_bytes(b"INFO | booted\n")
    backend.feed_bytes(b"DEBUG | partial ")
    backend.feed_bytes(b"line done\n" + _framed_my_info(0x2))
    assert "INFO | booted" in texts
    assert "DEBUG | partial line done" in texts
    assert backend.my_node_num == 0x2


def test_on_event_error_does_not_break_decoding():
    frames: list[bytes] = []

    def bad_sink(_t, _d):
        raise RuntimeError("sink boom")

    backend = MeshtasticBackend(frames.append, on_event=bad_sink)
    # Must not raise despite the sink throwing on every event.
    backend.feed_bytes(_framed_my_info(0x3) + _framed_config_complete(1))
    assert backend.my_node_num == 0x3
    assert backend.config_complete is True


def test_close_sends_disconnect():
    backend, frames, _, _ = _make_backend()
    backend.close()
    payload = StreamFramer().feed(frames[0])[0]
    assert mp.parse(payload)[4][0] == 1  # ToRadio.disconnect = true


def test_concurrent_feed_and_read_no_crash():
    # The reader thread mutates nodes/channels while the GUI thread reads node_list()/active_channels().
    # Without the lock this can raise "dictionary changed size during iteration"; with it, never.
    import threading

    backend, _, _, _ = _make_backend()
    stop = threading.Event()
    errors: list[Exception] = []

    def reader():
        while not stop.is_set():
            try:
                backend.active_channels()
                backend.primary_channel()
                backend.node_list()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for i in range(300):
            backend.feed_bytes(
                _framed_channel(i % 40, f"ch{i}", 1)
                + _framed_node_info(0x1000 + (i % 60), "n", "n", 43, 1.0)
            )
    finally:
        stop.set()
        t.join(timeout=3)
    assert not errors, errors[:3]
