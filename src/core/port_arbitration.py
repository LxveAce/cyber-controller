"""Shared reservations for a port's connection lifetime and outbound operations.

A reservation can move from a request thread to its flash worker before any
connection is closed. Access to the reserved port is scoped to one thread at a
time. Close/rebind helpers receive that access rather than reacquiring a lease.

This module does not open ports or dispatch commands. Connection managers,
writers and flash workers must share one instance for arbitration to apply.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4


class PortBusy(RuntimeError):
    """Another operation owns the port; callers must not silently replay writes."""


class PortUnavailable(RuntimeError):
    """Registry capacity is unavailable, distinct from a busy target port."""


class InvalidLease(RuntimeError):
    """Access has expired, belongs to another thread, or is still in use."""


def canonical_port(port: str) -> str:
    """Normalize Windows COM aliases without folding case on Unix device paths."""
    if (
        type(port) is not str or not port or len(port) > 512
        or port != port.strip() or any(ord(c) < 32 or ord(c) == 127 for c in port)
    ):
        raise ValueError("Invalid port identifier")
    match = re.fullmatch(r"(?:\\\\\.\\)?COM(\d+)", port, re.IGNORECASE)
    return f"COM{int(match[1])}" if match else port


class IdentityProvenance(str, Enum):
    DETECTED = "detected"
    PROFILE = "profile"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Dispatch identity only; health, capabilities and arm state stay live.

    firmware_forced maps directly to Device.firmware_forced. A manual firmware
    choice must have OPERATOR provenance; its resolved driver_type also belongs
    in this projection. An explicit "unknown" value can represent unresolved
    firmware, but neither that value nor provenance grants text-CLI authority.
    """

    device_id: str
    firmware: str
    protocol: str
    driver_type: str
    provenance: IdentityProvenance
    firmware_forced: bool = False

    def __post_init__(self) -> None:
        for name, limit in (
            ("device_id", 512), ("firmware", 128), ("protocol", 128), ("driver_type", 64),
        ):
            value = getattr(self, name)
            if (
                type(value) is not str or not value or value != value.strip()
                or not value.isprintable() or len(value) > limit
            ):
                raise ValueError(f"Invalid {name} in device identity")
            # Bound encoded storage too; do not silently truncate identity fields.
            try:
                encoded_size = len(value.encode("utf-8"))
            except UnicodeError:
                raise ValueError(f"Invalid {name} in device identity") from None
            if encoded_size > limit:
                raise ValueError(f"Invalid {name} in device identity")
        if type(self.provenance) is not IdentityProvenance:
            raise ValueError("Explicit identity provenance is required")
        if type(self.firmware_forced) is not bool:
            raise ValueError("firmware_forced must be a Boolean")
        if self.firmware_forced and self.provenance is not IdentityProvenance.OPERATOR:
            raise ValueError("Forced firmware requires operator provenance")


@dataclass(frozen=True)
class BindingToken:
    registry_id: str
    port: str
    generation: int


@dataclass(frozen=True)
class PortBinding:
    """One atomically captured identity, connection and resolved driver.

    Connection and driver are opaque references, not copies of transport state.
    Their identity may only be replaced through PortAccess.bind/unbind. Mutable
    policy facts must be read again by the final writer under this same access.
    """

    token: BindingToken
    device: DeviceIdentity
    connection: object
    driver: object


@dataclass
class _PortState:
    reservation: object | None = None
    binding: PortBinding | None = None
    active_thread: threading.Thread | None = None
    access_id: object | None = None
    generation: int = 0


class PortArbiter:
    """One owner for reservations; acquisition is immediate rather than queued.

    The internal lock protects metadata only. It is never held during I/O,
    callbacks, authentication or driver resolution. Those operations run inside
    lease.access(). A nested reserve raises PortBusy instead of deadlocking.
    """

    def __init__(self, *, max_ports: int = 256) -> None:
        if type(max_ports) is not int or max_ports < 1:
            raise ValueError("max_ports must be a positive integer")
        self._max_ports = max_ports
        self._lock = threading.Lock()
        self._states: dict[str, _PortState] = {}
        self._registry_id = uuid4().hex
        self._generation = 0

    def reserve(self, port: str) -> PortLease:
        """Reserve before launching a worker or beginning connection teardown."""
        port = canonical_port(port)
        with self._lock:
            state = self._states.get(port)
            if state is None:
                if len(self._states) >= self._max_ports:
                    raise PortUnavailable("Port registry capacity reached")
                state = self._states[port] = _PortState()
            if state.reservation is not None:
                raise PortBusy("Port is reserved by another operation")
            reservation = object()
            lease = PortLease(self, port, reservation)
            state.reservation = reservation
            return lease


class PortLease:
    """Transferable reservation with explicit, thread-scoped access.

    Typical worker lifecycle: reserve synchronously, pass the lease to the
    worker, use ``with lease.access() as port``, then close in a finally block.
    If thread launch fails, the request thread closes the unused lease.
    """

    def __init__(self, owner: PortArbiter, port: str, reservation: object) -> None:
        self._owner = owner
        self._port = port
        self._reservation = reservation

    @property
    def port(self) -> str:
        return self._port

    def _state_locked(self) -> _PortState:
        state = self._owner._states.get(self.port)
        if state is None or state.reservation is not self._reservation:
            raise InvalidLease("Reservation is no longer active")
        return state

    def access(self) -> _PortAccessContext:
        """Protect one synchronous operation without holding a metadata mutex."""
        return _PortAccessContext(self)

    def close(self) -> None:
        """Release an idle reservation; never release beneath an active write."""
        with self._owner._lock:
            state = self._owner._states.get(self.port)
            if state is None or state.reservation is not self._reservation:
                return  # repeated cleanup cannot release a newer reservation
            if state.active_thread is not None:
                raise InvalidLease("Cannot release a reservation during an operation")
            state.reservation = None
            if state.binding is None:
                del self._owner._states[self.port]


class _PortAccessContext:
    """A single-use context; an invalid exit cannot consume owner cleanup.

    Keep the actual Thread object, since integer thread identifiers can be
    recycled. Abandoning an active context does not release its reservation:
    recovery must first establish that its transport can no longer perform I/O.
    """

    def __init__(self, lease: PortLease) -> None:
        self._lease = lease
        self._access: PortAccess | None = None
        self._closed = False

    def __enter__(self) -> PortAccess:
        with self._lease._owner._lock:
            if self._access is not None:
                raise InvalidLease("Port access context is single-use")
            state = self._lease._state_locked()
            if state.active_thread is not None:
                raise InvalidLease("Reservation already has an active operation")
            access_id = object()
            access = PortAccess(self._lease, access_id)
            self._access = access
            state.active_thread = threading.current_thread()
            state.access_id = access_id
            return access

    def __exit__(self, *_: object) -> bool:
        with self._lease._owner._lock:
            if self._access is None or self._closed:
                raise InvalidLease("Port access context is not active")
            state = self._access._state_locked()
            state.active_thread = None
            state.access_id = None
            self._closed = True
        return False


class PortAccess:
    """Access valid only inside its lease context and on that context's thread."""

    def __init__(self, lease: PortLease, access_id: object) -> None:
        self._lease = lease
        self._access_id = access_id

    def _state_locked(self) -> _PortState:
        state = self._lease._state_locked()
        if (
            state.access_id is not self._access_id
            or state.active_thread is not threading.current_thread()
        ):
            raise InvalidLease("Port access is expired or belongs to another thread")
        return state

    def snapshot(self) -> PortBinding | None:
        with self._lease._owner._lock:
            return self._state_locked().binding

    @property
    def generation(self) -> int:
        """Current epoch, including an invalidated but still reserved binding."""
        with self._lease._owner._lock:
            return self._state_locked().generation

    def matches(self, token: BindingToken) -> bool:
        binding = self.snapshot()
        return binding is not None and binding.token == token

    def bind(
        self, device: DeviceIdentity, connection: object, driver: object,
    ) -> PortBinding:
        """Publish a resolved connection/device/driver together with a new epoch.

        Call after each open/attach/replacement or dispatch identity change,
        including reopen of the same connection object. Do not call for telemetry.
        Resolve the driver and perform any teardown within this existing access.
        """
        if (
            type(device) is not DeviceIdentity
            or connection is None or driver is None
        ):
            raise ValueError("A complete device, connection and driver are required")
        owner = self._lease._owner
        with owner._lock:
            state = self._state_locked()
            owner._generation += 1
            state.generation = owner._generation
            binding = PortBinding(
                BindingToken(owner._registry_id, self._lease.port, owner._generation),
                device, connection, driver,
            )
            state.binding = binding
            return binding

    def unbind(self) -> PortBinding | None:
        """Invalidate the old identity before closing/removing its connection.

        The returned connection can be closed under this same access. Even if
        close fails, queued requests cannot reuse the invalidated binding token.
        """
        with self._lease._owner._lock:
            state = self._state_locked()
            previous = state.binding
            if previous is not None:
                self._lease._owner._generation += 1
                state.generation = self._lease._owner._generation
            state.binding = None
            return previous
