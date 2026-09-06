"""Unwired managed Mesh owner for one ordered, exclusively owned transport incarnation.

No serial handle, probe, hub, registry or scheduler is created here. A binding provider must enforce
the captured incarnation and exclusive lease at actual write admission, and supply ordered RX.
Profile selection is a compatibility assumption before inventory, not firmware authentication.
"""

from __future__ import annotations

import copy
import secrets
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from src.protocols import meshtastic_stream as ms


@dataclass(frozen=True)
class OrderedSerialBinding:
    """Explicit provider contract; arbitrary connection-shaped objects are not admitted.

    write_receipt(incarnation, frame) must validate the opaque incarnation under its existing I/O
    barrier and exclude every competing writer. A getter followed by an unchecked write is invalid.
    These declarations describe required provider behavior; they do not implement a serial adapter.
    """

    incarnation: object
    write_receipt: Callable[[object, bytes], object]
    exclusive: bool
    ordered: bool
    profile: str = ms.MANAGED_PROFILE

    def __post_init__(self):
        if (
            self.incarnation is None
            or not callable(self.write_receipt)
            or self.exclusive is not True
            or self.ordered is not True
            or type(self.profile) is not str
            or self.profile != ms.MANAGED_PROFILE
        ):
            raise ValueError("profile_unavailable")


class MeshWireOwner:
    """One persistent parser and allocator; callers explicitly feed/tick/cancel/retire it."""

    def __init__(
        self,
        binding: OrderedSerialBinding,
        *,
        on_event=None,
        on_text=None,
        monotonic=None,
        limits=None,
        randbelow=secrets.randbelow,
        state: ms.ManagedWireState | None = None,
    ):
        self._session = uuid.uuid4().hex
        self._reason = None
        self._token = object()
        self._binding = binding
        self.backend: ms.MeshtasticBackend | None = None
        try:
            if type(binding) is not OrderedSerialBinding:
                raise ValueError("profile_unavailable")
            OrderedSerialBinding.__post_init__(binding)
        except Exception:
            self._reason = "profile_unavailable"
            return
        if state is not None:
            # A managed handoff can never silently fall back to a fresh random/default seed.
            if type(state) is not ms.ManagedWireState:
                raise ValueError("managed admission envelope required")
            ms.ManagedWireState.__post_init__(state)
            seed = state.wire.seed
        else:
            try:
                rank = randbelow(ms._ID_COUNT)
                if type(rank) is not int or not 0 <= rank < ms._ID_COUNT:
                    raise ValueError("invalid nonce rank")
                seed = ms._advance_id(1, rank)
            except Exception:
                self._reason = "nonce_unavailable"
                return
        kwargs = {"monotonic": monotonic} if monotonic is not None else {}
        self.backend = ms.MeshtasticBackend(
            self._write,
            on_event,
            on_text,
            config_id=seed,
            limits=limits,
            managed=True,
            bootstrap_token=self._token,
            managed_state=state,
            **kwargs,
        )

    def _write(self, frame: bytes) -> None:
        # Keep existing serial receipt semantics without opening a serial handle or substituting a
        # second low-level writer. The future binding is responsible for its admission barrier.
        from src.core.serial_handler import WriteDisposition, WriteReceipt

        receipt = self._binding.write_receipt(self._binding.incarnation, frame)
        valid = type(receipt) is WriteReceipt
        if valid:
            try:
                WriteReceipt.__post_init__(receipt)
                valid = receipt.bytes_requested == len(frame)
            except Exception:
                valid = False
        if valid and receipt.disposition is WriteDisposition.HOST_WRITE_COMPLETE:
            return
        zero = valid and receipt.disposition is WriteDisposition.DEFINITELY_NOT_WRITTEN
        raise ms.ManagedWriteError(definitely_not_written=zero)

    def begin_bootstrap(self) -> ms.ConfigRequest:
        if self.backend is None:
            return ms.ConfigRequest(self._session, None, None, False, self._reason)
        return self.backend.begin_bootstrap(self._token)

    def request_config(self) -> ms.ConfigRequest:
        if self.backend is None:
            return ms.ConfigRequest(self._session, None, None, False, self._reason)
        return self.backend.request_config()

    def mark_observation_gap(self) -> bool:
        """Record reduced trust only; this neither rebinds nor proves a transport incarnation."""
        return self.backend is not None and self.backend.mark_observation_gap(self._token)

    def cancel_bootstrap(self, request: ms.ConfigRequest) -> bool:
        return (
            type(request) is ms.ConfigRequest
            and self.backend is not None
            and request.session_id == self.backend.session_id
            and self.backend.cancel_bootstrap(request.attempt_id)
        )

    def feed_bytes(self, incarnation: object, data: bytes) -> None:
        if self.backend is not None and incarnation is self._binding.incarnation:
            self.backend.feed_bytes(data)

    def tick(self) -> None:
        if self.backend is not None:
            self.backend.tick()

    def retire(self) -> None:
        if self.backend is not None:
            self.backend.retire()

    close = retire

    def transport_lost(self) -> None:
        if self.backend is not None:
            self.backend.mark_input_lost()

    def export_state(self) -> ms.ManagedWireState:
        if self.backend is None:
            raise RuntimeError("no admitted managed owner")
        return self.backend.export_managed_state()

    def snapshot(self):
        if self.backend is not None:
            return self.backend.snapshot()
        return {
            "session_id": self._session,
            "retired": False,
            "config_complete": False,
            "nodes": [],
            "channels": [],
            "my_node_num": None,
            "lora_config": None,
            "config_status": {"state": "sync_failed", "reason": self._reason},
            "bootstrap_status": {
                "phase": "unverified_idle",
                "reason": self._reason,
                "publication_admitted": False,
            },
        }


class MeshConnectionSession:
    """Connection-lifetime adapter, prepared by SerialConnection before any port is opened.

    The provider alone delivers captured RX and maintenance on its existing reader. Consumers
    subscribe or inspect snapshots; they do not own a second parser, bootstrap or transport writer.
    This source-only provider does not yet enable managed chat or select a DeviceManager profile.
    """

    def __init__(self, connection, lease, *, state=None, monotonic=None, limits=None,
                 randbelow=secrets.randbelow):
        from src.core.serial_handler import ManagedSerialLease, SerialConnection

        if type(connection) is not SerialConnection or type(lease) is not ManagedSerialLease:
            raise ValueError("profile_unavailable")
        self._connection = connection
        self._lease = lease
        self._lock = threading.Lock()
        self._subscribers = {}
        self._started = False
        self._retired = False
        self._owner = MeshWireOwner(
            OrderedSerialBinding(lease.incarnation, self._write_receipt, True, True),
            on_event=self._publish, on_text=self._publish_debug, state=state,
            monotonic=monotonic, limits=limits, randbelow=randbelow,
        )
        if self._owner.backend is None:
            raise ValueError("Managed Mesh owner preparation failed")
        self.backend = self._owner.backend
        if state is not None:
            # Import never makes historical readiness current on a new physical handle. Known
            # drain/loss records refuse this transition and remain blocked by the existing core.
            self._owner.mark_observation_gap()

    def _write_receipt(self, incarnation, frame):
        from src.core.serial_handler import ManagedSerialUnavailable

        try:
            return self._connection.write_bound_bytes_receipt(self._lease, incarnation, frame)
        except ManagedSerialUnavailable:
            raise ms.ManagedWriteError(definitely_not_written=True) from None

    def subscribe(self, callback):
        """Return an exact removal closure. Subscriptions do not start or retire this session."""
        if not callable(callback):
            raise TypeError("Mesh session subscriber must be callable")
        token = object()
        with self._lock:
            if len(self._subscribers) >= 16:
                raise RuntimeError("Mesh session subscriber capacity reached")
            self._subscribers[token] = callback

        def remove():
            with self._lock:
                self._subscribers.pop(token, None)

        return remove

    def _publish(self, kind, payload):
        with self._lock:
            if self._retired:
                return
            subscribers = tuple(self._subscribers.items())
        for token, callback in subscribers:
            with self._lock:
                if self._retired or self._subscribers.get(token) is not callback:
                    continue
            try:
                callback(kind, copy.deepcopy(payload))
            except Exception:
                pass  # Ordinary subscribers isolate; BaseException reaches the core's owner fence.

    def _publish_debug(self, line):
        self._publish("mesh_log", {"session_id": self.backend.session_id, "line": line})

    def _reader_start(self, incarnation):
        with self._lock:
            if incarnation is not self._lease.incarnation or self._retired or self._started:
                return
            self._started = True
        try:
            self._owner.begin_bootstrap()
        except ms.ManagedWriteError:
            # The core already recorded this receipt outcome (including definite zero-entry).
            # It is not evidence of lost RX or an unexpected reader failure.
            pass

    def _receive(self, incarnation, batch):
        if incarnation is self._lease.incarnation:
            try:
                self._owner.feed_bytes(incarnation, batch)
            except ms.ManagedWriteError:
                pass

    def _maintain(self, incarnation):
        if incarnation is self._lease.incarnation:
            try:
                self._owner.tick()
            except ms.ManagedWriteError:
                pass

    def _transport_retired(self, *, lost):
        with self._lock:
            self._retired = True
        if lost:
            self._owner.transport_lost()
        else:
            self._owner.retire()

    def snapshot(self):
        result = self._owner.snapshot()
        result["transport_status"] = self._connection._managed_status(self._lease)
        return result

    def export_state(self):
        """Retain the same session until both provider resources and core ownership settle."""
        if not self._connection._managed_export_ready(self._lease):
            raise RuntimeError("Managed transport has not quiesced")
        return self._owner.export_state()
