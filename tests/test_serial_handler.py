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
