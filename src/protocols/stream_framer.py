"""Meshtastic Stream-API frame codec (comms rework, S3-b).

Meshtastic's device serial link is NOT a text CLI — it's a length-delimited PROTOBUF stream. Each frame is:

    0x94  0xC3  <len MSB>  <len LSB>  <payload …>

i.e. a 2-byte magic header (START1=0x94, START2=0xC3), a **big-endian** unsigned 16-bit payload length, then
that many protobuf bytes (a ToRadio client->device or FromRadio device->client message). The receiver treats a
declared length > 512 as a corrupted frame. (Verified against the Meshtastic client-API docs, 2026-07-01.)

This module owns ONLY the transport framing — turning a byte stream into complete payloads and back. It does
**not** decode the protobuf payload into Node/Position/Message: that needs the `meshtastic` library's generated
`.proto` types and a real radio to validate against, so it stays out of scope here (honest boundary). What this
gives S3 is a reliable, hardware-free, test-gated frame boundary layer that `StreamDriver` uses for the raw path
and that a future Meshtastic driver will feed into the protobuf decoder.
"""

from __future__ import annotations

from typing import Callable


class StreamFramer:
    """Incremental Meshtastic Stream-API framer: feed bytes, get back complete payloads.

    Stateful on the RX side (holds a buffer across :meth:`feed` calls so a frame split across reads
    reassembles). The TX side (:meth:`frame`) is stateless. Robust to partial reads, leading garbage
    (resyncs to the next magic header), a lone START1 not followed by START2, and oversized/corrupt length
    fields (skips and resyncs rather than trusting a bogus length).

    Non-frame bytes are exactly the human-readable debug/boot text a Meshtastic radio also prints on the
    same serial line (the client-API treats any byte that isn't a valid START1 as ASCII log text). Pass an
    ``on_skipped`` callback to receive those bytes as they're discarded, so a caller can surface the debug log
    instead of losing it. Default ``None`` preserves the original frames-only behavior.
    """

    START1 = 0x94
    START2 = 0xC3
    HEADER_LEN = 4
    MAX_PAYLOAD = 512  # Meshtastic: a declared length above this means the frame is corrupt.

    def __init__(self, on_skipped: Callable[[bytes], None] | None = None) -> None:
        self._buf = bytearray()
        self._on_skipped = on_skipped

    def _skip(self, data: bytes | bytearray) -> None:
        """Route discarded non-frame bytes (radio debug/boot text) to the optional sink."""
        if self._on_skipped is not None and data:
            try:
                self._on_skipped(bytes(data))
            except Exception:  # noqa: BLE001 — a text sink must never break framing
                pass

    def feed(self, data: bytes) -> list[bytes]:
        """Append *data* to the internal buffer and return every complete payload now available (in order).
        Incomplete trailing bytes stay buffered for the next call. Returns an empty list when no full frame
        is ready yet."""
        self._buf.extend(data)
        out: list[bytes] = []
        while True:
            payload = self._extract_one()
            if payload is None:  # need more bytes (note: an empty b"" frame is valid and is NOT None)
                break
            out.append(payload)
        return out

    def reset(self) -> None:
        """Drop any buffered partial frame (e.g. on reconnect)."""
        self._buf.clear()

    @property
    def buffered(self) -> int:
        """Bytes currently held waiting for a complete frame (diagnostics/tests)."""
        return len(self._buf)

    def _extract_one(self) -> bytes | None:
        """Pull one complete payload off the front of the buffer, discarding garbage/false starts as it goes.
        Returns the payload bytes (possibly empty), or None when more data is needed."""
        buf = self._buf
        while True:
            start = buf.find(self.START1)
            if start == -1:
                self._skip(buf)  # no possible frame start — the whole buffer is debug text
                buf.clear()
                return None
            if start > 0:
                self._skip(buf[:start])  # debug text before the magic byte
                del buf[:start]  # resync: discard leading garbage before the magic byte
            if len(buf) < 2:
                return None  # need START2 to decide
            if buf[1] != self.START2:
                self._skip(buf[:1])  # lone 0x94 that isn't a header — it was text
                del buf[:1]  # drop it and rescan
                continue
            if len(buf) < self.HEADER_LEN:
                return None  # have the magic, need the length bytes
            length = (buf[2] << 8) | buf[3]  # big-endian
            if length > self.MAX_PAYLOAD:
                self._skip(buf[:1])  # bogus length -> this 0x94 wasn't a real header
                del buf[:1]  # treat as corrupt, skip this START1 and resync
                continue
            if len(buf) < self.HEADER_LEN + length:
                return None  # full payload not here yet — wait
            payload = bytes(buf[self.HEADER_LEN:self.HEADER_LEN + length])
            del buf[:self.HEADER_LEN + length]
            return payload

    @classmethod
    def frame(cls, payload: bytes) -> bytes:
        """Wrap a protobuf *payload* in a Stream-API frame (the TX/encode side). Raises ValueError if the
        payload exceeds the 512-byte maximum."""
        n = len(payload)
        if n > cls.MAX_PAYLOAD:
            raise ValueError(f"payload {n} bytes exceeds Meshtastic max {cls.MAX_PAYLOAD}")
        return bytes((cls.START1, cls.START2, (n >> 8) & 0xFF, n & 0xFF)) + bytes(payload)
