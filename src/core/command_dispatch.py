"""Immutable command results shared by the final writer and its consumers.

This module defines the result boundary only; it does not authorize, dispatch,
or retry commands. The transport adapter, shared port lifecycle and HTTP/socket
consumers must be wired before these results describe live application writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from uuid import uuid4


class DispatchOutcome(str, Enum):
    INVALID_INPUT = "invalid_input"
    UNAUTHENTICATED = "unauthenticated"
    UNAUTHORIZED = "unauthorized"
    STALE_BINDING = "stale_binding"
    PORT_BUSY = "port_busy"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    UNAVAILABLE = "unavailable"
    DEFINITELY_NOT_WRITTEN = "definitely_not_written"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    HOST_WRITE_COMPLETE = "host_write_complete"


class DispatchErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    BINDING_CHANGED = "binding_changed"
    PORT_RESERVED = "port_reserved"
    TEXT_NOT_SUPPORTED = "text_not_supported"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    SERVICE_UNAVAILABLE = "service_unavailable"
    ZERO_WRITE = "zero_write"
    SHORT_WRITE = "short_write"
    INVALID_WRITE_COUNT = "invalid_write_count"
    WRITE_ERROR = "write_error"
    FLUSH_ERROR = "flush_error"
    UNKNOWN_DELIVERY = "unknown_delivery"


_RESULT_RULES = MappingProxyType({
    DispatchOutcome.INVALID_INPUT: (400, frozenset({DispatchErrorCode.INVALID_REQUEST})),
    DispatchOutcome.UNAUTHENTICATED: (401, frozenset({DispatchErrorCode.AUTHENTICATION_REQUIRED})),
    DispatchOutcome.UNAUTHORIZED: (403, frozenset({DispatchErrorCode.PERMISSION_DENIED})),
    DispatchOutcome.STALE_BINDING: (409, frozenset({DispatchErrorCode.BINDING_CHANGED})),
    DispatchOutcome.PORT_BUSY: (409, frozenset({DispatchErrorCode.PORT_RESERVED})),
    DispatchOutcome.UNSUPPORTED_TRANSPORT: (422, frozenset({DispatchErrorCode.TEXT_NOT_SUPPORTED})),
    DispatchOutcome.UNAVAILABLE: (503, frozenset({
        DispatchErrorCode.CONNECTION_UNAVAILABLE, DispatchErrorCode.SERVICE_UNAVAILABLE,
    })),
    DispatchOutcome.DEFINITELY_NOT_WRITTEN: (502, frozenset({DispatchErrorCode.ZERO_WRITE})),
    DispatchOutcome.DELIVERY_UNCERTAIN: (502, frozenset({
        DispatchErrorCode.SHORT_WRITE, DispatchErrorCode.INVALID_WRITE_COUNT,
        DispatchErrorCode.WRITE_ERROR, DispatchErrorCode.FLUSH_ERROR,
        DispatchErrorCode.UNKNOWN_DELIVERY,
    })),
    DispatchOutcome.HOST_WRITE_COMPLETE: (200, frozenset({None})),
})

_OPAQUE_ID = re.compile(r"[0-9a-f]{32}")
MAX_STEP_INDEX = 2**53 - 1


def _validate_id(value: object) -> None:
    if type(value) is not str or _OPAQUE_ID.fullmatch(value) is None:
        # Do not include rejected input, which may contain a command or secret.
        raise ValueError("Correlation IDs must be 32 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class RequestCorrelation:
    """Server-issued correlation for one operation, optionally one playback step.

    Step indices are zero-based. The server creates a request ID once and shares
    it between its response, private progress and audit metadata. A valid ID is
    neither authentication nor an idempotency key; never trust a client's ID as
    server-issued or use it to authorize a retry. Playback ownership is separate.
    """

    request_id: str
    run_id: str | None = None
    step_index: int | None = None

    def __post_init__(self) -> None:
        _validate_id(self.request_id)
        if self.run_id is None:
            if self.step_index is not None:
                raise ValueError("Playback correlation requires both run ID and step index")
            return
        _validate_id(self.run_id)
        if type(self.step_index) is not int or not 0 <= self.step_index <= MAX_STEP_INDEX:
            raise ValueError("Playback step index must be a nonnegative JavaScript-safe integer")

    @classmethod
    def create(
        cls, *, run_id: str | None = None, step_index: int | None = None,
    ) -> RequestCorrelation:
        """Create a new operation ID at the trusted server boundary."""
        return cls(uuid4().hex, run_id, step_index)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """A bounded result without command text, exception objects or caller data.

    Completion means the host finished its write, never that firmware received
    or executed it. A post-write accounting failure must preserve that outcome;
    report any accounting diagnostic separately without inviting command replay.
    Only ``host_write_complete`` can advance a playback sequence.
    """

    correlation: RequestCorrelation
    outcome: DispatchOutcome
    error_code: DispatchErrorCode | None = None

    def __post_init__(self) -> None:
        if type(self.correlation) is not RequestCorrelation:
            raise ValueError("A validated request correlation is required")
        if type(self.outcome) is not DispatchOutcome:
            raise ValueError("A recognized dispatch outcome is required")
        if self.error_code is not None and type(self.error_code) is not DispatchErrorCode:
            raise ValueError("A recognized dispatch error code is required")
        if self.error_code not in _RESULT_RULES[self.outcome][1]:
            raise ValueError("Dispatch error code does not match its outcome")

    @property
    def http_status(self) -> int:
        return _RESULT_RULES[self.outcome][0]

    @property
    def host_write_complete(self) -> bool:
        return self.outcome is DispatchOutcome.HOST_WRITE_COMPLETE

    @property
    def may_have_written(self) -> bool:
        return self.outcome in (
            DispatchOutcome.DELIVERY_UNCERTAIN, DispatchOutcome.HOST_WRITE_COMPLETE,
        )

    @property
    def retryable(self) -> bool:
        # Even a confirmed zero-byte failure needs a new operator action.
        return False

    def __bool__(self) -> bool:
        raise TypeError("Inspect the dispatch outcome or host_write_complete explicitly")

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """Return a fresh scalar-only payload for an initiating owner's response.

        This is not a room-selection or audit-authorization mechanism. Consumers
        must retain the typed outcome even for non-2xx responses, and may not
        convert HTTP 502 into an automatic retry or a fabricated terminal line.
        """
        return {
            "schema_version": 1,
            "request_id": self.correlation.request_id,
            "run_id": self.correlation.run_id,
            "step_index": self.correlation.step_index,
            "outcome": self.outcome.value,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "retryable": self.retryable,
            "may_have_written": self.may_have_written,
        }
