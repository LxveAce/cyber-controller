"""Receipt-aware retry boundaries for the connect-time handshake probe."""

from __future__ import annotations

import builtins
import logging
from enum import Enum
from types import SimpleNamespace

import pytest

from src.core import handshake
from src.core.serial_handler import WriteDisposition, WriteErrorCode, WriteReceipt
from src.models.device import Device


class _Disposition(str, Enum):
    DEFINITELY_NOT_WRITTEN = "definitely_not_written"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    HOST_WRITE_COMPLETE = "host_write_complete"


class _ExplodingHandler(logging.Handler):
    def emit(self, _record: logging.LogRecord) -> None:
        raise RuntimeError("logging observer failed")


class _ReceiptConn:
    def __init__(self, outcomes, *, reply=()) -> None:
        self.outcomes = list(outcomes)
        self.reply = tuple(reply)
        self.writes: list[str] = []
        self.callbacks: list = []

    def on_line(self, callback) -> None:
        self.callbacks.append(callback)

    def remove_line_callback(self, callback) -> None:
        self.callbacks.remove(callback)

    def write_receipt(self, command: str):
        self.writes.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        requested = len(command.encode("utf-8")) + 1
        if outcome in (_Disposition.HOST_WRITE_COMPLETE, "host_write_complete"):
            receipt = WriteReceipt(
                WriteDisposition.HOST_WRITE_COMPLETE,
                requested,
                requested,
            )
            for line in self.reply:
                for callback in tuple(self.callbacks):
                    callback(line)
        elif outcome in (
            _Disposition.DEFINITELY_NOT_WRITTEN,
            "definitely_not_written",
        ):
            receipt = WriteReceipt(
                WriteDisposition.DEFINITELY_NOT_WRITTEN,
                requested,
                0,
                WriteErrorCode.ZERO_WRITE,
            )
        elif outcome in (_Disposition.DELIVERY_UNCERTAIN, "delivery_uncertain"):
            receipt = WriteReceipt(
                WriteDisposition.DELIVERY_UNCERTAIN,
                requested,
                None,
                WriteErrorCode.WRITE_ERROR,
            )
        else:
            receipt = SimpleNamespace(disposition=outcome)
        return receipt


class _FailingLegacyConn:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.writes: list[str] = []
        self.callbacks: list = []

    def on_line(self, callback) -> None:
        self.callbacks.append(callback)

    def remove_line_callback(self, callback) -> None:
        self.callbacks.remove(callback)

    def write(self, command: str) -> None:
        self.writes.append(command)
        raise self.error


class _ReceiptAndStreamConn(_ReceiptConn):
    def __init__(self, outcomes) -> None:
        super().__init__(outcomes)
        self.raw = False
        self.raw_writes: list[bytes] = []
        self.byte_callbacks: list = []

    def on_bytes(self, callback) -> None:
        self.byte_callbacks.append(callback)

    def remove_byte_callback(self, callback) -> None:
        self.byte_callbacks.remove(callback)

    def write_bytes(self, payload: bytes) -> None:
        self.raw_writes.append(payload)


class _UncertainRawReceiptConn:
    def __init__(self, framed_reply: bytes, *, raise_after_delivery: bool) -> None:
        self.raw = False
        self.framed_reply = framed_reply
        self.raise_after_delivery = raise_after_delivery
        self.raw_writes: list[bytes] = []
        self.byte_callbacks: list = []

    def on_bytes(self, callback) -> None:
        self.byte_callbacks.append(callback)

    def remove_byte_callback(self, callback) -> None:
        self.byte_callbacks.remove(callback)

    def write_bytes_receipt(self, payload: bytes):
        self.raw_writes.append(payload)
        for callback in tuple(self.byte_callbacks):
            callback(self.framed_reply)
        if self.raise_after_delivery:
            raise RuntimeError("private raw transport detail")
        return SimpleNamespace(disposition=_Disposition.DELIVERY_UNCERTAIN)


@pytest.fixture(autouse=True)
def _no_probe_waits(monkeypatch):
    monkeypatch.setattr(handshake, "_wait_for_reply", lambda *args, **kwargs: None)
    monkeypatch.setattr(handshake.time, "sleep", lambda _seconds: None)


def _device() -> Device:
    return Device(port="COM_RECEIPT", firmware="marauder")


@pytest.mark.parametrize(
    "disposition",
    [_Disposition.DELIVERY_UNCERTAIN, "delivery_uncertain"],
)
def test_uncertain_receipt_is_attempted_once(disposition) -> None:
    conn = _ReceiptConn([disposition])

    result = handshake.probe_device(conn, _device())

    assert result.health == "no-reply"
    assert conn.writes == ["help"]
    assert conn.callbacks == []


def test_uncertain_receipt_waits_once_for_possible_in_flight_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ReceiptConn([_Disposition.DELIVERY_UNCERTAIN])

    def deliver_reply(lines, **_kwargs) -> None:
        lines.append("ESP32 Marauder v1.9.1")

    monkeypatch.setattr(handshake, "_wait_for_reply", deliver_reply)
    result = handshake.probe_device(conn, _device())

    assert result.health == "alive"
    assert conn.writes == ["help"]
    assert conn.callbacks == []


def test_definitely_not_written_receipt_is_not_replayed() -> None:
    conn = _ReceiptConn([_Disposition.DEFINITELY_NOT_WRITTEN])

    handshake.probe_device(conn, _device())

    assert conn.writes == ["help"]
    assert conn.callbacks == []


def test_legacy_write_exception_is_attempted_once_without_log_leak(caplog) -> None:
    secret = "private-transport-detail"
    conn = _FailingLegacyConn(RuntimeError(secret))
    caplog.set_level(logging.DEBUG, logger=handshake.__name__)

    handshake.probe_device(conn, _device())

    assert conn.writes == ["help"]
    assert conn.callbacks == []
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert all(secret not in repr(record.args) for record in caplog.records)


def test_receipt_writer_exception_is_attempted_once_without_log_leak(caplog) -> None:
    secret = "private-receipt-detail"
    conn = _ReceiptConn([RuntimeError(secret)])
    caplog.set_level(logging.DEBUG, logger=handshake.__name__)

    handshake.probe_device(conn, _device())

    assert conn.writes == ["help"]
    assert conn.callbacks == []
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert all(secret not in repr(record.args) for record in caplog.records)


def test_logging_failure_cannot_skip_uncertain_reply_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ReceiptConn([RuntimeError("private receipt detail")])
    waits: list[bool] = []
    monkeypatch.setattr(
        handshake,
        "_wait_for_reply",
        lambda *_args, **_kwargs: waits.append(True),
    )
    logger = logging.getLogger(handshake.__name__)
    prior_level = logger.level
    handler = _ExplodingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        result = handshake.probe_device(conn, _device())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert result.health == "no-reply"
    assert conn.writes == ["help"]
    assert waits == [True]


def test_malformed_receipt_comparison_fails_closed_and_observes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[bool] = []
    waits: list[bool] = []

    class _ExplodingEquality:
        def __eq__(self, _other: object) -> bool:
            comparisons.append(True)
            raise RuntimeError("private comparison detail")

    class _MalformedReceiptConn(_ReceiptConn):
        def write_receipt(self, command: str):
            self.writes.append(command)
            return SimpleNamespace(
                disposition=SimpleNamespace(value=_ExplodingEquality())
            )

    conn = _MalformedReceiptConn([])
    monkeypatch.setattr(
        handshake,
        "_wait_for_reply",
        lambda *_args, **_kwargs: waits.append(True),
    )

    result = handshake.probe_device(conn, _device())

    assert result.health == "no-reply"
    assert conn.writes == ["help"]
    assert waits == [True]
    assert comparisons == []
    assert conn.callbacks == []


def test_host_complete_silence_keeps_bounded_retry_schedule() -> None:
    complete = _Disposition.HOST_WRITE_COMPLETE
    conn = _ReceiptConn([complete] * 6)

    result = handshake.probe_device(conn, _device())

    assert result.health == "no-reply"
    assert conn.writes == ["help", "status"] * 3
    assert conn.callbacks == []


def test_legacy_success_does_not_import_typed_receipt_support(monkeypatch) -> None:
    writes: list[str] = []
    imports: list[str] = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in ("serial_handler", "src.core.serial_handler"):
            imports.append(name)
            raise ImportError("typed receipt dependency unavailable in legacy fixture")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    conn = SimpleNamespace(write=lambda command: writes.append(command))

    assert handshake._probe_write_completed(conn, "help")
    assert writes == ["help"]
    assert imports == []


@pytest.mark.parametrize(
    "shape",
    [
        "missing_counts",
        "short_count",
        "zero_count",
        "flush_error",
        "string_label",
        "foreign_enum_label",
        "nested_value_label",
        "typed_empty",
    ],
)
def test_malformed_complete_receipt_stops_at_managed_adapter_boundary(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    from src.core.device_manager import DeviceManager

    receipts = {
        "missing_counts": SimpleNamespace(
            disposition=WriteDisposition.HOST_WRITE_COMPLETE
        ),
        "short_count": SimpleNamespace(
            disposition=WriteDisposition.HOST_WRITE_COMPLETE,
            bytes_requested=5,
            bytes_reported=2,
            safe_error_code=WriteErrorCode.SHORT_WRITE,
        ),
        "zero_count": SimpleNamespace(
            disposition=WriteDisposition.HOST_WRITE_COMPLETE,
            bytes_requested=5,
            bytes_reported=0,
            safe_error_code=WriteErrorCode.ZERO_WRITE,
        ),
        "flush_error": SimpleNamespace(
            disposition=WriteDisposition.HOST_WRITE_COMPLETE,
            bytes_requested=5,
            bytes_reported=5,
            safe_error_code=WriteErrorCode.FLUSH_ERROR,
        ),
        "string_label": SimpleNamespace(disposition="host_write_complete"),
        "foreign_enum_label": SimpleNamespace(
            disposition=_Disposition.HOST_WRITE_COMPLETE
        ),
        "nested_value_label": SimpleNamespace(
            disposition=SimpleNamespace(value="host_write_complete")
        ),
        "typed_empty": WriteReceipt(WriteDisposition.HOST_WRITE_COMPLETE, 0, 0),
    }

    class ManagedAdapter(_ReceiptAndStreamConn):
        is_connected = True

        def write_receipt(self, command: str):
            self.writes.append(command)
            return receipts[shape]

    waits: list[bool] = []
    raw_probes: list[bool] = []
    monkeypatch.setattr(
        handshake,
        "_wait_for_reply",
        lambda *_args, **_kwargs: waits.append(True),
    )
    monkeypatch.setattr(
        handshake,
        "_probe_meshtastic_stream",
        lambda _conn: raw_probes.append(True),
    )
    conn = ManagedAdapter([])
    device = Device(port="COM_MALFORMED_RECEIPT", firmware="")
    manager = DeviceManager()
    manager.attach_connection(device, conn)

    result = manager.probe(device.port, timeout=0)

    assert result.health == "no-reply"
    assert conn.writes == ["help"]
    assert waits == [True]
    assert raw_probes == []
    assert conn.raw_writes == []
    assert conn.callbacks == []
    assert conn.byte_callbacks == []


def test_later_command_failure_stops_remaining_commands_and_attempts() -> None:
    conn = _ReceiptConn(
        [_Disposition.HOST_WRITE_COMPLETE, _Disposition.DELIVERY_UNCERTAIN]
    )

    handshake.probe_device(conn, _device())

    assert conn.writes == ["help", "status"]
    assert conn.callbacks == []


def test_failed_text_probe_does_not_fall_through_to_stream_probe() -> None:
    conn = _ReceiptAndStreamConn([_Disposition.DELIVERY_UNCERTAIN])
    device = Device(port="COM_UNKNOWN", firmware="")

    handshake.probe_device(conn, device)

    assert conn.writes == ["help"]
    assert conn.raw_writes == []
    assert conn.byte_callbacks == []


def test_partial_text_callback_registration_is_removed_before_return() -> None:
    class _PartialRegistrationConn:
        def __init__(self) -> None:
            self.callbacks: list = []

        def on_line(self, callback) -> None:
            self.callbacks.append(callback)
            raise RuntimeError("registration failed after append")

        def remove_line_callback(self, callback) -> None:
            self.callbacks.remove(callback)

        def write_receipt(self, _command: str):
            raise AssertionError("probe must not write without an observer")

    conn = _PartialRegistrationConn()
    result = handshake.probe_device(conn, _device())

    assert result.health == "unknown"
    assert conn.callbacks == []


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_partial_text_registration_control_exception_preserves_identity_after_cleanup(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type("registration interrupted after append")

    class _PartialRegistrationConn:
        def __init__(self) -> None:
            self.callbacks: list = []

        def on_line(self, callback) -> None:
            self.callbacks.append(callback)
            raise interruption

        def remove_line_callback(self, callback) -> None:
            self.callbacks.remove(callback)
            raise RuntimeError("secondary cleanup failure")

    conn = _PartialRegistrationConn()
    with pytest.raises(interruption_type) as caught:
        handshake.probe_device(conn, _device())

    assert caught.value is interruption
    assert conn.callbacks == []


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_text_probe_body_control_exception_survives_hostile_cleanup(
    interruption_type: type[BaseException],
) -> None:
    primary = interruption_type("typed writer interrupted")
    secondary = (
        SystemExit("secondary callback cleanup")
        if interruption_type is KeyboardInterrupt
        else KeyboardInterrupt("secondary callback cleanup")
    )

    class _InterruptedTextConn:
        def __init__(self) -> None:
            self.callbacks: list = []
            self.writes: list[str] = []
            self.remove_calls = 0

        def on_line(self, callback) -> None:
            self.callbacks.append(callback)

        def remove_line_callback(self, callback) -> None:
            self.remove_calls += 1
            self.callbacks.remove(callback)
            raise secondary

        def write_receipt(self, command: str):
            self.writes.append(command)
            raise primary

    conn = _InterruptedTextConn()
    with pytest.raises(interruption_type) as caught:
        handshake.probe_device(conn, _device())

    assert caught.value is primary
    assert conn.writes == ["help"]
    assert conn.remove_calls == 1
    assert conn.callbacks == []


def test_text_probe_cleanup_control_exception_outranks_ordinary_body_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_failure = RuntimeError("ordinary wait failure")
    cleanup_interruption = KeyboardInterrupt("callback cleanup interrupted")

    class _CleanupInterruptedConn(_ReceiptConn):
        def remove_line_callback(self, callback) -> None:
            self.callbacks.remove(callback)
            raise cleanup_interruption

    conn = _CleanupInterruptedConn([_Disposition.HOST_WRITE_COMPLETE])
    monkeypatch.setattr(
        handshake,
        "_wait_for_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(body_failure),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        handshake.probe_device(conn, _device())

    assert caught.value is cleanup_interruption
    assert conn.writes == ["help", "status"]
    assert conn.callbacks == []


def test_raw_probe_does_not_inspect_legacy_writer_when_typed_writer_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.protocols.stream_framer import StreamFramer

    class _TypedRawConn:
        def __init__(self) -> None:
            self.raw = False
            self.callbacks: list = []
            self.writes: list[bytes] = []

        @property
        def write_bytes(self):
            raise RuntimeError("obsolete writer property must not be read")

        def on_bytes(self, callback) -> None:
            self.callbacks.append(callback)

        def remove_byte_callback(self, callback) -> None:
            self.callbacks.remove(callback)

        def write_bytes_receipt(self, payload: bytes) -> WriteReceipt:
            self.writes.append(payload)
            for callback in tuple(self.callbacks):
                callback(StreamFramer.frame(b"synthetic reply"))
            return WriteReceipt(
                WriteDisposition.HOST_WRITE_COMPLETE,
                len(payload),
                len(payload),
            )

    monkeypatch.setattr(
        handshake,
        "_is_meshtastic_reply",
        lambda frames, _config_id: bool(frames),
    )
    conn = _TypedRawConn()

    assert handshake._probe_meshtastic_stream(conn, timeout=0.01)
    assert len(conn.writes) == 1
    assert conn.callbacks == []
    assert conn.raw is False


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_raw_probe_body_control_exception_survives_all_cleanup_failures(
    interruption_type: type[BaseException],
) -> None:
    primary = interruption_type("raw writer interrupted")

    class _InterruptedRawConn:
        def __init__(self) -> None:
            self._raw = False
            self.restore_armed = False
            self.restore_calls = 0
            self.remove_calls = 0
            self.callbacks: list = []
            self.writes: list[bytes] = []

        @property
        def raw(self) -> bool:
            return self._raw

        @raw.setter
        def raw(self, value: bool) -> None:
            if value is False and self.restore_armed:
                self.restore_calls += 1
                raise SystemExit("secondary raw restore interruption")
            self._raw = value
            if value is True:
                self.restore_armed = True

        def on_bytes(self, callback) -> None:
            self.callbacks.append(callback)

        def remove_byte_callback(self, callback) -> None:
            self.remove_calls += 1
            self.callbacks.remove(callback)
            raise KeyboardInterrupt("secondary raw callback interruption")

        def write_bytes_receipt(self, payload: bytes):
            self.writes.append(payload)
            raise primary

    conn = _InterruptedRawConn()
    with pytest.raises(interruption_type) as caught:
        handshake._probe_meshtastic_stream(conn, timeout=0)

    assert caught.value is primary
    assert len(conn.writes) == 1
    assert conn.restore_calls == 1
    assert conn.remove_calls == 1
    assert conn.callbacks == []


def test_raw_probe_normal_cleanup_propagates_first_control_and_attempts_all_steps() -> None:
    restore_interruption = KeyboardInterrupt("raw restore interrupted")

    class _CleanupInterruptedRawConn:
        def __init__(self) -> None:
            self._raw = False
            self.restore_armed = False
            self.restore_calls = 0
            self.remove_calls = 0
            self.callbacks: list = []
            self.writes: list[bytes] = []

        @property
        def raw(self) -> bool:
            return self._raw

        @raw.setter
        def raw(self, value: bool) -> None:
            if value is False and self.restore_armed:
                self.restore_calls += 1
                raise restore_interruption
            self._raw = value
            if value is True:
                self.restore_armed = True

        def on_bytes(self, callback) -> None:
            self.callbacks.append(callback)

        def remove_byte_callback(self, callback) -> None:
            self.remove_calls += 1
            self.callbacks.remove(callback)
            raise SystemExit("later callback cleanup interruption")

        def write_bytes_receipt(self, payload: bytes) -> WriteReceipt:
            self.writes.append(payload)
            return WriteReceipt(
                WriteDisposition.HOST_WRITE_COMPLETE,
                len(payload),
                len(payload),
            )

    conn = _CleanupInterruptedRawConn()
    with pytest.raises(KeyboardInterrupt) as caught:
        handshake._probe_meshtastic_stream(conn, timeout=0)

    assert caught.value is restore_interruption
    assert len(conn.writes) == 1
    assert conn.restore_calls == 1
    assert conn.remove_calls == 1
    assert conn.callbacks == []


@pytest.mark.parametrize("raise_after_delivery", [False, True])
def test_uncertain_raw_probe_observes_in_flight_reply_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    raise_after_delivery: bool,
) -> None:
    from src.protocols.stream_framer import StreamFramer

    conn = _UncertainRawReceiptConn(
        StreamFramer.frame(b"reply"),
        raise_after_delivery=raise_after_delivery,
    )
    monkeypatch.setattr(
        handshake,
        "_is_meshtastic_reply",
        lambda frames, _config_id: bool(frames),
    )

    assert handshake._probe_meshtastic_stream(conn, timeout=0.01)
    assert len(conn.raw_writes) == 1
    assert conn.byte_callbacks == []
    assert conn.raw is False


def test_raw_probe_logging_failure_cannot_erase_in_flight_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.protocols.stream_framer import StreamFramer

    conn = _UncertainRawReceiptConn(
        StreamFramer.frame(b"reply"),
        raise_after_delivery=True,
    )
    monkeypatch.setattr(
        handshake,
        "_is_meshtastic_reply",
        lambda frames, _config_id: bool(frames),
    )
    logger = logging.getLogger(handshake.__name__)
    prior_level = logger.level
    handler = _ExplodingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        assert handshake._probe_meshtastic_stream(conn, timeout=0.01)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert len(conn.raw_writes) == 1
    assert conn.byte_callbacks == []


def test_raw_probe_with_unreadable_mode_is_best_effort_and_never_writes() -> None:
    class _UnreadableRawConnection:
        @property
        def raw(self) -> bool:
            raise RuntimeError("private raw adapter detail")

        def write_bytes_receipt(self, _payload: bytes):
            raise AssertionError("raw probe must not write")

        def on_bytes(self, _callback) -> None:
            raise AssertionError("raw probe must not attach")

        def remove_byte_callback(self, _callback) -> None:
            raise AssertionError("raw probe must not detach")

    assert not handshake._probe_meshtastic_stream(
        _UnreadableRawConnection(),
        timeout=0.01,
    )


def test_raw_probe_ignores_malformed_fromradio_frame_without_replay() -> None:
    from src.protocols.stream_framer import StreamFramer

    malformed = bytes.fromhex("120a1a01ff22050801120178")
    conn = _UncertainRawReceiptConn(
        StreamFramer.frame(malformed),
        raise_after_delivery=False,
    )

    assert not handshake._probe_meshtastic_stream(conn, timeout=0)
    assert len(conn.raw_writes) == 1
    assert conn.byte_callbacks == []
    assert conn.raw is False


def test_host_complete_reply_preserves_classification_and_detaches_callback() -> None:
    conn = _ReceiptConn(
        [_Disposition.HOST_WRITE_COMPLETE, _Disposition.HOST_WRITE_COMPLETE],
        reply=("ESP32 Marauder v1.9.1", "scanall"),
    )
    device = _device()

    result = handshake.probe_device(conn, device)

    assert result.health == "alive"
    assert device.health == "alive"
    assert "Marauder" in result.banner
    assert "scanall" in result.live_commands
    assert conn.writes == ["help", "status"]
    assert conn.callbacks == []
