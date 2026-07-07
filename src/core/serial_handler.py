"""Serial handler — pyserial wrapper with read thread and callback system."""

from __future__ import annotations

import codecs
import logging
import re
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
        line_ending: str = "\n",
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.encoding = encoding
        # Per-firmware command terminator (default LF; Flipper needs CR). Settable after construction —
        # the UI applies the selected firmware's BaseProtocol.line_ending to the live connection.
        self.line_ending = line_ending

        self._serial: serial.Serial | None = None
        self._state = ConnectionState.DISCONNECTED
        self._read_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Serializes the _serial lifetime against writers: write() and disconnect()/teardown take this
        # so a concurrent close (hotplug/reader-error thread) can't null the handle mid-write (which
        # would raise an uncaught AttributeError in write()).
        self._io_lock = threading.Lock()

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

    def remove_line_callback(self, cb: Callable[[str], None]) -> None:
        """Remove a previously-registered line callback (idempotent — no error if absent).

        Lets subscribers detach so callbacks don't accumulate unbounded (e.g. a web client that
        re-subscribes or disconnects); the matching ``TargetIngestor.detach`` already probes for
        this method. Without it, repeated ``on_line`` registration leaks callbacks and amplifies
        every emitted serial line.
        """
        try:
            self._line_callbacks.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb: Callable[[ConnectionState], None]) -> None:
        """Register a callback fired on state transitions."""
        self._state_callbacks.append(cb)

    def remove_state_callback(self, cb: Callable[[ConnectionState], None]) -> None:
        """Remove a previously-registered state callback (idempotent). Symmetric with
        :meth:`remove_line_callback` so a borrower (e.g. a NodeLink) can fully unhook from a gateway
        that outlives it, instead of leaking a dead callback into ``_state_callbacks`` on every reuse."""
        try:
            self._state_callbacks.remove(cb)
        except ValueError:
            pass

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

        # Fully tear down any prior reader thread + handle before reopening. A write()/interrupt error
        # only sets state=ERROR without stopping the reader (the port may still be readable), so a stale
        # reader can still be blocked in read() on the old handle. If we reopened without stopping it, the
        # moment we close the old handle that reader wakes with a SerialException, enters its error path,
        # and calls _release_serial() — which would null out the FRESHLY-opened port and destroy the new
        # connection (a race, since connect() doesn't hold _io_lock while reopening). Stop+join it first.
        # This also frees the OS port so the re-open can't hit "Access is denied" on exclusive COM ports.
        if self._read_thread is not None and self._read_thread.is_alive():
            self._stop_event.set()
            self._read_thread.join(timeout=3.0)
        self._release_serial()
        self._read_thread = None

        self._set_state(ConnectionState.CONNECTING)
        try:
            # Open WITHOUT letting the adapter's DTR/RTS lines pulse the ESP32's EN/GPIO0 on connect.
            # pyserial asserts both by default; on CYD panels (esp. the CH340K 2-USB / Guition boards)
            # that lack the auto-reset transistor pair, an asserted DTR+RTS at open yanks GPIO0/EN low
            # and drops the chip into ROM download mode ("waiting for download") — the firmware never
            # runs and the display stays blank. That is exactly the "CYD shows no GUI" report: simply
            # connecting to monitor a CYD was bricking its screen until power-cycle. Deassert both first
            # so opening the port leaves the running firmware (and its on-screen GUI) undisturbed.
            self._serial = serial.Serial()
            self._serial.port = self.port
            self._serial.baudrate = self.baud
            self._serial.timeout = self.timeout
            self._serial.write_timeout = self.timeout
            self._serial.dtr = False
            self._serial.rts = False
            self._serial.open()
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
        # Join the reader thread OUTSIDE the I/O lock (the join can take up to 3s; holding the lock
        # there would needlessly block writers). Then take the lock only for the handle teardown so it
        # cannot interleave with an in-flight write().
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=3.0)
        self._release_serial()
        self._set_state(ConnectionState.DISCONNECTED)
        log.info("Disconnected from %s", self.port)

    def _release_serial(self) -> None:
        """Close and drop the serial handle under the I/O lock so a later connect() can reopen the
        port cleanly. Safe if already closed/None; called from disconnect() and the reader thread."""
        with self._io_lock:
            if self._serial is not None:
                try:
                    if self._serial.is_open:
                        self._serial.close()
                except Exception:
                    pass
                self._serial = None

    # ── I/O ──────────────────────────────────────────────────────────

    def write(self, data: str) -> None:
        """Send a single command line (exactly one trailing line terminator is appended; LF by default,
        CR for firmwares like Flipper — see :attr:`line_ending`).

        Security: the firmware serial protocol is newline-delimited, so an embedded
        newline/carriage-return (or other control character) would let ONE logical
        command expand into many — a command-injection vector when ``data`` carries
        over-the-air values (e.g. a scanned SSID routed by :class:`AutoRouter`). We
        reject any control character here so a caller cannot smuggle extra commands.

        Raises:
            RuntimeError: If not connected.
            ValueError: If *data* contains a newline or other control character.
        """
        cleaned = data.rstrip("\r\n")
        # C0 controls (0x00–0x1F), DEL (0x7F): never legitimate inside a single command. Validate the
        # input up front (before touching the port) so bad input is rejected fast.
        bad = [ch for ch in cleaned if ord(ch) < 0x20 or ord(ch) == 0x7F]
        if bad:
            raise ValueError(
                f"Refusing to send command with embedded control character(s) "
                f"{[hex(ord(c)) for c in bad]} — possible command injection"
            )
        payload = (cleaned + self.line_ending).encode(self.encoding)
        # Hold the I/O lock across the check+write+flush so disconnect()/teardown can't null/close the
        # handle between the guard and the write (which would raise an uncaught AttributeError).
        with self._io_lock:
            ser = self._serial
            if ser is None or not ser.is_open:
                raise RuntimeError(f"Not connected to {self.port}")
            try:
                ser.write(payload)
                ser.flush()
                log.debug("TX [%s]: %s", self.port, data.strip())
            except (serial.SerialException, OSError) as exc:
                self._set_state(ConnectionState.ERROR)
                self._emit_error(exc)
                raise

    def send_interrupt(self) -> None:
        """Send a raw Ctrl-C (0x03) to interrupt a blocking command — e.g. a long-running Flipper CLI command
        that otherwise holds the shell until it finishes.

        :meth:`write` deliberately rejects every control character (command-injection guard), so it can't send
        0x03. This is the narrow, explicit exception: it writes the single byte 0x03 — and nothing else, no
        line terminator — bypassing that guard for this one documented control code only.

        Raises:
            RuntimeError: If not connected.
        """
        with self._io_lock:
            ser = self._serial
            if ser is None or not ser.is_open:
                raise RuntimeError(f"Not connected to {self.port}")
            try:
                ser.write(b"\x03")
                ser.flush()
                log.debug("TX [%s]: <0x03 interrupt>", self.port)
            except (serial.SerialException, OSError) as exc:
                self._set_state(ConnectionState.ERROR)
                self._emit_error(exc)
                raise

    def write_bytes(self, payload: bytes) -> None:
        """Send a raw byte payload verbatim — no line terminator, no control-character guard.

        The binary transport for framed/stream protocols (e.g. a Meshtastic Stream-API protobuf frame via
        :class:`~src.core.drivers.StreamDriver`). :meth:`write` is text-only — it appends a line terminator and
        rejects control bytes (command-injection guard), which would corrupt a binary frame — so a stream driver
        needs this path instead. The caller (StreamFramer) owns framing; this just puts the exact bytes on the
        wire. Live TX against real radios stays bench-gated.

        Raises:
            RuntimeError: If not connected.
        """
        with self._io_lock:
            ser = self._serial
            if ser is None or not ser.is_open:
                raise RuntimeError(f"Not connected to {self.port}")
            try:
                ser.write(bytes(payload))
                ser.flush()
                log.debug("TX [%s]: <%d raw bytes>", self.port, len(payload))
            except (serial.SerialException, OSError) as exc:
                self._set_state(ConnectionState.ERROR)
                self._emit_error(exc)
                raise

    # ── Internal ─────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """Background thread: read lines until stopped or error."""
        buf = ""
        # One incremental decoder for the whole loop, so a multi-byte UTF-8 sequence split across two
        # reads reconstructs into a single code point (a per-read decode() would emit two U+FFFD).
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        while not self._stop_event.is_set():
            try:
                if not self._serial or not self._serial.is_open:
                    break
                raw = self._serial.read(self._serial.in_waiting or 1)
                if not raw:
                    continue
                buf += decoder.decode(raw)
                # Frame on ANY line terminator — LF, CRLF, or CR-only. A CR-only firmware (e.g. Flipper,
                # whose line_ending is "\r") never sends "\n", so splitting on "\n" alone would never frame
                # a line and `buf` would grow unbounded. Splitting on runs of \r/\n handles all three; the
                # last element is the still-incomplete tail, which stays buffered until its terminator lands.
                if "\n" in buf or "\r" in buf:
                    parts = re.split(r"[\r\n]+", buf)
                    buf = parts.pop()
                    for line in parts:
                        if line:
                            self._emit_line(line)
            except serial.SerialException as exc:
                if not self._stop_event.is_set():
                    log.error("Serial read error on %s: %s", self.port, exc)
                    self._set_state(ConnectionState.ERROR)
                    self._emit_error(exc)
                self._release_serial()
                break
            except Exception as exc:
                if not self._stop_event.is_set():
                    log.error("Unexpected reader error on %s: %s", self.port, exc)
                    # A non-SerialException (e.g. a bare OSError on device removal) must STILL move us
                    # out of CONNECTED — otherwise is_connected lies and connect() refuses to reopen.
                    self._set_state(ConnectionState.ERROR)
                    self._emit_error(exc)
                self._release_serial()
                break

    def _set_state(self, new_state: ConnectionState) -> None:
        if new_state != self._state:
            self._state = new_state
            # Snapshot the callback list: a subscriber attaching/detaching mid-fan-out (web/Qt threads)
            # must not skip or stale-fire callbacks during iteration.
            for cb in list(self._state_callbacks):
                try:
                    cb(new_state)
                except Exception:
                    log.exception("State callback error")

    def _emit_line(self, line: str) -> None:
        for cb in list(self._line_callbacks):
            try:
                cb(line)
            except Exception:
                log.exception("Line callback error")

    def _emit_error(self, exc: Exception) -> None:
        for cb in list(self._error_callbacks):
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
