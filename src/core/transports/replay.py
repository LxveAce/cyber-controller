"""Deterministic parser-level transcript simulation.

This module deliberately does *not* emulate a serial adapter.  It never opens a
port, socket, process, or background thread, and its evidence is explicitly
simulation evidence.  The small ``ReplayConnection`` surface exists so protocol
parsers and isolated connection consumers can be exercised without hardware.

Version 1 is text-only.  It models already-framed receive lines and exact text
write expectations; it does not claim to model incremental decoding, serial
framing, partial writes, flushes, or physical delivery.

Observer callbacks run outside the metadata lock but inside exclusive,
non-blocking drive ownership.  A callback may inspect the replay, but a nested
or concurrent drive is deliberately rejected and terminalizes the simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from src.core.serial_handler import ConnectionState

TRANSCRIPT_SCHEMA = "CyberControllerTransportTranscript@1"
EVIDENCE_SCHEMA = "CyberControllerReplayEvidence@1"
MAX_SOURCE_BYTES = 1_048_576
MAX_EVENTS = 10_000
MAX_TOTAL_EVENT_TEXT_BYTES = 1_048_576
MAX_RX_LINE_BYTES = 16_384
MAX_COMMAND_BYTES = 4_096
MAX_PATH_BYTES = 4_096
MAX_VIRTUAL_MS = 86_400_000
MAX_CALLBACKS_PER_CATEGORY = 64
MAX_RETAINED_OBSERVER_ERRORS = 1_024
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())

_SCENARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BASIS_SCHEMA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_BASIS_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_SIM_PORT_RE = re.compile(r"^sim://[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DISCONNECT_REASONS = frozenset({"scripted_end", "link_loss", "device_reset"})
_LINE_ENDINGS = frozenset({"\n", "\r", "\r\n"})
_EVENT_KINDS = frozenset({"connect", "expect_text_write", "rx_line", "disconnect"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)


class TranscriptValidationError(ValueError):
    """A transcript failed the strict, bounded version-1 schema."""


class ReplayMismatchError(RuntimeError):
    """An observed simulated write did not match the next transcript barrier.

    The message and public fields intentionally contain no payload or digest.
    """

    def __init__(
        self,
        *,
        seq: int | None,
        reason: str,
        expected_bytes: int | None,
        observed_bytes: int,
    ) -> None:
        self.seq = seq
        self.reason = reason
        self.expected_bytes = expected_bytes
        self.observed_bytes = observed_bytes
        where = "none" if seq is None else str(seq)
        super().__init__(
            "Replay write mismatch "
            f"(seq={where}, reason={reason}, expected_bytes={expected_bytes}, "
            f"observed_bytes={observed_bytes})"
        )


class ReplayStateError(RuntimeError):
    """A replay lifecycle operation was invalid for the current barrier/state."""


@dataclass(frozen=True, slots=True)
class TranscriptDevice:
    port: str
    firmware: str
    name: str
    simulated: bool = True


@dataclass(frozen=True, slots=True)
class TranscriptConnection:
    mode: str
    baud: int
    encoding: str
    line_ending: str


@dataclass(frozen=True, slots=True)
class TranscriptBasis:
    """Declarative provenance supplied by a fixture; not independently verified."""

    schema: str
    sha256: str
    entry_id: str


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    seq: int
    at_ms: int
    kind: str
    text: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayEventOutcome:
    seq: int
    at_ms: int
    observed_at_ms: int
    incarnation: int
    direction: str
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at_ms": self.at_ms,
            "observed_at_ms": self.observed_at_ms,
            "incarnation": self.incarnation,
            "direction": self.direction,
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class ReplayWriteOutcome:
    seq: int | None
    observed_at_ms: int
    incarnation: int
    bytes_requested: int
    expected_bytes: int | None
    disposition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "observed_at_ms": self.observed_at_ms,
            "incarnation": self.incarnation,
            "bytes_requested": self.bytes_requested,
            "expected_bytes": self.expected_bytes,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class ReplayObserverError:
    seq: int | None
    observed_at_ms: int
    incarnation: int
    category: str
    ordinal: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "observed_at_ms": self.observed_at_ms,
            "incarnation": self.incarnation,
            "category": self.category,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True, init=False)
class ReplayEvidence:
    """Detached, immutable, payload-free evidence for one simulated replay."""

    schema: str
    evidence_scope: str
    integrity_model: str
    simulated: bool
    hardware_evidence: bool
    scenario_id: str
    port: str
    source_size_bytes: int
    source_sha256: str
    transcript_sha256: str
    basis: TranscriptBasis | None
    basis_verification: str | None
    cursor: int
    total_events: int
    virtual_ms: int
    incarnation: int
    final_state: str
    blocked_on: str
    in_flight: bool
    complete: bool
    run_status: str
    terminal_reason: str | None
    events: tuple[ReplayEventOutcome, ...]
    writes: tuple[ReplayWriteOutcome, ...]
    observer_errors: tuple[ReplayObserverError, ...]
    observer_error_count: int
    observer_error_overflow: int
    input_error_count: int
    run_sha256: str

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("ReplayEvidence is created only by ReplayConnection.snapshot()")

    @classmethod
    def _create(cls, **values: Any) -> ReplayEvidence:
        expected = frozenset(cls.__dataclass_fields__)
        if frozenset(values) != expected:
            raise TypeError("Internal replay evidence fields are incomplete")
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(instance, name, values[name])
        return instance

    def _without_run_hash(self) -> dict[str, Any]:
        basis = None
        if self.basis is not None:
            basis = {
                "schema": self.basis.schema,
                "sha256": self.basis.sha256,
                "entry_id": self.basis.entry_id,
                "verification": self.basis_verification,
            }
        return {
            "schema": self.schema,
            "evidence_scope": self.evidence_scope,
            "integrity_model": self.integrity_model,
            "simulated": self.simulated,
            "hardware_evidence": self.hardware_evidence,
            "scenario_id": self.scenario_id,
            "port": self.port,
            "source_size_bytes": self.source_size_bytes,
            "source_sha256": self.source_sha256,
            "transcript_sha256": self.transcript_sha256,
            "basis": basis,
            "cursor": self.cursor,
            "total_events": self.total_events,
            "virtual_ms": self.virtual_ms,
            "incarnation": self.incarnation,
            "final_state": self.final_state,
            "blocked_on": self.blocked_on,
            "in_flight": self.in_flight,
            "complete": self.complete,
            "run_status": self.run_status,
            "terminal_reason": self.terminal_reason,
            "events": [item.to_dict() for item in self.events],
            "writes": [item.to_dict() for item in self.writes],
            "observer_errors": [item.to_dict() for item in self.observer_errors],
            "observer_error_count": self.observer_error_count,
            "observer_error_overflow": self.observer_error_overflow,
            "input_error_count": self.input_error_count,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-safe copy; mutating it cannot alter this snapshot."""
        out = self._without_run_hash()
        out["run_sha256"] = self.run_sha256
        return out


@dataclass(frozen=True, slots=True, init=False)
class TransportTranscript:
    """One fully validated, immutable version-1 transport transcript."""

    schema: str
    scenario_id: str
    device: TranscriptDevice
    connection: TranscriptConnection
    basis: TranscriptBasis | None
    events: tuple[TranscriptEvent, ...]
    source_size_bytes: int
    source_sha256: str
    transcript_sha256: str
    _source_bytes: bytes = field(repr=False, compare=False)

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Use TransportTranscript.from_bytes()")

    @property
    def has_write_expectations(self) -> bool:
        """Whether the scenario contains any simulated outbound expectation."""
        return any(event.kind == "expect_text_write" for event in self.events)

    @classmethod
    def from_bytes(cls, raw: bytes) -> TransportTranscript:
        """Parse exact UTF-8 JSON bytes using the strict version-1 schema."""
        if type(raw) is not bytes:
            raise TypeError("Transcript source must be exact bytes")
        if not raw:
            raise TranscriptValidationError("Transcript source is empty")
        if len(raw) > MAX_SOURCE_BYTES:
            raise TranscriptValidationError("Transcript source exceeds byte limit")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise TranscriptValidationError("UTF-8 BOM is not allowed")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise TranscriptValidationError("Transcript is not strict UTF-8") from exc
        try:
            obj = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_int=_bounded_json_int,
                parse_float=_reject_json_float,
                parse_constant=_reject_json_constant,
            )
        except TranscriptValidationError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise TranscriptValidationError("Transcript is not valid bounded JSON") from exc

        _check_nesting(obj)
        semantic = _validate_transcript_object(obj)
        canonical = _canonical_json(semantic)
        semantic_hash = hashlib.sha256(
            TRANSCRIPT_SCHEMA.encode("ascii") + b"\0" + canonical
        ).hexdigest()
        source_hash = hashlib.sha256(raw).hexdigest()
        instance = object.__new__(cls)
        values = {
            "schema": TRANSCRIPT_SCHEMA,
            "scenario_id": semantic["scenario_id"],
            "device": TranscriptDevice(**semantic["device"]),
            "connection": TranscriptConnection(**semantic["connection"]),
            "basis": TranscriptBasis(**semantic["basis"]) if semantic["basis"] else None,
            "events": tuple(TranscriptEvent(**event) for event in semantic["events"]),
            "source_size_bytes": len(raw),
            "source_sha256": source_hash,
            "transcript_sha256": semantic_hash,
            "_source_bytes": raw,
        }
        for name, field_value in values.items():
            object.__setattr__(instance, name, field_value)
        return instance


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise TranscriptValidationError("Duplicate JSON key")
        out[key] = value
    return out


def _bounded_json_int(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > 20:
        raise TranscriptValidationError("JSON integer exceeds digit limit")
    return int(token)


def _reject_json_float(_token: str) -> float:
    raise TranscriptValidationError("Floating-point JSON values are not allowed")


def _reject_json_constant(_token: str) -> float:
    raise TranscriptValidationError("Non-finite JSON values are not allowed")


def _check_nesting(value: Any, depth: int = 0) -> None:
    if depth > 8:
        raise TranscriptValidationError("Transcript nesting exceeds limit")
    if isinstance(value, dict):
        for child in value.values():
            _check_nesting(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_nesting(child, depth + 1)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _exact_object(value: Any, label: str, required: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise TranscriptValidationError(f"{label} must be an object")
    present = frozenset(value)
    missing = required - present
    unknown = present - required
    if missing:
        raise TranscriptValidationError(f"{label} missing fields: {sorted(missing)}")
    if unknown:
        raise TranscriptValidationError(f"{label} has unknown fields")
    return value


def _string(
    value: Any,
    label: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
    allow_trailing_line_endings: bool = False,
) -> str:
    if type(value) is not str:
        raise TranscriptValidationError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise TranscriptValidationError(f"{label} contains a surrogate") from exc
    if not allow_empty and not value:
        raise TranscriptValidationError(f"{label} must not be empty")
    if len(encoded) > max_bytes:
        raise TranscriptValidationError(f"{label} exceeds byte limit")
    checked = value.rstrip("\r\n") if allow_trailing_line_endings else value
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in checked):
        raise TranscriptValidationError(f"{label} contains a control character")
    if not allow_trailing_line_endings and len(checked) != len(value):
        raise TranscriptValidationError(f"{label} contains a line ending")
    return value


def _plain_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise TranscriptValidationError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _validate_transcript_object(value: Any) -> dict[str, Any]:
    top = _exact_object(
        value,
        "transcript",
        frozenset({"schema", "scenario_id", "device", "connection", "basis", "events"}),
    )
    if top["schema"] != TRANSCRIPT_SCHEMA:
        raise TranscriptValidationError("Unsupported transcript schema")

    scenario_id = _string(top["scenario_id"], "scenario_id", max_bytes=128)
    if _SCENARIO_RE.fullmatch(scenario_id) is None:
        raise TranscriptValidationError("scenario_id has invalid syntax")

    raw_device = _exact_object(
        top["device"],
        "device",
        frozenset({"port", "firmware", "name", "simulated"}),
    )
    port = _string(raw_device["port"], "device.port", max_bytes=256)
    if _SIM_PORT_RE.fullmatch(port) is None or ".." in port.split("/")[2:]:
        raise TranscriptValidationError("device.port must be a bounded sim:// identifier")
    firmware = _string(raw_device["firmware"], "device.firmware", max_bytes=128)
    name = _string(raw_device["name"], "device.name", max_bytes=256)
    if raw_device["simulated"] is not True:
        raise TranscriptValidationError("device.simulated must be true")
    device = {
        "port": port,
        "firmware": firmware,
        "name": name,
        "simulated": True,
    }

    raw_connection = _exact_object(
        top["connection"],
        "connection",
        frozenset({"mode", "baud", "encoding", "line_ending"}),
    )
    if raw_connection["mode"] != "text":
        raise TranscriptValidationError("Only text-mode transcripts are supported")
    if raw_connection["encoding"] != "utf-8":
        raise TranscriptValidationError("Only utf-8 transcripts are supported")
    baud = _plain_int(raw_connection["baud"], "connection.baud", minimum=1, maximum=10_000_000)
    line_ending = raw_connection["line_ending"]
    if type(line_ending) is not str or line_ending not in _LINE_ENDINGS:
        raise TranscriptValidationError("connection.line_ending must be LF, CR, or CRLF")
    connection = {
        "mode": "text",
        "baud": baud,
        "encoding": "utf-8",
        "line_ending": line_ending,
    }

    basis = None
    if top["basis"] is not None:
        raw_basis = _exact_object(
            top["basis"],
            "basis",
            frozenset({"schema", "sha256", "entry_id"}),
        )
        basis_schema = _string(raw_basis["schema"], "basis.schema", max_bytes=128)
        basis_sha = _string(raw_basis["sha256"], "basis.sha256", max_bytes=64)
        entry_id = _string(raw_basis["entry_id"], "basis.entry_id", max_bytes=256)
        if _BASIS_SCHEMA_RE.fullmatch(basis_schema) is None:
            raise TranscriptValidationError("basis.schema must be an opaque ASCII identifier")
        if _SHA256_RE.fullmatch(basis_sha) is None:
            raise TranscriptValidationError("basis.sha256 must be lowercase SHA-256")
        if _BASIS_ENTRY_RE.fullmatch(entry_id) is None:
            raise TranscriptValidationError("basis.entry_id must be an opaque ASCII identifier")
        basis = {
            "schema": basis_schema,
            "sha256": basis_sha,
            "entry_id": entry_id,
        }

    raw_events = top["events"]
    if type(raw_events) is not list or not raw_events:
        raise TranscriptValidationError("events must be a non-empty array")
    if len(raw_events) > MAX_EVENTS:
        raise TranscriptValidationError("events exceeds count limit")

    events: list[dict[str, Any]] = []
    total_text_bytes = 0
    connected = False
    previous_ms = 0
    for index, raw_event in enumerate(raw_events, start=1):
        if type(raw_event) is not dict:
            raise TranscriptValidationError(f"event {index} must be an object")
        kind = raw_event.get("kind")
        if type(kind) is not str or kind not in _EVENT_KINDS:
            raise TranscriptValidationError(f"event {index} has unsupported kind")
        required = {"seq", "at_ms", "kind"}
        if kind in {"expect_text_write", "rx_line"}:
            required.add("text")
        elif kind == "disconnect":
            required.add("reason")
        event_obj = _exact_object(raw_event, f"event {index}", frozenset(required))
        seq = _plain_int(event_obj["seq"], f"event {index}.seq", minimum=1, maximum=MAX_EVENTS)
        if seq != index:
            raise TranscriptValidationError("event seq values must be contiguous and one-based")
        at_ms = _plain_int(
            event_obj["at_ms"],
            f"event {index}.at_ms",
            minimum=0,
            maximum=MAX_VIRTUAL_MS,
        )
        if at_ms < previous_ms:
            raise TranscriptValidationError("event timestamps must be nondecreasing")
        previous_ms = at_ms

        event: dict[str, Any] = {"seq": seq, "at_ms": at_ms, "kind": kind}
        if kind == "connect":
            if connected:
                raise TranscriptValidationError("connect event encountered while connected")
            connected = True
        elif kind == "disconnect":
            if not connected:
                raise TranscriptValidationError("disconnect event encountered while disconnected")
            reason = event_obj["reason"]
            if type(reason) is not str or reason not in _DISCONNECT_REASONS:
                raise TranscriptValidationError("disconnect reason is unsupported")
            if reason == "scripted_end" and index != len(raw_events):
                raise TranscriptValidationError("scripted_end disconnect must be the final event")
            event["reason"] = reason
            connected = False
        else:
            if not connected:
                raise TranscriptValidationError(f"{kind} event encountered while disconnected")
            event_text = _string(
                event_obj["text"],
                f"event {index}.text",
                max_bytes=(MAX_RX_LINE_BYTES if kind == "rx_line" else MAX_COMMAND_BYTES),
                allow_empty=(kind == "expect_text_write"),
                allow_trailing_line_endings=(kind == "expect_text_write"),
            )
            if kind == "expect_text_write":
                _command_wire_bytes(event_text, line_ending)
            total_text_bytes += len(event_text.encode("utf-8"))
            if total_text_bytes > MAX_TOTAL_EVENT_TEXT_BYTES:
                raise TranscriptValidationError("event text exceeds aggregate byte limit")
            event["text"] = event_text
        events.append(event)

    if events[0]["kind"] != "connect" or events[0]["at_ms"] != 0:
        raise TranscriptValidationError("first event must be a connect barrier at 0 ms")
    if connected:
        raise TranscriptValidationError("transcript ends while connected")
    if events[-1]["kind"] != "disconnect" or events[-1].get("reason") != "scripted_end":
        raise TranscriptValidationError("final event must be a scripted_end disconnect")

    return {
        "schema": TRANSCRIPT_SCHEMA,
        "scenario_id": scenario_id,
        "device": device,
        "connection": connection,
        "basis": basis,
        "events": events,
    }


def _command_wire_bytes(value: str, line_ending: str) -> bytes:
    """Apply SerialConnection's text-command normalization without doing I/O."""
    # UTF-8 never encodes a Python code point into fewer than one byte.  Reject
    # before the linear rstrip so terminator padding cannot evade the bound or
    # make a tiny normalized command consume unbounded work.
    if len(value) > MAX_COMMAND_BYTES:
        raise TranscriptValidationError("Text command exceeds input length limit")
    cleaned = value.rstrip("\r\n")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned):
        raise TranscriptValidationError("Text command contains an embedded control character")
    try:
        payload = (cleaned + line_ending).encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise TranscriptValidationError("Text command contains a surrogate") from exc
    if len(payload) > MAX_COMMAND_BYTES:
        raise TranscriptValidationError("Text command exceeds wire byte limit")
    return payload


def _revalidate_transcript_instance(
    transcript: TransportTranscript,
) -> TransportTranscript:
    """Defend ReplayConnection admission even against a forged in-process object."""
    if transcript.schema != TRANSCRIPT_SCHEMA:
        raise TranscriptValidationError("Transcript object has an invalid schema")
    if type(transcript.device) is not TranscriptDevice:
        raise TranscriptValidationError("Transcript object has an invalid device record")
    if type(transcript.connection) is not TranscriptConnection:
        raise TranscriptValidationError("Transcript object has an invalid connection record")
    if transcript.basis is not None and type(transcript.basis) is not TranscriptBasis:
        raise TranscriptValidationError("Transcript object has an invalid basis record")
    if type(transcript.events) is not tuple or any(
        type(event) is not TranscriptEvent for event in transcript.events
    ):
        raise TranscriptValidationError("Transcript object has invalid events")
    if type(transcript._source_bytes) is not bytes:
        raise TranscriptValidationError("Transcript object has invalid source bytes")
    reparsed = TransportTranscript.from_bytes(transcript._source_bytes)

    basis = None
    if transcript.basis is not None:
        basis = {
            "schema": transcript.basis.schema,
            "sha256": transcript.basis.sha256,
            "entry_id": transcript.basis.entry_id,
        }
    events: list[dict[str, Any]] = []
    for event in transcript.events:
        item: dict[str, Any] = {
            "seq": event.seq,
            "at_ms": event.at_ms,
            "kind": event.kind,
        }
        if event.text is not None:
            item["text"] = event.text
        if event.reason is not None:
            item["reason"] = event.reason
        events.append(item)
    candidate = {
        "schema": transcript.schema,
        "scenario_id": transcript.scenario_id,
        "device": {
            "port": transcript.device.port,
            "firmware": transcript.device.firmware,
            "name": transcript.device.name,
            "simulated": transcript.device.simulated,
        },
        "connection": {
            "mode": transcript.connection.mode,
            "baud": transcript.connection.baud,
            "encoding": transcript.connection.encoding,
            "line_ending": transcript.connection.line_ending,
        },
        "basis": basis,
        "events": events,
    }
    semantic = _validate_transcript_object(candidate)
    expected_semantic_hash = hashlib.sha256(
        TRANSCRIPT_SCHEMA.encode("ascii") + b"\0" + _canonical_json(semantic)
    ).hexdigest()
    if transcript.transcript_sha256 != expected_semantic_hash:
        raise TranscriptValidationError("Transcript object semantic hash is invalid")
    _plain_int(
        transcript.source_size_bytes,
        "source_size_bytes",
        minimum=1,
        maximum=MAX_SOURCE_BYTES,
    )
    if (
        type(transcript.source_sha256) is not str
        or _SHA256_RE.fullmatch(transcript.source_sha256) is None
    ):
        raise TranscriptValidationError("Transcript object source hash is invalid")
    if (
        transcript.source_size_bytes != reparsed.source_size_bytes
        or transcript.source_sha256 != reparsed.source_sha256
        or transcript.transcript_sha256 != reparsed.transcript_sha256
    ):
        raise TranscriptValidationError("Transcript object does not match its source bytes")
    return reparsed


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TranscriptValidationError("Unable to inspect transcript path") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _reject_reparse_chain(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        if _is_reparse_point(current):
            raise TranscriptValidationError(f"{label} must not cross a reparse point")


def _validate_local_path_syntax(raw: str, label: str) -> None:
    lowered = raw.lower()
    if (
        any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw)
        or raw.startswith(("~", "\\\\", "//"))
        or lowered.startswith(("\\\\?\\", "\\\\.\\"))
        or "://" in raw
        or "$" in raw
        or "%" in raw
        or any(char in raw for char in "*?[]")
    ):
        raise TranscriptValidationError(f"{label} syntax is not allowed")
    for index, component in enumerate(re.split(r"[\\/]", raw)):
        if not component or (index == 0 and re.fullmatch(r"[A-Za-z]:", component)):
            continue
        if component.rstrip(" .") != component:
            raise TranscriptValidationError(f"{label} has a Windows-ambiguous component")
        stem = component.split(".", 1)[0].rstrip(" .").upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise TranscriptValidationError(f"{label} names a reserved Windows device")


def _validate_path_size(raw: str, label: str) -> None:
    """Bound path work before case-folding, splitting, or Path construction."""
    if len(raw) > MAX_PATH_BYTES:
        raise TranscriptValidationError(f"{label} exceeds length limit")
    try:
        encoded = raw.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise TranscriptValidationError(f"{label} contains a surrogate") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise TranscriptValidationError(f"{label} exceeds byte limit")


def _open_beneath_root_posix(root: Path, relative: Path) -> int:
    """Open a leaf by descriptor-relative, no-follow component walking."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or not _OPEN_SUPPORTS_DIR_FD:
        raise TranscriptValidationError("Platform lacks safe fixture traversal")
    if not relative.parts:
        raise TranscriptValidationError("Transcript path must name a regular file")

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | no_follow
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
        | no_follow
    )
    directory_fd = -1
    try:
        directory_fd = os.open(root.anchor, directory_flags)
        for component in (*root.parts[1:], *relative.parts[:-1]):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
    except OSError as exc:
        raise TranscriptValidationError("Unable to open transcript file safely") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _windows_opened_path(descriptor: int) -> str:
    """Return the normalized DOS path bound to an already-open Windows handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32_768)
    length = get_final_path(
        wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
        buffer,
        len(buffer),
        0,
    )
    if length == 0 or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), "Unable to resolve opened file handle")
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _windows_opened_path_matches(opened_path: str, expected: Path, root: Path) -> bool:
    """Bind the opened handle to the exact pre-open path without another lookup."""
    opened_key = os.path.normcase(os.path.normpath(opened_path))
    expected_key = os.path.normcase(os.path.normpath(os.fspath(expected)))
    root_key = os.path.normcase(os.path.normpath(os.fspath(root)))
    try:
        return (
            opened_key == expected_key
            and os.path.commonpath((opened_key, root_key)) == root_key
            and opened_key != root_key
        )
    except ValueError:
        return False


def load_transcript(
    path: str | os.PathLike[str],
    *,
    fixture_root: str | os.PathLike[str],
) -> TransportTranscript:
    """Load a local regular file contained by an explicit trusted fixture root.

    URL/device paths, traversal, environment expansion, ADS components, and
    symlink/reparse traversal are rejected.  No includes or secondary reads exist.
    """
    raw_path = os.fspath(path)
    if type(raw_path) is not str or not raw_path:
        raise TranscriptValidationError("Transcript path must be a non-empty string")
    _validate_path_size(raw_path, "Transcript path")
    raw_fixture_root = os.fspath(fixture_root)
    if type(raw_fixture_root) is not str or not raw_fixture_root:
        raise TranscriptValidationError("Fixture root must be a non-empty string")
    _validate_path_size(raw_fixture_root, "Fixture root")
    _validate_local_path_syntax(raw_path, "Transcript path")
    _validate_local_path_syntax(raw_fixture_root, "Fixture root")
    drive, tail = os.path.splitdrive(raw_path)
    if ":" in tail:
        raise TranscriptValidationError("Alternate data stream paths are not allowed")
    supplied = Path(raw_path)
    if drive and not supplied.is_absolute():
        raise TranscriptValidationError("Drive-relative transcript paths are not allowed")
    if ".." in supplied.parts:
        raise TranscriptValidationError("Transcript path traversal is not allowed")

    root_drive, root_tail = os.path.splitdrive(raw_fixture_root)
    if ":" in root_tail:
        raise TranscriptValidationError("Alternate data stream roots are not allowed")
    supplied_root = Path(raw_fixture_root)
    if root_drive and not supplied_root.is_absolute():
        raise TranscriptValidationError("Drive-relative fixture roots are not allowed")
    raw_root = supplied_root.absolute()
    try:
        _reject_reparse_chain(raw_root, "Fixture root")
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise TranscriptValidationError("Fixture root does not exist") from exc
    if not root.is_dir():
        raise TranscriptValidationError("Fixture root must be a directory")
    candidate = supplied if supplied.is_absolute() else root / supplied
    lexical_candidate = candidate.absolute()
    try:
        lexical_relative = lexical_candidate.relative_to(root)
    except ValueError as exc:
        raise TranscriptValidationError("Transcript must stay inside fixture root") from exc

    current = root
    for component in lexical_relative.parts:
        current = current / component
        if _is_reparse_point(current):
            raise TranscriptValidationError("Transcript path must not cross a reparse point")
    try:
        lexical_info = lexical_candidate.lstat()
    except OSError as exc:
        raise TranscriptValidationError("Unable to inspect transcript file") from exc
    if not stat.S_ISREG(lexical_info.st_mode):
        raise TranscriptValidationError("Transcript path must name a regular file")
    descriptor = -1
    try:
        if os.name == "nt":
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                info = resolved.stat()
            except (OSError, ValueError) as exc:
                raise TranscriptValidationError("Transcript must stay inside fixture root") from exc
            if not stat.S_ISREG(info.st_mode):
                raise TranscriptValidationError("Transcript path must name a regular file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(resolved, flags)
                opened_path = _windows_opened_path(descriptor)
            except OSError as exc:
                raise TranscriptValidationError("Unable to open transcript file safely") from exc
            if not _windows_opened_path_matches(opened_path, resolved, root):
                raise TranscriptValidationError("Opened transcript path changed during validation")
        else:
            descriptor = _open_beneath_root_posix(root, lexical_relative)

        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TranscriptValidationError("Opened transcript is not a regular file")
        if os.name == "nt" and (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise TranscriptValidationError("Transcript changed while being opened")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise TranscriptValidationError("Unable to read transcript file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return TransportTranscript.from_bytes(raw)


class ReplayConnection:
    """Manual-clock, in-memory, parser-level simulated line connection."""

    def __init__(self, transcript: TransportTranscript) -> None:
        if type(transcript) is not TransportTranscript:
            raise TypeError("ReplayConnection requires a validated TransportTranscript")
        self._transcript = _revalidate_transcript_instance(transcript)
        self._line_ending = self._transcript.connection.line_ending
        self._state = ConnectionState.DISCONNECTED
        self._cursor = 0
        self._virtual_ms = 0
        self._incarnation = 0
        self._run_status = "not_started"
        self._terminal_reason: str | None = None
        self._event_outcomes: list[ReplayEventOutcome] = []
        self._write_outcomes: list[ReplayWriteOutcome] = []
        self._observer_errors: list[ReplayObserverError] = []
        self._observer_error_count = 0
        self._input_error_count = 0
        self._stop_requested: tuple[str, ConnectionState] | None = None
        self._terminal = False
        self._drive_active = False
        self._drive_owner_ident: int | None = None
        self._line_callbacks: list[Callable[[str], None]] = []
        self._state_callbacks: list[Callable[[ConnectionState], None]] = []
        self._error_callbacks: list[Callable[[Exception], None]] = []
        self._lock = threading.RLock()

    @property
    def scenario_id(self) -> str:
        return self._transcript.scenario_id

    @property
    def port(self) -> str:
        return self._transcript.device.port

    @property
    def baud(self) -> int:
        return self._transcript.connection.baud

    @property
    def encoding(self) -> str:
        return self._transcript.connection.encoding

    @property
    def simulated(self) -> bool:
        return True

    @property
    def parser_level_simulation(self) -> bool:
        return True

    @property
    def hardware_evidence(self) -> bool:
        return False

    @property
    def raw(self) -> bool:
        return False

    @property
    def timeout(self) -> float:
        return 0.0

    @property
    def source_sha256(self) -> str:
        return self._transcript.source_sha256

    @property
    def transcript_sha256(self) -> str:
        return self._transcript.transcript_sha256

    @property
    def line_ending(self) -> str:
        return self._line_ending

    @line_ending.setter
    def line_ending(self, value: str) -> None:
        if type(value) is not str or value != self._transcript.connection.line_ending:
            self._record_input_error()
            raise ReplayStateError("Replay line ending is pinned by the transcript")
        with self._lock:
            self._line_ending = value

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    @property
    def virtual_ms(self) -> int:
        with self._lock:
            return self._virtual_ms

    @property
    def incarnation(self) -> int:
        with self._lock:
            return self._incarnation

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._run_status == "passed"

    @property
    def run_status(self) -> str:
        with self._lock:
            return self._run_status

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._drive_active

    @property
    def blocked_on(self) -> str:
        with self._lock:
            return self._blocked_on_locked()

    def _blocked_on_locked(self) -> str:
        if self._terminal and self._run_status != "passed":
            return "terminal"
        if self._cursor >= len(self._transcript.events):
            return "settling" if self._drive_active and not self._terminal else "done"
        event = self._transcript.events[self._cursor]
        if event.kind == "connect":
            return "connect"
        if event.kind == "expect_text_write":
            return "write" if event.at_ms <= self._virtual_ms else "time"
        return "time"

    def _add_callback(self, category: str, callback: Callable[..., None]) -> None:
        if not callable(callback):
            raise TypeError("Replay callback must be callable")
        with self._lock:
            callbacks = self._callbacks_for(category)
            if len(callbacks) >= MAX_CALLBACKS_PER_CATEGORY:
                raise ReplayStateError("Replay callback limit reached")
            callbacks.append(callback)

    def _remove_callback(self, category: str, callback: Callable[..., None]) -> None:
        with self._lock:
            callbacks = self._callbacks_for(category)
            # Callback registration is an identity contract.  Avoid list.remove's
            # user-controlled ``__eq__`` call while preserving first-match semantics.
            for index, registered in enumerate(callbacks):
                if registered is callback:
                    del callbacks[index]
                    break

    def _callbacks_for(self, category: str) -> list[Callable[..., None]]:
        if category == "line":
            return self._line_callbacks
        if category == "state":
            return self._state_callbacks
        if category == "error":
            return self._error_callbacks
        raise AssertionError("unknown callback category")

    def on_line(self, callback: Callable[[str], None]) -> None:
        self._add_callback("line", callback)

    def remove_line_callback(self, callback: Callable[[str], None]) -> None:
        self._remove_callback("line", callback)

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        self._add_callback("state", callback)

    def remove_state_callback(self, callback: Callable[[ConnectionState], None]) -> None:
        self._remove_callback("state", callback)

    def on_error(self, callback: Callable[[Exception], None]) -> None:
        self._add_callback("error", callback)

    def remove_error_callback(self, callback: Callable[[Exception], None]) -> None:
        self._remove_callback("error", callback)

    def _record_observer_error(self, seq: int | None, category: str) -> None:
        with self._lock:
            self._observer_error_count += 1
            if len(self._observer_errors) < MAX_RETAINED_OBSERVER_ERRORS:
                self._observer_errors.append(
                    ReplayObserverError(
                        seq=seq,
                        observed_at_ms=self._virtual_ms,
                        incarnation=self._incarnation,
                        category=category,
                        ordinal=self._observer_error_count,
                    )
                )
            if not self._terminal:
                self._run_status = "callback_failed"

    def _record_input_error(self) -> None:
        with self._lock:
            if self._terminal:
                return
            self._input_error_count += 1
            if not self._observer_error_count:
                self._run_status = "input_rejected"

    def _notify(self, category: str, value: Any, seq: int | None) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks_for(category))
        for callback in callbacks:
            try:
                callback(value)
            except Exception:
                self._record_observer_error(seq, category)
            except BaseException:
                self._record_observer_error(seq, category)
                raise
            with self._lock:
                if self._stop_requested is not None:
                    break

    def _notify_terminal_state(self, state: ConnectionState, seq: int | None) -> None:
        """Fan out a terminal state fully, then propagate the first BaseException."""
        with self._lock:
            callbacks = tuple(self._state_callbacks)
        first_base_exception: BaseException | None = None
        for callback in callbacks:
            try:
                callback(state)
            except Exception:
                self._record_observer_error(seq, "state")
            except BaseException as exc:
                self._record_observer_error(seq, "state")
                if first_base_exception is None:
                    first_base_exception = exc
        if first_base_exception is not None:
            raise first_base_exception

    def _transition(self, state: ConnectionState, seq: int | None) -> None:
        with self._lock:
            stopped = self._stop_requested is not None
            if not stopped and self._state is state:
                return
            if not stopped:
                self._state = state
        if stopped:
            self._settle_stop_request()
            return
        self._notify("state", state, seq)
        self._settle_stop_request()

    def _claim_event_locked(self, event: TranscriptEvent, outcome: str) -> bool:
        """Atomically claim an event unless cancellation already linearized."""
        if self._terminal or self._stop_requested is not None:
            return False
        if (
            self._cursor >= len(self._transcript.events)
            or self._transcript.events[self._cursor] is not event
        ):
            raise ReplayStateError("Replay cursor changed unexpectedly")
        direction = {
            "connect": "connect",
            "expect_text_write": "tx",
            "rx_line": "rx",
            "disconnect": "disconnect",
        }[event.kind]
        self._cursor += 1
        self._event_outcomes.append(
            ReplayEventOutcome(
                seq=event.seq,
                at_ms=event.at_ms,
                observed_at_ms=self._virtual_ms,
                incarnation=self._incarnation,
                direction=direction,
                outcome=outcome,
            )
        )
        if self._run_status == "not_started":
            self._run_status = "running"
        return True

    def _refresh_status(self) -> None:
        with self._lock:
            if self._terminal or self._stop_requested is not None:
                return
            if (
                self._cursor == len(self._transcript.events)
                and self._state is ConnectionState.DISCONNECTED
            ):
                self._terminal = True
                self._run_status = (
                    "callback_failed"
                    if self._observer_error_count
                    else "input_rejected"
                    if self._input_error_count
                    else "passed"
                )
            elif self._observer_error_count:
                self._run_status = "callback_failed"
            elif self._input_error_count:
                self._run_status = "input_rejected"
            elif self._cursor:
                self._run_status = "running"
            else:
                self._run_status = "not_started"

    def _commit_terminal_state(
        self,
        status: str,
        reason: str,
        state: ConnectionState,
        *,
        force: bool = False,
    ) -> bool:
        with self._lock:
            if self._terminal and not force:
                return False
            changed = self._state is not state
            self._terminal = True
            self._run_status = status
            self._terminal_reason = reason
            self._state = state
            self._stop_requested = None
            return changed

    def _mark_interrupted(self) -> None:
        changed = self._commit_terminal_state(
            "interrupted",
            "callback_base_exception",
            ConnectionState.ERROR,
            force=True,
        )
        if changed:
            try:
                self._notify_terminal_state(ConnectionState.ERROR, None)
            except BaseException:
                # Preserve the primary BaseException already propagating through
                # the drive boundary; secondary failures remain count-only.
                pass

    def _terminalize(self, status: str, reason: str, state: ConnectionState) -> None:
        with self._lock:
            if self._terminal:
                return
            settlement = self._settle_stop_request_locked()
            if settlement is None:
                changed = self._state is not state
                self._terminal = True
                self._run_status = status
                self._terminal_reason = reason
                self._state = state
                self._stop_requested = None
            else:
                changed = False
        if settlement is not None:
            self._notify_stop_settlement(settlement)
        elif changed:
            self._notify_terminal_state(state, None)

    def _settle_stop_request_locked(self) -> tuple[ConnectionState, bool] | None:
        """Commit a pending stop while the caller owns ``self._lock``."""
        if self._terminal:
            self._stop_requested = None
            return None
        if self._stop_requested is None:
            return None
        reason, state = self._stop_requested
        self._stop_requested = None
        self._terminal = True
        self._run_status = "operator_stopped"
        self._terminal_reason = reason
        changed = self._state is not state
        self._state = state
        return state, changed

    def _notify_stop_settlement(self, settlement: tuple[ConnectionState, bool] | None) -> None:
        if settlement is not None and settlement[1]:
            self._notify_terminal_state(settlement[0], None)

    def _settle_stop_request(self) -> None:
        with self._lock:
            settlement = self._settle_stop_request_locked()
        self._notify_stop_settlement(settlement)

    def _finish_drive(self) -> None:
        """Atomically consume the last stop request and release drive ownership."""
        with self._lock:
            settlement = self._settle_stop_request_locked()
            notify_while_owned = settlement is not None and settlement[1]
            if not notify_while_owned:
                self._drive_active = False
                self._drive_owner_ident = None
        if notify_while_owned:
            try:
                self._notify_stop_settlement(settlement)
            except BaseException:
                self._mark_interrupted()
                raise
            finally:
                with self._lock:
                    self._drive_active = False
                    self._drive_owner_ident = None

    @contextmanager
    def _driving(self) -> Iterator[None]:
        owner_ident = threading.get_ident()
        with self._lock:
            if self._drive_active:
                if not self._terminal and self._stop_requested is None:
                    self._stop_requested = (
                        "concurrent_or_reentrant_drive",
                        ConnectionState.ERROR,
                    )
                raise ReplayStateError("Concurrent or reentrant replay drive is not allowed")
            self._drive_active = True
            self._drive_owner_ident = owner_ident
        try:
            yield
        except (
            ReplayMismatchError,
            ReplayStateError,
            TranscriptValidationError,
            TypeError,
            ValueError,
        ):
            raise
        except BaseException:
            self._mark_interrupted()
            raise
        finally:
            try:
                self._finish_drive()
            except BaseException:
                self._mark_interrupted()
                raise

    def _next_event(self) -> TranscriptEvent | None:
        with self._lock:
            if self._cursor >= len(self._transcript.events):
                return None
            return self._transcript.events[self._cursor]

    def _reject_lifecycle(self, reason: str) -> None:
        self._terminalize("operator_stopped", reason, ConnectionState.ERROR)
        raise ReplayStateError(f"Replay lifecycle rejected ({reason})")

    def connect(self) -> None:
        """Consume a due connect barrier; never opens any external resource."""
        with self._driving():
            with self._lock:
                stopped = self._stop_requested is not None
                if not stopped and self._state is ConnectionState.CONNECTED:
                    return
                if not stopped and self._terminal:
                    raise ReplayStateError("Replay is terminal")
                event = None if stopped else self._next_event()
                due = event is not None and event.at_ms <= self._virtual_ms
                valid = event is not None and event.kind == "connect" and due
                if valid:
                    self._incarnation += 1
                    claimed = self._claim_event_locked(event, "barrier_consumed")
                    if not claimed:
                        self._incarnation -= 1
                        stopped = True
            if stopped:
                self._settle_stop_request()
                return
            if not valid:
                self._reject_lifecycle("connect_barrier_not_due")
            assert event is not None
            self._transition(ConnectionState.CONNECTING, event.seq)
            with self._lock:
                if self._terminal:
                    raise ReplayStateError("Replay terminated during state notification")
            self._transition(ConnectionState.CONNECTED, event.seq)
            with self._lock:
                if self._terminal:
                    raise ReplayStateError("Replay terminated during state notification")
            self._refresh_status()

    def _consume_due_automatic(self, limit_ms: int, *, move_clock: bool) -> int:
        consumed = 0
        while True:
            self._settle_stop_request()
            with self._lock:
                stopped = self._stop_requested is not None
                if stopped or self._terminal:
                    break
                event = self._next_event()
                if event is None or event.kind in {"connect", "expect_text_write"}:
                    break
                if event.at_ms > limit_ms:
                    break
                if move_clock and event.at_ms > self._virtual_ms:
                    self._virtual_ms = event.at_ms
                claimed = self._claim_event_locked(
                    event,
                    (
                        "simulated_emission_claimed"
                        if event.kind == "rx_line"
                        else event.reason or "disconnected"
                    ),
                )
                if not claimed:
                    stopped = True
            if stopped:
                self._settle_stop_request()
                break
            if event.kind == "rx_line":
                if self.state is not ConnectionState.CONNECTED:
                    self._reject_lifecycle("receive_while_disconnected")
                self._notify("line", event.text, event.seq)
                self._settle_stop_request()
            else:
                self._transition(ConnectionState.DISCONNECTED, event.seq)
            consumed += 1
        self._refresh_status()
        return consumed

    def _advance_to_during_drive(self, target_ms: int) -> int:
        """Commit a prevalidated target while this caller owns the drive."""
        with self._lock:
            stopped = self._stop_requested is not None
            if not stopped and self._terminal:
                if self._run_status == "passed":
                    return 0
                raise ReplayStateError("Replay is terminal")
            if not stopped and target_ms < self._virtual_ms:
                self._record_input_error()
                raise ValueError("Replay clock cannot move backwards")
            if not stopped:
                self._virtual_ms = target_ms
        if stopped:
            self._settle_stop_request()
            return 0
        return self._consume_due_automatic(target_ms, move_clock=False)

    def advance_to(self, target_ms: int) -> int:
        """Move the manual clock monotonically and deliver due automatic events."""
        if type(target_ms) is not int or not 0 <= target_ms <= MAX_VIRTUAL_MS:
            self._record_input_error()
            raise ValueError("target_ms must be a bounded plain integer")
        with self._driving():
            return self._advance_to_during_drive(target_ms)

    def advance_by(self, delta_ms: int) -> int:
        """Advance the manual clock by a bounded non-negative delta."""
        if type(delta_ms) is not int or delta_ms < 0:
            self._record_input_error()
            raise ValueError("delta_ms must be a non-negative plain integer")
        with self._driving():
            with self._lock:
                stopped = self._stop_requested is not None
                target = self._virtual_ms if stopped else self._virtual_ms + delta_ms
            if stopped:
                self._settle_stop_request()
                return 0
            if target > MAX_VIRTUAL_MS:
                self._record_input_error()
                raise ValueError("Replay clock exceeds maximum")
            return self._advance_to_during_drive(target)

    def drain(self) -> int:
        """Advance only as needed through automatic events, stopping at a barrier."""
        with self._driving():
            with self._lock:
                if self._terminal:
                    if self._run_status == "passed":
                        return 0
                    raise ReplayStateError("Replay is terminal")
                event = self._next_event()
                if event is None or event.kind in {"connect", "expect_text_write"}:
                    return 0
                limit = MAX_VIRTUAL_MS
            return self._consume_due_automatic(limit, move_clock=True)

    def _mismatch(
        self,
        *,
        event: TranscriptEvent | None,
        reason: str,
        expected_bytes: int | None,
        observed_bytes: int,
    ) -> None:
        error = ReplayMismatchError(
            seq=(event.seq if event else None),
            reason=reason,
            expected_bytes=expected_bytes,
            observed_bytes=observed_bytes,
        )
        with self._lock:
            stopped = self._stop_requested is not None
            if not stopped:
                self._write_outcomes.append(
                    ReplayWriteOutcome(
                        seq=event.seq if event else None,
                        observed_at_ms=self._virtual_ms,
                        incarnation=self._incarnation,
                        bytes_requested=observed_bytes,
                        expected_bytes=expected_bytes,
                        disposition=f"simulated_{reason}",
                    )
                )
                changed = self._state is not ConnectionState.ERROR
                self._terminal = True
                self._run_status = "mismatch"
                self._terminal_reason = reason
                self._state = ConnectionState.ERROR
                self._stop_requested = None
            else:
                changed = False
        if stopped:
            self._settle_stop_request()
            raise ReplayStateError("Replay stopped before mismatch classification")
        if changed:
            self._notify_terminal_state(ConnectionState.ERROR, event.seq if event else None)
        self._notify("error", error, event.seq if event else None)
        raise error

    def write(self, data: str) -> None:
        """Consume one exact, due text-write expectation without any real I/O."""
        if type(data) is not str:
            self._record_input_error()
            raise TypeError("Replay text write requires str")
        try:
            observed = _command_wire_bytes(data, self._line_ending)
        except TranscriptValidationError as exc:
            self._record_input_error()
            raise ValueError(str(exc)) from exc
        with self._driving():
            with self._lock:
                stopped = self._stop_requested is not None
                if not stopped and self._terminal:
                    raise ReplayStateError("Replay is terminal")
                event = None if stopped else self._next_event()
                connected = not stopped and self._state is ConnectionState.CONNECTED
                now = self._virtual_ms
            if stopped:
                self._settle_stop_request()
                raise ReplayStateError("Replay stopped before simulated write")
            if not connected:
                self._mismatch(
                    event=event,
                    reason="write_while_disconnected",
                    expected_bytes=None,
                    observed_bytes=len(observed),
                )
            if event is None or event.kind != "expect_text_write":
                self._mismatch(
                    event=event,
                    reason="unexpected_write",
                    expected_bytes=None,
                    observed_bytes=len(observed),
                )
            expected = _command_wire_bytes(event.text or "", self._line_ending)
            if event.at_ms > now:
                self._mismatch(
                    event=event,
                    reason="write_before_due",
                    expected_bytes=len(expected),
                    observed_bytes=len(observed),
                )
            if observed != expected:
                self._mismatch(
                    event=event,
                    reason="payload_mismatch",
                    expected_bytes=len(expected),
                    observed_bytes=len(observed),
                )
            with self._lock:
                stopped = self._stop_requested is not None
                if not stopped:
                    self._claim_event_locked(event, "simulated_exact_match")
                    self._write_outcomes.append(
                        ReplayWriteOutcome(
                            seq=event.seq,
                            observed_at_ms=self._virtual_ms,
                            incarnation=self._incarnation,
                            bytes_requested=len(observed),
                            expected_bytes=len(expected),
                            disposition="simulated_exact_match",
                        )
                    )
            if stopped:
                self._settle_stop_request()
                raise ReplayStateError("Replay stopped before simulated write commit")
            self._consume_due_automatic(now, move_clock=False)
            self._refresh_status()

    def disconnect(self) -> None:
        """Consume a due scripted disconnect or terminalize as an operator stop."""
        with self._driving():
            with self._lock:
                stopped = self._stop_requested is not None
                if not stopped and self._state is ConnectionState.DISCONNECTED:
                    if self._terminal or self._cursor == 0:
                        return
                    event = None
                    already_disconnected = True
                else:
                    already_disconnected = False
                if not stopped and self._terminal:
                    return
                if not stopped and not already_disconnected:
                    event = self._next_event()
                    if (
                        event is not None
                        and event.kind == "disconnect"
                        and event.at_ms <= self._virtual_ms
                    ):
                        if not self._claim_event_locked(event, event.reason or "disconnected"):
                            event = None
                            stopped = True
                    else:
                        event = None
            if stopped:
                self._settle_stop_request()
                return
            if event is not None:
                self._transition(ConnectionState.DISCONNECTED, event.seq)
                self._refresh_status()
                return
            self._terminalize("operator_stopped", "manual_disconnect", ConnectionState.DISCONNECTED)

    def cancel(self) -> None:
        """Idempotently stop an unfinished replay; no transcript event is fabricated."""
        owns_notification = False
        try:
            with self._lock:
                if self._terminal:
                    return
                if self._drive_active:
                    if self._stop_requested is None:
                        self._stop_requested = ("cancelled", ConnectionState.DISCONNECTED)
                    return
                changed = self._state is not ConnectionState.DISCONNECTED
                self._terminal = True
                self._run_status = "operator_stopped"
                self._terminal_reason = "cancelled"
                self._state = ConnectionState.DISCONNECTED
                self._stop_requested = None
                self._drive_active = True
                self._drive_owner_ident = threading.get_ident()
                owns_notification = True
            if changed:
                self._notify_terminal_state(ConnectionState.DISCONNECTED, None)
        except BaseException:
            self._mark_interrupted()
            raise
        finally:
            if owns_notification:
                with self._lock:
                    self._drive_active = False
                    self._drive_owner_ident = None

    close = cancel

    def snapshot(self) -> ReplayEvidence:
        """Return immutable simulation evidence with a domain-separated run hash."""
        with self._lock:
            evidence = ReplayEvidence._create(
                schema=EVIDENCE_SCHEMA,
                evidence_scope="parser_level_simulation",
                integrity_model="unauthenticated_checksum",
                simulated=True,
                hardware_evidence=False,
                scenario_id=self.scenario_id,
                port=self.port,
                source_size_bytes=self._transcript.source_size_bytes,
                source_sha256=self.source_sha256,
                transcript_sha256=self.transcript_sha256,
                basis=self._transcript.basis,
                basis_verification=(
                    "declared_unverified" if self._transcript.basis is not None else None
                ),
                cursor=self._cursor,
                total_events=len(self._transcript.events),
                virtual_ms=self._virtual_ms,
                incarnation=self._incarnation,
                final_state=self._state.value,
                blocked_on=self._blocked_on_locked(),
                in_flight=self._drive_active,
                complete=self._run_status == "passed",
                run_status=self._run_status,
                terminal_reason=self._terminal_reason,
                events=tuple(self._event_outcomes),
                writes=tuple(self._write_outcomes),
                observer_errors=tuple(self._observer_errors),
                observer_error_count=self._observer_error_count,
                observer_error_overflow=(self._observer_error_count - len(self._observer_errors)),
                input_error_count=self._input_error_count,
                run_sha256="",
            )
        run_hash = hashlib.sha256(
            EVIDENCE_SCHEMA.encode("ascii") + b"\0" + _canonical_json(evidence._without_run_hash())
        ).hexdigest()
        object.__setattr__(evidence, "run_sha256", run_hash)
        return evidence
