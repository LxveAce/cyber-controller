"""Meshtastic Stream-API framer (comms rework, S3-b). Byte format verified against the Meshtastic client-API
docs: 0x94 0xC3 <len MSB> <len LSB> <payload>, big-endian length, max 512. Framing only — no protobuf field
decode (that needs the meshtastic lib + a radio). Golden-fixture-gated so a format regression is caught here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.drivers import StreamDriver
from src.protocols.stream_framer import StreamFramer

_GOLDEN = json.loads((Path(__file__).parent / "golden" / "meshtastic_stream_frames.json").read_text())["frames"]


# ── golden byte format (encode + decode against the spec) ──────────────

@pytest.mark.parametrize("g", _GOLDEN, ids=[g["name"] for g in _GOLDEN])
def test_golden_encode_matches_spec(g):
    payload = bytes.fromhex(g["payload_hex"])
    assert StreamFramer.frame(payload).hex() == g["frame_hex"]


@pytest.mark.parametrize("g", _GOLDEN, ids=[g["name"] for g in _GOLDEN])
def test_golden_decode_roundtrip(g):
    frame = bytes.fromhex(g["frame_hex"])
    assert StreamFramer().feed(frame) == [bytes.fromhex(g["payload_hex"])]


# ── decode behavior ────────────────────────────────────────────────────

def test_two_back_to_back_frames():
    f = StreamFramer()
    stream = StreamFramer.frame(b"\x08\x01") + StreamFramer.frame(b"AB")
    assert f.feed(stream) == [b"\x08\x01", b"AB"]


def test_frame_split_across_reads_reassembles():
    f = StreamFramer()
    whole = StreamFramer.frame(b"hello world")
    out = []
    for i in range(len(whole)):  # feed one byte at a time
        out += f.feed(whole[i:i + 1])
    assert out == [b"hello world"]
    assert f.buffered == 0


def test_leading_garbage_resyncs():
    f = StreamFramer()
    assert f.feed(b"\x00\x11garbage" + StreamFramer.frame(b"\x08\x02")) == [b"\x08\x02"]


def test_lone_start1_not_followed_by_start2_is_dropped():
    f = StreamFramer()
    # 0x94 not followed by 0xC3 must not swallow the real frame that follows.
    assert f.feed(b"\x94\x00\x94\x41" + StreamFramer.frame(b"OK")) == [b"OK"]


def test_truncated_frame_waits_then_completes():
    f = StreamFramer()
    whole = StreamFramer.frame(b"partial-1234")
    assert f.feed(whole[:6]) == []       # header + a little payload — not enough
    assert f.buffered == 6
    assert f.feed(whole[6:]) == [b"partial-1234"]


def test_oversize_length_is_rejected_and_resyncs():
    f = StreamFramer()
    # A header claiming 513 bytes (0x0201) is corrupt; the framer must skip it and still find the next frame.
    bogus = bytes((0x94, 0xC3, 0x02, 0x01)) + b"\x00\x00\x00"
    assert f.feed(bogus + StreamFramer.frame(b"good")) == [b"good"]


def test_empty_payload_frame_is_valid():
    f = StreamFramer()
    assert f.feed(StreamFramer.frame(b"")) == [b""]  # b"" is a real frame, distinct from "need more data"


def test_encode_rejects_oversize_payload():
    with pytest.raises(ValueError):
        StreamFramer.frame(b"x" * (StreamFramer.MAX_PAYLOAD + 1))


def test_max_payload_roundtrips():
    payload = bytes(range(256)) * 2  # exactly 512
    assert StreamFramer().feed(StreamFramer.frame(payload)) == [payload]


# ── StreamDriver.deliver_raw uses the framer ───────────────────────────

class _RawConn:
    def __init__(self):
        self.raw: list[bytes] = []

    def write_bytes(self, data: bytes) -> None:
        self.raw.append(data)


def test_stream_driver_deliver_raw_frames_and_writes():
    conn = _RawConn()
    assert StreamDriver().deliver_raw(conn, b"\x08\x01") is True
    assert conn.raw == [StreamFramer.frame(b"\x08\x01")]  # framed, not raw payload


def test_stream_driver_deliver_raw_without_binary_path_is_honest():
    class _NoBin:
        pass
    with pytest.raises(NotImplementedError):
        StreamDriver().deliver_raw(_NoBin(), b"\x08\x01")
