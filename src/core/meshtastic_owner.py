"""Unwired managed Mesh owner for one ordered, exclusively owned transport incarnation.

No serial handle, probe, hub, registry or scheduler is created here. A binding provider must enforce
the captured incarnation and exclusive lease at actual write admission, and supply ordered RX.
Profile selection is a compatibility assumption before inventory, not firmware authentication.
"""

from __future__ import annotations

import secrets
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
