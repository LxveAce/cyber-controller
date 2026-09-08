"""The packaged smoke check must deliver its result line best-effort and at most once, and must
never turn a completed check into a crash, in a frozen --windowed build whose standard streams are
None or backed by an unusable descriptor.

Contract: ONE authoritative channel per line. A usable stream is written and flushed
there and any failure is swallowed WITHOUT replaying elsewhere (a flush error after a write is
ambiguous). With no stream the bytes go to the raw descriptor, honouring each os.write count and
advancing through partial writes; zero progress or an OSError terminates finitely. Delivery is not
promised when the channel is unusable, nor exactly-once after an ambiguous error.

Inert: no frozen build, no Qt, no server, no real descriptor write. os.write is recorded, never
performed.
"""
from __future__ import annotations

import io

from src.ui import packaged_smoke

TEXT = '{"status": "ok"}'
LINE = (TEXT + "\n").encode("utf-8")


class _WriteThenFlushFails(io.StringIO):
    """Delivers the bytes on write, then raises on flush (the ambiguous case)."""

    def flush(self):
        raise OSError(22, "Invalid argument")


class _WriteRaises:
    def __init__(self, exc):
        self.exc = exc
        self.wrote = False

    def write(self, value):
        self.wrote = True
        raise self.exc

    def flush(self):  # pragma: no cover - never reached; write raises first
        raise AssertionError("flush must not run after write raised")


def _descriptor(monkeypatch, counts):
    """Record os.write calls; return the scripted byte-count for each call (an int, or an exception
    instance to raise). Appends exactly the accepted prefix so partial writes are visible."""
    got = bytearray()
    calls = []
    script = list(counts)

    def fake_write(fd, data):
        calls.append((fd, bytes(data)))
        step = script.pop(0) if script else len(data)
        if isinstance(step, BaseException):
            raise step
        n = min(step, len(data))
        got.extend(bytes(data)[:n])
        return n

    monkeypatch.setattr(packaged_smoke.os, "write", fake_write)
    return got, calls


def _no_descriptor(monkeypatch):
    calls = []
    monkeypatch.setattr(packaged_smoke.os, "write",
                        lambda fd, data: (calls.append(fd), len(data))[1])
    return calls


# ---- stream channel: one authoritative channel, never replayed --------------------------------

def test_healthy_stream_delivers_once_and_never_touches_the_descriptor(monkeypatch):
    calls = _no_descriptor(monkeypatch)
    out = io.StringIO()
    packaged_smoke._emit(TEXT, out, 1)
    assert out.getvalue() == TEXT + "\n"
    assert calls == [], "a working stream is the only channel used"


def test_delivered_then_flush_failure_is_not_replayed(monkeypatch):
    # The stream already emitted the line; a flush failure must NOT replay it to
    # the descriptor (that produced two complete JSON lines).
    got, calls = _descriptor(monkeypatch, [])
    stream = _WriteThenFlushFails()
    packaged_smoke._emit(TEXT, stream, 1)
    assert stream.getvalue() == TEXT + "\n", "the stream received exactly one line"
    assert calls == [], "no descriptor replay after an ambiguous flush failure"


def test_stream_write_failure_is_swallowed_without_replay(monkeypatch):
    got, calls = _descriptor(monkeypatch, [])
    for exc in (ValueError("I/O operation on closed file"), OSError(9, "Bad file descriptor")):
        calls.clear()
        stream = _WriteRaises(exc)
        packaged_smoke._emit(TEXT, stream, 1)  # must not raise
        assert stream.wrote and calls == [], "a failed stream write is not replayed"


# ---- descriptor channel: honour the count, advance, terminate finitely -------------------------

def test_absent_stream_delivers_the_full_line_to_the_descriptor(monkeypatch):
    got, calls = _descriptor(monkeypatch, [len(LINE)])
    packaged_smoke._emit(TEXT, None, 1)
    assert got == LINE and len(calls) == 1


def test_repeated_short_writes_deliver_the_whole_line_in_order(monkeypatch):
    got, calls = _descriptor(monkeypatch, [2] * (len(LINE)))  # two bytes at a time
    packaged_smoke._emit(TEXT, None, 1)
    assert got == LINE, "every byte delivered exactly once, in order"
    assert len(calls) == (len(LINE) + 1) // 2
    # each call was handed the remaining suffix, never the whole payload again
    assert calls[1][1] == LINE[2:]


def test_zero_progress_terminates_finitely_without_spinning(monkeypatch):
    got, calls = _descriptor(monkeypatch, [0])
    packaged_smoke._emit(TEXT, None, 1)  # must return, not spin
    assert got == b"" and len(calls) == 1


def test_failure_after_a_partial_write_does_not_restart_the_payload(monkeypatch):
    got, calls = _descriptor(monkeypatch, [3, OSError(5, "I/O error")])
    packaged_smoke._emit(TEXT, None, 1)  # must not raise
    assert got == LINE[:3], "only the accepted prefix; the payload is not re-sent"
    assert calls[1][1] == LINE[3:], "the retry offered the suffix, not the whole line"


def test_both_channels_unusable_is_silent(monkeypatch):
    got, calls = _descriptor(monkeypatch, [OSError(9, "Bad file descriptor")])
    packaged_smoke._emit(TEXT, None, 1)  # no stream, descriptor fails immediately -> silent
    assert got == b"" and len(calls) == 1


# ---- Unicode boundaries: byte-accurate, never re-encoded or restarted --------------------------

def test_unicode_bytes_survive_byte_at_a_time_partial_writes(monkeypatch):
    text = "café \U0001f600 end"   # é (2 bytes) + emoji (4 bytes)
    expected = (text + "\n").encode("utf-8")
    got, calls = _descriptor(monkeypatch, [1] * len(expected))   # one byte per write
    packaged_smoke._emit(text, None, 1)
    assert got == expected, "multibyte characters reassemble across partial writes"
    assert len(calls) == len(expected)


def test_lone_surrogate_is_replaced_not_raised(monkeypatch):
    got, calls = _descriptor(monkeypatch, [999])   # one write accepts the whole line
    packaged_smoke._emit("x \ud800 y", None, 1)   # a lone surrogate
    assert got.endswith(b" y\n") and calls, "encoded errors='replace', delivered without raising"
