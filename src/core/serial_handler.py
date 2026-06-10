"""Serial handler — pyserial wrapper with read thread and callback system."""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Callable

import serial

log = logging.getLogger(__name__)


class ConnectionState(Enum):
    """Serial connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class SerialConnection:
    """Thread-safe serial port wrapper.

    Opens a pyserial connection on :meth:`connect`, spins up a reader
    thread that emits decoded lines to registered callbacks, and
    provides a :meth:`write` method for sending commands.

    Usage::

        conn = SerialConnection("COM3", baud=115200)
        conn.on_line(lambda line: print(line))
        conn.on_state_change(lambda s: print(s))
        conn.connect()
        conn.write("scanap")
        # ...
        conn.disconnect()
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout: float = 1.0,
        encoding: str = "utf-8",
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.encoding = encoding

        self._serial: serial.Serial | None = None
        self._state = ConnectionState.DISCONNECTED
        self._read_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Callback lists
        self._line_callbacks: list[Callable[[str], None]] = []
        self._state_callbacks: list[Callable[[ConnectionState], None]] = []
        self._error_callbacks: list[Callable[[Exception], None]] = []

    # ── Properties ───────────────────────────────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    # ── Callback registration ────────────────────────────────────────

    def on_line(self, cb: Callable[[str], None]) -> None:
        """Register a callback fired for every received line."""
        self._line_callbacks.append(cb)

    def on_state_change(self, cb: Callable[[ConnectionState], None]) -> None:
        """Register a callback fired on state transitions."""
        self._state_callbacks.append(cb)

    def on_error(self, cb: Callable[[Exception], None]) -> None:
        """Register a callback fired on read errors."""
        self._error_callbacks.append(cb)

    # ── Connection lifecycle ─────────────────────────────────────────

    def connect(self) -> None:
        """Open the serial port and start the reader thread.

        Raises:
            serial.SerialException: If the port cannot be opened.
        """
        if self._state == ConnectionState.CONNECTED:
            log.warning("Already connected to %s", self.port)
            return

        self._set_state(ConnectionState.CONNECTING)
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self._stop_event.clear()
            self._read_thread = threading.Thread(
                target=self._reader_loop,
                name=f"serial-reader-{self.port}",
                daemon=True,
            )
            self._read_thread.start()
            self._set_state(ConnectionState.CONNECTED)
            log.info("Connected to %s @ %d baud", self.port, self.baud)
        except serial.SerialException as exc:
            self._set_state(ConnectionState.ERROR)
            self._emit_error(exc)
            raise

    def disconnect(self) -> None:
        """Stop the reader thread and close the port."""
        if self._state == ConnectionState.DISCONNECTED:
            return
        self._stop_event.set()
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._set_state(ConnectionState.DISCONNECTED)
        log.info("Disconnected from %s", self.port)

    # ── I/O ──────────────────────────────────────────────────────────

    def write(self, data: str) -> None:
        """Send a single command line (exactly one trailing newline is appended).

        Security: the firmware serial protocol is newline-delimited, so an embedded
        newline/carriage-return (or other control character) would let ONE logical
        command expand into many — a command-injection vector when ``data`` carries
        over-the-air values (e.g. a scanned SSID routed by :class:`AutoRouter`). We
        reject any control character here so a caller cannot smuggle extra commands.

        Raises:
            RuntimeError: If not connected.
            ValueError: If *data* contains a newline or other control character.
        """
        if not self._serial or not self._serial.is_open:
            raise RuntimeError(f"Not connected to {self.port}")
        cleaned = data.rstrip("\r\n")
        # C0 controls (0x00–0x1F), DEL (0x7F): never legitimate inside a single command.
        bad = [ch for ch in cleaned if ord(ch) < 0x20 or ord(ch) == 0x7F]
        if bad:
            raise ValueError(
                f"Refusing to send command with embedded control character(s) "
                f"{[hex(ord(c)) for c in bad]} — possible command injection"
            )
        payload = (cleaned + "\n").encode(self.encoding)
        try:
            self._serial.write(payload)
            self._serial.flush()
            log.debug("TX [%s]: %s", self.port, data.strip())
        except serial.SerialException as exc:
            self._set_state(ConnectionState.ERROR)
            self._emit_error(exc)
            raise

    # ── Internal ─────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """Background thread: read lines until stopped or error."""
        buf = ""
        while not self._stop_event.is_set():
            try:
                if not self._serial or not self._serial.is_open:
                    break
                raw = self._serial.read(self._serial.in_waiting or 1)
                if not raw:
                    continue
                buf += raw.decode(self.encoding, errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line:
                        self._emit_line(line)
            except serial.SerialException as exc:
                if not self._stop_event.is_set():
                    log.error("Serial read error on %s: %s", self.port, exc)
                    self._set_state(ConnectionState.ERROR)
                    self._emit_error(exc)
                break
            except Exception as exc:
                if not self._stop_event.is_set():
                    log.error("Unexpected reader error on %s: %s", self.port, exc)
                    self._emit_error(exc)
                break

    def _set_state(self, new_state: ConnectionState) -> None:
        if new_state != self._state:
            self._state = new_state
            for cb in self._state_callbacks:
                try:
                    cb(new_state)
                except Exception:
                    log.exception("State callback error")

    def _emit_line(self, line: str) -> None:
        for cb in self._line_callbacks:
            try:
                cb(line)
            except Exception:
                log.exception("Line callback error")

    def _emit_error(self, exc: Exception) -> None:
        for cb in self._error_callbacks:
            try:
                cb(exc)
            except Exception:
                log.exception("Error callback error")

    # ── Context manager ──────────────────────────────────────────────

    def __enter__(self) -> SerialConnection:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
