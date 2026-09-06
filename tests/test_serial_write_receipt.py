"""Adversarial tests for typed, exactly-once serial write receipts."""

from __future__ import annotations

import dis
import linecache
import logging
import sys
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

serial = pytest.importorskip("serial")
serial_handler = pytest.importorskip("src.core.serial_handler")

ConnectionState = serial_handler.ConnectionState
IncompleteSerialWrite = serial_handler.IncompleteSerialWrite
SerialConnection = serial_handler.SerialConnection
WriteDisposition = serial_handler.WriteDisposition
WriteErrorCode = serial_handler.WriteErrorCode
WriteReceipt = serial_handler.WriteReceipt
_FULL_COUNT = object()


class _FakeSerial:
    def __init__(
        self,
        reported: object = _FULL_COUNT,
        *,
        write_exc: Exception | None = None,
        flush_exc: Exception | None = None,
    ) -> None:
        self.is_open = True
        self.reported = reported
        self.write_exc = write_exc
        self.flush_exc = flush_exc
        self.writes: list[bytes] = []
        self.flushes = 0
        self.closed = False
        self.dtr = True
        self.rts = True

    def open(self) -> None:
        self.is_open = True

    @property
    def in_waiting(self) -> int:
        return 0

    def read(self, _size: int) -> bytes:
        time.sleep(0.002)
        return b""

    def write(self, payload: bytes) -> object:
        self.writes.append(payload)
        if self.write_exc is not None:
            raise self.write_exc
        return len(payload) if self.reported is _FULL_COUNT else self.reported

    def flush(self) -> None:
        self.flushes += 1
        if self.flush_exc is not None:
            raise self.flush_exc

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _make_conn(
    reported: object = _FULL_COUNT,
    *,
    write_exc: Exception | None = None,
    flush_exc: Exception | None = None,
    encoding: str = "utf-8",
    line_ending: str = "\n",
) -> tuple[SerialConnection, _FakeSerial]:
    conn = SerialConnection("COM-RECEIPT", encoding=encoding, line_ending=line_ending)
    fake = _FakeSerial(reported, write_exc=write_exc, flush_exc=flush_exc)
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED
    return conn, fake


def test_receipt_is_frozen_and_contains_only_finite_safe_types() -> None:
    receipt = WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        4,
        2,
        WriteErrorCode.SHORT_WRITE,
    )
    with pytest.raises(FrozenInstanceError):
        receipt.bytes_reported = 3
    assert {item.value for item in WriteDisposition} == {
        "definitely_not_written",
        "delivery_uncertain",
        "host_write_complete",
    }
    assert {item.value for item in WriteErrorCode} == {
        "serial_zero_write",
        "serial_short_write",
        "serial_invalid_write_count",
        "serial_write_error",
        "serial_flush_error",
    }


def test_text_receipt_counts_encoded_multibyte_payload_and_crlf() -> None:
    conn, fake = _make_conn(line_ending="\r\n")
    receipt = conn.write_receipt("café")
    encoded = "café\r\n".encode()

    assert fake.writes == [encoded]
    assert fake.flushes == 1
    assert receipt == WriteReceipt(
        WriteDisposition.HOST_WRITE_COMPLETE,
        len(encoded),
        len(encoded),
    )


def test_empty_binary_payload_exact_zero_is_complete_and_flushes() -> None:
    conn, fake = _make_conn(0)

    receipt = conn.write_bytes_receipt(b"")

    assert receipt == WriteReceipt(WriteDisposition.HOST_WRITE_COMPLETE, 0, 0)
    assert fake.writes == [b""]
    assert fake.flushes == 1

    legacy, legacy_fake = _make_conn(0)
    assert legacy.write_bytes(b"") is None
    assert legacy_fake.writes == [b""]
    assert legacy_fake.flushes == 1


def test_empty_binary_payload_invalid_count_and_exception_still_fail_closed() -> None:
    invalid, invalid_fake = _make_conn(None)
    invalid_receipt = invalid.write_bytes_receipt(b"")
    assert invalid_receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        0,
        None,
        WriteErrorCode.INVALID_COUNT,
    )
    assert invalid_fake.flushes == 0

    failure = serial.SerialException("empty write failure")
    failed, failed_fake = _make_conn(write_exc=failure)
    failed_receipt = failed.write_bytes_receipt(b"")
    assert failed_receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        0,
        None,
        WriteErrorCode.WRITE_ERROR,
    )
    assert failed_fake.flushes == 0


@pytest.mark.parametrize(
    ("reported", "disposition", "reported_receipt", "error_code"),
    [
        (0, WriteDisposition.DEFINITELY_NOT_WRITTEN, 0, WriteErrorCode.ZERO_WRITE),
        (1, WriteDisposition.DELIVERY_UNCERTAIN, 1, WriteErrorCode.SHORT_WRITE),
        (None, WriteDisposition.DELIVERY_UNCERTAIN, None, WriteErrorCode.INVALID_COUNT),
        (True, WriteDisposition.DELIVERY_UNCERTAIN, None, WriteErrorCode.INVALID_COUNT),
        (-1, WriteDisposition.DELIVERY_UNCERTAIN, None, WriteErrorCode.INVALID_COUNT),
        (99, WriteDisposition.DELIVERY_UNCERTAIN, None, WriteErrorCode.INVALID_COUNT),
    ],
)
def test_bad_counts_are_classified_once_without_flush(
    reported: object,
    disposition: WriteDisposition,
    reported_receipt: int | None,
    error_code: WriteErrorCode,
) -> None:
    conn, fake = _make_conn(reported)
    receipt = conn.write_bytes_receipt(b"abc")

    assert receipt == WriteReceipt(disposition, 3, reported_receipt, error_code)
    assert fake.writes == [b"abc"]
    assert fake.flushes == 0
    assert conn.state is ConnectionState.ERROR
    with pytest.raises(RuntimeError, match="quarantined"):
        conn.write_bytes_receipt(b"second-attempt")
    assert fake.writes == [b"abc"]


@pytest.mark.parametrize(
    "args",
    [
        ("host_write_complete", 1, 1, None),
        (WriteDisposition.HOST_WRITE_COMPLETE, -1, None, None),
        (WriteDisposition.HOST_WRITE_COMPLETE, 1, 0, None),
        (WriteDisposition.HOST_WRITE_COMPLETE, 1, 1, WriteErrorCode.FLUSH_ERROR),
        (WriteDisposition.DEFINITELY_NOT_WRITTEN, 0, 0, WriteErrorCode.ZERO_WRITE),
        (WriteDisposition.DELIVERY_UNCERTAIN, 3, 99, WriteErrorCode.INVALID_COUNT),
        (WriteDisposition.DELIVERY_UNCERTAIN, 3, 1, WriteErrorCode.WRITE_ERROR),
    ],
)
def test_receipt_rejects_nonfinite_or_inconsistent_public_states(args: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        WriteReceipt(*args)


def test_receipt_rejects_disposition_class_spoof() -> None:
    class _DispositionClassSpoof(str):
        @property
        def __class__(self) -> type:
            return WriteDisposition

    spoof = _DispositionClassSpoof(WriteDisposition.DELIVERY_UNCERTAIN.value)
    assert isinstance(spoof, WriteDisposition)
    assert type(spoof) is not WriteDisposition

    with pytest.raises(ValueError, match="Invalid serial write receipt enum"):
        WriteReceipt(spoof, 3, 1, WriteErrorCode.SHORT_WRITE)


def test_receipt_rejects_safe_error_code_class_spoof() -> None:
    class _ErrorCodeClassSpoof(str):
        @property
        def __class__(self) -> type:
            return WriteErrorCode

        def __hash__(self) -> int:
            return hash(WriteErrorCode.WRITE_ERROR)

        def __eq__(self, other: object) -> bool:
            return other is WriteErrorCode.WRITE_ERROR

    spoof = _ErrorCodeClassSpoof(WriteErrorCode.WRITE_ERROR.value)
    assert isinstance(spoof, WriteErrorCode)
    assert type(spoof) is not WriteErrorCode

    with pytest.raises(ValueError, match="Invalid serial write receipt enum"):
        WriteReceipt(WriteDisposition.DELIVERY_UNCERTAIN, 3, None, spoof)


@pytest.mark.parametrize(
    ("enum_type", "member", "receipt_args"),
    [
        (
            WriteDisposition,
            WriteDisposition.DELIVERY_UNCERTAIN,
            (3, 1, WriteErrorCode.SHORT_WRITE),
        ),
        (
            WriteErrorCode,
            WriteErrorCode.WRITE_ERROR,
            (WriteDisposition.DELIVERY_UNCERTAIN, 3, None),
        ),
    ],
)
def test_receipt_rejects_unregistered_exact_enum_instances(
    enum_type: type,
    member: object,
    receipt_args: tuple[object, ...],
) -> None:
    forged = str.__new__(enum_type, member.value)
    object.__setattr__(forged, "_name_", "FORGED")
    object.__setattr__(forged, "_value_", member.value)
    assert type(forged) is enum_type
    assert all(forged is not registered for registered in enum_type)

    with pytest.raises(ValueError, match="Invalid serial write receipt enum"):
        if enum_type is WriteDisposition:
            WriteReceipt(forged, *receipt_args)
        else:
            WriteReceipt(*receipt_args, forged)


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["write", "flush"])
def test_control_flow_interruption_quarantines_before_original_is_reraised(
    interruption_type: type[BaseException],
    stage: str,
) -> None:
    interruption = interruption_type("synthetic control interruption")

    class _InterruptedSerial(_FakeSerial):
        def write(self, payload: bytes) -> object:
            self.writes.append(payload)
            if stage == "write":
                raise interruption
            return len(payload)

        def flush(self) -> None:
            self.flushes += 1
            if stage == "flush":
                raise interruption

    conn = SerialConnection("COM-CONTROL-INTERRUPT")
    fake = _InterruptedSerial()
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED
    errors: list[Exception] = []
    conn.on_error(errors.append)

    with pytest.raises(interruption_type) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        len(b"one-attempt"),
        len(b"one-attempt") if stage == "flush" else None,
        WriteErrorCode.FLUSH_ERROR if stage == "flush" else WriteErrorCode.WRITE_ERROR,
    )
    assert conn.state is ConnectionState.ERROR
    assert len(errors) == 1
    assert isinstance(errors[0], IncompleteSerialWrite)
    assert errors[0].receipt == caught.value.receipt
    prior_writes = list(fake.writes)
    prior_flushes = fake.flushes

    with pytest.raises(RuntimeError, match="quarantined"):
        conn.write_bytes_receipt(b"must-not-replay")

    assert fake.writes == prior_writes
    assert fake.flushes == prior_flushes


def test_write_interruption_observer_cannot_replace_original_control_exception() -> None:
    interruption = KeyboardInterrupt("original write interruption")

    class _InterruptedSerial(_FakeSerial):
        def write(self, payload: bytes) -> object:
            self.writes.append(payload)
            raise interruption

    conn = SerialConnection("COM-INTERRUPT-OBSERVER")
    fake = _InterruptedSerial()
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED

    def interrupt_error_observer(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            raise SystemExit("secondary observer interruption")

    conn.on_state_change(interrupt_error_observer)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert fake.writes == [b"one-attempt"]
    assert not conn._state_notification_dispatching


def test_write_interruption_resumes_nested_disconnect_notification() -> None:
    interruption = KeyboardInterrupt("original write interruption")

    class _InterruptedSerial(_FakeSerial):
        def write(self, payload: bytes) -> object:
            self.writes.append(payload)
            raise interruption

    conn = SerialConnection("COM-INTERRUPT-DISCONNECT")
    fake = _InterruptedSerial()
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED
    observed: list[ConnectionState] = []

    def disconnect_then_interrupt(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            conn.disconnect()
            raise SystemExit("secondary observer interruption")

    conn.on_state_change(disconnect_then_interrupt)
    conn.on_state_change(observed.append)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert conn.state is ConnectionState.DISCONNECTED
    assert observed == [ConnectionState.DISCONNECTED]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching
    assert fake.writes == [b"one-attempt"]


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_reader_failure_interruption_resumes_nested_disconnect_notification(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("reader error observer interruption")
    conn, fake = _make_conn()
    observed: list[ConnectionState] = []

    def disconnect_then_interrupt(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            conn.disconnect()
            raise interruption

    conn.on_state_change(disconnect_then_interrupt)
    conn.on_state_change(observed.append)

    with pytest.raises(interruption_type) as caught:
        conn._handle_reader_failure(RuntimeError("synthetic reader failure"), "reader failed")

    assert caught.value is interruption
    assert conn.state is ConnectionState.DISCONNECTED
    assert fake.closed
    assert observed == [ConnectionState.DISCONNECTED]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("phase", ["post-write", "post-flush"])
def test_control_interruption_between_transport_calls_and_receipt_publication(
    phase: str,
) -> None:
    interruption = KeyboardInterrupt(f"synthetic {phase} interruption")
    conn, fake = _make_conn()
    fired = False

    def interrupt_between_lines(frame, event: str, arg):
        nonlocal fired
        del arg
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._perform_write_transport_attempt.__code__
            and frame.f_locals["transaction"].reported_value == len(b"one-attempt")
            and frame.f_locals["transaction"].completion_published is False
            and (
                (phase == "post-write" and fake.flushes == 0)
                or (phase == "post-flush" and fake.flushes == 1)
            )
        ):
            fired = True
            raise interruption
        return interrupt_between_lines

    sys.settrace(interrupt_between_lines)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        len(b"one-attempt"),
        len(b"one-attempt"),
        WriteErrorCode.FLUSH_ERROR,
    )
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == (1 if phase == "post-flush" else 0)

    with pytest.raises(RuntimeError, match="quarantined"):
        conn.write_bytes_receipt(b"must-not-replay")


def test_interruption_after_invalid_none_count_preserves_invalid_count_classification() -> None:
    interruption = KeyboardInterrupt("post-return invalid-count interruption")
    conn, fake = _make_conn(reported=None)
    fired = False

    def interrupt_after_return(frame, event: str, arg):
        nonlocal fired
        del arg
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._perform_write_transport_attempt.__code__
            and frame.f_locals["transaction"].reported_value is None
        ):
            fired = True
            raise interruption
        return interrupt_after_return

    sys.settrace(interrupt_after_return)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        len(b"one-attempt"),
        safe_error_code=WriteErrorCode.INVALID_COUNT,
    )
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == 0


@pytest.mark.parametrize(
    ("reported", "expected_receipt", "expected_state", "expected_quarantine"),
    [
        (
            _FULL_COUNT,
            WriteReceipt(WriteDisposition.HOST_WRITE_COMPLETE, 11, 11),
            ConnectionState.CONNECTED,
            False,
        ),
        (
            0,
            WriteReceipt(
                WriteDisposition.DEFINITELY_NOT_WRITTEN,
                11,
                0,
                WriteErrorCode.ZERO_WRITE,
            ),
            ConnectionState.ERROR,
            True,
        ),
    ],
    ids=["host-complete", "failed-write"],
)
def test_write_lock_exit_control_interruption_preserves_finite_outcome(
    reported: object,
    expected_receipt: WriteReceipt,
    expected_state: ConnectionState,
    expected_quarantine: bool,
) -> None:
    interruption = KeyboardInterrupt("write lock exit interrupted")

    class _InterruptingLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self._armed = True

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()
            if self._armed:
                self._armed = False
                raise interruption

    conn, fake = _make_conn(reported)
    conn._io_lock = _InterruptingLock()

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert caught.value.receipt == expected_receipt
    assert conn.state is expected_state
    assert conn._write_quarantined is expected_quarantine
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == (1 if reported is _FULL_COUNT else 0)


def test_write_pre_return_trace_interruption_retains_host_complete_receipt() -> None:
    interruption = KeyboardInterrupt("write return handoff interrupted")
    conn, fake = _make_conn()
    logged = False
    fired = False

    def mark_logged(*_args, **_kwargs) -> None:
        nonlocal logged
        logged = True

    conn._safe_log = mark_logged

    def interrupt_before_return(frame, event: str, arg):
        nonlocal fired
        del arg
        if (
            not fired
            and logged
            and event == "line"
            and frame.f_code is SerialConnection._run_write_payload_attempt.__code__
            and frame.f_locals["transaction"].completion_published is True
        ):
            fired = True
            raise interruption
        return interrupt_before_return

    sys.settrace(interrupt_before_return)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.HOST_WRITE_COMPLETE,
        11,
        11,
    )
    assert conn.state is ConnectionState.CONNECTED
    assert not conn._write_quarantined
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == 1


def test_write_flush_try_header_interruption_is_settled_by_outer_frame() -> None:
    interruption = KeyboardInterrupt("flush try header interrupted")
    conn, fake = _make_conn()
    fired = False

    def interrupt_flush_try_header(frame, event: str, arg):
        nonlocal fired
        del arg
        source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._perform_write_transport_attempt.__code__
            and source == "try:"
            and frame.f_locals["transaction"].reported_value == 11
            and fake.writes == [b"one-attempt"]
            and fake.flushes == 0
        ):
            fired = True
            raise interruption
        return interrupt_flush_try_header

    sys.settrace(interrupt_flush_try_header)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        11,
        11,
        WriteErrorCode.FLUSH_ERROR,
    )
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == 0
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert conn._state_notifications == []


def test_write_failure_emit_handoff_interruption_recovers_notification() -> None:
    interruption = KeyboardInterrupt("failure emit handoff interrupted")
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    fired = False

    def interrupt_emit_handoff(frame, event: str, arg):
        nonlocal fired
        del arg
        source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
        attempt = frame.f_locals.get("attempt")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._run_write_payload_attempt.__code__
            and source == "self._emit_write_failure("
            and conn.state is ConnectionState.ERROR
            and attempt is not None
            and attempt.cause is not None
            and frame.f_locals.get("interrupted") is None
        ):
            fired = True
            raise interruption
        return interrupt_emit_handoff

    sys.settrace(interrupt_emit_handoff)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == WriteReceipt(
        WriteDisposition.DEFINITELY_NOT_WRITTEN,
        11,
        0,
        WriteErrorCode.ZERO_WRITE,
    )
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == 0
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert isinstance(errors[0], IncompleteSerialWrite)
    assert errors[0].receipt == caught.value.receipt
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("transition_window", ["pre-generation", "post-append"])
def test_write_interruption_repairs_partial_error_transition(
    transition_window: str,
) -> None:
    interruption = KeyboardInterrupt(f"{transition_window} transition interrupted")
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    starting_generation = conn._state_generation
    fired = False

    def interrupt_partial_transition(frame, event: str, arg):
        nonlocal fired
        del arg
        source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
        target = (
            source == "self._state_generation += 1"
            and conn.state is ConnectionState.ERROR
            and conn._state_generation == starting_generation
            if transition_window == "pre-generation"
            else source == "self._trim_state_notifications_locked()"
            and any(
                item.state is ConnectionState.ERROR and not item.ready
                for item in conn._state_notifications
            )
        )
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._transition_state_locked.__code__
            and frame.f_locals.get("new_state") is ConnectionState.ERROR
            and target
        ):
            fired = True
            raise interruption
        return interrupt_partial_transition

    sys.settrace(interrupt_partial_transition)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("trace_kind", ["line", "opcode"])
@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("failure_stage", ["zero", "write-exception", "flush-exception"])
def test_write_interruption_after_quarantine_completes_owned_error_transition(
    trace_kind: str,
    interruption_type: type[BaseException],
    failure_stage: str,
) -> None:
    interruption = interruption_type(f"{failure_stage} after quarantine")
    if failure_stage == "zero":
        conn, fake = _make_conn(0)
        expected_receipt = WriteReceipt(
            WriteDisposition.DEFINITELY_NOT_WRITTEN,
            11,
            0,
            WriteErrorCode.ZERO_WRITE,
        )
    elif failure_stage == "write-exception":
        conn, fake = _make_conn(
            write_exc=serial.SerialException("private write failure")
        )
        expected_receipt = WriteReceipt(
            WriteDisposition.DELIVERY_UNCERTAIN,
            11,
            None,
            WriteErrorCode.WRITE_ERROR,
        )
    else:
        conn, fake = _make_conn(
            flush_exc=serial.SerialException("private flush failure")
        )
        expected_receipt = WriteReceipt(
            WriteDisposition.DELIVERY_UNCERTAIN,
            11,
            11,
            WriteErrorCode.FLUSH_ERROR,
        )

    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    starting_generation = conn._state_generation
    write_code = SerialConnection._run_write_payload_attempt.__code__
    instructions = list(dis.get_instructions(write_code))
    quarantine_store_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_write_quarantined"
    )
    after_quarantine = instructions[quarantine_store_index + 1]
    assert after_quarantine.starts_line is not None
    fired = False

    def interrupt_half_transition(frame, event: str, arg):
        nonlocal fired
        del arg
        if frame.f_code is write_code:
            frame.f_trace_opcodes = True
            reached_window = (
                event == "line"
                and trace_kind == "line"
                and frame.f_lineno == after_quarantine.starts_line
            ) or (
                event == "opcode"
                and trace_kind == "opcode"
                and frame.f_lasti == after_quarantine.offset
            )
            if not fired and reached_window and conn._write_quarantined:
                fired = True
                sys.settrace(None)
                raise interruption
        return interrupt_half_transition

    sys.settrace(interrupt_half_transition)
    try:
        with pytest.raises(interruption_type) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt == expected_receipt
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == (1 if failure_stage == "flush-exception" else 0)
    assert conn.state is ConnectionState.ERROR
    assert conn._state_generation == starting_generation + 1
    assert conn._write_quarantined
    assert conn._write_transaction is None
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert isinstance(errors[0], IncompleteSerialWrite)
    assert errors[0].receipt == expected_receipt
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_notification_drain_claim_interruption_is_reset_and_recovered() -> None:
    interruption = KeyboardInterrupt("notification drain claim interrupted")
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    fired = False

    def interrupt_after_drain_claim(frame, event: str, arg):
        nonlocal fired
        del arg
        owner = frame.f_locals.get("owner")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._run_state_notification_drain.__code__
            and owner is not None
            and conn._state_notification_drain_owner is owner
        ):
            fired = True
            raise interruption
        return interrupt_after_drain_claim

    sys.settrace(interrupt_after_drain_claim)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_notification_is_retained_across_delivery_call_handoff(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("notification delivery handoff interrupted")
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    original_delivery = conn._deliver_state_notification
    fired = False

    def interrupt_before_delivery(owner) -> None:
        nonlocal fired
        assert len(owner.pending) == 1
        if not fired:
            fired = True
            raise interruption
        original_delivery(owner)

    monkeypatch.setattr(
        conn,
        "_deliver_state_notification",
        interrupt_before_delivery,
    )

    with pytest.raises(interruption_type) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert fired
    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_notification_resume_does_not_replay_completed_callback(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("post-callback notification interruption")
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    original_begin = conn._begin_notification_callback
    fired = False

    def interrupt_after_callback(*args, **kwargs) -> None:
        nonlocal fired
        original_begin(*args, **kwargs)
        if not fired:
            fired = True
            raise interruption

    monkeypatch.setattr(
        conn,
        "_begin_notification_callback",
        interrupt_after_callback,
    )

    with pytest.raises(interruption_type) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fired
    assert fake.writes == [b"one-attempt"]
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("delete_stage", ["before", "after"])
def test_notification_transfer_dual_ownership_recovers_interruption(
    delete_stage: str,
) -> None:
    interruption = KeyboardInterrupt(f"notification {delete_stage}-delete interruption")

    class _InterruptingQueue(list):
        armed = True

        def __delitem__(self, index) -> None:
            if self.armed and delete_stage == "before":
                self.armed = False
                raise interruption
            super().__delitem__(index)
            if self.armed:
                self.armed = False
                raise interruption

    conn, fake = _make_conn(0)
    conn._state_notifications = _InterruptingQueue()
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_hostile_callback_introspection_cannot_trigger_replay(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("hostile callback control flow")

    class _HostileCallback:
        def __init__(self) -> None:
            object.__setattr__(self, "calls", 0)
            object.__setattr__(self, "descriptor_calls", 0)

        def __getattribute__(self, name: str):
            if name in {"__code__", "__func__", "func"}:
                descriptor_calls = object.__getattribute__(self, "descriptor_calls")
                object.__setattr__(self, "descriptor_calls", descriptor_calls + 1)
                raise RuntimeError("hostile callback descriptor")
            return object.__getattribute__(self, name)

        def __call__(self, _state: ConnectionState) -> None:
            calls = object.__getattribute__(self, "calls")
            object.__setattr__(self, "calls", calls + 1)
            raise interruption

    conn, fake = _make_conn(0)
    callback = _HostileCallback()
    conn.on_state_change(callback)

    with pytest.raises(interruption_type) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert object.__getattribute__(callback, "calls") == 1
    assert object.__getattribute__(callback, "descriptor_calls") == 0
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_hostile_control_exception_receipt_setter_cannot_mutate_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setter_calls = 0
    conn = SerialConnection("COM-HOSTILE-INTERRUPTION")

    class _HostileInterrupt(KeyboardInterrupt):
        def __setattr__(self, name: str, value: object) -> None:
            nonlocal setter_calls
            if name == "receipt":
                setter_calls += 1
                conn.disconnect()
            super().__setattr__(name, value)

    interruption = _HostileInterrupt("original write interruption")

    class _InterruptedSerial(_FakeSerial):
        def write(self, payload: bytes) -> object:
            self.writes.append(payload)
            raise interruption

    failed = _InterruptedSerial()
    fresh = _FakeSerial()
    fresh.is_open = False
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    replaced = False

    def replace_on_error(state: ConnectionState) -> None:
        nonlocal replaced
        if state is ConnectionState.ERROR and not replaced:
            replaced = True
            conn.connect()

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.on_state_change(replace_on_error)

    with pytest.raises(_HostileInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    try:
        assert caught.value is interruption
        assert setter_calls == 0
        assert interruption.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert not fresh.closed
    finally:
        conn.disconnect()


def test_hostile_control_exception_dict_descriptor_is_never_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dict_calls = 0
    conn = SerialConnection("COM-HOSTILE-DICT")

    class _HostileDictInterrupt(KeyboardInterrupt):
        @property
        def __dict__(self):
            nonlocal dict_calls
            dict_calls += 1
            conn.disconnect()
            raise SystemExit("hostile exception dictionary descriptor")

    interruption = _HostileDictInterrupt("original write interruption")

    class _InterruptedSerial(_FakeSerial):
        def write(self, payload: bytes) -> object:
            self.writes.append(payload)
            raise interruption

    failed = _InterruptedSerial()
    fresh = _FakeSerial()
    fresh.is_open = False
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    replaced = False

    def replace_on_error(state: ConnectionState) -> None:
        nonlocal replaced
        if state is ConnectionState.ERROR and not replaced:
            replaced = True
            conn.connect()

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.on_state_change(replace_on_error)

    with pytest.raises(_HostileDictInterrupt) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    try:
        assert caught.value is interruption
        assert dict_calls == 0
        assert caught.value.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert not fresh.closed
    finally:
        conn.disconnect()


def test_write_exception_is_uncertain_and_never_retried_or_flushed() -> None:
    failure = serial.SerialException("synthetic transport failure")
    conn, fake = _make_conn(write_exc=failure)

    receipt = conn.send_interrupt_receipt()

    assert receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        1,
        safe_error_code=WriteErrorCode.WRITE_ERROR,
    )
    assert fake.writes == [b"\x03"]
    assert fake.flushes == 0
    assert conn.state is ConnectionState.ERROR


def test_flush_exception_after_full_count_is_uncertain_and_not_retried() -> None:
    failure = OSError("synthetic flush failure")
    conn, fake = _make_conn(flush_exc=failure)

    receipt = conn.write_bytes_receipt(b"abc")

    assert receipt == WriteReceipt(
        WriteDisposition.DELIVERY_UNCERTAIN,
        3,
        3,
        WriteErrorCode.FLUSH_ERROR,
    )
    assert fake.writes == [b"abc"]
    assert fake.flushes == 1
    assert conn.state is ConnectionState.ERROR


@pytest.mark.parametrize("surface", ["text", "bytes", "interrupt"])
def test_all_legacy_surfaces_return_none_after_one_complete_attempt(surface: str) -> None:
    conn, fake = _make_conn()
    if surface == "text":
        result = conn.write("status")
        expected = b"status\n"
    elif surface == "bytes":
        result = conn.write_bytes(b"raw")
        expected = b"raw"
    else:
        result = conn.send_interrupt()
        expected = b"\x03"

    assert result is None
    assert fake.writes == [expected]
    assert fake.flushes == 1


@pytest.mark.parametrize("reported", [0, 1, None, True, -1, 99])
def test_legacy_bad_count_is_serial_exception_carrying_receipt(reported: object) -> None:
    conn, fake = _make_conn(reported)

    with pytest.raises(IncompleteSerialWrite) as caught:
        conn.write_bytes(b"abc")

    assert isinstance(caught.value, serial.SerialException)
    assert caught.value.receipt.disposition is not WriteDisposition.HOST_WRITE_COMPLETE
    assert fake.writes == [b"abc"]
    assert fake.flushes == 0


@pytest.mark.parametrize(
    "failure",
    [serial.SerialException("serial failure"), OSError("os failure")],
)
def test_legacy_transport_failure_preserves_original_exception_and_adds_receipt(
    failure: Exception,
) -> None:
    conn, fake = _make_conn(write_exc=failure)

    with pytest.raises(type(failure)) as caught:
        conn.write("status")

    assert caught.value is failure
    assert caught.value.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
    assert fake.writes == [b"status\n"]
    assert fake.flushes == 0


def test_legacy_flush_failure_preserves_original_exception_and_receipt() -> None:
    failure = serial.SerialException("flush failure")
    conn, fake = _make_conn(flush_exc=failure)

    with pytest.raises(serial.SerialException) as caught:
        conn.send_interrupt()

    assert caught.value is failure
    assert caught.value.receipt.safe_error_code is WriteErrorCode.FLUSH_ERROR
    assert fake.writes == [b"\x03"]
    assert fake.flushes == 1


def test_legacy_unattachable_exception_falls_back_to_serial_compatible_wrapper() -> None:
    class _UnattachableSerialError(serial.SerialException):
        def __setattr__(self, name: str, value: object) -> None:
            if name == "receipt":
                raise RuntimeError("receipt is read-only")
            super().__setattr__(name, value)

    failure = _UnattachableSerialError("transport failed")
    conn, _fake = _make_conn(write_exc=failure)

    with pytest.raises(IncompleteSerialWrite) as caught:
        conn.write_bytes(b"abc")

    assert isinstance(caught.value, serial.SerialException)
    assert isinstance(caught.value, OSError)
    assert caught.value.__cause__ is failure
    assert caught.value.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR


def test_legacy_hostile_receipt_setter_cannot_mutate_callback_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setter_calls = 0
    conn = SerialConnection("COM-LEGACY-HOSTILE-SETTER")

    class _HostileSerialError(serial.SerialException):
        def __setattr__(self, name: str, value: object) -> None:
            nonlocal setter_calls
            if name == "receipt":
                setter_calls += 1
                conn.disconnect()
                raise SystemExit("hostile legacy receipt setter")
            super().__setattr__(name, value)

    failure = _HostileSerialError("failed transport incarnation")
    failed = _FakeSerial(write_exc=failure)
    fresh = _FakeSerial()
    fresh.is_open = False
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    replaced = False

    def replace_on_error(_error: Exception) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            conn.connect()

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.on_error(replace_on_error)

    with pytest.raises(IncompleteSerialWrite) as caught:
        conn.write("status")

    try:
        assert caught.value.__cause__ is failure
        assert caught.value.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
        assert setter_calls == 0
        assert failed.closed
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert not fresh.closed
    finally:
        conn.disconnect()


def test_validation_and_unavailable_errors_remain_pre_io() -> None:
    conn, fake = _make_conn()
    with pytest.raises(ValueError):
        conn.write_receipt("status\nreboot")
    assert fake.writes == []
    assert conn.state is ConnectionState.CONNECTED

    disconnected = SerialConnection("COM-NONE")
    with pytest.raises(RuntimeError, match="Not connected"):
        disconnected.write_receipt("status")


def test_disconnect_after_quarantine_restores_unavailable_error_contract() -> None:
    conn, _fake = _make_conn(0)
    conn.write_bytes_receipt(b"failed")
    conn.disconnect()

    with pytest.raises(RuntimeError, match="Not connected"):
        conn.write_bytes_receipt(b"after-disconnect")


def test_receipt_repr_logs_and_error_text_never_contain_payload_or_transport_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "receipt-secret-never-log"
    failure = serial.SerialException(f"driver echoed {secret}")
    conn, _fake = _make_conn(write_exc=failure)

    with caplog.at_level(logging.DEBUG, logger="src.core.serial_handler"):
        receipt = conn.write_receipt(secret)

    assert secret not in repr(receipt)
    assert secret not in caplog.text
    assert str(receipt.safe_error_code.value) not in caplog.text
    assert all(secret not in repr(record.args) for record in caplog.records)

    count_conn, _count_fake = _make_conn(1)
    with pytest.raises(IncompleteSerialWrite) as caught:
        count_conn.write(secret)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value.receipt)


def test_error_callback_gets_sanitized_receipt_and_rethrow_cannot_log_transport_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "transport-secret-must-not-reach-callback-log"
    failure = serial.SerialException(f"adapter echoed {secret}")
    conn, _fake = _make_conn(write_exc=failure)
    seen: list[Exception] = []

    def rethrow(error: Exception) -> None:
        seen.append(error)
        raise error

    conn.on_error(rethrow)
    with caplog.at_level(logging.DEBUG, logger="src.core.serial_handler"):
        receipt = conn.write_receipt("status")

    assert receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
    assert len(seen) == 1
    assert isinstance(seen[0], IncompleteSerialWrite)
    assert seen[0] is not failure
    assert seen[0].receipt == receipt
    assert seen[0].receipt is not receipt
    assert secret not in caplog.text
    assert all(secret not in repr(record.args) for record in caplog.records)
    assert all(
        record.exc_info is None or secret not in repr(record.exc_info)
        for record in caplog.records
    )


def test_callback_failure_receipt_class_spoof_cannot_escape_or_select_log_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "hostile-class-detail"

    class _ExplodingReceiptClass:
        @property
        def __class__(self) -> type:
            raise RuntimeError(secret)

    error = RuntimeError("safe outer error")
    error.receipt = _ExplodingReceiptClass()
    conn, _fake = _make_conn()

    with caplog.at_level(logging.ERROR, logger="src.core.serial_handler"):
        conn._log_callback_failure("State", error)

    assert "State callback error" in caplog.text
    assert "after a serial write" not in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_callback_failure_receipt_lookup_control_exception_is_contained(
    caplog: pytest.LogCaptureFixture,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("hostile receipt lookup")

    class _HostileCallbackError(RuntimeError):
        @property
        def receipt(self):
            raise interruption

    conn, _fake = _make_conn()
    with caplog.at_level(logging.ERROR, logger="src.core.serial_handler"):
        conn._log_callback_failure("State", _HostileCallbackError("safe outer error"))

    assert "State callback error" in caplog.text
    assert "after a serial write" not in caplog.text


def test_legacy_write_failure_inside_line_callback_is_logged_without_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "callback-transport-secret"
    conn, _fake = _make_conn(write_exc=serial.SerialException(secret))
    conn.on_line(lambda _line: conn.write("status"))

    with caplog.at_level(logging.ERROR, logger="src.core.serial_handler"):
        conn._emit_line("trigger")

    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert all(secret not in repr(record.args) for record in caplog.records)


def test_hostile_logging_and_callbacks_cannot_erase_delivery_truth() -> None:
    class _ExplodingHandler(logging.Handler):
        def emit(self, _record: logging.LogRecord) -> None:
            raise RuntimeError("observer failed")

    logger = logging.getLogger("src.core.serial_handler")
    prior_level = logger.level
    handler = _ExplodingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        typed, typed_fake = _make_conn()
        typed_receipt = typed.write_bytes_receipt(b"typed")
        assert typed_receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE
        assert typed_fake.writes == [b"typed"]

        legacy, legacy_fake = _make_conn()
        assert legacy.write("legacy") is None
        assert legacy_fake.writes == [b"legacy\n"]

        failed, failed_fake = _make_conn(0)
        failed.on_error(lambda error: (_ for _ in ()).throw(error))
        failed_receipt = failed.write_bytes_receipt(b"failed")
        assert failed_receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
        assert failed_fake.writes == [b"failed"]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


def test_state_and_error_callbacks_run_after_io_lock_is_released() -> None:
    conn, _fake = _make_conn(0)
    callback_observations: list[tuple[str, bool]] = []

    def observe(label: str) -> None:
        acquired = conn._io_lock.acquire(blocking=False)
        callback_observations.append((label, acquired))
        if acquired:
            conn._io_lock.release()

    conn.on_state_change(lambda _state: observe("state"))
    conn.on_error(lambda _error: observe("error"))

    receipt = conn.write_bytes_receipt(b"abc")

    assert receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
    assert callback_observations == [("state", True), ("error", True)]


def test_failure_callbacks_can_reenter_disconnect_without_deadlock() -> None:
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []

    def disconnect_on_state(state: ConnectionState) -> None:
        states.append(state)
        conn.disconnect()

    conn.on_state_change(disconnect_on_state)
    conn.on_error(lambda _error: conn.disconnect())
    receipts: list[WriteReceipt] = []
    writer = threading.Thread(target=lambda: receipts.append(conn.write_bytes_receipt(b"abc")))
    writer.start()
    writer.join(timeout=2.0)

    assert not writer.is_alive()
    assert receipts[0].disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
    assert states[:2] == [ConnectionState.ERROR, ConnectionState.DISCONNECTED]
    assert fake.closed


def test_reader_callback_write_failure_can_disconnect_without_self_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disconnected = threading.Event()

    class _LineThenIdleSerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__(write_exc=serial.SerialException("write failure"))
            self._read_once = False

        @property
        def in_waiting(self) -> int:
            return 5 if not self._read_once else 0

        def read(self, _size: int) -> bytes:
            if not self._read_once:
                self._read_once = True
                return b"line\n"
            time.sleep(0.002)
            return b""

    fresh = _LineThenIdleSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-READER-DISCONNECT")
    conn.on_line(lambda _line: conn.write("status"))

    def disconnect_on_error(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            conn.disconnect()
            disconnected.set()

    conn.on_state_change(disconnect_on_error)
    conn.connect()

    assert disconnected.wait(1.0)
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None
    assert fresh.closed
    assert fresh.writes == [b"status\n"]


def test_reader_error_disconnect_suppresses_stale_state_and_error_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disconnected = threading.Event()

    class _ImmediateReadFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise serial.SerialException("reader failed")

    fresh = _ImmediateReadFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-READER-STATE-FENCE")
    events: list[tuple[str, ConnectionState]] = []
    errors: list[Exception] = []

    def first(state: ConnectionState) -> None:
        events.append(("first", state))
        if state is ConnectionState.ERROR:
            conn.disconnect()
            disconnected.set()

    conn.on_state_change(first)
    conn.on_state_change(lambda state: events.append(("second", state)))
    conn.on_error(errors.append)
    conn.connect()

    assert disconnected.wait(1.0)
    assert conn.state is ConnectionState.DISCONNECTED
    assert ("second", ConnectionState.DISCONNECTED) in events
    assert ("second", ConnectionState.ERROR) not in events
    assert errors == []
    assert conn._serial is None
    assert fresh.closed


def test_reentrant_disconnect_does_not_deliver_stale_error_state_to_later_callback() -> None:
    conn, _fake = _make_conn(0)
    events: list[tuple[str, ConnectionState]] = []

    def first(state: ConnectionState) -> None:
        events.append(("first", state))
        if state is ConnectionState.ERROR:
            conn.disconnect()

    conn.on_state_change(first)
    conn.on_state_change(lambda state: events.append(("second", state)))
    conn.write_bytes_receipt(b"abc")

    assert ("second", ConnectionState.DISCONNECTED) in events
    assert ("second", ConnectionState.ERROR) not in events


def test_reconnect_from_state_callback_suppresses_stale_old_incarnation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, failed = _make_conn(0)
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    errors: list[Exception] = []

    def reconnect(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            conn.connect()

    conn.on_state_change(reconnect)
    conn.on_error(errors.append)

    failed_receipt = conn.write_bytes_receipt(b"first")
    try:
        assert failed_receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
        assert failed.closed
        assert conn.state is ConnectionState.CONNECTED
        assert errors == []

        fresh_receipt = conn.write_bytes_receipt(b"second")
        assert fresh_receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE
        assert fresh.writes == [b"second"]
    finally:
        conn.disconnect()


def test_write_cannot_enter_while_connect_is_starting_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = threading.Event()
    allow_start = threading.Event()

    class _BlockingStartThread:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            start_entered.set()
            assert allow_start.wait(2.0)

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout
            pass

    real_thread = threading.Thread
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    monkeypatch.setattr(serial_handler.threading, "Thread", _BlockingStartThread)
    conn = SerialConnection("COM-STARTING")
    connect_errors: list[Exception] = []
    connector = real_thread(target=lambda: _capture_exception(conn.connect, connect_errors))
    connector.start()
    assert start_entered.wait(1.0)

    with pytest.raises(RuntimeError, match="Not connected"):
        conn.write_bytes_receipt(b"must-not-enter")
    with pytest.raises(RuntimeError, match="already in progress"):
        conn.connect()
    assert fresh.writes == []

    allow_start.set()
    connector.join(timeout=2.0)
    assert not connector.is_alive()
    assert connect_errors == []
    assert conn.state is ConnectionState.CONNECTED
    assert conn.write_bytes_receipt(b"after-connect").disposition is (
        WriteDisposition.HOST_WRITE_COMPLETE
    )
    conn.disconnect()


def test_concurrent_connect_is_rejected_before_lifecycle_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-CONCURRENT-CONNECT")
    barrier = threading.Barrier(3)
    returns: list[int] = []
    errors: list[Exception] = []

    def connect_once(marker: int) -> None:
        barrier.wait()
        try:
            conn.connect()
        except Exception as exc:
            errors.append(exc)
        else:
            returns.append(marker)

    conn._lifecycle_lock.acquire()
    first = threading.Thread(target=connect_once, args=(1,))
    second = threading.Thread(target=connect_once, args=(2,))
    first.start()
    second.start()
    barrier.wait()
    time.sleep(0.05)
    conn._lifecycle_lock.release()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    try:
        assert not first.is_alive() and not second.is_alive()
        assert len(returns) == 1
        assert len(errors) == 1
        assert "already in progress" in str(errors[0])
        assert conn.state is ConnectionState.CONNECTED
    finally:
        conn.disconnect()


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "window",
    ["connecting-observer", "opened-local-candidate", "reader-started"],
)
def test_interrupted_startup_settles_owned_resources_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
    window: str,
) -> None:
    interruption = interruption_type("synthetic startup interruption")
    candidates: list[_Candidate] = []
    reader_gates: list[threading.Event] = []
    observer_interrupted = False
    open_interrupted = False
    reader_interrupted = False

    class _Candidate(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.is_open = False
            self.close_calls = 0

        def open(self) -> None:
            nonlocal open_interrupted
            self.is_open = True
            if window == "opened-local-candidate" and not open_interrupted:
                open_interrupted = True
                raise interruption

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def serial_factory() -> _Candidate:
        candidate = _Candidate()
        candidates.append(candidate)
        return candidate

    class _InterruptedReader(threading.Thread):
        def start(self) -> None:
            nonlocal reader_interrupted
            reader_gates.append(self._args[0])
            super().start()
            if window == "reader-started" and not reader_interrupted:
                reader_interrupted = True
                raise interruption

    class _ThreadingView:
        Thread = _InterruptedReader

        def __getattr__(self, name: str) -> object:
            return getattr(threading, name)

    conn = SerialConnection("COM-STARTUP-INTERRUPT")

    def interrupt_connecting(state: ConnectionState) -> None:
        nonlocal observer_interrupted
        if (
            window == "connecting-observer"
            and state is ConnectionState.CONNECTING
            and not observer_interrupted
        ):
            observer_interrupted = True
            raise interruption

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    if window == "reader-started":
        monkeypatch.setattr(serial_handler, "threading", _ThreadingView())
    conn.on_state_change(interrupt_connecting)

    with pytest.raises(interruption_type) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn.state is ConnectionState.ERROR
    assert not conn._connect_in_progress
    assert conn._connect_attempt is None
    assert conn._serial is None
    assert all(not candidate.is_open for candidate in candidates)
    assert all(candidate.close_calls == 1 for candidate in candidates)
    assert all(gate.is_set() for gate in reader_gates)
    assert conn._read_thread is None or not conn._read_thread.is_alive()
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching

    # Restore the real threading module before proving that a fresh independent attempt works.
    monkeypatch.setattr(serial_handler, "threading", threading)
    conn.connect()
    try:
        assert conn.state is ConnectionState.CONNECTED
        assert conn._connect_attempt is None
        assert conn.write_bytes_receipt(b"fresh") == WriteReceipt(
            WriteDisposition.HOST_WRITE_COMPLETE,
            5,
            5,
        )
    finally:
        conn.disconnect()


def test_interrupted_attempt_cannot_clear_callback_started_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("first attempt interrupted")
    candidates: list[_FakeSerial] = []
    interrupted = False
    conn = SerialConnection("COM-ATTEMPT-FENCE")

    def serial_factory() -> _FakeSerial:
        candidate = _FakeSerial()
        candidate.is_open = False
        candidates.append(candidate)
        return candidate

    def replace_after_abort(state: ConnectionState) -> None:
        nonlocal interrupted
        if state is ConnectionState.CONNECTING and not interrupted:
            interrupted = True
            raise interruption
        if state is ConnectionState.ERROR:
            conn.connect()

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn.on_state_change(replace_after_abort)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    try:
        assert caught.value is interruption
        assert len(candidates) == 1
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is candidates[0]
        assert conn._connect_attempt is None
        assert not conn._connect_in_progress
        assert conn.write_bytes_receipt(b"replacement").disposition is (
            WriteDisposition.HOST_WRITE_COMPLETE
        )
    finally:
        conn.disconnect()


def test_disconnect_wins_when_connecting_observer_then_interrupts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("disconnect won before interruption")
    factory_called = False
    conn = SerialConnection("COM-DISCONNECT-INTERRUPT")
    observed: list[ConnectionState] = []

    def serial_factory() -> _FakeSerial:
        nonlocal factory_called
        factory_called = True
        return _FakeSerial()

    def disconnect_then_interrupt(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTING:
            conn.disconnect()
            raise interruption

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn.on_state_change(disconnect_then_interrupt)
    conn.on_state_change(observed.append)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None
    assert conn._connect_attempt is None
    assert not conn._connect_in_progress
    assert not factory_called
    assert observed == [ConnectionState.DISCONNECTED]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("window", ["claim", "release"])
def test_connect_admission_has_no_split_boolean_token_interruption_window(
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    interruption = KeyboardInterrupt(f"synthetic admission {window} interruption")
    candidates: list[_FakeSerial] = []

    class _AdmissionInterruptConnection(SerialConnection):
        def __init__(self, port: str) -> None:
            object.__setattr__(self, "_armed_window", None)
            super().__init__(port)

        def __setattr__(self, name: str, value: object) -> None:
            object.__setattr__(self, name, value)
            armed = object.__getattribute__(self, "_armed_window")
            if name == "_connect_attempt" and (
                (armed == "claim" and value is not None)
                or (armed == "release" and value is None)
            ):
                object.__setattr__(self, "_armed_window", None)
                raise interruption

    def serial_factory() -> _FakeSerial:
        candidate = _FakeSerial()
        candidate.is_open = False
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn = _AdmissionInterruptConnection("COM-ADMISSION-INTERRUPT")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)
    conn._armed_window = window

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn._connect_attempt is None
    assert not conn._connect_in_progress
    if window == "release":
        assert conn.state is ConnectionState.CONNECTED
        assert observed == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]
        conn.disconnect()
    else:
        assert conn.state is ConnectionState.DISCONNECTED
        assert observed == []
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching

    conn.connect()
    try:
        assert conn.state is ConnectionState.CONNECTED
        assert conn._connect_attempt is None
    finally:
        conn.disconnect()


def test_redundant_connect_release_interruption_preserves_existing_connection() -> None:
    interruption = KeyboardInterrupt("redundant admission release interrupted")
    fired = False
    conn, existing = _make_conn()

    def interrupt_before_admission_release(frame, event: str, arg):
        nonlocal fired
        del arg
        attempt = frame.f_locals.get("attempt")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._release_connect_admission_locked.__code__
            and attempt is not None
            and not attempt.startup_started
            and conn._connect_attempt is attempt
            and conn.state is ConnectionState.CONNECTED
        ):
            fired = True
            raise interruption
        return interrupt_before_admission_release

    sys.settrace(interrupt_before_admission_release)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.connect()
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert conn.state is ConnectionState.CONNECTED
    assert conn._serial is existing and existing.is_open
    assert not existing.closed
    assert not conn._write_quarantined
    assert conn._connect_attempt is None
    assert conn.write_bytes_receipt(b"still-connected") == WriteReceipt(
        WriteDisposition.HOST_WRITE_COMPLETE,
        15,
        15,
    )
    conn.disconnect()


def test_connect_failure_handler_entry_interruption_readies_reserved_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_failure = serial.SerialException("synthetic open failure")
    interruption = KeyboardInterrupt("failure handler entry interrupted")
    fired = False

    class _OpenFailureSerial(_FakeSerial):
        def open(self) -> None:
            raise open_failure

    candidate = _OpenFailureSerial()
    candidate.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-FAILURE-HANDLER-TRACE")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)

    def interrupt_failure_handler(frame, event: str, arg):
        nonlocal fired
        del arg
        failure = frame.f_locals.get("failure")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._run_connect_attempt.__code__
            and isinstance(failure, serial_handler._ConnectFailure)
            and conn.state is ConnectionState.ERROR
        ):
            fired = True
            raise interruption
        return interrupt_failure_handler

    sys.settrace(interrupt_failure_handler)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.connect()
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert candidate.closed
    assert conn._connect_attempt is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_connect_failure_final_drain_control_exception_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_failure = serial.SerialException("synthetic open failure")
    interruption = KeyboardInterrupt("failure notification drain interrupted")

    class _OpenFailureSerial(_FakeSerial):
        def open(self) -> None:
            raise open_failure

    candidate = _OpenFailureSerial()
    candidate.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-FAILURE-DRAIN-INTERRUPT")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)
    original_drain = conn._drain_state_notifications
    armed = True

    def interrupt_final_drain() -> None:
        nonlocal armed
        if armed and observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]:
            armed = False
            raise interruption
        original_drain()

    monkeypatch.setattr(conn, "_drain_state_notifications", interrupt_final_drain)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert candidate.closed
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert conn._connect_attempt is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize(
    "open_failure",
    [serial.SerialException("serial open failure"), RuntimeError("runtime open failure")],
    ids=["serial-error", "unexpected-error"],
)
def test_connect_open_failure_transition_publication_gap_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
    open_failure: Exception,
) -> None:
    interruption = KeyboardInterrupt("post-transition publication interrupted")
    fired = False

    class _OpenFailureSerial(_FakeSerial):
        def open(self) -> None:
            raise open_failure

    candidate = _OpenFailureSerial()
    candidate.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-ERROR-PUBLICATION-TRACE")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)

    def interrupt_after_transition(frame, event: str, arg):
        nonlocal fired
        del arg
        attempt = frame.f_locals.get("attempt")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._connect_locked.__code__
            and frame.f_locals.get("exc") is open_failure
            and conn.state is ConnectionState.ERROR
            and attempt is not None
            and attempt.aborted_state_generation is None
            and "state_generation" in frame.f_locals
        ):
            fired = True
            raise interruption
        return interrupt_after_transition

    sys.settrace(interrupt_after_transition)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.connect()
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert candidate.closed
    assert conn._connect_attempt is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_candidate_is_owned_before_next_trace_line_and_settled_on_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("candidate publication trace interrupted")
    fired = False
    candidate = _FakeSerial()
    candidate.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-CANDIDATE-PUBLICATION-TRACE")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)

    def interrupt_after_candidate_publication(frame, event: str, arg):
        nonlocal fired
        del arg
        attempt = frame.f_locals.get("attempt")
        if (
            not fired
            and event == "line"
            and frame.f_code is SerialConnection._connect_locked.__code__
            and attempt is not None
            and attempt.candidate_serial is candidate
            and frame.f_locals.get("candidate_serial") is None
        ):
            fired = True
            raise interruption
        return interrupt_after_candidate_publication

    sys.settrace(interrupt_after_candidate_publication)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            conn.connect()
    finally:
        sys.settrace(None)

    assert fired
    assert caught.value is interruption
    assert observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert candidate.closed
    assert conn._connect_attempt is None
    assert conn._state_notifications == []


def test_prior_reader_join_interruption_is_settled_by_attempt_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("prior reader join interrupted")

    class _PriorReader:
        def __init__(self) -> None:
            self.alive = True
            self.join_calls = 0

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            assert timeout == 3.0
            self.join_calls += 1
            if self.join_calls == 1:
                raise interruption
            self.alive = False

    old = _FakeSerial()
    prior_reader = _PriorReader()
    fresh = _FakeSerial()
    fresh.is_open = False
    conn = SerialConnection("COM-PRIOR-READER-INTERRUPT")
    conn._serial = old
    conn._read_thread = prior_reader
    conn._state = ConnectionState.ERROR
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert prior_reader.join_calls == 2
    assert not prior_reader.alive
    assert old.closed
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert conn._read_thread is None
    assert conn._connect_attempt is None
    assert not conn._connect_in_progress

    conn.connect()
    try:
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh
    finally:
        conn.disconnect()


def test_connect_cleanup_failure_cannot_replace_original_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("original startup interruption")

    class _UnsettledCandidate(_FakeSerial):
        def open(self) -> None:
            self.is_open = True
            raise interruption

        def close(self) -> None:
            raise SystemExit("secondary cleanup interruption")

    candidate = _UnsettledCandidate()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-CLEANUP-INTERRUPT")

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is candidate
    assert conn._write_quarantined
    assert conn._connect_attempt is None
    assert not conn._connect_in_progress
    candidate.is_open = False
    conn.disconnect()


def test_abort_admission_release_failure_cannot_replace_original_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("original startup interruption")
    secondary = SystemExit("abort admission release interrupted")

    class _ReleaseInterruptedConnection(SerialConnection):
        def __init__(self, port: str) -> None:
            object.__setattr__(self, "_interrupt_release", False)
            super().__init__(port)

        def __setattr__(self, name: str, value: object) -> None:
            object.__setattr__(self, name, value)
            if (
                name == "_connect_attempt"
                and value is None
                and object.__getattribute__(self, "_interrupt_release")
            ):
                object.__setattr__(self, "_interrupt_release", False)
                raise secondary

    class _InterruptedCandidate(_FakeSerial):
        def open(self) -> None:
            self.is_open = True
            raise primary

    candidate = _InterruptedCandidate()
    candidate.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = _ReleaseInterruptedConnection("COM-ABORT-RELEASE-INTERRUPT")
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)
    conn._interrupt_release = True

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is primary
    assert observed == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert candidate.closed
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert conn._connect_attempt is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_ordinary_open_failure_retains_candidate_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_failure = serial.SerialException("ordinary open failure")
    factory_calls = 0

    class _UnsettledOpenFailure(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.allow_close = False
            self.close_calls = 0

        def open(self) -> None:
            self.is_open = True
            raise open_failure

        def close(self) -> None:
            self.close_calls += 1
            if not self.allow_close:
                raise RuntimeError("exclusive handle still busy")
            super().close()

    failed = _UnsettledOpenFailure()
    failed.is_open = False
    fresh = _FakeSerial()
    fresh.is_open = False

    def serial_factory() -> _FakeSerial:
        nonlocal factory_calls
        factory_calls += 1
        return failed if factory_calls == 1 else fresh

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn = SerialConnection("COM-ORDINARY-OPEN-CLOSE-FAILURE")

    with pytest.raises(serial.SerialException) as caught:
        conn.connect()

    assert caught.value is open_failure
    assert conn._serial is failed and failed.is_open
    assert failed.close_calls == 1
    assert conn._write_quarantined
    assert conn._connect_attempt is None

    with pytest.raises(serial.SerialException, match="could not be closed"):
        conn.connect()
    assert factory_calls == 1
    assert conn._serial is failed and failed.is_open
    assert failed.close_calls == 2

    failed.allow_close = True
    conn.connect()
    try:
        assert failed.close_calls == 3
        assert failed.closed
        assert factory_calls == 2
        assert conn._serial is fresh and fresh.is_open
        assert conn.state is ConnectionState.CONNECTED
    finally:
        conn.disconnect()


def test_reconnect_retains_unsettled_prior_handle_until_close_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("original startup interruption")
    factory_calls = 0

    class _UnsettledCandidate(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.allow_close = False
            self.close_calls = 0

        def open(self) -> None:
            self.is_open = True
            raise interruption

        def close(self) -> None:
            self.close_calls += 1
            if not self.allow_close:
                raise RuntimeError("exclusive handle still busy")
            super().close()

    candidate = _UnsettledCandidate()
    fresh = _FakeSerial()
    fresh.is_open = False

    def serial_factory() -> _FakeSerial:
        nonlocal factory_calls
        factory_calls += 1
        return candidate if factory_calls == 1 else fresh

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn = SerialConnection("COM-UNSETTLED-RETRY")

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn._serial is candidate and candidate.is_open
    assert candidate.close_calls == 1

    with pytest.raises(serial.SerialException, match="could not be closed"):
        conn.connect()

    assert factory_calls == 1
    assert conn._serial is candidate and candidate.is_open
    assert candidate.close_calls == 2
    assert conn.state is ConnectionState.ERROR
    assert conn._connect_attempt is None

    candidate.allow_close = True
    conn.connect()
    try:
        assert factory_calls == 2
        assert candidate.close_calls == 3
        assert candidate.closed
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh
    finally:
        conn.disconnect()


def test_connect_failure_observer_replacement_notifications_are_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_failure = serial.SerialException("synthetic initial open failure")

    class _FailedCandidate(_FakeSerial):
        def open(self) -> None:
            raise open_failure

    failed = _FailedCandidate()
    fresh = _FakeSerial()
    fresh.is_open = False
    candidates = iter((failed, fresh))
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: next(candidates))
    conn = SerialConnection("COM-FAILURE-REPLACEMENT")
    observed: list[ConnectionState] = []
    interruption = KeyboardInterrupt("replacement observer interruption")

    def replace_then_interrupt(_failure: Exception) -> None:
        conn.connect()
        raise interruption

    conn.on_state_change(observed.append)
    conn.on_error(replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    try:
        assert caught.value is interruption
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert conn._connect_attempt is None
        assert observed == [
            ConnectionState.CONNECTING,
            ConnectionState.ERROR,
            ConnectionState.CONNECTED,
        ]
        assert conn._state_notifications == []
        assert not conn._state_notification_dispatching
    finally:
        conn.disconnect()


def test_connected_observer_interruption_does_not_rollback_committed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("post-commit observer interruption")
    candidate = _FakeSerial()
    candidate.is_open = False
    conn = SerialConnection("COM-POST-COMMIT-INTERRUPT")

    def interrupt_connected(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            raise interruption

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn.on_state_change(interrupt_connected)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    try:
        assert caught.value is interruption
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is candidate and candidate.is_open
        assert conn._connect_attempt is None
        assert not conn._connect_in_progress
        assert conn.write_bytes_receipt(b"committed").disposition is (
            WriteDisposition.HOST_WRITE_COMPLETE
        )
    finally:
        conn.remove_state_callback(interrupt_connected)
        conn.disconnect()


def test_connected_observer_disconnect_interruption_delivers_nested_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("post-commit disconnect interruption")
    candidate = _FakeSerial()
    candidate.is_open = False
    conn = SerialConnection("COM-POST-COMMIT-DISCONNECT")
    observed: list[ConnectionState] = []

    def disconnect_then_interrupt(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            conn.disconnect()
            raise interruption

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn.on_state_change(disconnect_then_interrupt)
    conn.on_state_change(observed.append)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn.connect()

    assert caught.value is interruption
    assert conn.state is ConnectionState.DISCONNECTED
    assert candidate.closed
    assert observed == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_connecting_callback_and_logger_failure_cannot_wedge_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingHandler(logging.Handler):
        def emit(self, _record: logging.LogRecord) -> None:
            raise RuntimeError("logger exploded")

    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-CONNECTING-CALLBACK")

    def fail_on_connecting(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTING:
            raise ValueError("private callback detail")

    conn.on_state_change(fail_on_connecting)
    logger = logging.getLogger(serial_handler.__name__)
    prior_level = logger.level
    handler = _ExplodingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        conn.connect()
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._connect_in_progress
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        conn.disconnect()


def test_reentrant_connect_failure_preserves_outer_lifecycle_lock_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-REENTRANT-LOG-CONNECT")
    fresh = _FakeSerial()
    fresh.is_open = False
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    reentrant_errors: list[Exception] = []

    class _ConnectOnceHandler(logging.Handler):
        fired = False

        def emit(self, record: logging.LogRecord) -> None:
            if not self.fired and record.levelno == logging.INFO:
                self.fired = True
                _capture_exception(conn.connect, reentrant_errors)

    logger = logging.getLogger(serial_handler.__name__)
    prior_level = logger.level
    handler = _ConnectOnceHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        conn.connect()
        assert handler.fired
        assert len(reentrant_errors) == 1
        assert "already in progress" in str(reentrant_errors[0])
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert conn._connect_attempt is None
        assert conn._lifecycle_lock._recursion_count() == 0
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        conn.disconnect()


def test_disconnect_during_blocked_open_serializes_then_closes_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_started = threading.Event()
    allow_open = threading.Event()

    class _BlockingOpenSerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.is_open = False
            self.close_calls = 0

        def open(self) -> None:
            open_started.set()
            assert allow_open.wait(2.0)
            self.is_open = True

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    candidate = _BlockingOpenSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)
    conn = SerialConnection("COM-BLOCKED-OPEN")
    connect_errors: list[Exception] = []
    connector = threading.Thread(target=lambda: _capture_exception(conn.connect, connect_errors))
    connector.start()
    assert open_started.wait(1.0)

    disconnect_done = threading.Event()
    disconnector = threading.Thread(target=lambda: (conn.disconnect(), disconnect_done.set()))
    disconnector.start()
    assert not disconnect_done.wait(0.05)
    assert conn.state is ConnectionState.CONNECTING

    allow_open.set()
    connector.join(timeout=2.0)
    disconnector.join(timeout=2.0)
    assert not connector.is_alive()
    assert not disconnector.is_alive()
    assert disconnect_done.is_set()
    assert connect_errors == []
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None
    assert not conn._connect_in_progress
    assert not candidate.is_open
    assert candidate.close_calls >= 1


def test_disconnect_during_connecting_callback_wins_without_stale_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    states: list[ConnectionState] = []
    connect_errors: list[Exception] = []
    serial_factory_called = False
    conn = SerialConnection("COM-CONNECT-CANCEL")

    def serial_factory() -> _FakeSerial:
        nonlocal serial_factory_called
        serial_factory_called = True
        return _FakeSerial()

    def block_connecting(state: ConnectionState) -> None:
        states.append(state)
        if state is ConnectionState.CONNECTING:
            callback_entered.set()
            assert allow_callback.wait(2.0)

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    conn.on_state_change(block_connecting)
    opener = threading.Thread(
        target=lambda: _capture_exception(conn.connect, connect_errors)
    )
    opener.start()
    assert callback_entered.wait(1.0)

    conn.disconnect()
    allow_callback.set()
    opener.join(timeout=2.0)

    assert not opener.is_alive()
    assert len(connect_errors) == 1
    assert "changed before startup" in str(connect_errors[0])
    assert states == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None and not serial_factory_called
    assert not conn._connect_in_progress


def test_connect_cannot_publish_until_older_disconnect_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    class _BlockingDisconnectConnection(SerialConnection):
        def __init__(self, port: str) -> None:
            super().__init__(port)
            self.block_next_release = True

        def _release_serial(
            self,
            *,
            expected_identity: tuple[object | None, int] | None = None,
        ) -> bool:
            if self.block_next_release:
                self.block_next_release = False
                cleanup_started.set()
                assert allow_cleanup.wait(2.0)
            return super()._release_serial(expected_identity=expected_identity)

    old = _FakeSerial()
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = _BlockingDisconnectConnection("COM-DISCONNECT-ORDER")
    conn._serial = old
    conn._state = ConnectionState.ERROR

    disconnect_done = threading.Event()
    disconnector = threading.Thread(target=lambda: (conn.disconnect(), disconnect_done.set()))
    disconnector.start()
    assert cleanup_started.wait(1.0)

    connect_errors: list[Exception] = []
    connector = threading.Thread(target=lambda: _capture_exception(conn.connect, connect_errors))
    connector.start()
    time.sleep(0.05)
    assert conn._serial is old
    assert not disconnect_done.is_set()
    assert fresh.writes == []

    allow_cleanup.set()
    disconnector.join(timeout=2.0)
    connector.join(timeout=2.0)
    assert not disconnector.is_alive() and not connector.is_alive()
    assert disconnect_done.is_set()
    assert connect_errors == []
    try:
        assert old.closed
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh
        assert fresh.is_open
    finally:
        conn.disconnect()


def test_connected_fast_path_waits_for_in_progress_disconnect_then_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    class _BlockingDisconnectConnection(SerialConnection):
        def __init__(self, port: str) -> None:
            super().__init__(port)
            self.block_next_release = True

        def _release_serial(
            self,
            *,
            expected_identity: tuple[object | None, int] | None = None,
        ) -> bool:
            if self.block_next_release:
                self.block_next_release = False
                cleanup_started.set()
                assert allow_cleanup.wait(2.0)
            return super()._release_serial(expected_identity=expected_identity)

    old = _FakeSerial()
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = _BlockingDisconnectConnection("COM-CONNECTED-DISCONNECT-ORDER")
    conn._serial = old
    conn._state = ConnectionState.CONNECTED

    disconnector = threading.Thread(target=conn.disconnect)
    disconnector.start()
    assert cleanup_started.wait(1.0)

    connect_errors: list[Exception] = []
    connector = threading.Thread(
        target=lambda: _capture_exception(conn.connect, connect_errors)
    )
    connector.start()
    time.sleep(0.05)
    assert connector.is_alive()

    allow_cleanup.set()
    disconnector.join(timeout=2.0)
    connector.join(timeout=2.0)
    try:
        assert not disconnector.is_alive() and not connector.is_alive()
        assert connect_errors == []
        assert old.closed
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
    finally:
        conn.disconnect()


@pytest.mark.parametrize(
    "open_failure",
    [serial.SerialException("open failed"), RuntimeError("startup failed")],
)
def test_failed_connect_retains_ownership_until_cleanup_and_error_transition(
    monkeypatch: pytest.MonkeyPatch,
    open_failure: Exception,
) -> None:
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    class _OpenFailureSerial(_FakeSerial):
        def open(self) -> None:
            raise open_failure

    class _BlockingCleanupConnection(SerialConnection):
        def __init__(self, port: str) -> None:
            super().__init__(port)
            self.release_calls = 0

        def _release_serial(
            self,
            *,
            expected_identity: tuple[object | None, int] | None = None,
        ) -> bool:
            self.release_calls += 1
            if self.release_calls == 2:
                cleanup_started.set()
                assert allow_cleanup.wait(2.0)
            return super()._release_serial(expected_identity=expected_identity)

    failed = _OpenFailureSerial()
    fresh = _FakeSerial()
    serial_instances = [failed, fresh]
    monkeypatch.setattr(
        serial_handler.serial,
        "Serial",
        lambda: serial_instances.pop(0),
    )
    conn = _BlockingCleanupConnection("COM-FAILED-STARTUP")
    first_errors: list[Exception] = []
    first = threading.Thread(target=lambda: _capture_exception(conn.connect, first_errors))
    first.start()
    assert cleanup_started.wait(1.0)

    with pytest.raises(RuntimeError, match="already in progress"):
        conn.connect()
    assert fresh.writes == []
    assert len(serial_instances) == 1

    allow_cleanup.set()
    first.join(timeout=2.0)
    assert not first.is_alive()
    assert first_errors == [open_failure]
    assert conn.state is ConnectionState.ERROR
    assert not conn._connect_in_progress

    conn.connect()
    try:
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh
    finally:
        conn.disconnect()


def test_connected_callback_can_write_before_connect_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-CONNECTED-CALLBACK")
    receipts: list[WriteReceipt] = []

    def write_on_connected(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            receipts.append(conn.write_bytes_receipt(b"first-command"))

    conn.on_state_change(write_on_connected)
    conn.connect()
    try:
        assert [receipt.disposition for receipt in receipts] == [
            WriteDisposition.HOST_WRITE_COMPLETE
        ]
        assert fresh.writes == [b"first-command"]
    finally:
        conn.disconnect()


def test_connected_callback_can_wait_while_reader_callback_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_ready = threading.Event()

    class _ReplySerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.reply_read = False

        @property
        def in_waiting(self) -> int:
            return 6 if reply_ready.is_set() and not self.reply_read else 0

        def read(self, _size: int) -> bytes:
            if reply_ready.is_set() and not self.reply_read:
                self.reply_read = True
                return b"reply\n"
            time.sleep(0.002)
            return b""

        def write(self, payload: bytes) -> int:
            reported = super().write(payload)
            reply_ready.set()
            return reported

    fresh = _ReplySerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-CONNECTED-REPLY")
    reader_disconnected = threading.Event()
    wait_results: list[bool] = []

    def disconnect_on_reply(_line: str) -> None:
        conn.disconnect()
        reader_disconnected.set()

    def request_on_connected(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            receipt = conn.write_bytes_receipt(b"request")
            assert receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE
            wait_results.append(reader_disconnected.wait(1.0))

    conn.on_line(disconnect_on_reply)
    conn.on_state_change(request_on_connected)
    conn.connect()

    assert wait_results == [True]
    assert reader_disconnected.is_set()
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None
    assert fresh.closed
    assert fresh.writes == [b"request"]


def test_same_generation_reader_error_cannot_overtake_queued_write_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_reader = threading.Event()
    reader_released = threading.Event()

    class _WriteThenReadFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            assert fail_reader.wait(2.0)
            raise serial.SerialException("raw reader detail")

        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            fail_reader.set()
            return 0

        def close(self) -> None:
            super().close()
            reader_released.set()

    fresh = _WriteThenReadFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-SAME-GENERATION-ERRORS")
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    receipts: list[WriteReceipt] = []
    reader_error_delivered = threading.Event()
    conn.on_state_change(states.append)

    def capture_error(error: Exception) -> None:
        errors.append(error)
        if not isinstance(error, IncompleteSerialWrite):
            reader_error_delivered.set()

    conn.on_error(capture_error)

    def fail_write_on_connected(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            receipts.append(conn.write_bytes_receipt(b"request"))
            assert reader_released.wait(1.0)
            assert errors == []

    conn.on_state_change(fail_write_on_connected)
    conn.connect()

    assert [receipt.disposition for receipt in receipts] == [
        WriteDisposition.DEFINITELY_NOT_WRITTEN
    ]
    assert states == [
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
        ConnectionState.ERROR,
    ]
    assert reader_error_delivered.wait(1.0)
    assert len(errors) == 2
    assert isinstance(errors[0], IncompleteSerialWrite)
    assert isinstance(errors[1], serial.SerialException)
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None and fresh.closed
    assert conn._state_notifications == []


def test_connect_error_callback_nested_reconnect_has_no_lifecycle_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_ready = threading.Event()

    class _FailOpenSerial(_FakeSerial):
        def open(self) -> None:
            raise serial.SerialException("initial open failure")

    class _ReplySerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.reply_read = False

        @property
        def in_waiting(self) -> int:
            return 6 if reply_ready.is_set() and not self.reply_read else 0

        def read(self, _size: int) -> bytes:
            if reply_ready.is_set() and not self.reply_read:
                self.reply_read = True
                return b"reply\n"
            time.sleep(0.002)
            return b""

        def write(self, payload: bytes) -> int:
            reported = super().write(payload)
            reply_ready.set()
            return reported

    failed = _FailOpenSerial()
    fresh = _ReplySerial()
    serial_instances = [failed, fresh]
    monkeypatch.setattr(
        serial_handler.serial,
        "Serial",
        lambda: serial_instances.pop(0),
    )
    conn = SerialConnection("COM-NESTED-ERROR-RECONNECT")
    reader_disconnected = threading.Event()
    connected_waits: list[bool] = []
    conn.on_line(lambda _line: (conn.disconnect(), reader_disconnected.set()))

    def request_on_connected(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            receipt = conn.write_bytes_receipt(b"request")
            assert receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE
            connected_waits.append(reader_disconnected.wait(1.0))

    conn.on_state_change(request_on_connected)
    conn.on_error(lambda _error: conn.connect())

    with pytest.raises(serial.SerialException, match="initial open failure"):
        conn.connect()

    assert connected_waits == [True]
    assert reader_disconnected.is_set()
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None
    assert failed.closed and fresh.closed


def test_connected_fanout_stops_if_first_callback_quarantines_incarnation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _FakeSerial(0)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-CONNECTED-FAILURE")
    events: list[tuple[str, ConnectionState]] = []

    def first(state: ConnectionState) -> None:
        events.append(("first", state))
        if state is ConnectionState.CONNECTED:
            receipt = conn.write_bytes_receipt(b"first-command")
            assert receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN

    conn.on_state_change(first)
    conn.on_state_change(lambda state: events.append(("second", state)))
    conn.connect()
    try:
        assert conn.state is ConnectionState.ERROR
        assert ("second", ConnectionState.ERROR) in events
        assert ("second", ConnectionState.CONNECTED) not in events
        assert fresh.writes == [b"first-command"]
    finally:
        conn.disconnect()


def test_concurrent_disconnect_cannot_notify_before_checked_connected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_fence_checked = threading.Event()
    allow_connected_callback = threading.Event()

    class _PausedConnectedNotification(SerialConnection):
        def __init__(self, port: str) -> None:
            super().__init__(port)
            self.connected_fence_paused = False

        def _state_notification_is_current(self, notification) -> bool:
            current = super()._state_notification_is_current(notification)
            if (
                notification.kind == "connected"
                and not self.connected_fence_paused
                and current
            ):
                self.connected_fence_paused = True
                connected_fence_checked.set()
                assert allow_connected_callback.wait(2.0)
            return current

    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = _PausedConnectedNotification("COM-STATE-ORDER")
    states: list[ConnectionState] = []
    conn.on_state_change(states.append)

    connector = threading.Thread(target=conn.connect)
    connector.start()
    assert connected_fence_checked.wait(1.0)

    disconnector = threading.Thread(target=conn.disconnect)
    disconnector.start()
    deadline = time.monotonic() + 1.0
    while conn.state is not ConnectionState.DISCONNECTED:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    disconnector.join(timeout=1.0)
    assert not disconnector.is_alive()
    assert states == [ConnectionState.CONNECTING]

    allow_connected_callback.set()
    connector.join(timeout=2.0)
    disconnector.join(timeout=2.0)
    assert not connector.is_alive() and not disconnector.is_alive()
    assert states == [
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
    ]
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn._serial is None and fresh.closed


def test_blocked_state_callback_keeps_pending_notification_queue_bounded() -> None:
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    states: list[ConnectionState] = []
    conn = SerialConnection("COM-BOUNDED-STATE-QUEUE")

    def block_first(state: ConnectionState) -> None:
        states.append(state)
        if len(states) == 1:
            callback_entered.set()
            assert allow_callback.wait(2.0)

    conn.on_state_change(block_first)
    changed, generation = conn._transition_state(ConnectionState.CONNECTING)
    assert changed
    dispatcher = threading.Thread(
        target=lambda: conn._emit_state_change(
            ConnectionState.CONNECTING,
            generation,
        )
    )
    dispatcher.start()
    assert callback_entered.wait(1.0)

    for index in range(300):
        state = (
            ConnectionState.CONNECTED
            if index % 2 == 0
            else ConnectionState.DISCONNECTED
        )
        changed, generation = conn._transition_state(state)
        assert changed
        conn._emit_state_change(state, generation)

    assert len(conn._state_notifications) <= (
        serial_handler._MAX_PENDING_STATE_NOTIFICATIONS
    )
    allow_callback.set()
    dispatcher.join(timeout=2.0)

    assert not dispatcher.is_alive()
    assert conn._state_notifications == []
    assert states[-1] is conn.state


def test_connect_fails_closed_when_previous_reader_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StuckReader:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            del timeout
            pass

    conn, old = _make_conn()
    conn._state = ConnectionState.ERROR
    conn._read_thread = _StuckReader()
    serial_factory_called = False

    def serial_factory() -> _FakeSerial:
        nonlocal serial_factory_called
        serial_factory_called = True
        return _FakeSerial()

    monkeypatch.setattr(serial_handler.serial, "Serial", serial_factory)
    with pytest.raises(serial.SerialException, match="did not stop"):
        conn.connect()

    assert not serial_factory_called
    assert conn._serial is old
    assert old.is_open
    assert conn.state is ConnectionState.ERROR
    with pytest.raises(RuntimeError, match="quarantined"):
        conn.write_bytes_receipt(b"must-not-use-stuck-reader-handle")


def test_immediate_new_reader_failure_cannot_leave_false_connected_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_failed = threading.Event()

    class _ImmediateFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise serial.SerialException("immediate read failure")

    fresh = _ImmediateFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-IMMEDIATE-FAIL")
    states: list[ConnectionState] = []
    conn.on_state_change(states.append)
    conn.on_error(lambda _error: reader_failed.set())
    conn.connect()

    assert reader_failed.wait(1.0)
    assert states[0] is ConnectionState.CONNECTING
    assert ConnectionState.ERROR in states
    if ConnectionState.CONNECTED in states:
        assert states.index(ConnectionState.CONNECTED) < states.index(ConnectionState.ERROR)
    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert fresh.closed
    with pytest.raises(RuntimeError, match="Not connected"):
        conn.write_bytes_receipt(b"must-not-write")


def test_stale_connected_reservation_cannot_block_reader_error_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_check_entered = threading.Event()
    allow_connected_check = threading.Event()
    reader_error_delivered = threading.Event()

    class _ImmediateFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise serial.SerialException("immediate read failure")

    class _PausedConnection(SerialConnection):
        def _connection_incarnation_is_current(self, *args, **kwargs) -> bool:
            connected_check_entered.set()
            assert allow_connected_check.wait(2.0)
            return super()._connection_incarnation_is_current(*args, **kwargs)

    fresh = _ImmediateFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = _PausedConnection("COM-STALE-CONNECTED-EVENT")
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(lambda error: (errors.append(error), reader_error_delivered.set()))

    connector = threading.Thread(target=conn.connect)
    connector.start()
    assert connected_check_entered.wait(1.0)
    deadline = time.monotonic() + 1.0
    while conn.state is not ConnectionState.ERROR:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    # The newer ERROR generation cancels and drains the stale unready CONNECTED entry, so the
    # observer is not hostage to the paused older emitter.
    assert reader_error_delivered.wait(1.0)

    allow_connected_check.set()
    connector.join(timeout=2.0)
    assert not connector.is_alive()
    assert states == [ConnectionState.CONNECTING, ConnectionState.ERROR]
    assert len(errors) == 1
    assert conn._state_notifications == []

    conn.disconnect()
    assert states[-1] is ConnectionState.DISCONNECTED


@pytest.mark.parametrize(
    "reader_exc",
    [
        serial.SerialException("reader echoed TRANSPORT-SECRET"),
        RuntimeError("reader echoed TRANSPORT-SECRET"),
    ],
    ids=["serial-error", "unexpected-error"],
)
def test_reader_failure_cleanup_survives_hostile_logger_without_detail_leak(
    monkeypatch: pytest.MonkeyPatch,
    reader_exc: Exception,
) -> None:
    reader_failed = threading.Event()
    records: list[logging.LogRecord] = []

    class _ReadFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise reader_exc

    class _ExplodingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)
            if record.getMessage().startswith("Serial reader stopped"):
                raise RuntimeError("hostile logger detail")

    fresh = _ReadFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn = SerialConnection("COM-READER-LOGGER")
    conn.on_error(lambda _error: reader_failed.set())
    logger = logging.getLogger(serial_handler.__name__)
    prior_level = logger.level
    handler = _ExplodingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        conn.connect()
        assert reader_failed.wait(1.0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert conn.state is ConnectionState.ERROR
    assert conn._serial is None
    assert fresh.closed
    assert "TRANSPORT-SECRET" not in repr(
        [(record.msg, record.args) for record in records]
    )


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_reader_failure_logger_control_exception_readies_reserved_error(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("reader logger interrupted")
    reader_failure = serial.SerialException("private reader transport detail")
    conn, failed = _make_conn()
    observed: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(observed.append)
    conn.on_error(errors.append)
    monkeypatch.setattr(
        conn,
        "_safe_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption),
    )

    with pytest.raises(interruption_type) as caught:
        conn._handle_reader_failure(reader_failure, "sanitized reader failure")

    assert caught.value is interruption
    assert observed == [ConnectionState.ERROR]
    assert errors == [reader_failure]
    assert failed.closed
    assert conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_reader_failure_interruption_before_transition_completes_error(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("reader transition handoff interrupted")
    reader_failure = serial.SerialException("private reader transport detail")
    conn, failed = _make_conn()
    observed: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(observed.append)
    conn.on_error(errors.append)
    original_transition = conn._transition_state_locked
    fired = False

    def interrupt_before_transition(
        new_state: ConnectionState,
        **kwargs: object,
    ):
        nonlocal fired
        if not fired and new_state is ConnectionState.ERROR:
            fired = True
            raise interruption
        return original_transition(new_state, **kwargs)

    monkeypatch.setattr(conn, "_transition_state_locked", interrupt_before_transition)

    with pytest.raises(interruption_type) as caught:
        conn._handle_reader_failure(reader_failure, "sanitized reader failure")

    assert caught.value is interruption
    assert fired
    assert observed == [ConnectionState.ERROR]
    assert errors == [reader_failure]
    assert failed.closed
    assert conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_reader_failure_observer_interruption_cannot_close_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("reader observer interrupted after replacement")
    reader_failure = serial.SerialException("failed reader incarnation")
    conn, failed = _make_conn()
    fresh = _FakeSerial()
    fresh.is_open = False
    observed: list[ConnectionState] = []
    replaced = False

    def replace_then_interrupt(_error: Exception) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            conn.connect()
            raise interruption

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.on_state_change(observed.append)
    conn.on_error(replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn._handle_reader_failure(reader_failure, "sanitized reader failure")

    try:
        assert caught.value is interruption
        assert failed.closed
        assert observed == [
            ConnectionState.ERROR,
            ConnectionState.CONNECTED,
        ]
        assert conn.state is ConnectionState.CONNECTED
        assert conn._serial is fresh and fresh.is_open
        assert not fresh.closed
        assert conn._state_notifications == []
        assert not conn._state_notification_dispatching
    finally:
        conn.disconnect()


def test_reader_error_callback_cannot_write_on_known_failed_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_finished = threading.Event()
    callback_errors: list[Exception] = []

    class _ReadFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise serial.SerialException("read failure")

    failed = _ReadFailureSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: failed)
    conn = SerialConnection("COM-READER-NO-LATE-TX")

    def write_on_error(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            _capture_exception(
                lambda: conn.write_bytes_receipt(b"late"),
                callback_errors,
            )
            callback_finished.set()

    conn.on_state_change(write_on_error)
    conn.connect()

    assert callback_finished.wait(1.0)
    assert len(callback_errors) == 1
    assert "Not connected" in str(callback_errors[0])
    assert failed.writes == []
    assert failed.closed and conn._serial is None


def test_reader_error_callback_reconnect_rejection_does_not_wedge_later_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_failed = threading.Event()
    reconnect_errors: list[Exception] = []
    observed_states: list[ConnectionState] = []

    class _ReadFailureSerial(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise serial.SerialException("read failure")

    failed = _ReadFailureSerial()
    serial_instances: list[_FakeSerial] = [failed]
    monkeypatch.setattr(
        serial_handler.serial,
        "Serial",
        lambda: serial_instances.pop(0),
    )
    conn = SerialConnection("COM-READER-RECONNECT")

    def reconnect_on_error(state: ConnectionState) -> None:
        if state is ConnectionState.ERROR:
            _capture_exception(conn.connect, reconnect_errors)
            reader_failed.set()

    conn.on_state_change(reconnect_on_error)
    conn.on_state_change(observed_states.append)
    conn.connect()
    assert reader_failed.wait(1.0)
    assert reconnect_errors
    assert "reader thread" in str(reconnect_errors[0])
    assert observed_states[-1] is ConnectionState.ERROR

    deadline = time.monotonic() + 1.0
    while conn._read_thread is not None and conn._read_thread.is_alive():
        assert time.monotonic() < deadline
        time.sleep(0.002)

    fresh = _FakeSerial()
    serial_instances.append(fresh)
    conn.connect()
    try:
        assert conn.state is ConnectionState.CONNECTED
        assert conn.write_bytes_receipt(b"after-error").disposition is (
            WriteDisposition.HOST_WRITE_COMPLETE
        )
    finally:
        conn.disconnect()


def _capture_exception(callable_obj, errors: list[Exception]) -> None:
    try:
        callable_obj()
    except Exception as exc:
        errors.append(exc)


def test_disconnect_cannot_close_handle_until_flush_finishes() -> None:
    flush_entered = threading.Event()
    allow_flush = threading.Event()

    class _BlockingFlushSerial(_FakeSerial):
        def flush(self) -> None:
            self.flushes += 1
            flush_entered.set()
            assert allow_flush.wait(2.0)

    conn = SerialConnection("COM-BARRIER")
    fake = _BlockingFlushSerial()
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED
    receipts: list[WriteReceipt] = []
    writer = threading.Thread(target=lambda: receipts.append(conn.write_bytes_receipt(b"abc")))
    writer.start()
    assert flush_entered.wait(1.0)

    disconnected = threading.Event()
    closer = threading.Thread(target=lambda: (conn.disconnect(), disconnected.set()))
    closer.start()
    assert not disconnected.wait(0.05)
    assert fake.is_open and not fake.closed

    allow_flush.set()
    writer.join(timeout=2.0)
    closer.join(timeout=2.0)
    assert not writer.is_alive() and not closer.is_alive()
    assert receipts[0].disposition is WriteDisposition.HOST_WRITE_COMPLETE
    assert fake.closed and disconnected.is_set()


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_disconnect_interruption_after_close_completes_disconnected(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("disconnect teardown interrupted")
    conn, initial = _make_conn()
    observed: list[ConnectionState] = []
    conn.on_state_change(observed.append)
    original_release = conn._release_serial
    fired = False

    def close_then_interrupt(
        *,
        expected_identity: tuple[object | None, int] | None = None,
    ) -> bool:
        nonlocal fired
        released = original_release(expected_identity=expected_identity)
        if not fired:
            fired = True
            raise interruption
        return released

    monkeypatch.setattr(conn, "_release_serial", close_then_interrupt)

    with pytest.raises(interruption_type) as caught:
        conn.disconnect()

    assert caught.value is interruption
    assert fired
    assert initial.closed
    assert conn._serial is None
    assert conn.state is ConnectionState.DISCONNECTED
    assert observed == [ConnectionState.DISCONNECTED]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_concurrent_writer_queued_behind_failure_cannot_touch_same_incarnation() -> None:
    write_entered = threading.Event()
    allow_result = threading.Event()

    class _BlockingZeroSerial(_FakeSerial):
        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            write_entered.set()
            assert allow_result.wait(2.0)
            return 0

    conn = SerialConnection("COM-QUEUED")
    fake = _BlockingZeroSerial()
    conn._serial = fake
    conn._state = ConnectionState.CONNECTED
    receipts: list[WriteReceipt] = []
    errors: list[Exception] = []

    first = threading.Thread(
        target=lambda: receipts.append(conn.write_bytes_receipt(b"first"))
    )
    second = threading.Thread(
        target=lambda: _capture_exception(
            lambda: conn.write_bytes_receipt(b"second"),
            errors,
        )
    )
    first.start()
    assert write_entered.wait(1.0)
    second.start()
    assert second.is_alive()

    allow_result.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert not first.is_alive() and not second.is_alive()
    assert receipts[0].disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
    assert len(errors) == 1 and "quarantined" in str(errors[0])
    assert fake.writes == [b"first"]


def test_successful_reconnect_clears_failed_incarnation_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, failed = _make_conn(0)
    assert conn.write_bytes_receipt(b"first").disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN

    fresh = _FakeSerial()
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.connect()
    try:
        receipt = conn.write_bytes_receipt(b"second")
        assert receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE
        assert fresh.writes == [b"second"]
        assert failed.closed
    finally:
        conn.disconnect()


@pytest.mark.parametrize(
    ("raw", "interruption_type"),
    [
        (False, KeyboardInterrupt),
        (False, SystemExit),
        (True, KeyboardInterrupt),
        (True, SystemExit),
    ],
    ids=["line-keyboard", "line-system-exit", "bytes-keyboard", "bytes-system-exit"],
)
def test_reader_callback_control_flow_fails_closed_for_whole_read_iteration(
    raw: bool,
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("reader callback stopped the loop")

    class _OneChunkSerial(_FakeSerial):
        @property
        def in_waiting(self) -> int:
            return 2

        def read(self, _size: int) -> bytes:
            return b"x\n"

    conn = SerialConnection("COM-READER-CALLBACK-CONTROL", raw=raw)
    failed = _OneChunkSerial()
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    callback_calls: list[object] = []
    states: list[ConnectionState] = []
    errors: list[Exception] = []

    def interrupt_callback(value: object) -> None:
        callback_calls.append(value)
        raise interruption

    if raw:
        conn.on_bytes(interrupt_callback)
    else:
        conn.on_line(interrupt_callback)
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    with pytest.raises(interruption_type) as caught:
        conn._reader_loop()

    assert caught.value is interruption
    assert callback_calls == [b"x\n" if raw else "x"]
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1 and isinstance(errors[0], serial.SerialException)
    assert failed.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("window", ["before", "after"])
def test_reader_async_control_window_around_line_callback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    interruption = KeyboardInterrupt(f"reader {window}-callback window")

    class _OneLineSerial(_FakeSerial):
        @property
        def in_waiting(self) -> int:
            return 2

        def read(self, _size: int) -> bytes:
            return b"x\n"

    conn = SerialConnection("COM-READER-CALLBACK-WINDOW")
    failed = _OneLineSerial()
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    calls: list[str] = []
    errors: list[Exception] = []
    conn.on_line(calls.append)
    conn.on_error(errors.append)
    original_emit = conn._emit_line

    def interrupt_around_emit(line: str) -> None:
        if window == "before":
            raise interruption
        original_emit(line)
        raise interruption

    monkeypatch.setattr(conn, "_emit_line", interrupt_around_emit)

    with pytest.raises(KeyboardInterrupt) as caught:
        conn._reader_loop()

    assert caught.value is interruption
    assert calls == ([] if window == "before" else ["x"])
    assert len(errors) == 1
    assert failed.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("phase", ["state", "error"])
def test_builtin_control_callback_is_consumed_without_poison_replay(phase: str) -> None:
    conn, fake = _make_conn(0)
    states: list[ConnectionState] = []
    if phase == "state":
        conn.on_state_change(sys.exit)
    else:
        conn.on_state_change(states.append)
        conn.on_error(sys.exit)

    with pytest.raises(SystemExit) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert fake.flushes == 0
    assert states == ([] if phase == "state" else [ConnectionState.ERROR])
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching
    conn._drain_state_notifications()
    assert conn._state_notifications == []


def test_write_failure_callback_receipts_are_isolated_from_transaction_truth() -> None:
    conn, fake = _make_conn(0)
    callback_receipts: list[WriteReceipt] = []

    def poison_receipt(error: Exception) -> None:
        callback_receipts.append(error.receipt)
        object.__setattr__(error.receipt, "disposition", WriteDisposition.HOST_WRITE_COMPLETE)
        object.__setattr__(error.receipt, "bytes_reported", error.receipt.bytes_requested)
        object.__setattr__(error.receipt, "safe_error_code", None)

    def capture_pristine(error: Exception) -> None:
        callback_receipts.append(error.receipt)

    conn.on_error(poison_receipt)
    conn.on_error(capture_pristine)
    receipt = conn.write_bytes_receipt(b"one-attempt")

    assert fake.writes == [b"one-attempt"]
    assert receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
    assert callback_receipts[0].disposition is WriteDisposition.HOST_WRITE_COMPLETE
    assert callback_receipts[1].safe_error_code is WriteErrorCode.ZERO_WRITE
    assert callback_receipts[1] is not callback_receipts[0]
    assert receipt is not callback_receipts[0]
    assert receipt is not callback_receipts[1]


def test_callback_receipt_mutation_and_reconnect_cannot_forge_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, failed = _make_conn(0)
    fresh = _FakeSerial()
    fresh.is_open = False
    poisoned: list[WriteReceipt] = []

    def poison_then_replace(error: Exception) -> None:
        poisoned.append(error.receipt)
        object.__setattr__(error.receipt, "disposition", WriteDisposition.HOST_WRITE_COMPLETE)
        object.__setattr__(error.receipt, "bytes_reported", error.receipt.bytes_requested)
        object.__setattr__(error.receipt, "safe_error_code", None)
        conn.connect()

    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    conn.on_error(poison_then_replace)
    receipt = conn.write_bytes_receipt(b"old")

    try:
        assert receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
        assert receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
        assert poisoned[0].disposition is WriteDisposition.HOST_WRITE_COMPLETE
        assert failed.closed
        assert conn._serial is fresh and fresh.is_open
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
    finally:
        conn.disconnect()


def test_legacy_receipt_is_not_aliased_to_mutable_callback_receipt() -> None:
    conn, _fake = _make_conn(0)
    callback_receipts: list[WriteReceipt] = []

    def poison_receipt(error: Exception) -> None:
        callback_receipts.append(error.receipt)
        object.__setattr__(error.receipt, "disposition", WriteDisposition.HOST_WRITE_COMPLETE)
        object.__setattr__(error.receipt, "bytes_reported", error.receipt.bytes_requested)
        object.__setattr__(error.receipt, "safe_error_code", None)

    conn.on_error(poison_receipt)
    with pytest.raises(IncompleteSerialWrite) as caught:
        conn.write_bytes(b"old")

    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert caught.value.receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
    assert caught.value.receipt is not callback_receipts[0]


def test_reentrant_adapter_write_failure_cannot_quarantine_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-REENTRANT-WRITE-REPLACEMENT")
    fresh = _FakeSerial()
    fresh.is_open = False
    states: list[ConnectionState] = []
    errors: list[Exception] = []

    class _ReplacingZeroSerial(_FakeSerial):
        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            conn.disconnect()
            conn.connect()
            return 0

    old = _ReplacingZeroSerial()
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)

    receipt = conn.write_bytes_receipt(b"old")

    try:
        assert receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
        assert old.writes == [b"old"]
        assert old.closed
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert ConnectionState.ERROR not in states
        assert errors == []
    finally:
        conn.disconnect()


def test_reentrant_full_write_replacement_is_not_flushed_or_marked_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-REENTRANT-FULL-WRITE")
    states: list[ConnectionState] = []

    class _ReplacingWriteSerial(_FakeSerial):
        replaced = False

        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            if not self.replaced:
                self.replaced = True
                conn.disconnect()
                conn.connect()
            return len(payload)

    reused = _ReplacingWriteSerial()
    conn._serial = reused
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_state_change(states.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: reused)

    receipt = conn.write_bytes_receipt(b"old-incarnation")

    try:
        assert receipt == WriteReceipt(
            WriteDisposition.DELIVERY_UNCERTAIN,
            15,
            15,
            WriteErrorCode.FLUSH_ERROR,
        )
        assert reused.writes == [b"old-incarnation"]
        assert reused.flushes == 0
        assert conn._serial is reused and reused.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert states == [
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
        ]
    finally:
        conn.disconnect()


@pytest.mark.parametrize("reuse_handle", [False, True])
def test_reentrant_flush_replacement_cannot_publish_stale_host_complete(
    monkeypatch: pytest.MonkeyPatch,
    reuse_handle: bool,
) -> None:
    conn = SerialConnection("COM-REENTRANT-FLUSH")
    states: list[ConnectionState] = []
    errors: list[Exception] = []

    class _ReplacingFlushSerial(_FakeSerial):
        replaced = False

        def flush(self) -> None:
            self.flushes += 1
            if not self.replaced:
                self.replaced = True
                conn.disconnect()
                conn.connect()

    old = _ReplacingFlushSerial()
    fresh = old if reuse_handle else _FakeSerial()
    if fresh is not old:
        fresh.is_open = False
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    payload = b"old-flush"

    receipt = conn.write_bytes_receipt(payload)

    try:
        assert receipt == WriteReceipt(
            WriteDisposition.DELIVERY_UNCERTAIN,
            len(payload),
            len(payload),
            WriteErrorCode.FLUSH_ERROR,
        )
        assert old.writes == [payload]
        assert old.flushes == 1
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert conn._write_transaction is None
        assert ConnectionState.ERROR not in states
        assert errors == []
    finally:
        conn.disconnect()


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["write", "flush"])
def test_queued_writer_cannot_enter_interrupted_incarnation_before_settlement(
    interruption_type: type[BaseException],
    stage: str,
) -> None:
    reached_settlement = threading.Event()
    allow_settlement = threading.Event()
    interruption = interruption_type(f"interrupted {stage}")

    class _PausedSettlementConnection(SerialConnection):
        def _settle_escaped_write_interruption(self, transaction, exc) -> None:
            reached_settlement.set()
            assert allow_settlement.wait(2.0)
            super()._settle_escaped_write_interruption(transaction, exc)

    class _InterruptedSerial(_FakeSerial):
        interrupted = False

        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            if stage == "write" and not self.interrupted:
                self.interrupted = True
                raise interruption
            return len(payload)

        def flush(self) -> None:
            self.flushes += 1
            if stage == "flush" and not self.interrupted:
                self.interrupted = True
                raise interruption

    conn = _PausedSettlementConnection("COM-QUEUED-CONTROL-WRITE")
    failed = _InterruptedSerial()
    conn._serial = failed
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    first_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            conn.write_bytes_receipt(b"first")
        except BaseException as exc:
            first_errors.append(exc)

    first = threading.Thread(target=run_first, name=f"interrupted-{stage}")
    first.start()
    assert reached_settlement.wait(1.0)
    calls_before_second = (list(failed.writes), failed.flushes)

    try:
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert conn._write_transaction is not None
        with pytest.raises(RuntimeError, match="write already in progress"):
            conn.write_bytes_receipt(b"second")
        assert (failed.writes, failed.flushes) == calls_before_second
    finally:
        allow_settlement.set()

    first.join(timeout=2.0)
    assert not first.is_alive()
    assert first_errors == [interruption]
    expected_code = (
        WriteErrorCode.WRITE_ERROR
        if stage == "write"
        else WriteErrorCode.FLUSH_ERROR
    )
    assert interruption.receipt.safe_error_code is expected_code
    assert failed.writes == [b"first"]
    assert failed.flushes == (1 if stage == "flush" else 0)
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert conn._write_transaction is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_stale_interrupted_write_cannot_claim_failed_connect_error_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_at_settle = threading.Event()
    connect_error_reserved = threading.Event()
    writer_done = threading.Event()
    interruption = KeyboardInterrupt("old write interrupted")

    class _RetainedHandle(_FakeSerial):
        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            raise interruption

        def close(self) -> None:
            raise OSError("retain old handle")

    class _PausedConnection(SerialConnection):
        def _settle_escaped_write_interruption(self, transaction, exc) -> None:
            writer_at_settle.set()
            assert connect_error_reserved.wait(2.0)
            super()._settle_escaped_write_interruption(transaction, exc)

        def _emit_state_change(self, new_state, state_generation, **kwargs) -> None:
            if (
                new_state is ConnectionState.ERROR
                and threading.current_thread().name == "failed-connector"
            ):
                connect_error_reserved.set()
                assert writer_done.wait(2.0)
            super()._emit_state_change(new_state, state_generation, **kwargs)

    conn = _PausedConnection("COM-WRITE-CONNECT-OWNER")
    old = _RetainedHandle()
    conn._serial = old
    conn._serial_incarnation = 7
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 10
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    write_errors: list[BaseException] = []
    connect_errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: pytest.fail("must not open"))

    def run_writer() -> None:
        try:
            conn.write_bytes_receipt(b"old")
        except BaseException as exc:
            write_errors.append(exc)
        finally:
            writer_done.set()

    writer = threading.Thread(target=run_writer, name="old-writer")
    writer.start()
    assert writer_at_settle.wait(1.0)

    connector = threading.Thread(
        target=lambda: _capture_exception(conn.connect, connect_errors),
        name="failed-connector",
    )
    conn.disconnect()
    connector.start()
    writer.join(timeout=3.0)
    connector.join(timeout=3.0)

    assert not writer.is_alive() and not connector.is_alive()
    assert write_errors == [interruption]
    assert interruption.receipt.safe_error_code is WriteErrorCode.WRITE_ERROR
    assert len(connect_errors) == 1
    assert "could not be closed" in str(connect_errors[0])
    assert states == [
        ConnectionState.DISCONNECTED,
        ConnectionState.CONNECTING,
        ConnectionState.ERROR,
    ]
    assert len(errors) == 1
    assert errors[0] is connect_errors[0]
    assert not isinstance(errors[0], IncompleteSerialWrite)
    assert conn._serial is old and old.is_open
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_interrupted_reader_cannot_claim_queued_write_error_reservation() -> None:
    reader_at_settle = threading.Event()
    write_error_reserved = threading.Event()
    allow_reader_settlement = threading.Event()
    reader_done = threading.Event()
    interruption = KeyboardInterrupt("reader interrupted")

    class _SharedHandle(_FakeSerial):
        def read(self, _size: int) -> bytes:
            raise interruption

        def write(self, payload: bytes) -> int:
            self.writes.append(payload)
            return 0

    class _PausedConnection(SerialConnection):
        def _settle_interrupted_reader_failure(self, attempt, exc) -> None:
            reader_at_settle.set()
            assert allow_reader_settlement.wait(2.0)
            super()._settle_interrupted_reader_failure(attempt, exc)

        def _emit_write_failure(self, *args, **kwargs) -> None:
            write_error_reserved.set()
            assert reader_done.wait(2.0)
            super()._emit_write_failure(*args, **kwargs)

    conn = _PausedConnection("COM-READER-WRITE-OWNER")
    shared = _SharedHandle()
    conn._serial = shared
    conn._serial_incarnation = 4
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 10
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    reader_errors: list[BaseException] = []
    receipts: list[WriteReceipt] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    def run_reader() -> None:
        try:
            conn._reader_loop()
        except BaseException as exc:
            reader_errors.append(exc)
        finally:
            reader_done.set()

    reader = threading.Thread(target=run_reader, name="interrupted-reader")
    reader.start()
    assert reader_at_settle.wait(1.0)
    writer = threading.Thread(
        target=lambda: receipts.append(conn.write_bytes_receipt(b"old")),
        name="zero-writer",
    )
    writer.start()
    assert write_error_reserved.wait(1.0)
    allow_reader_settlement.set()
    reader.join(timeout=3.0)
    writer.join(timeout=3.0)

    assert not reader.is_alive() and not writer.is_alive()
    assert reader_errors == [interruption]
    assert receipts[0].safe_error_code is WriteErrorCode.ZERO_WRITE
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 2
    assert isinstance(errors[0], IncompleteSerialWrite)
    assert errors[0].receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert isinstance(errors[1], serial.SerialException)
    assert not isinstance(errors[1], IncompleteSerialWrite)
    assert shared.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_reader_decoder_setup_failure_cannot_leave_false_connected_state() -> None:
    conn, failed = _make_conn(encoding="codec-that-does-not-exist")
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    with pytest.raises(LookupError):
        conn._reader_loop()

    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1 and isinstance(errors[0], serial.SerialException)
    assert failed.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_reader_first_handle_check_control_flow_cannot_leave_false_connected() -> None:
    interruption = SystemExit("reader handle check interrupted")

    class _InterruptingOpenStateSerial(_FakeSerial):
        def __init__(self) -> None:
            self._is_open = True
            self.open_checks = 0
            super().__init__()

        @property
        def is_open(self) -> bool:
            self.open_checks += 1
            if self.open_checks == 1:
                raise interruption
            return self._is_open

        @is_open.setter
        def is_open(self, value: bool) -> None:
            self._is_open = value

    conn = SerialConnection("COM-READER-FIRST-CHECK")
    failed = _InterruptingOpenStateSerial()
    conn._serial = failed
    conn._state = ConnectionState.CONNECTED
    errors: list[Exception] = []
    conn.on_error(errors.append)

    with pytest.raises(SystemExit) as caught:
        conn._reader_loop()

    assert caught.value is interruption
    assert len(errors) == 1
    assert failed.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_reader_gate_control_flow_aborts_connecting_incarnation() -> None:
    interruption = KeyboardInterrupt("reader gate interrupted")
    conn, failed = _make_conn()
    conn._state = ConnectionState.CONNECTING
    conn._state_generation = 4
    conn._serial_incarnation = 2
    loop_attempt = serial_handler._ReaderLoopAttempt(
        active_failure=serial_handler._ReaderFailureAttempt(
            (failed, 2, ConnectionState.CONNECTING, 4)
        )
    )
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    class _InterruptingGate:
        def wait(self) -> None:
            raise interruption

    with pytest.raises(KeyboardInterrupt) as caught:
        conn._reader_loop_after_connect(_InterruptingGate(), loop_attempt)

    assert caught.value is interruption
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1 and isinstance(errors[0], serial.SerialException)
    assert failed.closed and conn._serial is None
    assert conn.state is ConnectionState.ERROR
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_opaque_callback_pre_call_interruption_retries_it_and_siblings_once(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("before opaque callback CALL")
    conn, fake = _make_conn(0)
    opaque_calls: list[ConnectionState] = []
    sibling_calls: list[ConnectionState] = []
    conn.on_state_change(opaque_calls.append)
    conn.on_state_change(sibling_calls.append)
    invoke_code = SerialConnection._invoke_notification_callback.__code__
    fired = False

    def interrupt_once(frame, event, _arg):
        nonlocal fired
        if not fired and frame.f_code is invoke_code and event == "line":
            fired = True
            sys.settrace(None)
            raise interruption
        return interrupt_once

    sys.settrace(interrupt_once)
    try:
        with pytest.raises(interruption_type) as caught:
            conn.write_bytes_receipt(b"one-attempt")
    finally:
        sys.settrace(None)

    assert caught.value is interruption
    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert opaque_calls == [ConnectionState.ERROR]
    assert sibling_calls == [ConnectionState.ERROR]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_builtin_control_callback_does_not_drop_later_sibling() -> None:
    conn, fake = _make_conn(0)
    sibling_calls: list[ConnectionState] = []
    conn.on_state_change(sys.exit)
    conn.on_state_change(sibling_calls.append)

    with pytest.raises(SystemExit) as caught:
        conn.write_bytes_receipt(b"one-attempt")

    assert caught.value.receipt.safe_error_code is WriteErrorCode.ZERO_WRITE
    assert fake.writes == [b"one-attempt"]
    assert sibling_calls == [ConnectionState.ERROR]
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_disconnect_close_hook_replacement_survives_outer_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-DISCONNECT-CLOSE-REPLACEMENT")
    states: list[ConnectionState] = []

    class _ReplacingCloseSerial(_FakeSerial):
        replacing = False

        def close(self) -> None:
            if not self.replacing:
                self.replacing = True
                conn.disconnect()
                conn.connect()
                return
            super().close()

    reused = _ReplacingCloseSerial()
    conn._serial = reused
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_state_change(states.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: reused)

    conn.disconnect()

    try:
        assert conn._serial is reused and reused.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert states == [
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
        ]
        assert conn._state_notifications == []
        assert not conn._state_notification_dispatching
    finally:
        conn.disconnect()


def test_disconnect_is_open_hook_reused_replacement_is_not_stale_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-DISCONNECT-OPEN-CHECK-REPLACEMENT")
    states: list[ConnectionState] = []

    class _ReplacingOpenCheckSerial(_FakeSerial):
        def __init__(self) -> None:
            self._is_open = True
            self.replacing = False
            super().__init__()

        @property
        def is_open(self) -> bool:
            if not self.replacing:
                self.replacing = True
                conn.disconnect()
                conn.connect()
            return self._is_open

        @is_open.setter
        def is_open(self, value: bool) -> None:
            self._is_open = value

    reused = _ReplacingOpenCheckSerial()
    conn._serial = reused
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_state_change(states.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: reused)

    conn.disconnect()

    try:
        assert conn._serial is reused and reused.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert states == [
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
        ]
        assert conn._state_notifications == []
        assert not conn._state_notification_dispatching
    finally:
        conn.disconnect()


def test_write_is_open_hook_replacement_prevents_stale_adapter_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-WRITE-OPEN-CHECK-REPLACEMENT")
    fresh = _FakeSerial()
    fresh.is_open = False

    class _ReplacingOpenCheckSerial(_FakeSerial):
        def __init__(self) -> None:
            self._is_open = True
            self.replaced = False
            super().__init__()

        @property
        def is_open(self) -> bool:
            if not self.replaced:
                self.replaced = True
                conn.disconnect()
                conn.connect()
            return self._is_open

        @is_open.setter
        def is_open(self, value: bool) -> None:
            self._is_open = value

    old = _ReplacingOpenCheckSerial()
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)

    with pytest.raises(RuntimeError, match="changed before write"):
        conn.write_bytes_receipt(b"must-not-hit-old")

    try:
        assert old.writes == []
        assert old.closed
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
    finally:
        conn.disconnect()


def test_write_is_open_metadata_never_invokes_hostile_truthiness() -> None:
    class _HostileTruth:
        calls = 0

        def __bool__(self) -> bool:
            self.calls += 1
            raise AssertionError("adapter metadata truthiness must not execute")

    marker = _HostileTruth()

    class _MalformedOpenSerial(_FakeSerial):
        @property
        def is_open(self) -> object:
            return marker

        @is_open.setter
        def is_open(self, _value: bool) -> None:
            pass

    conn = SerialConnection("COM-MALFORMED-WRITE-METADATA")
    malformed = _MalformedOpenSerial()
    conn._serial = malformed
    conn._state = ConnectionState.CONNECTED

    with pytest.raises(RuntimeError, match="Not connected"):
        conn.write_bytes_receipt(b"must-not-write")

    assert marker.calls == 0
    assert malformed.writes == []
    assert conn.state is ConnectionState.CONNECTED
    assert not conn._write_quarantined


@pytest.mark.parametrize("hook", ["in_waiting", "read"])
def test_reader_adapter_hook_replacement_is_fenced_and_stale_chunk_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    conn = SerialConnection("COM-READER-HOOK-REPLACEMENT")
    fresh = _FakeSerial()
    fresh.is_open = False
    lines: list[str] = []
    errors: list[Exception] = []

    class _ReplacingReaderSerial(_FakeSerial):
        replaced = False

        @property
        def in_waiting(self) -> int:
            if hook == "in_waiting" and not self.replaced:
                self.replaced = True
                conn.disconnect()
                conn.connect()
                raise OSError("old in_waiting failed after replacement")
            return 6

        def read(self, _size: int) -> bytes:
            if hook == "read" and not self.replaced:
                self.replaced = True
                conn.disconnect()
                conn.connect()
                return b"stale\n"
            return b""

    old = _ReplacingReaderSerial()
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    conn.on_line(lines.append)
    conn.on_error(errors.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)

    conn._reader_loop()

    try:
        assert old.closed
        assert lines == []
        assert errors == []
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
        assert conn._state_notifications == []
        assert not conn._state_notification_dispatching
    finally:
        conn.disconnect()


@pytest.mark.parametrize("malformed_stage", ["is_open", "in_waiting", "read", "decode"])
def test_reader_malformed_adapter_metadata_fails_closed_without_truthiness(
    monkeypatch: pytest.MonkeyPatch,
    malformed_stage: str,
) -> None:
    conn = SerialConnection("COM-MALFORMED-READER-METADATA")

    class _HostileTruth:
        calls = 0

        def __bool__(self) -> bool:
            self.calls += 1
            raise AssertionError("adapter metadata truthiness must not execute")

    marker = _HostileTruth()

    class _MalformedReaderSerial(_FakeSerial):
        @property
        def is_open(self) -> object:
            return marker if malformed_stage == "is_open" else True

        @is_open.setter
        def is_open(self, _value: bool) -> None:
            pass

        @property
        def in_waiting(self) -> object:
            return True if malformed_stage == "in_waiting" else 1

        def read(self, _size: int) -> object:
            return bytearray(b"x") if malformed_stage == "read" else b"x"

    if malformed_stage == "decode":
        class _MalformedDecoder:
            def __init__(self, *, errors: str) -> None:
                assert errors == "replace"

            def decode(self, _chunk: bytes) -> object:
                return marker

        monkeypatch.setattr(
            serial_handler.codecs,
            "getincrementaldecoder",
            lambda _encoding: _MalformedDecoder,
        )

    malformed = _MalformedReaderSerial()
    conn._serial = malformed
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    states: list[ConnectionState] = []
    errors: list[Exception] = []
    conn.on_state_change(states.append)
    conn.on_error(errors.append)

    conn._reader_loop()

    assert marker.calls == 0
    assert states == [ConnectionState.ERROR]
    assert len(errors) == 1 and isinstance(errors[0], serial.SerialException)
    assert conn.state is ConnectionState.ERROR
    assert conn._write_quarantined
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


def test_candidate_open_hook_disconnect_wins_over_stale_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = SerialConnection("COM-CANDIDATE-OPEN-DISCONNECT")
    states: list[ConnectionState] = []

    class _DisconnectingOpenSerial(_FakeSerial):
        def open(self) -> None:
            self.is_open = True
            conn.disconnect()

    candidate = _DisconnectingOpenSerial()
    candidate.is_open = False
    conn.on_state_change(states.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: candidate)

    with pytest.raises(serial.SerialException, match="changed during open"):
        conn.connect()

    assert candidate.closed
    assert conn._serial is None
    assert conn.state is ConnectionState.DISCONNECTED
    assert states == [
        ConnectionState.CONNECTING,
        ConnectionState.DISCONNECTED,
    ]
    assert conn._connect_attempt is None
    assert conn._state_notifications == []
    assert not conn._state_notification_dispatching


@pytest.mark.parametrize("raw", [False, True])
def test_reader_callback_replacement_stops_old_reader_before_more_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    raw: bool,
) -> None:
    conn = SerialConnection("COM-READER-CALLBACK-REPLACEMENT", raw=raw)
    main_thread = threading.current_thread()

    class _FreshSerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.read_threads: list[threading.Thread] = []

        def read(self, _size: int) -> bytes:
            self.read_threads.append(threading.current_thread())
            time.sleep(0.002)
            return b""

    fresh = _FreshSerial()
    fresh.is_open = False

    class _OldSerial(_FakeSerial):
        delivered = False

        @property
        def in_waiting(self) -> int:
            return len(b"first\nsecond\n")

        def read(self, _size: int) -> bytes:
            if self.delivered:
                raise AssertionError("old reader continued after callback replacement")
            self.delivered = True
            return b"first\nsecond\n"

    old = _OldSerial()
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    observed: list[object] = []

    def replace_from_callback(item: object) -> None:
        observed.append(item)
        conn.disconnect()
        conn.connect()

    if raw:
        conn.on_bytes(replace_from_callback)
    else:
        conn.on_line(replace_from_callback)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)

    conn._reader_loop()

    try:
        expected = [b"first\nsecond\n"] if raw else ["first"]
        assert observed == expected
        assert old.closed
        assert all(thread is not main_thread for thread in fresh.read_threads)
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
    finally:
        conn.disconnect()


@pytest.mark.parametrize("hook", ["constructor", "decode"])
def test_reader_decoder_hook_replacement_cannot_dispatch_or_adopt_fresh_handle(
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    conn = SerialConnection("COM-READER-DECODER-REPLACEMENT")
    main_thread = threading.current_thread()
    replaced = False

    class _FreshSerial(_FakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.read_threads: list[threading.Thread] = []

        def read(self, _size: int) -> bytes:
            self.read_threads.append(threading.current_thread())
            time.sleep(0.002)
            return b""

    fresh = _FreshSerial()
    fresh.is_open = False

    class _OldSerial(_FakeSerial):
        @property
        def in_waiting(self) -> int:
            return 2

        def read(self, _size: int) -> bytes:
            return b"x\n"

    class _ReplacingDecoder:
        def __init__(self, *, errors: str) -> None:
            nonlocal replaced
            assert errors == "replace"
            if hook == "constructor" and not replaced:
                replaced = True
                conn.disconnect()
                conn.connect()

        def decode(self, _chunk: bytes) -> str:
            nonlocal replaced
            if hook == "decode" and not replaced:
                replaced = True
                conn.disconnect()
                conn.connect()
            return "stale\n"

    old = _OldSerial()
    conn._serial = old
    conn._serial_incarnation = 3
    conn._state = ConnectionState.CONNECTED
    conn._state_generation = 8
    lines: list[str] = []
    conn.on_line(lines.append)
    monkeypatch.setattr(serial_handler.serial, "Serial", lambda: fresh)
    monkeypatch.setattr(
        serial_handler.codecs,
        "getincrementaldecoder",
        lambda _encoding: _ReplacingDecoder,
    )

    conn._reader_loop()

    try:
        assert replaced
        assert lines == []
        assert old.closed
        assert all(thread is not main_thread for thread in fresh.read_threads)
        assert conn._serial is fresh and fresh.is_open
        assert conn._serial_incarnation == 4
        assert conn.state is ConnectionState.CONNECTED
        assert not conn._write_quarantined
    finally:
        conn.disconnect()
