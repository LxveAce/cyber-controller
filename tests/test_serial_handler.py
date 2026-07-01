"""Tests for ``src.core.serial_handler.SerialConnection`` write hardening.

The module does ``import serial`` at top level, so if pyserial is missing the
whole module import fails — we therefore ``importorskip`` it, which SKIPS this
file cleanly instead of erroring when pyserial is absent.

We never open a real port: we construct a ``SerialConnection`` and monkeypatch
its ``_serial`` with a tiny fake (``is_open=True`` + ``write``/``flush``), then
assert:
    * a clean command ('scanap') is accepted and the encoded payload is written;
    * a newline-bearing command ('a\\nreboot') raises ValueError before any
      bytes reach the port (command-injection defense).
"""

from __future__ import annotations

import pytest

# pyserial is the gating dep: serial_handler imports `serial` at module top.
pytest.importorskip("serial")
serial_handler = pytest.importorskip("src.core.serial_handler")

SerialConnection = serial_handler.SerialConnection


class _FakeSerial:
    """Minimal stand-in for ``serial.Serial`` exposing only what write() touches."""

    def __init__(self) -> None:
        self.is_open = True
        self.written: list[bytes] = []
        self.flushed = 0

    def write(self, payload: bytes) -> int:
        self.written.append(payload)
        return len(payload)

    def flush(self) -> None:
        self.flushed += 1


def _make_conn() -> tuple[SerialConnection, _FakeSerial]:
    conn = SerialConnection("COM-TEST", baud=115200)
    fake = _FakeSerial()
    conn._serial = fake  # do NOT open a real port
    return conn, fake


def test_write_clean_command_accepted() -> None:
    conn, fake = _make_conn()
    conn.write("scanap")
    # Exactly one newline-terminated payload reached the (fake) port.
    assert fake.written == [b"scanap\n"]
    assert fake.flushed == 1


def test_write_strips_trailing_newline_only() -> None:
    conn, fake = _make_conn()
    # A single trailing newline is normal line termination, not injection.
    conn.write("reboot\n")
    assert fake.written == [b"reboot\n"]


def test_write_rejects_embedded_newline() -> None:
    conn, fake = _make_conn()
    with pytest.raises(ValueError):
        conn.write("a\nreboot")
    # Nothing was written — rejected before reaching the port.
    assert fake.written == []


@pytest.mark.parametrize("payload", ["a\rreboot", "scan\x00ap", "led\x07"])
def test_write_rejects_other_control_chars(payload: str) -> None:
    conn, fake = _make_conn()
    with pytest.raises(ValueError):
        conn.write(payload)
    assert fake.written == []


def test_write_without_serial_raises_runtime_error() -> None:
    # Constructed but never connected and no fake attached -> not connected.
    conn = SerialConnection("COM-NONE")
    with pytest.raises(RuntimeError):
        conn.write("scanap")


def test_send_interrupt_writes_raw_ctrl_c() -> None:
    # send_interrupt() must put exactly one 0x03 byte on the wire — no line terminator, and bypassing the
    # control-char guard that write() enforces (so a blocking Flipper command can be stopped).
    conn, fake = _make_conn()
    conn.send_interrupt()
    assert fake.written == [b"\x03"]
    assert fake.flushed == 1


def test_send_interrupt_without_serial_raises_runtime_error() -> None:
    conn = SerialConnection("COM-NONE")
    with pytest.raises(RuntimeError):
        conn.send_interrupt()


# ── Reader-loop / lifecycle hardening (bug-hunt fixes #1, #10, #23, #24) ───────────────────────────

import threading  # noqa: E402
import time  # noqa: E402

ConnectionState = serial_handler.ConnectionState


class _ScriptedSerial:
    """A fake serial that returns scripted byte chunks, then optionally raises, then idles.

    Idle reads sleep briefly so the (real-code) reader loop doesn't hot-spin in the test window.
    """

    def __init__(self, chunks, raise_exc=None) -> None:
        self.is_open = True
        self._chunks = list(chunks)
        self._raise_exc = raise_exc
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._raise_exc is not None:
            exc, self._raise_exc = self._raise_exc, None
            raise exc
        time.sleep(0.005)
        return b""

    def write(self, payload: bytes) -> int:
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _run_reader(conn) -> threading.Thread:
    conn._set_state(ConnectionState.CONNECTED)
    th = threading.Thread(target=conn._reader_loop, daemon=True)
    th.start()
    return th


def test_release_serial_closes_and_nulls() -> None:
    conn = SerialConnection("X")
    fake = _ScriptedSerial([])
    conn._serial = fake
    conn._release_serial()
    assert fake.closed and conn._serial is None


def test_reader_loop_reconstructs_split_utf8() -> None:
    # '✓' = U+2713 = bytes e2 9c 93, split across two reads. A per-read decode would emit U+FFFD x3;
    # the incremental decoder must reconstruct the single code point.
    conn = SerialConnection("X")
    got: list[str] = []
    done = threading.Event()
    conn.on_line(lambda ln: (got.append(ln), done.set()))
    conn._serial = _ScriptedSerial([b"\xe2\x9c", b"\x93 ok\n"])
    th = _run_reader(conn)
    assert done.wait(2.0), "no line emitted"
    conn._stop_event.set()
    th.join(timeout=2.0)
    assert got == ["✓ ok"]


def test_reader_loop_generic_exception_sets_error_and_releases() -> None:
    # A non-SerialException (here ValueError) must STILL move the connection to ERROR (so is_connected
    # stops lying) and release the handle (so a later connect() can reopen).
    conn = SerialConnection("X")
    fake = _ScriptedSerial([b"hello\n"], raise_exc=ValueError("boom"))
    done = threading.Event()
    conn.on_error(lambda _e: done.set())
    conn._serial = fake
    th = _run_reader(conn)
    assert done.wait(2.0), "error callback never fired"
    th.join(timeout=2.0)
    assert conn.state == ConnectionState.ERROR
    assert conn._serial is None and fake.closed


def test_connect_releases_stale_handle_before_reopen(monkeypatch) -> None:
    # An ERROR-state connection with a leftover handle must be reopenable: connect() closes the stale
    # handle first (else Windows raises "Access is denied" on the exclusive COM port).
    conn = SerialConnection("X")
    stale = _ScriptedSerial([])
    conn._serial = stale
    conn._state = ConnectionState.ERROR
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda **_kw: _ScriptedSerial([]))
    conn.connect()
    try:
        assert stale.closed, "stale handle not released before reopen"
        assert conn.state == ConnectionState.CONNECTED
    finally:
        conn.disconnect()


def test_emit_line_snapshot_allows_detach_during_emit() -> None:
    # Detaching a callback during fan-out must not skip the remaining snapshot nor raise.
    conn = SerialConnection("X")
    seen: list[tuple[str, str]] = []

    def cb_b(ln: str) -> None:
        seen.append(("b", ln))

    def cb_a(ln: str) -> None:
        seen.append(("a", ln))
        conn.remove_line_callback(cb_b)  # mutate the list mid-iteration

    conn.on_line(cb_a)
    conn.on_line(cb_b)
    conn._emit_line("x")  # both fire (snapshot), b detached for next time
    assert ("a", "x") in seen and ("b", "x") in seen
    seen.clear()
    conn._emit_line("y")
    assert seen == [("a", "y")]
