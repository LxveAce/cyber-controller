"""Serial handler — pyserial wrapper with read thread and callback system."""

from __future__ import annotations

import codecs
import dis
import functools
import logging
import re
import threading
import types
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import serial

log = logging.getLogger(__name__)

# Hard cap on an un-terminated line buffer. A device that streams bytes with no CR/LF (a wedged
# firmware, a binary flood, or a hostile device) would otherwise grow `buf` without bound and
# exhaust memory. When the buffer passes this without a terminator, flush it as one line so memory
# stays bounded. 64 KiB is far larger than any real protocol line yet small enough to stay cheap.
_MAX_LINE_CHARS = 64 * 1024
# A callback can block forever. Lifecycle methods must still return, and queued state metadata must
# remain finite while later callers churn the connection behind that observer.
_MAX_PENDING_STATE_NOTIFICATIONS = 64
_NO_WRITE_RESULT = object()


class ConnectionState(Enum):
    """Serial connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class WriteDisposition(str, Enum):
    """Finite delivery truth available at the host serial boundary."""

    DEFINITELY_NOT_WRITTEN = "definitely_not_written"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    HOST_WRITE_COMPLETE = "host_write_complete"


class WriteErrorCode(str, Enum):
    """Payload-free error reasons safe to expose to higher layers."""

    ZERO_WRITE = "serial_zero_write"
    SHORT_WRITE = "serial_short_write"
    INVALID_COUNT = "serial_invalid_write_count"
    WRITE_ERROR = "serial_write_error"
    FLUSH_ERROR = "serial_flush_error"


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """Immutable, payload-free result of one low-level serial write attempt."""

    disposition: WriteDisposition
    bytes_requested: int
    bytes_reported: int | None = None
    safe_error_code: WriteErrorCode | None = None

    def __post_init__(self) -> None:
        if type(self.bytes_requested) is not int or self.bytes_requested < 0:
            raise ValueError("Invalid requested byte count")
        if self.bytes_reported is not None and (
            type(self.bytes_reported) is not int
            or self.bytes_reported < 0
            or self.bytes_reported > self.bytes_requested
        ):
            raise ValueError("Invalid reported byte count")
        valid_disposition = any(
            self.disposition is member for member in WriteDisposition
        )
        valid_error_code = self.safe_error_code is None or any(
            self.safe_error_code is member for member in WriteErrorCode
        )
        if not valid_disposition or not valid_error_code:
            raise ValueError("Invalid serial write receipt enum")

        if self.disposition is WriteDisposition.HOST_WRITE_COMPLETE:
            consistent = (
                self.bytes_reported == self.bytes_requested
                and self.safe_error_code is None
            )
        elif self.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN:
            consistent = (
                self.bytes_requested > 0
                and self.bytes_reported == 0
                and self.safe_error_code is WriteErrorCode.ZERO_WRITE
            )
        else:
            consistent = (
                self.safe_error_code is WriteErrorCode.SHORT_WRITE
                and self.bytes_reported is not None
                and 0 < self.bytes_reported < self.bytes_requested
            ) or (
                self.safe_error_code in {
                    WriteErrorCode.INVALID_COUNT,
                    WriteErrorCode.WRITE_ERROR,
                }
                and self.bytes_reported is None
            ) or (
                self.safe_error_code is WriteErrorCode.FLUSH_ERROR
                and self.bytes_reported == self.bytes_requested
            )
        if not consistent:
            raise ValueError("Inconsistent serial write receipt")


class IncompleteSerialWrite(serial.SerialException):
    """Legacy-compatible exception for a returned zero, short, or invalid count."""

    def __init__(self, receipt: WriteReceipt) -> None:
        self.receipt = receipt
        code = receipt.safe_error_code
        safe_code = code.value if code is not None else "serial_write_incomplete"
        super().__init__(f"Serial write did not complete ({safe_code})")


@dataclass(frozen=True, slots=True, repr=False)
class _WriteAttempt:
    """Internal bridge between receipt and legacy APIs; never logged or exposed."""

    receipt: WriteReceipt
    cause: BaseException | None = None


@dataclass(slots=True, repr=False)
class _WriteTransaction:
    """Cross-frame facts needed to settle control flow escaping a post-entry write."""

    requested: int
    serial_handle: object | None = None
    incarnation: int = 0
    starting_state_generation: int = 0
    write_entered: bool = False
    reported_value: object = _NO_WRITE_RESULT
    completion_published: bool = False
    attempt: _WriteAttempt | None = None
    lock_depths: tuple[int, int, int] = (0, 0, 0)


@dataclass(slots=True, repr=False)
class _StateNotification:
    """One generation-ordered state event, made ready only after lifecycle locks are released."""

    state: ConnectionState
    generation: int
    reservation_owner: object | None = None
    ready: bool = False
    kind: str = "generic"
    serial_handle: object | None = None
    incarnation: int | None = None
    error: Exception | None = None
    write_receipt_facts: tuple[
        WriteDisposition,
        int,
        int | None,
        WriteErrorCode | None,
    ] | None = None
    emit_state: bool = True
    state_callbacks: list[Callable[[ConnectionState], None]] | None = None
    state_index: int = 0
    error_callbacks: list[Callable[[Exception], None]] | None = None
    error_index: int = 0
    active_callback: object | None = None
    active_phase: str | None = None
    active_index: int = -1


@dataclass(slots=True, repr=False)
class _StateDrainAttempt:
    """Cross-frame ownership of one notification taken but not yet delivered."""

    pending: list[_StateNotification]
    notification_lock_depth: int = 0


@dataclass(slots=True, repr=False)
class _ConnectAttempt:
    """Identity token and resources owned by one admitted connection startup."""

    startup_started: bool = False
    candidate_serial: serial.Serial | None = None
    reader_gate: threading.Event | None = None
    reader_thread: threading.Thread | None = None
    reader_loop_attempt: _ReaderLoopAttempt | None = None
    connecting_state_generation: int | None = None
    aborted_state_generation: int | None = None
    connected_notification: tuple[object, int, int] | None = None
    lock_depths: tuple[int, int, int] = (0, 0, 0)


@dataclass(slots=True, repr=False)
class _DisconnectAttempt:
    """Cross-frame identity and transition facts for one public disconnect."""

    identity: tuple[
        object | None,
        int,
        ConnectionState,
        int,
        threading.Thread | None,
    ] | None = None
    disconnected_state_generation: int | None = None
    lock_depths: tuple[int, int, int] = (0, 0, 0)


@dataclass(slots=True, repr=False)
class _ReaderFailureAttempt:
    """Cross-frame identity and notification facts for one failed reader."""

    identity: tuple[object | None, int, ConnectionState, int]
    state_changed: bool = False
    state_generation: int | None = None
    error_notification_started: bool = False
    lock_depths: tuple[int, int, int] | None = None


@dataclass(slots=True, repr=False)
class _ReaderLoopAttempt:
    """Outer reader-loop ownership established before one adapter read begins."""

    active_failure: _ReaderFailureAttempt | None = None
    failure: Exception | None = None
    log_message: str = "Serial reader interrupted during transport I/O"
    escaped: bool = False


class _ConnectFailure(Exception):
    """Internal carrier that defers connect-failure callbacks beyond lifecycle ownership."""

    def __init__(
        self,
        cause: Exception,
        state_generation: int,
        state_changed: bool,
    ) -> None:
        super().__init__()
        self.cause = cause
        self.state_generation = state_generation
        self.state_changed = state_changed


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
        raw: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.encoding = encoding
        # Per-firmware command terminator (default LF; Flipper needs CR). Settable after construction —
        # the UI applies the selected firmware's BaseProtocol.line_ending to the live connection.
        self.line_ending = line_ending
        # Raw byte mode: when True the reader thread hands each raw read straight to `on_bytes` subscribers
        # and does NOT run the incremental UTF-8 line decoder (which would corrupt binary protobuf and split
        # a framed stream on stray \n/\r bytes). A Meshtastic StreamAPI connection sets this so its
        # length-delimited protobuf frames arrive intact; every text-CLI firmware leaves it False. Settable
        # live (the reader checks it each iteration) so a backend can enable it right after connect.
        self.raw = raw

        self._serial: serial.Serial | None = None
        self._state = ConnectionState.DISCONNECTED
        self._read_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Serializes the _serial lifetime against writers: write() and disconnect()/teardown take this
        # so a concurrent close (hotplug/reader-error thread) can't null the handle mid-write (which
        # would raise an uncaught AttributeError in write()).
        # Ownership-aware locks let an outer control-flow boundary unwind a critical section whose
        # Python ``with`` exit bytecode was itself interrupted. Other threads remain excluded.
        self._io_lock = threading.RLock()
        # Serializes connect/disconnect as whole lifecycle mutations. RLock preserves existing
        # same-thread callback reentrancy; the I/O lock remains the shorter handle/write barrier.
        self._lifecycle_lock = threading.RLock()
        # State transitions enqueue under `_io_lock`; emitters make their event ready only after
        # releasing lifecycle locks. One cooperative drainer preserves generation order without
        # making another thread's disconnect wait for a user callback that may be waiting on it.
        # RLock exposes current-thread ownership recovery. No user callback runs while it is held;
        # the reentrancy is used only to release a trace-abandoned critical section safely.
        self._state_notification_lock = threading.RLock()
        self._state_notifications: list[_StateNotification] = []
        self._state_notification_drain_owner: _StateDrainAttempt | None = None
        # A returned bad byte count or any exception after entering write() invalidates this
        # handle's delivery history. Keep it readable for the existing reader loop, but forbid all
        # further TX until connect() establishes a fresh serial incarnation.
        self._write_quarantined = False
        self._serial_incarnation = 0
        # One payload-free identity token spans adapter entry through receipt/state settlement.
        # It remains published if a control-flow exception unwinds ``_io_lock``, so a queued
        # writer cannot enter the same incarnation during the outer recovery gap.
        self._write_transaction: _WriteTransaction | None = None
        self._connect_attempt: _ConnectAttempt | None = None
        self._state_generation = 0

        # Callback lists
        self._line_callbacks: list[Callable[[str], None]] = []
        self._byte_callbacks: list[Callable[[bytes], None]] = []
        self._state_callbacks: list[Callable[[ConnectionState], None]] = []
        self._error_callbacks: list[Callable[[Exception], None]] = []

    # ── Properties ───────────────────────────────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def _connect_in_progress(self) -> bool:
        """Whether one identity-owned startup currently holds admission."""
        return self._connect_attempt is not None

    @property
    def _state_notification_dispatching(self) -> bool:
        """Whether an identity-owned callback drain is active."""
        return self._state_notification_drain_owner is not None

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

    def on_bytes(self, cb: Callable[[bytes], None]) -> None:
        """Register a callback fired with each RAW byte chunk (only when :attr:`raw` is True).

        The binary counterpart of :meth:`on_line`, for framed/stream protocols (Meshtastic protobuf) whose
        bytes must not pass through the text line decoder. The subscriber (e.g. a MeshtasticBackend feeding a
        StreamFramer) owns reassembly/framing; this just hands over exactly what ``serial.read`` returned."""
        self._byte_callbacks.append(cb)

    def remove_byte_callback(self, cb: Callable[[bytes], None]) -> None:
        """Remove a previously-registered byte callback (idempotent — no error if absent), so a stream
        backend can fully detach on disconnect instead of leaking a dead callback."""
        try:
            self._byte_callbacks.remove(cb)
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

    def _release_connect_admission_locked(self, attempt: _ConnectAttempt) -> bool:
        """Release admission only while *attempt* still owns it; caller holds ``_io_lock``."""
        if self._connect_attempt is not attempt:
            return False
        self._connect_attempt = None
        return True

    def _transition_connect_failure_locked(
        self,
        attempt: _ConnectAttempt,
        *,
        quarantine: bool = False,
    ) -> tuple[bool, int]:
        """Publish ERROR only while this startup still owns its CONNECTING generation."""
        if (
            self._connect_attempt is not attempt
            or attempt.connecting_state_generation is None
            or self._state is not ConnectionState.CONNECTING
            or self._state_generation != attempt.connecting_state_generation
        ):
            return False, self._state_generation
        if quarantine:
            self._write_quarantined = True
        return self._transition_state_locked(
            ConnectionState.ERROR,
            reservation_owner=attempt,
        )

    def _abort_connect_attempt(self, attempt: _ConnectAttempt) -> int | None:
        """Settle one interrupted startup without touching a newer attempt.

        The original control-flow exception is owned by :meth:`connect`; every cleanup step here is
        best-effort so a close/join/observer failure cannot replace it. Admission is released only
        after this attempt's gate, reader and handles have been fenced from future I/O.
        """
        handles_to_close: list[object] = []
        unsettled_handles: list[object] = []
        # Admission losers must fail immediately; waiting for the active owner's lifecycle lock can
        # deadlock a caller that is also coordinating that owner's startup test/gate.
        with self._io_lock:
            if self._connect_attempt is not attempt:
                return attempt.aborted_state_generation
        with self._lifecycle_lock:
            with self._io_lock:
                if self._connect_attempt is not attempt:
                    return attempt.aborted_state_generation
                if not attempt.startup_started:
                    # This attempt only owned admission. In particular, an interrupted redundant
                    # connect must not close or quarantine the healthy connection it discovered.
                    self._release_connect_admission_locked(attempt)
                    return attempt.aborted_state_generation
                self._stop_event.set()
                self._write_quarantined = True
                reader_gate = attempt.reader_gate
                reader = attempt.reader_thread or self._read_thread

            if reader_gate is not None:
                try:
                    reader_gate.set()
                except BaseException:
                    pass
            if reader is not None and reader is not threading.current_thread():
                try:
                    if reader.is_alive():
                        reader.join(timeout=3.0)
                except BaseException:
                    pass

            with self._io_lock:
                if self._connect_attempt is not attempt:
                    return attempt.aborted_state_generation

                published = self._serial
                if published is not None:
                    self._serial = None
                    handles_to_close.append(published)
                candidate = attempt.candidate_serial
                if candidate is not None and all(
                    candidate is not handle for handle in handles_to_close
                ):
                    handles_to_close.append(candidate)

                if self._read_thread is reader:
                    try:
                        reader_alive = reader is not None and reader.is_alive()
                    except BaseException:
                        reader_alive = True
                    if not reader_alive:
                        self._read_thread = None

                owned_error_generation = self._owned_state_notification_generation(
                    attempt,
                    ConnectionState.ERROR,
                )
                if owned_error_generation is not None:
                    # A trace-time interruption can land after the ERROR transition reserves its
                    # notification but before the startup handler publishes that generation on the
                    # attempt. While this attempt still owns admission, that current ERROR is its
                    # startup outcome, so recover the already-reserved generation for later drain.
                    attempt.aborted_state_generation = owned_error_generation
                elif self._state is not ConnectionState.DISCONNECTED:
                    state_changed, state_generation = self._transition_state_locked(
                        ConnectionState.ERROR,
                        reservation_owner=attempt,
                    )
                    if state_changed:
                        attempt.aborted_state_generation = state_generation

            # Keep admission until detached resources have settled. A failed best-effort close is
            # retained as the connection's quarantined handle so the next connect can retry cleanup
            # instead of forgetting a potentially open exclusive port.
            for handle in handles_to_close:
                try:
                    handle.close()
                except BaseException:
                    unsettled_handles.append(handle)

            with self._io_lock:
                if self._connect_attempt is attempt:
                    if unsettled_handles and self._serial is None:
                        self._serial = unsettled_handles[0]
                    self._release_connect_admission_locked(attempt)

        return attempt.aborted_state_generation

    def connect(self) -> None:
        """Open the serial port and start the reader thread.

        Raises:
            serial.SerialException: If the port cannot be opened.
        """
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        attempt = _ConnectAttempt(
            lock_depths=self._capture_owned_lock_depths(*operation_locks)
        )
        try:
            self._run_connect_attempt(attempt)
        except BaseException:
            # Keep one outer control-flow boundary around claim, cleanup-carrier handling, lock
            # exit, and notification handoff. Trace-time interruption at any of those seams still
            # settles the identity-owned attempt before the original object is re-raised.
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            self._settle_interrupted_connect(attempt)
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            raise

    def _run_connect_attempt(self, attempt: _ConnectAttempt) -> None:
        """Run one startup; :meth:`connect` owns its outer control-flow interruption fence."""
        # Atomically claim admission before waiting for lifecycle serialization. This both rejects a
        # second queued opener and makes an apparent CONNECTED fast path wait for an in-progress
        # disconnect before it decides whether a fresh open is required.
        connected_notification: tuple[object, int, int] | None = None
        try:
            # The exception boundary starts before the identity claim. Even if a control-flow
            # interruption lands in a custom ``__setattr__`` immediately after publication, the
            # owned attempt is discoverable and can be settled by the handler below. A derived
            # boolean avoids a second assignment that could become permanently inconsistent.
            with self._io_lock:
                if self._connect_attempt is not None:
                    raise RuntimeError("Serial connection attempt already in progress")
                self._connect_attempt = attempt

            # Revalidate under lifecycle serialization. A disconnect may have owned the lock when
            # admission was claimed, so state observed before this point is not a stable fast path.
            with self._lifecycle_lock, self._io_lock:
                already_connected = self._state is ConnectionState.CONNECTED
                reader_reentry = (
                    not already_connected
                    and self._read_thread is threading.current_thread()
                    and self._read_thread.is_alive()
                )
                if already_connected or reader_reentry:
                    self._release_connect_admission_locked(attempt)
                    state_changed = False
                    connecting_state_generation = self._state_generation
                else:
                    # Publish ownership before changing lifecycle state. An interruption after this
                    # marker may safely tear down only the resources this startup goes on to own.
                    attempt.startup_started = True
                    state_changed, connecting_state_generation = (
                        self._transition_state_locked(
                            ConnectionState.CONNECTING,
                            reservation_owner=attempt,
                        )
                    )
                    attempt.connecting_state_generation = connecting_state_generation

            if reader_reentry:
                # Reject before publishing CONNECTING: the current reader must finish its error
                # teardown before any replacement can safely own the shared handle.
                raise RuntimeError("Cannot reconnect from the serial reader thread")
            if already_connected:
                self._safe_log(logging.WARNING, "Already connected to %s", self.port)
                return

            # Notify without holding the lifecycle lock. A callback can safely coordinate with
            # another lifecycle thread; any state change it causes is detected before OS open.
            if state_changed:
                self._emit_state_change(
                    ConnectionState.CONNECTING,
                    connecting_state_generation,
                    reservation_owner=attempt,
                )

            with self._lifecycle_lock:
                connected_notification = self._connect_locked(attempt)
        except _ConnectFailure as failure:
            try:
                if failure.state_changed:
                    self._emit_state_change(
                        ConnectionState.ERROR,
                        failure.state_generation,
                        error=failure.cause,
                        reservation_owner=attempt,
                    )
                else:
                    self._emit_error(
                        failure.cause,
                        expected_state=ConnectionState.ERROR,
                        state_generation=failure.state_generation,
                        reservation_owner=attempt,
                    )
            except BaseException:
                try:
                    # A failure observer can synchronously establish a replacement while the old
                    # notification owns the drainer, then interrupt. Resume delivery of the
                    # replacement's ready events without replacing the active exception.
                    self._drain_state_notifications()
                except BaseException:
                    pass
                raise
            else:
                # With no active observer exception, control flow originating in this final drain
                # is the primary outcome and must reach connect()'s outer settlement boundary.
                self._drain_state_notifications()
            raise failure.cause from None

        if connected_notification is not None:
            serial_handle, incarnation, state_generation = connected_notification
            try:
                self._emit_connected_state_change(
                    serial_handle,
                    incarnation,
                    state_generation,
                    reservation_owner=attempt,
                )
            except BaseException:
                try:
                    self._drain_state_notifications()
                except BaseException:
                    pass
                raise

    def _settle_interrupted_connect(self, attempt: _ConnectAttempt) -> None:
        """Best-effort finalization that never replaces the active control-flow exception."""
        committed_notification = attempt.connected_notification
        if committed_notification is not None:
            try:
                with self._io_lock:
                    self._release_connect_admission_locked(attempt)
            except BaseException:
                try:
                    with self._io_lock:
                        if self._connect_attempt is attempt:
                            object.__setattr__(self, "_connect_attempt", None)
                except BaseException:
                    pass
            try:
                self._emit_connected_state_change(
                    *committed_notification,
                    reservation_owner=attempt,
                )
            except BaseException:
                pass
            try:
                self._drain_state_notifications()
            except BaseException:
                pass
            return

        aborted_generation = attempt.aborted_state_generation
        try:
            settled_generation = self._abort_connect_attempt(attempt)
            if settled_generation is not None:
                aborted_generation = settled_generation
        except BaseException:
            # Abort recorded its generation before its final admission clear. A hostile cleanup
            # hook cannot replace the already-active startup interruption.
            pass
        if attempt.aborted_state_generation is not None:
            aborted_generation = attempt.aborted_state_generation
        try:
            with self._io_lock:
                if self._connect_attempt is attempt:
                    object.__setattr__(self, "_connect_attempt", None)
        except BaseException:
            pass
        try:
            if aborted_generation is not None:
                self._emit_state_change(
                    ConnectionState.ERROR,
                    aborted_generation,
                    reservation_owner=attempt,
                )
        except BaseException:
            pass
        try:
            self._drain_state_notifications()
        except BaseException:
            pass

    def _connect_locked(
        self,
        attempt: _ConnectAttempt,
    ) -> tuple[object, int, int] | None:
        """Implement one connect while ``_lifecycle_lock`` is held by the caller."""
        with self._io_lock:
            startup_is_current = (
                self._connect_attempt is attempt
                and self._state is ConnectionState.CONNECTING
            )
        if not startup_is_current:
            exc = serial.SerialException("Serial connection changed before startup")
            with self._io_lock:
                self._release_connect_admission_locked(attempt)
                state_generation = self._state_generation
            raise _ConnectFailure(
                exc,
                state_generation,
                False,
            ) from exc

        # Fully tear down any prior reader thread + handle before reopening. A write()/interrupt error
        # only sets state=ERROR without stopping the reader (the port may still be readable), so a stale
        # reader can still be blocked in read() on the old handle. If we reopened without stopping it, the
        # moment we close the old handle that reader wakes with a SerialException, enters its error path,
        # and calls _release_serial() — which would null out the FRESHLY-opened port and destroy the new
        # connection (a race, since connect() doesn't hold _io_lock while reopening). Stop+join it first.
        # This also frees the OS port so the re-open can't hit "Access is denied" on exclusive COM ports.
        if self._read_thread is not None and self._read_thread.is_alive():
            self._stop_event.set()
            if self._read_thread is threading.current_thread():
                # A reader error callback runs on the reader itself. Reopening there would either
                # self-join or let the old loop tear down the fresh handle when the callback
                # returns.
                # Reject this reentrant lifecycle change without wedging later reconnect attempts.
                exc = RuntimeError("Cannot reconnect from the serial reader thread")
                with self._io_lock:
                    state_changed, state_generation = (
                        self._transition_connect_failure_locked(attempt)
                    )
                    attempt.aborted_state_generation = state_generation
                    self._release_connect_admission_locked(attempt)
                raise _ConnectFailure(
                    exc,
                    state_generation,
                    state_changed,
                ) from exc
            self._read_thread.join(timeout=3.0)
            if self._read_thread.is_alive():
                exc = serial.SerialException("Previous serial reader did not stop")
                with self._io_lock:
                    state_changed, state_generation = (
                        self._transition_connect_failure_locked(
                            attempt,
                            quarantine=True,
                        )
                    )
                    attempt.aborted_state_generation = state_generation
                    self._release_connect_admission_locked(attempt)
                raise _ConnectFailure(
                    exc,
                    state_generation,
                    state_changed,
                ) from exc
        prior_released = self._release_serial()
        if prior_released is False:
            exc = serial.SerialException("Previous serial handle could not be closed")
            with self._io_lock:
                state_changed, state_generation = (
                    self._transition_connect_failure_locked(
                        attempt,
                        quarantine=True,
                    )
                )
                attempt.aborted_state_generation = state_generation
                self._release_connect_admission_locked(attempt)
            raise _ConnectFailure(
                exc,
                state_generation,
                state_changed,
            ) from exc
        self._read_thread = None

        reader_gate: threading.Event | None = None
        candidate_serial: serial.Serial | None = None
        try:
            # Open WITHOUT letting the adapter's DTR/RTS lines pulse the ESP32's EN/GPIO0 on connect.
            # pyserial asserts both by default; on CYD panels (esp. the CH340K 2-USB / Guition boards)
            # that lack the auto-reset transistor pair, an asserted DTR+RTS at open yanks GPIO0/EN low
            # and drops the chip into ROM download mode ("waiting for download") — the firmware never
            # runs and the display stays blank. That is exactly the "CYD shows no GUI" report: simply
            # connecting to monitor a CYD was bricking its screen until power-cycle. Deassert both first
            # so opening the port leaves the running firmware (and its on-screen GUI) undisturbed.
            attempt.candidate_serial = serial.Serial()
            candidate_serial = attempt.candidate_serial
            candidate_serial.port = self.port
            candidate_serial.baudrate = self.baud
            candidate_serial.timeout = self.timeout
            candidate_serial.write_timeout = self.timeout
            candidate_serial.dtr = False
            candidate_serial.rts = False
            candidate_serial.open()
            # Keep the opened candidate local until the lifecycle still owns CONNECTING. A
            # concurrent disconnect may cancel a blocked OS open; publishing only here prevents
            # the resumed open from becoming an unreachable exclusive COM handle.
            with self._io_lock:
                if (
                    self._connect_attempt is not attempt
                    or self._state is not ConnectionState.CONNECTING
                    or self._serial is not None
                ):
                    raise serial.SerialException("Serial connection changed during open")
                self._serial = candidate_serial
                self._stop_event.clear()
            reader_gate = threading.Event()
            attempt.reader_gate = reader_gate
            attempt.reader_loop_attempt = _ReaderLoopAttempt(
                active_failure=_ReaderFailureAttempt(
                    (
                        candidate_serial,
                        self._serial_incarnation,
                        ConnectionState.CONNECTING,
                        self._state_generation,
                    )
                )
            )
            self._read_thread = threading.Thread(
                target=self._reader_loop_after_connect,
                args=(reader_gate, attempt.reader_loop_attempt),
                name=f"serial-reader-{self.port}",
                daemon=True,
            )
            attempt.reader_thread = self._read_thread
            self._read_thread.start()
            with self._io_lock:
                opened_serial = self._serial
                if opened_serial is None:
                    raise serial.SerialException("Serial connection changed during startup")
                opened_is_open = opened_serial.is_open
                if (
                    self._serial is not opened_serial
                    or opened_is_open is not True
                    or self._state is not ConnectionState.CONNECTING
                ):
                    raise serial.SerialException("Serial connection changed during startup")
                self._serial_incarnation += 1
                self._write_quarantined = False
                self._write_transaction = None
                incarnation = self._serial_incarnation
                _, connected_state_generation = self._transition_state_locked(
                    ConnectionState.CONNECTED,
                    reservation_owner=attempt,
                )
                attempt.reader_loop_attempt.active_failure = _ReaderFailureAttempt(
                    (
                        opened_serial,
                        incarnation,
                        ConnectionState.CONNECTED,
                        connected_state_generation,
                    )
                )
                if attempt.reader_loop_attempt.escaped:
                    raise serial.SerialException("Serial reader exited during startup")
            # Start the reader before external CONNECTED callbacks. Public connect() emits those
            # only after releasing the lifecycle lock, so a reply callback can safely disconnect
            # while the CONNECTED callback waits. Its state/incarnation token prevents stale
            # fan-out.
            reader_gate.set()
            start_reader = self._connection_incarnation_is_current(
                opened_serial,
                incarnation,
            )
            if start_reader:
                self._safe_log(logging.INFO, "Connected to %s @ %d baud", self.port, self.baud)
            with self._io_lock:
                start_reader = start_reader and (
                    self._serial is opened_serial
                    and self._serial_incarnation == incarnation
                    and not self._write_quarantined
                    and self._state is ConnectionState.CONNECTED
                )
                if not start_reader:
                    self._stop_event.set()
                reader_gate.set()
                attempt.connected_notification = (
                    opened_serial,
                    incarnation,
                    connected_state_generation,
                )
                self._release_connect_admission_locked(attempt)
            if start_reader:
                return opened_serial, incarnation, connected_state_generation
            # The CONNECTED transition already reserved notification order. Return its token even
            # after a reader race made it stale so public connect() can ready and drain (skip) that
            # queue entry instead of blocking every later ERROR/DISCONNECTED event behind it.
            return opened_serial, incarnation, connected_state_generation
        except serial.SerialException as exc:
            self._stop_event.set()
            if reader_gate is not None:
                reader_gate.set()
            # Retain startup ownership through teardown and the ERROR transition. Releasing the
            # in-progress flag first would let a second connect publish a fresh handle that this old
            # exception path could then close.
            self._release_serial()
            self._close_candidate_serial(candidate_serial)
            with self._io_lock:
                state_changed, state_generation = (
                    self._transition_connect_failure_locked(attempt)
                )
                attempt.aborted_state_generation = state_generation
                self._release_connect_admission_locked(attempt)
            raise _ConnectFailure(
                exc,
                state_generation,
                state_changed,
            ) from exc
        except Exception as exc:
            # A NON-SerialException after open() succeeds — most plausibly _read_thread.start()
            # raising RuntimeError under thread/handle exhaustion — used to propagate uncaught,
            # leaving the freshly-opened exclusive COM handle open with no owner to close it. On
            # Windows the port then stays locked ("Access is denied") until exit. Release it first.
            self._stop_event.set()
            if reader_gate is not None:
                reader_gate.set()
            self._release_serial()
            self._close_candidate_serial(candidate_serial)
            with self._io_lock:
                state_changed, state_generation = (
                    self._transition_connect_failure_locked(attempt)
                )
                attempt.aborted_state_generation = state_generation
                self._release_connect_admission_locked(attempt)
            raise _ConnectFailure(
                exc,
                state_generation,
                state_changed,
            ) from exc

    def disconnect(self) -> None:
        """Stop the reader thread and close the port."""
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        attempt = _DisconnectAttempt(
            lock_depths=self._capture_owned_lock_depths(*operation_locks)
        )
        try:
            self._run_disconnect_attempt(attempt)
        except BaseException:
            # As with connect/write, a trace exception can interrupt ``with`` exit bytecode after
            # an RLock was acquired. Release only locks owned by this thread, ready any transition
            # it already completed, and preserve the exact active exception.
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            self._settle_interrupted_disconnect(attempt)
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            raise

    def _run_disconnect_attempt(self, attempt: _DisconnectAttempt) -> None:
        """Run one disconnect; :meth:`disconnect` owns interruption settlement."""
        state_generation: int | None
        with self._lifecycle_lock:
            with self._io_lock:
                attempt.identity = (
                    self._serial,
                    self._serial_incarnation,
                    self._state,
                    self._state_generation,
                    self._read_thread,
                )
            state_generation = self._disconnect_locked(attempt)
            attempt.disconnected_state_generation = state_generation
        if state_generation is not None:
            try:
                self._emit_state_change(
                    ConnectionState.DISCONNECTED,
                    state_generation,
                    reservation_owner=attempt,
                )
            except BaseException:
                try:
                    self._drain_state_notifications()
                except BaseException:
                    pass
                raise
            self._safe_log(logging.INFO, "Disconnected from %s", self.port)

    def _settle_interrupted_disconnect(self, attempt: _DisconnectAttempt) -> None:
        """Finish only this disconnect identity and preserve the active exception."""
        state_generation = attempt.disconnected_state_generation
        try:
            settled_generation = self._complete_interrupted_disconnect(attempt)
            if settled_generation is not None:
                state_generation = settled_generation
        except BaseException:
            pass
        self._release_locks_owned_by_current_thread(
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
            baseline_depths=attempt.lock_depths,
        )

        try:
            owned_generation = self._owned_state_notification_generation(
                attempt,
                ConnectionState.DISCONNECTED,
            )
            if owned_generation is not None:
                state_generation = owned_generation
        except BaseException:
            pass
        if state_generation is not None:
            try:
                self._emit_state_change(
                    ConnectionState.DISCONNECTED,
                    state_generation,
                    reservation_owner=attempt,
                )
            except BaseException:
                pass
        try:
            self._drain_state_notifications()
        except BaseException:
            pass

    def _complete_interrupted_disconnect(
        self,
        attempt: _DisconnectAttempt,
    ) -> int | None:
        """Best-effort identity-fenced teardown after disconnect control flow escapes."""
        identity = attempt.identity
        if identity is None:
            return None
        serial_handle, incarnation, starting_state, starting_generation, reader = identity
        if starting_state is ConnectionState.DISCONNECTED:
            return None

        detached_handle: object | None = None
        unsettled_handle: object | None = None
        state_generation: int | None = attempt.disconnected_state_generation
        with self._lifecycle_lock:
            with self._io_lock:
                if not self._disconnect_identity_is_current_locked(attempt):
                    return state_generation
                # Block queued writers before closing outside the short I/O critical section.
                self._stop_event.set()
                self._write_quarantined = True

            if reader is not None and reader is not threading.current_thread():
                try:
                    if reader.is_alive():
                        reader.join(timeout=3.0)
                except BaseException:
                    pass

            with self._io_lock:
                if not self._disconnect_identity_is_current_locked(attempt):
                    return state_generation
                if self._serial is serial_handle:
                    detached_handle = serial_handle
                    self._serial = None

            if detached_handle is not None:
                try:
                    detached_is_open = detached_handle.is_open
                    with self._io_lock:
                        if not self._disconnect_identity_is_current_locked(attempt):
                            return state_generation
                    if detached_is_open is True:
                        detached_handle.close()
                    elif detached_is_open is not False:
                        unsettled_handle = detached_handle
                except BaseException:
                    unsettled_handle = detached_handle

            with self._io_lock:
                if not self._disconnect_identity_is_current_locked(attempt):
                    return state_generation
                if unsettled_handle is not None and self._serial is None:
                    # Never forget a possibly-open exclusive port. A later connect retries close.
                    self._serial = unsettled_handle
                if self._state is ConnectionState.DISCONNECTED:
                    owned_generation = self._owned_state_notification_generation(
                        attempt,
                        ConnectionState.DISCONNECTED,
                    )
                    if owned_generation is not None:
                        state_generation = owned_generation
                else:
                    changed, generation = self._transition_state_locked(
                        ConnectionState.DISCONNECTED,
                        reservation_owner=attempt,
                    )
                    if changed:
                        state_generation = generation

        return state_generation

    def _disconnect_identity_is_current_locked(
        self,
        attempt: _DisconnectAttempt,
    ) -> bool:
        """Fence interrupted cleanup from a newer serial incarnation."""
        identity = attempt.identity
        if identity is None:
            return False
        serial_handle, incarnation, starting_state, starting_generation, _reader = identity
        if self._serial_incarnation != incarnation:
            return False
        if self._serial is not None and self._serial is not serial_handle:
            return False
        if self._state_generation == starting_generation:
            return self._state is starting_state
        owned_generation = self._owned_state_notification_generation(
            attempt,
            ConnectionState.DISCONNECTED,
        )
        return (
            owned_generation == self._state_generation
            and self._state is ConnectionState.DISCONNECTED
        )

    def _disconnect_locked(self, attempt: _DisconnectAttempt) -> int | None:
        """Implement one disconnect while ``_lifecycle_lock`` is held by the caller."""
        identity = attempt.identity
        if identity is None or identity[2] is ConnectionState.DISCONNECTED:
            return None
        self._stop_event.set()
        # Join the reader thread OUTSIDE the I/O lock (the join can take up to 3s; holding the lock
        # there would needlessly block writers). Then take the lock only for the handle teardown so it
        # cannot interleave with an in-flight write().
        reader = identity[4]
        if (
            reader
            and reader.is_alive()
            and reader is not threading.current_thread()
        ):
            reader.join(timeout=3.0)
        with self._io_lock:
            if not self._disconnect_identity_is_current_locked(attempt):
                return None
            self._release_serial(expected_identity=(identity[0], identity[1]))
            if not self._disconnect_identity_is_current_locked(attempt):
                return None
            state_changed, state_generation = self._transition_state_locked(
                ConnectionState.DISCONNECTED,
                reservation_owner=attempt,
            )
        return state_generation if state_changed else None

    def _release_serial(
        self,
        *,
        expected_identity: tuple[object | None, int] | None = None,
    ) -> bool:
        """Close and drop the serial handle under the I/O lock so a later connect() can reopen the
        port cleanly. Return false and retain the handle if ordinary cleanup fails, so a future
        connect cannot forget a potentially open exclusive port."""
        with self._io_lock:
            handle = self._serial
            incarnation = self._serial_incarnation
            if expected_identity is not None and (
                handle is not expected_identity[0]
                or incarnation != expected_identity[1]
            ):
                return True
            if handle is None:
                return True
            try:
                handle_is_open = handle.is_open
                if (
                    self._serial is not handle
                    or self._serial_incarnation != incarnation
                ):
                    return True
                if handle_is_open is True:
                    handle.close()
                elif handle_is_open is not False:
                    return False
            except Exception:
                return False
            if (
                self._serial is handle
                and self._serial_incarnation == incarnation
            ):
                self._serial = None
            return True

    def _close_candidate_serial(self, candidate: serial.Serial | None) -> bool:
        """Close a local candidate, retaining ownership when ordinary cleanup fails."""
        if candidate is None:
            return True
        try:
            candidate.close()
        except Exception:
            with self._io_lock:
                if self._serial is None:
                    self._serial = candidate
                self._write_quarantined = True
            return False
        with self._io_lock:
            if self._serial is candidate:
                self._serial = None
        return True

    # ── I/O ──────────────────────────────────────────────────────────

    def _command_payload(self, data: str) -> bytes:
        """Validate and encode one text command before any transport I/O."""
        cleaned = data.rstrip("\r\n")
        # C0 controls (0x00–0x1F), DEL (0x7F): never legitimate inside a single command. Validate
        # the input up front (before touching the port) so bad input is rejected fast.
        bad = [ch for ch in cleaned if ord(ch) < 0x20 or ord(ch) == 0x7F]
        if bad:
            raise ValueError(
                f"Refusing to send command with embedded control character(s) "
                f"{[hex(ord(c)) for c in bad]} — possible command injection"
            )
        return (cleaned + self.line_ending).encode(self.encoding)

    def write_receipt(self, data: str) -> WriteReceipt:
        """Attempt one text-command write and return payload-free delivery truth.

        Validation, encoding, and unavailable-connection errors happen before transport I/O and
        keep their existing exception behavior. Once ``serial.write`` is entered, all outcomes are
        returned as a finite receipt and the connection is quarantined unless host completion is
        established.
        """
        return self._write_payload_attempt(self._command_payload(data)).receipt

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
        self._raise_legacy_failure(self._write_payload_attempt(self._command_payload(data)))

    def send_interrupt_receipt(self) -> WriteReceipt:
        """Attempt one raw Ctrl-C write and return payload-free delivery truth."""
        return self._write_payload_attempt(b"\x03").receipt

    def send_interrupt(self) -> None:
        """Send a raw Ctrl-C (0x03) to interrupt a blocking command — e.g. a long-running Flipper CLI command
        that otherwise holds the shell until it finishes.

        :meth:`write` deliberately rejects every control character (command-injection guard), so it can't send
        0x03. This is the narrow, explicit exception: it writes the single byte 0x03 — and nothing else, no
        line terminator — bypassing that guard for this one documented control code only.

        Raises:
            RuntimeError: If not connected.
        """
        self._raise_legacy_failure(self._write_payload_attempt(b"\x03"))

    def write_bytes_receipt(self, payload: bytes) -> WriteReceipt:
        """Attempt one verbatim binary write and return payload-free delivery truth."""
        return self._write_payload_attempt(bytes(payload)).receipt

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
        self._raise_legacy_failure(self._write_payload_attempt(bytes(payload)))

    def _write_payload_attempt(self, payload: bytes) -> _WriteAttempt:
        """Perform one write behind a cross-frame control-flow settlement boundary."""
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        transaction = _WriteTransaction(
            requested=len(payload),
            lock_depths=self._capture_owned_lock_depths(*operation_locks),
        )
        try:
            return self._run_write_payload_attempt(payload, transaction)
        except BaseException as exc:
            # A trace/signal exception on a nested ``try`` header can escape every handler in that
            # same frame. This wrapper frame remains active across the call and owns final receipt,
            # quarantine and notification recovery for every post-entry escape.
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=transaction.lock_depths,
            )
            if (
                transaction.write_entered
                or self._write_transaction is transaction
            ):
                self._settle_escaped_write_interruption(transaction, exc)
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=transaction.lock_depths,
            )
            raise

    def _run_write_payload_attempt(
        self,
        payload: bytes,
        transaction: _WriteTransaction,
    ) -> _WriteAttempt:
        """Run one payload transaction; the public private wrapper settles escaped control flow."""
        # Keep the lifetime barrier through flush: disconnect cannot close/null the handle while
        # this attempt may still be performing I/O. State/error callbacks are deliberately deferred
        # until after the lock is released because callbacks may synchronously call disconnect().
        with self._io_lock:
            serial_handle = self._serial
            incarnation = self._serial_incarnation
            starting_state_generation = self._state_generation
            starting_state = self._state
            starting_quarantine = self._write_quarantined
            if serial_handle is None:
                raise RuntimeError(f"Not connected to {self.port}")
            if starting_quarantine:
                raise RuntimeError("Serial transport is quarantined; reconnect required")
            # ``connect()`` publishes CONNECTED while its outer call is still in progress. A
            # CONNECTED callback may perform its first synchronous write, but ERROR callbacks and
            # every earlier startup phase must remain unable to touch the transport.
            if starting_state is not ConnectionState.CONNECTED:
                raise RuntimeError(f"Not connected to {self.port}")

            active_transaction = self._write_transaction
            if (
                active_transaction is not None
                and self._write_incarnation_is_current_locked(active_transaction)
            ):
                raise RuntimeError("Serial write already in progress")
            transaction.serial_handle = serial_handle
            transaction.incarnation = incarnation
            transaction.starting_state_generation = starting_state_generation
            self._write_transaction = transaction

            serial_is_open = serial_handle.is_open
            if (
                self._serial is not serial_handle
                or self._serial_incarnation != incarnation
                or self._state_generation != starting_state_generation
                or self._state is not starting_state
                or self._write_quarantined is not starting_quarantine
            ):
                raise RuntimeError("Serial connection changed before write")
            if serial_is_open is not True:
                raise RuntimeError(f"Not connected to {self.port}")

            # The marker precedes the helper call. A control exception before actual adapter entry
            # is conservatively uncertain; the call opcode remains protected by this ``with`` so an
            # exception escaping any nested helper header still releases the non-reentrant lock.
            transaction.write_entered = True
            attempt = self._perform_write_transport_attempt(
                serial_handle,
                payload,
                transaction,
            )
            transaction.attempt = attempt
            if attempt.receipt.disposition is not WriteDisposition.HOST_WRITE_COMPLETE:
                failure_is_current = (
                    self._serial is serial_handle
                    and self._serial_incarnation == transaction.incarnation
                    and self._state is ConnectionState.CONNECTED
                    and self._state_generation == transaction.starting_state_generation
                    and not self._write_quarantined
                )
                if failure_is_current:
                    self._write_quarantined = True
                    state_changed, state_generation = self._transition_state_locked(
                        ConnectionState.ERROR,
                        reservation_owner=transaction,
                    )
                else:
                    state_changed = False
                    state_generation = None
            else:
                state_changed = False
                state_generation = None
            self._release_write_transaction_locked(
                transaction,
                completed=(
                    attempt.receipt.disposition
                    is WriteDisposition.HOST_WRITE_COMPLETE
                ),
            )

        if attempt.cause is not None and state_generation is not None:
            self._emit_write_failure(
                attempt,
                serial_handle,
                transaction.incarnation,
                state_generation,
                state_changed,
                reservation_owner=transaction,
            )
        else:
            # Never log the payload: the text path can carry a Dead Man's Switch password and the
            # raw path can carry protocol credentials. Byte count is sufficient diagnostics.
            self._safe_log(
                logging.DEBUG,
                "TX [%s]: <%d bytes>",
                self.port,
                transaction.requested,
            )
        return attempt

    def _perform_write_transport_attempt(
        self,
        serial_handle: object,
        payload: bytes,
        transaction: _WriteTransaction,
    ) -> _WriteAttempt:
        """Call write/flush exactly once; the caller's ``with`` owns lock cleanup on BaseException."""
        try:
            transaction.reported_value = serial_handle.write(payload)
        except Exception as exc:
            return _WriteAttempt(
                WriteReceipt(
                    WriteDisposition.DELIVERY_UNCERTAIN,
                    transaction.requested,
                    safe_error_code=WriteErrorCode.WRITE_ERROR,
                ),
                exc,
            )

        reported = transaction.reported_value
        if (
            type(reported) is not int
            or reported < 0
            or reported > transaction.requested
        ):
            receipt = WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                transaction.requested,
                None,
                WriteErrorCode.INVALID_COUNT,
            )
            return _WriteAttempt(receipt, IncompleteSerialWrite(receipt))
        if reported == 0 and transaction.requested > 0:
            receipt = WriteReceipt(
                WriteDisposition.DEFINITELY_NOT_WRITTEN,
                transaction.requested,
                reported,
                WriteErrorCode.ZERO_WRITE,
            )
            return _WriteAttempt(receipt, IncompleteSerialWrite(receipt))
        if reported < transaction.requested:
            receipt = WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                transaction.requested,
                reported,
                WriteErrorCode.SHORT_WRITE,
            )
            return _WriteAttempt(receipt, IncompleteSerialWrite(receipt))

        if (
            self._serial is not serial_handle
            or self._serial_incarnation != transaction.incarnation
            or self._state is not ConnectionState.CONNECTED
            or self._state_generation != transaction.starting_state_generation
            or self._write_quarantined
        ):
            # The adapter returned the full count, but a reentrant lifecycle hook replaced or
            # invalidated the assigned incarnation before flush. Do not touch the new transport;
            # without a completed flush the old write remains delivery-uncertain.
            receipt = WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                transaction.requested,
                reported,
                WriteErrorCode.FLUSH_ERROR,
            )
            return _WriteAttempt(receipt, IncompleteSerialWrite(receipt))

        try:
            serial_handle.flush()
        except Exception as exc:
            return _WriteAttempt(
                WriteReceipt(
                    WriteDisposition.DELIVERY_UNCERTAIN,
                    transaction.requested,
                    reported,
                    WriteErrorCode.FLUSH_ERROR,
                ),
                exc,
            )

        if not self._write_incarnation_is_current_locked(transaction):
            # A successful adapter return is not proof that it flushed the original incarnation:
            # a reentrant flush hook may have disconnected/reopened this object in the meantime.
            receipt = WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                transaction.requested,
                reported,
                WriteErrorCode.FLUSH_ERROR,
            )
            return _WriteAttempt(receipt, IncompleteSerialWrite(receipt))

        attempt = _WriteAttempt(
            WriteReceipt(
                WriteDisposition.HOST_WRITE_COMPLETE,
                transaction.requested,
                reported,
            )
        )
        # Only this cross-frame marker authorizes HOST_WRITE_COMPLETE during outer settlement.
        transaction.attempt = attempt
        transaction.completion_published = True
        return attempt

    def _settle_escaped_write_interruption(
        self,
        transaction: _WriteTransaction,
        exc: BaseException,
    ) -> None:
        """Best-effort finite settlement for a control exception escaping the inner write frame."""
        if not transaction.write_entered:
            try:
                with self._io_lock:
                    self._release_write_transaction_locked(
                        transaction,
                        completed=True,
                    )
            except BaseException:
                pass
            return

        if transaction.completion_published and transaction.attempt is not None:
            receipt = transaction.attempt.receipt
        else:
            receipt = self._receipt_for_interrupted_write(
                transaction.requested,
                transaction.reported_value,
            )
        attempt = _WriteAttempt(receipt, exc)
        self._attach_receipt_to_exception(
            exc,
            receipt,
            suppress_control=True,
        )

        if transaction.completion_published:
            try:
                with self._io_lock:
                    self._release_write_transaction_locked(
                        transaction,
                        completed=True,
                    )
            except BaseException:
                pass

        failure_generation: int | None = None
        if not transaction.completion_published and transaction.serial_handle is not None:
            try:
                failure_generation = self._owned_state_notification_generation(
                    transaction,
                    ConnectionState.ERROR,
                )
            except BaseException:
                pass
            try:
                with self._io_lock:
                    if (
                        failure_generation is None
                        and self._serial is transaction.serial_handle
                        and self._serial_incarnation == transaction.incarnation
                        and self._state is ConnectionState.CONNECTED
                        and self._state_generation
                        == transaction.starting_state_generation
                    ):
                        self._write_quarantined = True
                        try:
                            changed, generation = self._transition_state_locked(
                                ConnectionState.ERROR,
                                reservation_owner=transaction,
                            )
                        except BaseException:
                            changed = False
                            generation = None
                        if changed:
                            failure_generation = generation
            except BaseException:
                pass

            # A first recovery step can itself be interrupted after completing the transition.
            # Re-read its finite generation under the failed-incarnation fence before notifying.
            if failure_generation is None:
                try:
                    failure_generation = self._owned_state_notification_generation(
                        transaction,
                        ConnectionState.ERROR,
                    )
                except BaseException:
                    pass

            try:
                with self._io_lock:
                    self._release_write_transaction_locked(transaction)
            except BaseException:
                pass

        if failure_generation is not None and transaction.serial_handle is not None:
            try:
                self._emit_write_failure(
                    attempt,
                    transaction.serial_handle,
                    transaction.incarnation,
                    failure_generation,
                    True,
                    reservation_owner=transaction,
                )
            except BaseException:
                pass
        try:
            self._drain_state_notifications()
        except BaseException:
            pass

    def _write_incarnation_is_current_locked(
        self,
        transaction: _WriteTransaction,
    ) -> bool:
        """Return whether a transaction still names the writable lifecycle generation."""
        return (
            self._serial is transaction.serial_handle
            and self._serial_incarnation == transaction.incarnation
            and self._state is ConnectionState.CONNECTED
            and self._state_generation == transaction.starting_state_generation
            and not self._write_quarantined
        )

    def _release_write_transaction_locked(
        self,
        transaction: _WriteTransaction,
        *,
        completed: bool = False,
    ) -> bool:
        """Release only this admission, and only after success or a fail-closed fence."""
        if self._write_transaction is not transaction:
            return False
        if not completed and self._write_incarnation_is_current_locked(transaction):
            # Recovery could not yet quarantine the affected incarnation. Retaining admission is
            # the bounded fail-closed result: later writers reject without touching the adapter.
            return False
        self._write_transaction = None
        return True

    @staticmethod
    def _receipt_for_interrupted_write(
        requested: int,
        reported: object,
    ) -> WriteReceipt:
        """Conservatively classify an interruption anywhere after write entry."""
        if type(reported) is not int or reported < 0 or reported > requested:
            return WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                requested,
                safe_error_code=(
                    WriteErrorCode.WRITE_ERROR
                    if reported is _NO_WRITE_RESULT
                    else WriteErrorCode.INVALID_COUNT
                ),
            )
        if reported == 0 and requested > 0:
            return WriteReceipt(
                WriteDisposition.DEFINITELY_NOT_WRITTEN,
                requested,
                reported,
                WriteErrorCode.ZERO_WRITE,
            )
        if reported < requested:
            return WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                requested,
                reported,
                WriteErrorCode.SHORT_WRITE,
            )
        return WriteReceipt(
            WriteDisposition.DELIVERY_UNCERTAIN,
            requested,
            reported,
            WriteErrorCode.FLUSH_ERROR,
        )

    @staticmethod
    def _attach_receipt_to_exception(
        exc: BaseException,
        receipt: WriteReceipt,
        *,
        suppress_control: bool = False,
    ) -> bool:
        """Attach finite delivery truth without invoking a subclass's attribute hooks."""
        try:
            # Calling even ``BaseException.__getattribute__`` can still honor a hostile subclass
            # data descriptor named ``__dict__``. Invoke BaseException's concrete getset descriptor
            # directly so neither ``__getattribute__`` nor subclass descriptors participate.
            error_state = BaseException.__dict__["__dict__"].__get__(exc, BaseException)
            error_state["receipt"] = receipt
        except Exception:
            return False
        except BaseException:
            if suppress_control:
                return False
            raise
        return True

    @staticmethod
    def _legacy_exception_allows_receipt_attachment(exc: BaseException) -> bool:
        """Statically reject legacy exception classes whose receipt write could run user code."""
        try:
            exception_type = type(exc)
            mro = type.__getattribute__(exception_type, "__mro__")
            for owner in mro:
                if owner is BaseException:
                    break
                namespace = type.__getattribute__(owner, "__dict__")
                if "__setattr__" in namespace or "receipt" in namespace:
                    return False
        except Exception:
            return False
        return True

    @staticmethod
    def _raise_legacy_failure(attempt: _WriteAttempt) -> None:
        """Preserve legacy ``None``/exception behavior without repeating the write attempt."""
        if attempt.cause is None:
            return
        # Existing unmodified SerialException/OSError classes keep their original exception
        # instance. A custom setter or receipt descriptor is never invoked: those classes receive
        # the serial-compatible wrapper instead, preserving the failed transport incarnation.
        if (
            not SerialConnection._legacy_exception_allows_receipt_attachment(attempt.cause)
            or not SerialConnection._attach_receipt_to_exception(
                attempt.cause,
                attempt.receipt,
            )
        ):
            raise IncompleteSerialWrite(attempt.receipt) from attempt.cause
        raise attempt.cause

    def _emit_write_failure(
        self,
        attempt: _WriteAttempt,
        failed_serial: object,
        incarnation: int,
        state_generation: int,
        state_changed: bool,
        *,
        reservation_owner: object,
    ) -> None:
        """Emit sanitized failure callbacks only while their failed incarnation is current."""
        receipt = attempt.receipt
        receipt_facts = (
            receipt.disposition,
            receipt.bytes_requested,
            receipt.bytes_reported,
            receipt.safe_error_code,
        )
        if state_changed:
            self._ready_state_notification(
                ConnectionState.ERROR,
                state_generation,
                kind="write_failure",
                serial_handle=failed_serial,
                incarnation=incarnation,
                write_receipt_facts=receipt_facts,
                reservation_owner=reservation_owner,
            )
        else:
            self._emit_error(
                None,
                expected_state=ConnectionState.ERROR,
                state_generation=state_generation,
                kind="write_failure",
                serial_handle=failed_serial,
                incarnation=incarnation,
                write_receipt_facts=receipt_facts,
            )

    @staticmethod
    def _safe_log(level: int, message: str, *args: object) -> None:
        """Keep observer failures from changing transport or lifecycle outcomes."""
        try:
            log.log(level, message, *args)
        except Exception:
            # A hostile/broken handler is an observer failure, not a transport outcome. Do not log
            # that exception through another handler and do not invite a replay of completed bytes.
            pass

    @staticmethod
    def _capture_owned_lock_depths(*locks: object) -> tuple[int, ...]:
        """Snapshot this thread's recursion depth before one recoverable operation."""
        depths: list[int] = []
        for lock in locks:
            try:
                recursion_count = getattr(lock, "_recursion_count", None)
                depth = recursion_count() if callable(recursion_count) else 0
                depths.append(depth if type(depth) is int and depth >= 0 else 0)
            except Exception:
                depths.append(0)
        return tuple(depths)

    @staticmethod
    def _release_locks_owned_by_current_thread(
        *locks: object,
        baseline_depths: tuple[int, ...] | None = None,
    ) -> None:
        """Best-effort unwind RLocks only to this operation's captured entry depth."""
        targets = baseline_depths or (0,) * len(locks)
        for index, lock in enumerate(locks):
            target_depth = targets[index] if index < len(targets) else 0
            try:
                is_owned = getattr(lock, "_is_owned", None)
                recursion_count = getattr(lock, "_recursion_count", None)
                release = getattr(lock, "release", None)
                if (
                    not callable(is_owned)
                    or not callable(recursion_count)
                    or not callable(release)
                ):
                    continue
                while is_owned() and recursion_count() > target_depth:
                    release()
            except BaseException:
                pass

    # ── Internal ─────────────────────────────────────────────────────

    def _connection_incarnation_is_current(
        self,
        serial_handle: object,
        incarnation: int,
        state_generation: int | None = None,
    ) -> bool:
        """Return whether a newly published connection is still the active incarnation."""
        with self._io_lock:
            return (
                self._serial is serial_handle
                and self._serial_incarnation == incarnation
                and (
                    state_generation is None
                    or self._state_generation == state_generation
                )
                and not self._write_quarantined
                and self._state is ConnectionState.CONNECTED
            )

    def _emit_connected_state_change(
        self,
        serial_handle: object,
        incarnation: int,
        state_generation: int,
        *,
        reservation_owner: object,
    ) -> None:
        """Emit CONNECTED only while the published serial incarnation remains current."""
        self._ready_state_notification(
            ConnectionState.CONNECTED,
            state_generation,
            kind="connected",
            serial_handle=serial_handle,
            incarnation=incarnation,
            reservation_owner=reservation_owner,
        )

    def _reader_loop_after_connect(
        self,
        ready: threading.Event,
        loop_attempt: _ReaderLoopAttempt | None = None,
    ) -> None:
        """Keep a new reader dormant until connect publishes its incarnation atomically."""
        attempt = loop_attempt or _ReaderLoopAttempt()
        try:
            ready.wait()
            if not self._stop_event.is_set():
                if attempt.active_failure is None:
                    attempt.active_failure = self._new_reader_failure_attempt()
                self._run_reader_loop(attempt)
        except BaseException:
            attempt.escaped = True
            self._settle_escaped_reader_loop_interruption(attempt)
            raise

    def _reader_loop(self) -> None:
        """Background thread: read lines until stopped or error."""
        loop_attempt = _ReaderLoopAttempt()
        try:
            loop_attempt.active_failure = self._new_reader_failure_attempt()
            self._run_reader_loop(loop_attempt)
        except BaseException:
            loop_attempt.escaped = True
            self._settle_escaped_reader_loop_interruption(loop_attempt)
            raise

    def _settle_escaped_reader_loop_interruption(
        self,
        loop_attempt: _ReaderLoopAttempt,
    ) -> None:
        """Fail closed for any unexpected exit from gate, setup, read, or fan-out."""
        failure_attempt = loop_attempt.active_failure
        if failure_attempt is None:
            try:
                failure_attempt = self._new_reader_failure_attempt()
                loop_attempt.active_failure = failure_attempt
            except BaseException:
                return
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        if failure_attempt.lock_depths is None:
            failure_attempt.lock_depths = self._capture_owned_lock_depths(
                *operation_locks
            )
        self._release_locks_owned_by_current_thread(
            *operation_locks,
            baseline_depths=failure_attempt.lock_depths,
        )
        failure = (
            loop_attempt.failure
            if loop_attempt.failure is not None
            else serial.SerialException(
                "Serial reader interrupted during transport I/O"
            )
        )
        self._settle_interrupted_reader_failure(failure_attempt, failure)
        self._release_locks_owned_by_current_thread(
            *operation_locks,
            baseline_depths=failure_attempt.lock_depths,
        )

    def _run_reader_loop(self, loop_attempt: _ReaderLoopAttempt) -> None:
        """Read until stop/failure; :meth:`_reader_loop` owns adapter-call interruption."""
        buf = ""
        seeded_failure = loop_attempt.active_failure
        if seeded_failure is None:
            seeded_failure = self._new_reader_failure_attempt()
            loop_attempt.active_failure = seeded_failure
        pinned_serial, pinned_incarnation, _state, _generation = (
            seeded_failure.identity
        )
        if pinned_serial is None:
            return
        # One incremental decoder for the whole loop, so a multi-byte UTF-8 sequence split across two
        # reads reconstructs into a single code point (a per-read decode() would emit two U+FFFD).
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        if not self._reader_incarnation_is_current(
            pinned_serial,
            pinned_incarnation,
        ):
            return
        while not self._stop_event.is_set():
            try:
                next_failure = self._new_reader_failure_attempt_for_incarnation(
                    pinned_serial,
                    pinned_incarnation,
                )
                if next_failure is None:
                    return
                loop_attempt.active_failure = next_failure
                serial_handle, incarnation, _state, _generation = (
                    next_failure.identity
                )
                serial_is_open = serial_handle.is_open
                if serial_is_open is not True:
                    if self._reader_incarnation_is_current(serial_handle, incarnation):
                        raise serial.SerialException("Serial reader handle is closed")
                    break
                if not self._reader_incarnation_is_current(serial_handle, incarnation):
                    break
                waiting = serial_handle.in_waiting
                if not self._reader_incarnation_is_current(serial_handle, incarnation):
                    break
                if type(waiting) is not int or waiting < 0:
                    raise serial.SerialException("Serial reader returned invalid byte availability")
                chunk = serial_handle.read(waiting if waiting > 0 else 1)
                if not self._reader_incarnation_is_current(serial_handle, incarnation):
                    # The chunk belongs to an old incarnation. Do not dispatch it or let this old
                    # reader continue alongside the replacement's reader thread.
                    break
                if type(chunk) is not bytes:
                    raise serial.SerialException("Serial reader returned invalid byte data")
                if chunk == b"":
                    continue
                if self.raw:
                    # Binary/stream mode: hand the bytes over verbatim, no text decode, no line split.
                    self._emit_bytes(chunk)
                    if not self._reader_incarnation_is_current(serial_handle, incarnation):
                        return
                    continue
                decoded = decoder.decode(chunk)
                if not self._reader_incarnation_is_current(serial_handle, incarnation):
                    return
                if type(decoded) is not str:
                    raise serial.SerialException("Serial decoder returned invalid text data")
                buf += decoded
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
                            if not self._reader_incarnation_is_current(
                                serial_handle,
                                incarnation,
                            ):
                                return
                # Cap the un-terminated tail: a device streaming without any CR/LF would grow `buf`
                # forever. Once it exceeds the cap, flush it as a line and reset so memory is bounded.
                if len(buf) > _MAX_LINE_CHARS:
                    self._emit_line(buf)
                    if not self._reader_incarnation_is_current(
                        serial_handle,
                        incarnation,
                    ):
                        return
                    buf = ""
            except serial.SerialException as exc:
                loop_attempt.failure = exc
                loop_attempt.log_message = "Serial reader stopped after a transport error"
                self._run_reader_failure(
                    loop_attempt.active_failure or self._new_reader_failure_attempt(),
                    exc,
                    loop_attempt.log_message,
                )
                loop_attempt.active_failure = None
                break
            except Exception as exc:
                # A non-SerialException (e.g. a bare OSError on device removal) must STILL move us
                # out of CONNECTED — otherwise is_connected lies and connect() refuses to reopen.
                loop_attempt.failure = exc
                loop_attempt.log_message = "Serial reader stopped after an unexpected error"
                self._run_reader_failure(
                    loop_attempt.active_failure or self._new_reader_failure_attempt(),
                    exc,
                    loop_attempt.log_message,
                )
                loop_attempt.active_failure = None
                break

    def _new_reader_failure_attempt(self) -> _ReaderFailureAttempt:
        """Capture the current reader identity and entry lock depths before adapter I/O."""
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        lock_depths = self._capture_owned_lock_depths(*operation_locks)
        with self._io_lock:
            identity = (
                self._serial,
                self._serial_incarnation,
                self._state,
                self._state_generation,
            )
        return _ReaderFailureAttempt(identity, lock_depths=lock_depths)

    def _new_reader_failure_attempt_for_incarnation(
        self,
        serial_handle: object,
        incarnation: int,
    ) -> _ReaderFailureAttempt | None:
        """Capture failure facts only while the reader's originally assigned incarnation remains."""
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        lock_depths = self._capture_owned_lock_depths(*operation_locks)
        with self._io_lock:
            if (
                self._serial is not serial_handle
                or self._serial_incarnation != incarnation
                or self._state in {
                    ConnectionState.CONNECTING,
                    ConnectionState.DISCONNECTED,
                }
            ):
                return None
            identity = (
                serial_handle,
                incarnation,
                self._state,
                self._state_generation,
            )
        return _ReaderFailureAttempt(identity, lock_depths=lock_depths)

    def _reader_incarnation_is_current(
        self,
        serial_handle: object,
        incarnation: int,
    ) -> bool:
        """Fence reader adapter hooks and chunks from a replacement incarnation."""
        with self._io_lock:
            return (
                self._serial is serial_handle
                and self._serial_incarnation == incarnation
                and self._state not in {
                    ConnectionState.CONNECTING,
                    ConnectionState.DISCONNECTED,
                }
            )

    def _handle_reader_failure(self, exc: Exception, log_message: str) -> None:
        """Fail closed before notifying observers about a reader exception."""
        operation_locks = (
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
        )
        attempt = _ReaderFailureAttempt(
            (
                self._serial,
                self._serial_incarnation,
                self._state,
                self._state_generation,
            )
        )
        try:
            attempt.lock_depths = self._capture_owned_lock_depths(*operation_locks)
            self._run_reader_failure(attempt, exc, log_message)
        except BaseException:
            # Keep a cross-frame boundary above every transition/cleanup/notification line. A
            # control exception at an inner ``try`` header or RLock exit still leaves enough stable
            # identity to fail closed without touching a callback-created replacement.
            if attempt.lock_depths is None:
                attempt.lock_depths = self._capture_owned_lock_depths(*operation_locks)
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            self._settle_interrupted_reader_failure(attempt, exc)
            self._release_locks_owned_by_current_thread(
                *operation_locks,
                baseline_depths=attempt.lock_depths,
            )
            raise

    def _run_reader_failure(
        self,
        attempt: _ReaderFailureAttempt,
        exc: Exception,
        log_message: str,
    ) -> None:
        """Run reader teardown; :meth:`_handle_reader_failure` owns interruption recovery."""
        failed_serial, failed_incarnation, starting_state, starting_generation = (
            attempt.identity
        )
        if self._stop_event.is_set():
            self._release_serial(
                expected_identity=(failed_serial, failed_incarnation)
            )
            return

        with self._io_lock:
            if (
                self._serial is not failed_serial
                or self._serial_incarnation != failed_incarnation
            ):
                return
            unchanged_identity = (
                self._state is starting_state
                and self._state_generation == starting_generation
            )
            direct_error = (
                self._state is ConnectionState.ERROR
                and self._state_generation == starting_generation + 1
            )
            if not unchanged_identity and not direct_error:
                return
            self._write_quarantined = True
            state_changed, state_generation = self._transition_state_locked(
                ConnectionState.ERROR,
                reservation_owner=attempt,
            )
        attempt.state_changed = state_changed
        attempt.state_generation = state_generation
        # Drop the known-failed handle before callbacks: an ERROR observer must never be able to
        # transmit in the window between the state transition and reader cleanup.
        self._release_serial(
            expected_identity=(failed_serial, failed_incarnation)
        )
        self._safe_log(logging.ERROR, log_message)
        if state_changed:
            self._emit_state_change(
                ConnectionState.ERROR,
                state_generation,
                error=exc,
                reservation_owner=attempt,
            )
        else:
            attempt.error_notification_started = True
            self._emit_error(
                exc,
                expected_state=ConnectionState.ERROR,
                state_generation=state_generation,
                reservation_owner=attempt,
            )

    def _settle_interrupted_reader_failure(
        self,
        attempt: _ReaderFailureAttempt,
        exc: Exception,
    ) -> None:
        """Complete ERROR and close only the reader incarnation that actually failed."""
        failed_serial, failed_incarnation, starting_state, starting_generation = (
            attempt.identity
        )
        if self._stop_event.is_set():
            try:
                with self._io_lock:
                    if (
                        self._serial is failed_serial
                        and self._serial_incarnation == failed_incarnation
                    ):
                        self._release_serial(
                            expected_identity=(failed_serial, failed_incarnation)
                        )
            except BaseException:
                pass
            try:
                self._drain_state_notifications()
            except BaseException:
                pass
            return

        owned_state_generation: int | None = None
        owned_error_only_generation: int | None = None
        competing_error_generation: int | None = None
        try:
            owned_state_generation = self._owned_state_notification_generation(
                attempt,
                ConnectionState.ERROR,
            )
            owned_error_only_generation = self._owned_state_notification_generation(
                attempt,
                ConnectionState.ERROR,
                emit_state=False,
            )
        except BaseException:
            pass
        try:
            with self._io_lock:
                failed_incarnation_is_current = (
                    self._serial_incarnation == failed_incarnation
                    and (
                        self._serial is failed_serial
                        or self._serial is None
                    )
                    and self._state not in {
                        ConnectionState.DISCONNECTED,
                    }
                )
                if failed_incarnation_is_current:
                    if (
                        self._state_generation == starting_generation
                        and self._state is not ConnectionState.ERROR
                    ):
                        self._write_quarantined = True
                        attempt.state_changed, attempt.state_generation = (
                            self._transition_state_locked(
                                ConnectionState.ERROR,
                                reservation_owner=attempt,
                            )
                        )
                        if attempt.state_changed:
                            owned_state_generation = attempt.state_generation
                    elif (
                        self._state is ConnectionState.ERROR
                        and owned_state_generation == self._state_generation
                    ):
                        self._write_quarantined = True
                        attempt.state_changed = True
                        attempt.state_generation = owned_state_generation
                    elif (
                        self._state is ConnectionState.ERROR
                        and self._state_generation == starting_generation + 1
                    ):
                        # A concurrent write on this exact incarnation won the direct ERROR
                        # transition. Preserve its reservation and queue this reader error behind it.
                        competing_error_generation = (
                            self._matching_write_failure_reservation_generation(
                                failed_serial,
                                failed_incarnation,
                                starting_generation,
                            )
                        )
        except BaseException:
            pass
        self._release_locks_owned_by_current_thread(
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
            baseline_depths=attempt.lock_depths,
        )

        try:
            with self._io_lock:
                if (
                    self._serial is failed_serial
                    and self._serial_incarnation == failed_incarnation
                ):
                    self._release_serial(
                        expected_identity=(failed_serial, failed_incarnation)
                    )
        except BaseException:
            pass
        self._release_locks_owned_by_current_thread(
            self._io_lock,
            self._lifecycle_lock,
            self._state_notification_lock,
            baseline_depths=attempt.lock_depths,
        )

        try:
            owned_state_generation = self._owned_state_notification_generation(
                attempt,
                ConnectionState.ERROR,
            )
            owned_error_only_generation = self._owned_state_notification_generation(
                attempt,
                ConnectionState.ERROR,
                emit_state=False,
            )
        except BaseException:
            pass

        try:
            if owned_state_generation is not None:
                self._emit_state_change(
                    ConnectionState.ERROR,
                    owned_state_generation,
                    error=exc,
                    reservation_owner=attempt,
                )
            elif owned_error_only_generation is not None:
                # The notification is already ready; this merely lets the common drain resume it.
                pass
            elif competing_error_generation is not None:
                self._emit_error(
                    exc,
                    expected_state=ConnectionState.ERROR,
                    state_generation=competing_error_generation,
                    reservation_owner=attempt,
                )
            elif (
                starting_state is ConnectionState.ERROR
                and attempt.state_generation is not None
                and not attempt.error_notification_started
            ):
                self._emit_error(
                    exc,
                    expected_state=ConnectionState.ERROR,
                    state_generation=attempt.state_generation,
                    reservation_owner=attempt,
                )
        except BaseException:
            pass
        try:
            # A reader-error observer can synchronously disconnect while it owns the drainer, then
            # interrupt. Resume any nested current notification without replacing the active one.
            self._drain_state_notifications()
        except BaseException:
            pass

    def _set_state(self, new_state: ConnectionState) -> int:
        state_changed, state_generation = self._transition_state(new_state)
        if state_changed:
            self._emit_state_change(new_state, state_generation)
        return state_generation

    def _transition_state(
        self,
        new_state: ConnectionState,
        *,
        reservation_owner: object | None = None,
    ) -> tuple[bool, int]:
        """Change state atomically without invoking external callbacks."""
        with self._io_lock:
            return self._transition_state_locked(
                new_state,
                reservation_owner=reservation_owner,
            )

    def _transition_state_locked(
        self,
        new_state: ConnectionState,
        *,
        reservation_owner: object | None = None,
    ) -> tuple[bool, int]:
        """Transition while the caller owns ``_io_lock`` and reserve notification order."""
        if new_state is self._state:
            return False, self._state_generation
        previous_state = self._state
        previous_generation = self._state_generation
        try:
            self._state = new_state
            self._state_generation += 1
            state_generation = self._state_generation
            notification = _StateNotification(
                new_state,
                state_generation,
                reservation_owner=reservation_owner,
            )
            with self._state_notification_lock:
                # A newer generation makes every not-yet-ready older event stale. Ready them as
                # cancelled entries so they cannot become a permanent head-of-line barrier if their
                # original emitter never resumes.
                for pending in self._state_notifications:
                    if not pending.ready:
                        pending.ready = True
                self._state_notifications.append(notification)
                self._trim_state_notifications_locked()
            return True, state_generation
        except BaseException:
            # State, generation and queue reservation span several bytecodes. If control flow lands
            # after state publication, finish that internal transaction without invoking observers;
            # the owning lifecycle boundary will attach metadata, ready it, and re-raise exactly.
            try:
                self._repair_interrupted_state_transition_locked(
                    new_state,
                    previous_state,
                    previous_generation,
                    reservation_owner=reservation_owner,
                )
            except BaseException:
                pass
            raise

    def _repair_interrupted_state_transition_locked(
        self,
        new_state: ConnectionState,
        previous_state: ConnectionState,
        previous_generation: int,
        *,
        reservation_owner: object | None = None,
    ) -> None:
        """Complete only a partially published transition while ``_io_lock`` remains owned."""
        if previous_state is new_state or self._state is not new_state:
            return
        if self._state_generation <= previous_generation:
            self._state_generation = previous_generation + 1
        generation = self._state_generation
        with self._state_notification_lock:
            current: _StateNotification | None = None
            for pending in self._state_notifications:
                if (
                    current is None
                    and pending.state is new_state
                    and pending.generation == generation
                    and pending.reservation_owner is reservation_owner
                ):
                    current = pending
                elif not pending.ready:
                    pending.ready = True
            if current is None:
                self._state_notifications.append(
                    _StateNotification(
                        new_state,
                        generation,
                        reservation_owner=reservation_owner,
                    )
                )
            self._trim_state_notifications_locked()

    def _owned_state_notification_generation(
        self,
        reservation_owner: object,
        state: ConnectionState,
        *,
        emit_state: bool = True,
    ) -> int | None:
        """Return only a reservation created by the exact operation token."""
        with self._state_notification_lock:
            candidates = list(self._state_notifications)
            drain_owner = self._state_notification_drain_owner
            if drain_owner is not None:
                candidates.extend(drain_owner.pending)
            for notification in candidates:
                if (
                    notification.reservation_owner is reservation_owner
                    and notification.state is state
                    and notification.emit_state is emit_state
                ):
                    return notification.generation
        return None

    def _matching_write_failure_reservation_generation(
        self,
        serial_handle: object | None,
        incarnation: int,
        starting_generation: int,
    ) -> int | None:
        """Identify the exact competing writer whose ERROR the reader must not claim."""
        with self._state_notification_lock:
            candidates = list(self._state_notifications)
            drain_owner = self._state_notification_drain_owner
            if drain_owner is not None:
                candidates.extend(drain_owner.pending)
            for notification in candidates:
                owner = notification.reservation_owner
                if (
                    notification.state is ConnectionState.ERROR
                    and notification.generation == starting_generation + 1
                    and notification.emit_state
                    and type(owner) is _WriteTransaction
                    and owner.serial_handle is serial_handle
                    and owner.incarnation == incarnation
                    and owner.starting_state_generation == starting_generation
                ):
                    return notification.generation
        return None

    def _trim_state_notifications_locked(self) -> None:
        """Bound pending metadata while preferentially retaining state transitions."""
        while len(self._state_notifications) > _MAX_PENDING_STATE_NOTIFICATIONS:
            discard = next(
                (
                    index
                    for index, item in enumerate(self._state_notifications[:-1])
                    if not item.emit_state
                ),
                0,
            )
            del self._state_notifications[discard]

    def _emit_state_change(
        self,
        new_state: ConnectionState,
        state_generation: int,
        *,
        error: Exception | None = None,
        reservation_owner: object | None = None,
    ) -> None:
        self._ready_state_notification(
            new_state,
            state_generation,
            error=error,
            reservation_owner=reservation_owner,
        )

    def _ready_state_notification(
        self,
        state: ConnectionState,
        generation: int,
        *,
        kind: str = "generic",
        serial_handle: object | None = None,
        incarnation: int | None = None,
        error: Exception | None = None,
        write_receipt_facts: tuple[
            WriteDisposition,
            int,
            int | None,
            WriteErrorCode | None,
        ] | None = None,
        reservation_owner: object | None = None,
    ) -> None:
        """Mark one reserved event ready, then cooperatively drain ready events in order."""
        with self._state_notification_lock:
            notification = next(
                (
                    item
                    for item in self._state_notifications
                    if item.state is state
                    and item.generation == generation
                    and item.reservation_owner is reservation_owner
                    and not item.ready
                ),
                None,
            )
            if notification is None:
                return
            notification.kind = kind
            notification.serial_handle = serial_handle
            notification.incarnation = incarnation
            notification.error = error
            notification.write_receipt_facts = write_receipt_facts
            notification.ready = True
        self._drain_state_notifications()

    def _drain_state_notifications(self) -> None:
        """Deliver the ready queue without holding an internal lock across user callbacks."""
        owner = _StateDrainAttempt(
            [],
            self._capture_owned_lock_depths(self._state_notification_lock)[0],
        )
        try:
            self._run_state_notification_drain(owner)
        except BaseException as primary:
            # Identity ownership lets an interruption after publication clear only this drain.
            # Delivery progress lives on the notification, so requeue resumes after callbacks that
            # definitely returned while rolling back a callback whose CALL was never entered.
            try:
                self._recover_interrupted_notification_delivery(owner, primary)
            except BaseException:
                # Never let a secondary classifier interruption replace ``primary``. Conservatively
                # retry the active callback; at-least-once is safer than silently losing the event.
                self._rollback_active_notification_callback(owner)
            self._release_locks_owned_by_current_thread(
                self._state_notification_lock,
                baseline_depths=(owner.notification_lock_depth,),
            )
            try:
                with self._state_notification_lock:
                    if self._state_notification_drain_owner is owner:
                        if owner.pending:
                            notification = owner.pending.pop()
                            if not any(
                                notification is queued
                                for queued in self._state_notifications
                            ):
                                self._state_notifications.insert(0, notification)
                        self._state_notification_drain_owner = None
            except BaseException:
                pass
            raise

    def _run_state_notification_drain(self, owner: _StateDrainAttempt) -> None:
        """Claim and run one notification drain; the public wrapper owns interruption reset."""
        with self._state_notification_lock:
            if not self._claim_state_notification_drain_locked(owner):
                return

        while True:
            with self._state_notification_lock:
                notification_taken = self._take_state_notification_locked(owner)
            if not notification_taken:
                return
            # Keep the event cross-frame until the delivery helper returns. An interruption at any
            # Python instruction in this call handoff therefore leaves it recoverable; the helper
            # consumes it explicitly when a user callback raises a control-flow exception.
            self._deliver_state_notification(owner)

    def _claim_state_notification_drain_locked(
        self,
        owner: _StateDrainAttempt,
    ) -> bool:
        """Publish one drain owner; caller's frame holds the notification lock."""
        if self._state_notification_drain_owner is not None:
            return False
        self._state_notification_drain_owner = owner
        return True

    def _take_state_notification_locked(
        self,
        owner: _StateDrainAttempt,
    ) -> bool:
        """Take one ready event or release this owner; caller's frame holds the lock."""
        if (
            not self._state_notifications
            or not self._state_notifications[0].ready
        ):
            if self._state_notification_drain_owner is owner:
                self._state_notification_drain_owner = None
            return False
        notification = self._state_notifications[0]
        owner.pending.append(notification)
        del self._state_notifications[0]
        return True

    def _deliver_state_notification(self, owner: _StateDrainAttempt) -> None:
        """Fan out one event while its generation/incarnation remains current."""
        notification = owner.pending[0]
        if notification.emit_state:
            if notification.state_callbacks is None:
                notification.state_callbacks = list(self._state_callbacks)
            while notification.state_index < len(notification.state_callbacks):
                if not self._state_notification_is_current(notification):
                    owner.pending.clear()
                    return
                callback_index = notification.state_index
                callback = notification.state_callbacks[callback_index]
                try:
                    self._begin_notification_callback(
                        notification,
                        callback,
                        notification.state,
                        "state",
                        callback_index,
                    )
                except Exception as exc:
                    self._log_callback_failure("State", exc)
                notification.active_callback = None
                notification.active_phase = None
                notification.active_index = -1

        if (
            notification.error is None
            and notification.write_receipt_facts is None
        ):
            owner.pending.clear()
            return
        if notification.error_callbacks is None:
            notification.error_callbacks = list(self._error_callbacks)
        while notification.error_index < len(notification.error_callbacks):
            if not self._state_notification_error_is_current(notification):
                owner.pending.clear()
                return
            callback_index = notification.error_index
            callback = notification.error_callbacks[callback_index]
            callback_error = notification.error
            if notification.write_receipt_facts is not None:
                callback_error = IncompleteSerialWrite(
                    WriteReceipt(*notification.write_receipt_facts)
                )
            if callback_error is None:
                owner.pending.clear()
                return
            try:
                self._begin_notification_callback(
                    notification,
                    callback,
                    callback_error,
                    "error",
                    callback_index,
                )
            except Exception as exc:
                self._log_callback_failure("Error", exc)
            notification.active_callback = None
            notification.active_phase = None
            notification.active_index = -1
        owner.pending.clear()

    def _begin_notification_callback(
        self,
        notification: _StateNotification,
        callback: Callable[[object], None],
        argument: object,
        phase: str,
        callback_index: int,
    ) -> None:
        """Advance the durable cursor before one callback; recovery rolls back a missed CALL."""
        notification.active_index = callback_index
        notification.active_phase = phase
        # Publish the callback object last: recovery treats non-None as proof that both rollback
        # coordinates above are complete.
        notification.active_callback = callback
        if phase == "state":
            notification.state_index = callback_index + 1
        else:
            notification.error_index = callback_index + 1
        self._invoke_notification_callback(callback, argument)

    @staticmethod
    def _invoke_notification_callback(
        callback: Callable[[object], None],
        argument: object,
    ) -> None:
        """Keep the external callback CALL in a one-call frame for interruption classification."""
        callback(argument)

    def _recover_interrupted_notification_delivery(
        self,
        owner: _StateDrainAttempt,
        exc: BaseException,
    ) -> None:
        """Classify an active callback as entered, completed, or not-yet-called."""
        if not owner.pending:
            return
        notification = owner.pending[0]
        callback = notification.active_callback
        if callback is None:
            return
        if self._callback_frame_was_entered(callback, exc):
            # Preserve synchronous semantics: callback-originated control flow abandons the
            # remainder of this one event and must not replay the callback.
            owner.pending.clear()
            return
        if self._opaque_notification_callback_call_was_reached(callback, exc):
            # Builtin/C callables have no callback frame to find. Once the one-CALL trampoline
            # reached their invocation, keep the advanced cursor so this opaque callback remains
            # at-most-once. Retain the event itself so a later drain can deliver untouched siblings.
            notification.active_callback = None
            notification.active_phase = None
            notification.active_index = -1
            return
        if not self._notification_callback_call_completed(exc):
            self._rollback_notification_callback(notification)
        notification.active_callback = None
        notification.active_phase = None
        notification.active_index = -1

    @staticmethod
    def _notification_callback_call_completed(exc: BaseException) -> bool:
        """Use frozen traceback instruction offsets to distinguish pre- and post-CALL interrupts."""
        begin_code = SerialConnection._begin_notification_callback.__code__
        invoke_code = SerialConnection._invoke_notification_callback.__code__
        call_offsets = {
            begin_code: next(
                instruction.offset
                for instruction in dis.get_instructions(begin_code)
                if instruction.opname == "CALL"
            ),
            invoke_code: next(
                instruction.offset
                for instruction in dis.get_instructions(invoke_code)
                if instruction.opname == "CALL"
            ),
        }
        traceback_descriptor = BaseException.__dict__["__traceback__"]
        traceback = traceback_descriptor.__get__(exc, BaseException)
        completed: bool | None = None
        while traceback is not None:
            call_offset = call_offsets.get(traceback.tb_frame.f_code)
            if call_offset is not None:
                completed = traceback.tb_lasti > call_offset
            traceback = traceback.tb_next
        # If the durable active marker exists but both helper frames already returned, the callback
        # completed and the cursor must remain advanced.
        return True if completed is None else completed

    @staticmethod
    def _rollback_notification_callback(notification: _StateNotification) -> None:
        """Restore the durable cursor when an async exception fired before callback entry."""
        callback_index = max(notification.active_index, 0)
        if notification.active_phase == "state":
            notification.state_index = callback_index
        elif notification.active_phase == "error":
            notification.error_index = callback_index

    def _rollback_active_notification_callback(
        self,
        owner: _StateDrainAttempt,
    ) -> None:
        """Conservatively recover if callback classification itself is interrupted."""
        if not owner.pending:
            return
        notification = owner.pending[0]
        if notification.active_callback is not None:
            self._rollback_notification_callback(notification)
            notification.active_callback = None
            notification.active_phase = None
            notification.active_index = -1

    @staticmethod
    def _callback_frame_was_entered(
        callback: Callable[[object], None],
        exc: BaseException,
    ) -> bool:
        """Return whether a Python frame owned by ``callback`` appears in ``exc``."""
        try:
            callback_codes = SerialConnection._callback_python_codes(callback)
            if callback_codes is None:
                return False
            traceback_descriptor = BaseException.__dict__["__traceback__"]
            traceback = traceback_descriptor.__get__(exc, BaseException)
            while traceback is not None:
                if any(
                    traceback.tb_frame.f_code is code
                    for code in callback_codes
                ):
                    return True
                traceback = traceback.tb_next
        except BaseException:
            return False
        return False

    @staticmethod
    def _callback_python_codes(
        callback: Callable[[object], None],
    ) -> tuple[object, ...] | None:
        """Return exact Python callback code objects, empty for known opaque, None if unknown."""
        try:
            callback_codes: list[object] = []
            while type(callback) is functools.partial:
                callback = object.__getattribute__(callback, "func")
            if type(callback) is types.FunctionType:
                callback_codes.append(object.__getattribute__(callback, "__code__"))
            elif type(callback) is types.MethodType:
                callback_function = object.__getattribute__(callback, "__func__")
                callback_codes.append(
                    object.__getattribute__(callback_function, "__code__")
                )
            else:
                callback_type = type(callback)
                for callback_owner in type.__getattribute__(callback_type, "__mro__"):
                    namespace = type.__getattribute__(callback_owner, "__dict__")
                    call_function = namespace.get("__call__")
                    if type(call_function) is types.FunctionType:
                        callback_codes.append(
                            object.__getattribute__(call_function, "__code__")
                        )
            return tuple(callback_codes)
        except BaseException:
            return None

    @staticmethod
    def _opaque_notification_callback_call_was_reached(
        callback: Callable[[object], None],
        exc: BaseException,
    ) -> bool:
        """Recognize an entered builtin/C callback without evaluating callback descriptors."""
        callback_codes = SerialConnection._callback_python_codes(callback)
        if callback_codes is None or callback_codes:
            return False
        try:
            invoke_code = SerialConnection._invoke_notification_callback.__code__
            call_offset = next(
                instruction.offset
                for instruction in dis.get_instructions(invoke_code)
                if instruction.opname == "CALL"
            )
            traceback_descriptor = BaseException.__dict__["__traceback__"]
            traceback = traceback_descriptor.__get__(exc, BaseException)
            while traceback is not None:
                if (
                    traceback.tb_frame.f_code is invoke_code
                    and traceback.tb_lasti >= call_offset
                ):
                    return True
                traceback = traceback.tb_next
        except BaseException:
            return False
        return False

    def _state_notification_is_current(
        self,
        notification: _StateNotification,
    ) -> bool:
        with self._io_lock:
            if (
                self._state is not notification.state
                or self._state_generation != notification.generation
            ):
                return False
            if notification.kind != "connected":
                return True
            return (
                self._serial is notification.serial_handle
                and self._serial_incarnation == notification.incarnation
                and not self._write_quarantined
            )

    def _state_notification_error_is_current(
        self,
        notification: _StateNotification,
    ) -> bool:
        with self._io_lock:
            if (
                self._state is not notification.state
                or self._state_generation != notification.generation
            ):
                return False
            if notification.kind != "write_failure":
                return True
            return (
                self._serial_incarnation == notification.incarnation
                and self._write_quarantined
            )

    def _emit_line(self, line: str) -> None:
        for cb in list(self._line_callbacks):
            try:
                cb(line)
            except Exception as exc:
                self._log_callback_failure("Line", exc)

    def _emit_bytes(self, data: bytes) -> None:
        # Snapshot the list: a subscriber attaching/detaching mid-fan-out must not skip or stale-fire.
        for cb in list(self._byte_callbacks):
            try:
                cb(data)
            except Exception as exc:
                self._log_callback_failure("Byte", exc)

    def _emit_error(
        self,
        exc: Exception | None,
        *,
        expected_state: ConnectionState | None = None,
        state_generation: int | None = None,
        kind: str = "error_only",
        serial_handle: object | None = None,
        incarnation: int | None = None,
        write_receipt_facts: tuple[
            WriteDisposition,
            int,
            int | None,
            WriteErrorCode | None,
        ] | None = None,
        reservation_owner: object | None = None,
    ) -> None:
        if expected_state is not None and state_generation is not None:
            notification = _StateNotification(
                expected_state,
                state_generation,
                reservation_owner=reservation_owner,
                ready=True,
                kind=kind,
                serial_handle=serial_handle,
                incarnation=incarnation,
                error=exc,
                write_receipt_facts=write_receipt_facts,
                emit_state=False,
            )
            with self._state_notification_lock:
                self._state_notifications.append(notification)
                self._trim_state_notifications_locked()
            self._drain_state_notifications()
            return

        if exc is None:
            return
        for cb in list(self._error_callbacks):
            try:
                cb(exc)
            except Exception as callback_exc:
                self._log_callback_failure("Error", callback_exc)

    def _log_callback_failure(self, callback_kind: str, exc: Exception) -> None:
        """Report a callback failure without leaking details or destabilizing lifecycle state."""
        try:
            error_state = BaseException.__dict__["__dict__"].__get__(exc, BaseException)
            receipt = error_state.get("receipt")
        except BaseException:
            receipt = None
        if type(receipt) is WriteReceipt:
            message = "%s callback failed after a serial write"
        else:
            message = "%s callback error"
        self._safe_log(logging.ERROR, message, callback_kind)

    # ── Context manager ──────────────────────────────────────────────

    def __enter__(self) -> SerialConnection:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
