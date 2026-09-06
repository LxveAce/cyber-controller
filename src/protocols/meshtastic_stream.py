"""Bounded standalone StreamAPI client; no serial handle or scheduler is owned here.

Configuration rows have no request ID. An abandoned burst retains its wire owner until completion.
Omitting wire_state asserts a clean standalone stream. A managed same-stream replacement MUST transfer
wire_state: a fresh backend/session token alone is not clean-stream evidence.
Managed mode is constructed by MeshWireOwner with receipt normalization and its admission envelope.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Callable

from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer

log = logging.getLogger(__name__)
_DEFAULT_CONFIG_ID = 0x12345678
_TEXT_BUF_CAP = 8192
_RESERVED_CONFIG_IDS = (mp.NODELESS_WANT_CONFIG_ID, mp.NODES_ONLY_WANT_CONFIG_ID)
_ID_COUNT = 0xFFFFFFFF - len(_RESERVED_CONFIG_IDS)  # nonzero uint32, excluding special modes
FULL_CONFIG_WIRE_SCHEME = "full-config-uint32-excluding-0-69420-69421-v1"
MANAGED_PROFILE = "meshtastic-ordered-serial-api-v1"
MANAGED_STATE_SCHEME = "meshtastic-managed-admission-v2"
_REVISION_LIMIT = 0xFFFFFFFFFFFFFFFF


def _valid_id(value):
    return type(value) is int and 1 <= value <= 0xFFFFFFFF and value not in _RESERVED_CONFIG_IDS


def _advance_id(seed, count):
    rank = seed - 1 - sum(seed > reserved for reserved in _RESERVED_CONFIG_IDS)
    value = (rank + count) % _ID_COUNT + 1
    for reserved in _RESERVED_CONFIG_IDS:
        value += int(value >= reserved)
    return value


def _issued_ordinal(wire, value):
    """Position in this epoch's nonrepeating rank interval, without changing its scheme."""
    def rank(number):
        return number - 1 - sum(number > reserved for reserved in _RESERVED_CONFIG_IDS)
    ordinal = (rank(value) - rank(wire.seed)) % _ID_COUNT
    if ordinal >= wire.issued:
        raise ValueError("config ID was not allocated in the retained epoch")
    return ordinal


def _content_size(value):
    # Accounting only: preserve existing numeric semantics, including nonfinite telemetry. This is
    # encoded-content accounting, not Python heap or a future public JSON serialization policy.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class ConfigLimits:
    deadline_seconds: float = 15.0
    nodes: int = 512
    inventory_bytes: int = 512 * 1024
    input_bytes: int = 1024 * 1024
    input_frames: int = 4096
    rx_chunks: int = 64
    rx_bytes: int = 64 * 1024
    parser_slice: int = 4096
    events: int = 128
    event_bytes: int = 64 * 1024
    pump_actions: int = 256
    pump_callbacks: int = 64

    def __post_init__(self):
        if (type(self.deadline_seconds) not in (int, float)
                or not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 15):
            raise ValueError("invalid config deadline")
        ceilings = (512, 524288, 1048576, 4096, 64, 65536, 4096, 128, 65536, 256, 64)
        for name, ceiling in zip(tuple(self.__dataclass_fields__)[1:], ceilings):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("invalid config capacity")


@dataclass(frozen=True)
class ConfigWireState:
    """Bounded owner handover, not a public DTO. Export only after retirement actually quiesces."""
    epoch: str
    seed: int
    issued: int = 0
    outstanding_id: int | None = None
    resync_required: bool = False
    # Required even for reconstructed private records: old issued counts used different ranks.
    scheme: str = field(kw_only=True)

    def __post_init__(self):
        if (type(getattr(self, "scheme", None)) is not str or self.scheme != FULL_CONFIG_WIRE_SCHEME
                or type(self.epoch) is not str or len(self.epoch) != 32
                or any(c not in "0123456789abcdef" for c in self.epoch)
                or not _valid_id(self.seed) or type(self.issued) is not int
                or not 0 <= self.issued <= _ID_COUNT or type(self.resync_required) is not bool):
            raise ValueError("invalid config wire state")
        if self.outstanding_id is not None and (
                not self.issued or not _valid_id(self.outstanding_id)
                or self.outstanding_id != _advance_id(self.seed, self.issued - 1)):
            raise ValueError("invalid outstanding config owner")


@dataclass(frozen=True)
class ConfigRequest:
    session_id: str
    attempt_id: int | None
    request_id: int | None
    accepted: bool
    reason: str


class ManagedWriteUnavailable(RuntimeError):
    """A staged managed operation was refused before entering its writer."""


class ManagedWriteError(RuntimeError):
    """Payload-free normalized receipt failure; an ID write still keeps its tombstone."""

    def __init__(self, *, definitely_not_written=False):
        super().__init__("managed write was not host-complete")
        self.definitely_not_written = definitely_not_written is True


@dataclass(frozen=True)
class ConfigOutcome:
    """One historical inventory B/ordinary-config terminal result, never current readiness."""
    session_id: str
    attempt_id: int
    request_id: int
    state: str
    reason: str | None

    def __post_init__(self):
        if (type(self.session_id) is not str or len(self.session_id) != 32
                or any(c not in "0123456789abcdef" for c in self.session_id)
                or type(self.attempt_id) is not int or not 1 <= self.attempt_id <= _REVISION_LIMIT
                or not _valid_id(self.request_id)
                or type(self.state) is not str or self.state not in {"ready", "sync_failed"}
                or (self.state == "ready" and self.reason is not None)
                or (self.state == "sync_failed" and (
                    type(self.reason) is not str or not 1 <= len(self.reason) <= 64))):
            raise ValueError("invalid historical config outcome")


@dataclass(frozen=True)
class ManagedWireState:
    """Required in-memory admission envelope, separate from the allocator's rank scheme."""
    wire: ConfigWireState
    synchronized: bool
    operation: int
    sync_id: int | None
    inventory_id: int | None
    phase: str
    reason: str | None
    disconnect: str
    outstanding_purpose: str | None
    profile: str = field(kw_only=True)
    scheme: str = field(kw_only=True)
    last_config: ConfigOutcome | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if type(self.wire) is not ConfigWireState:
            raise ValueError("invalid managed wire record")
        ConfigWireState.__post_init__(self.wire)
        if (type(getattr(self, "scheme", None)) is not str
                or self.scheme != MANAGED_STATE_SCHEME
                or type(getattr(self, "profile", None)) is not str
                or self.profile != MANAGED_PROFILE
                or type(self.synchronized) is not bool or type(self.operation) is not int
                or not 0 <= self.operation <= _REVISION_LIMIT
                or any(v is not None and not _valid_id(v)
                       for v in (self.sync_id, self.inventory_id))
                or (self.inventory_id is not None
                    and (self.sync_id is None or self.inventory_id == self.sync_id))
                or type(self.phase) is not str
                or self.phase not in {"unverified_idle", "failed", "ready"}
                or (self.reason is not None
                    and (type(self.reason) is not str or len(self.reason) > 64))
                or type(self.disconnect) is not str or self.disconnect not in {
                    "not_started", "host_complete", "not_written", "uncertain"}
                or (self.outstanding_purpose is not None and (
                    type(self.outstanding_purpose) is not str
                    or self.outstanding_purpose not in {"sync", "inventory"}))
                or (self.operation == 0) != (self.sync_id is None)
                or (self.synchronized and self.sync_id is None)
                or (self.synchronized and self.disconnect != "host_complete")
                or (self.wire.outstanding_id is None) != (self.outstanding_purpose is None)
                or (self.outstanding_purpose == "sync" and self.wire.outstanding_id != self.sync_id)
                or (self.disconnect == "uncertain" and not self.wire.resync_required)):
            raise ValueError("invalid managed admission state")
        if "last_config" not in self.__dict__:
            raise ValueError("managed outcome field is missing")
        if self.last_config is not None:
            if type(self.last_config) is not ConfigOutcome:
                raise ValueError("invalid historical config outcome")
            ConfigOutcome.__post_init__(self.last_config)
            outcome_ordinal = _issued_ordinal(self.wire, self.last_config.request_id)
            if self.last_config.attempt_id > outcome_ordinal + 1:
                raise ValueError("config attempt exceeds its allocated progress")
        else:
            outcome_ordinal = None
        if self.operation == 0:
            if (self.phase != "unverified_idle" or self.wire.issued != 0
                    or self.synchronized or self.inventory_id is not None or self.reason is not None
                    or self.disconnect != "not_started" or self.last_config is not None):
                raise ValueError("invalid untouched managed record")
            return
        if self.phase not in {"failed", "ready"}:
            raise ValueError("a handoff cannot restore an active bootstrap without pending work")
        a = _issued_ordinal(self.wire, self.sync_id)
        if self.operation > a + 1:
            raise ValueError("bootstrap operation exceeds its allocated progress")
        if a >= _ID_COUNT - 1:
            raise ValueError("A was allocated without room for a distinct B")
        if self.inventory_id is not None:
            if (_issued_ordinal(self.wire, self.inventory_id) != a + 1
                    or self.disconnect != "host_complete" or self.last_config is None
                    or outcome_ordinal < a + 1):
                raise ValueError("invalid allocated bootstrap A/B sequence")
        elif self.synchronized or self.phase == "ready" or a != self.wire.issued - 1:
            raise ValueError("synchronization requires allocated B after the completed A barrier")
        if ((self.phase == "ready" and (self.reason is not None or self.inventory_id is None))
                or (self.phase == "failed" and not self.reason)):
            raise ValueError("invalid historical bootstrap result")
        if self.outstanding_purpose == "sync":
            if (self.synchronized or self.inventory_id is not None
                    or self.disconnect != "host_complete"):
                raise ValueError("unfinished A cannot grant synchronization")
        elif self.outstanding_purpose == "inventory":
            if (not self.synchronized or self.inventory_id is None
                    or _issued_ordinal(self.wire, self.wire.outstanding_id) <= a):
                raise ValueError("inventory ownership requires the completed sync barrier")
            if (self.last_config.request_id != self.wire.outstanding_id
                    or self.last_config.state != "sync_failed"):
                raise ValueError("retired inventory owner requires its matching failed outcome")
        if self.inventory_id is not None and self.last_config.request_id == self.inventory_id:
            expected_state = "ready" if self.phase == "ready" else "sync_failed"
            if self.last_config.state != expected_state or self.last_config.reason != self.reason:
                raise ValueError("same B has contradictory terminal outcomes")
        if outcome_ordinal is not None and (
                outcome_ordinal == a or (self.inventory_id is None and outcome_ordinal > a)):
            raise ValueError("inventory outcome contradicts the allocated bootstrap history")


@dataclass
class _Inventory:
    nodes: dict[int, mp.MeshNode] = field(default_factory=dict)
    channels: dict[int, mp.MeshChannel] = field(default_factory=dict)
    my_node_num: int | None = None
    lora_config: mp.MeshConfig | None = None
    node_sizes: dict[int, int] = field(default_factory=dict)
    channel_sizes: dict[int, int] = field(default_factory=dict)
    size: int = field(default_factory=lambda: _content_size(
        {"nodes": [], "channels": [], "my_node_num": None, "lora_config": None}))

    def view(self):
        return {"nodes": [asdict(n) for n in sorted(self.nodes.values(), key=lambda n: (not n.is_local, n.num))],
                "channels": [asdict(c) for c in sorted(self.channels.values(), key=lambda c: c.index)],
                "my_node_num": self.my_node_num,
                "lora_config": asdict(self.lora_config) if self.lora_config is not None else None}


@dataclass
class _Attempt:
    number: int
    request_id: int
    deadline: float
    phase: str = "queued"
    inventory: _Inventory = field(default_factory=_Inventory)
    input_bytes: int = 0
    input_frames: int = 0
    purpose: str = "inventory"


@dataclass
class _Bootstrap:
    number: int
    sync_id: int
    deadline: float
    phase: str = "disconnect_queued"
    inventory_id: int | None = None
    reason: str | None = None

    @property
    def active(self):
        return self.phase not in {"ready", "failed"}


class MeshtasticBackend:
    """One source, two bounded inventories, one config writer and one framer lease.

    tick() is caller-driven. Reads derive deadline truth without I/O; no thread is created. Legacy
    writer success means only that its call returned. An exception after entry may be uncertain.
    """

    def __init__(self, writer: Callable[[bytes], None],
                 on_event: Callable[[str, dict], None] | None = None,
                 on_text: Callable[[str], None] | None = None,
                 config_id: int = _DEFAULT_CONFIG_ID, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 limits: ConfigLimits | None = None,
                 wire_state: ConfigWireState | None = None,
                 managed: bool = False, bootstrap_token: object | None = None,
                 managed_state: ManagedWireState | None = None) -> None:
        if type(managed) is not bool or (managed and bootstrap_token is None):
            raise ValueError("managed admission requires an owner token")
        if managed_state is not None:
            if not managed or wire_state is not None or type(managed_state) is not ManagedWireState:
                raise ValueError("managed replacement requires its admission envelope")
            ManagedWireState.__post_init__(managed_state)
            wire_state = managed_state.wire
        elif managed and wire_state is not None:
            raise ValueError("managed replacement requires its admission envelope")
        if not _valid_id(config_id):
            raise ValueError("full config ID must be an exact nonzero uint32 outside special modes")
        if wire_state is not None and type(wire_state) is not ConfigWireState:
            raise ValueError("invalid wire state")
        if wire_state is not None:
            ConfigWireState.__post_init__(wire_state)
        self._wire = wire_state or ConfigWireState(uuid.uuid4().hex, config_id, scheme=FULL_CONFIG_WIRE_SCHEME)
        self._wire_request = self._wire.outstanding_id
        self._managed, self._bootstrap_token = managed, bootstrap_token
        self._synchronized = managed_state.synchronized if managed_state else False
        self._publication = False
        self._bootstrap: _Bootstrap | None = None
        self._disconnect = managed_state.disconnect if managed_state else "not_started"
        self._wire_purpose = managed_state.outstanding_purpose if managed_state else None
        self._identified = False
        self._discarded_text = 0
        self._last_config = managed_state.last_config if managed_state else None
        if managed_state and managed_state.operation:
            self._bootstrap = _Bootstrap(
                managed_state.operation, managed_state.sync_id, 0,
                managed_state.phase, managed_state.inventory_id, managed_state.reason)
        self._resync_required = self._wire.resync_required
        self._writer, self._on_event, self._on_text = writer, on_event, on_text
        self._clock, self._limits = monotonic, limits or ConfigLimits()
        if type(self._limits) is not ConfigLimits:
            raise ValueError("invalid config limits")
        self._session_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._inventory = _Inventory()
        if self._inventory.size > self._limits.inventory_bytes:
            raise ValueError("inventory limit smaller than empty view")
        self._pending: _Attempt | None = None
        self._attempt_seq = max(
            managed_state.operation if managed_state else 0,
            self._last_config.attempt_id if self._last_config else 0)
        self._revision = 0
        self._last_attempt = self._last_request = self._confirmed_attempt = None
        self._last_bytes = self._last_frames = 0
        self._state = "sync_failed" if self._wire_request or self._resync_required else "idle"
        self._reason = "resync_required" if self._resync_required else "busy_draining" if self._wire_request else None
        if self._last_config is not None:
            outcome = self._last_config
            self._last_attempt, self._last_request = outcome.attempt_id, outcome.request_id
            if outcome.state == "sync_failed" and not self._resync_required:
                self._state, self._reason = outcome.state, outcome.reason
        if self._bootstrap is not None and self._bootstrap.phase == "failed":
            b = self._bootstrap
            latest = b.inventory_id if b.inventory_id is not None else b.sync_id
            if (self._last_config is None or _issued_ordinal(self._wire, latest)
                    > _issued_ordinal(self._wire, self._last_config.request_id)):
                self._last_attempt = b.number + int(b.inventory_id is not None)
                self._last_request = latest
                if not self._resync_required:
                    self._state, self._reason = "sync_failed", b.reason
        self._attempt_seq = max(self._attempt_seq, self._last_attempt or 0)
        self._changed = False
        self._retired = self._writer_active = self._reset_pending = False
        self._input_active = False
        self._pump_owner: int | None = None
        self._rx: deque[bytes] = deque()
        self._rx_current = b""
        self._rx_offset = self._rx_bytes = 0
        self._payloads: deque[bytes] = deque()
        self._effects: deque[tuple[str, object, int]] = deque()
        self._event_bytes = self._dropped_events = 0
        self._status_dirty = False
        self._text_buf = bytearray()
        self._framer = StreamFramer(on_skipped=self._on_skipped_bytes)

    def begin_bootstrap(self, profile_token: object) -> ConfigRequest:
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            if not self._managed or profile_token is not self._bootstrap_token:
                return ConfigRequest(self._session_id, None, None, False, "profile_unavailable")
            if self._retired:
                return ConfigRequest(self._session_id, None, None, False, "retired")
            if self._bootstrap is not None and self._bootstrap.active:
                b = self._bootstrap
                return ConfigRequest(self._session_id, b.number, b.sync_id, True, "coalesced")
            if self._pending or self._wire_request or self._writer_active or self._resync_required:
                return ConfigRequest(
                    self._session_id, None, self._wire_request, False, "busy_draining")
            if self._synchronized:
                return ConfigRequest(self._session_id, None, None, False, "already_synchronized")
            if self._wire.issued > _ID_COUNT - 2:
                self._state, self._reason, self._status_dirty = "sync_failed", "id_exhausted", True
                return ConfigRequest(self._session_id, None, None, False, "id_exhausted")
            request_id = _advance_id(self._wire.seed, self._wire.issued)
            self._wire = replace(self._wire, issued=self._wire.issued + 1, outstanding_id=None)
            self._attempt_seq += 1
            self._bootstrap = _Bootstrap(
                self._attempt_seq, request_id, now + 2 * self._limits.deadline_seconds)
            self._pending = _Attempt(
                self._attempt_seq, request_id, now + self._limits.deadline_seconds,
                phase="disconnect_queued", purpose="sync")
            self._disconnect = "not_started"
            self._last_attempt, self._last_request = self._attempt_seq, request_id
            self._state, self._reason, self._status_dirty = "syncing", None, True
            result = ConfigRequest(self._session_id, self._attempt_seq, request_id, True, "started")
        self._pump()
        return result

    def cancel_bootstrap(self, operation: int) -> bool:
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            b = self._bootstrap
            cancelled = (type(operation) is int and b is not None
                         and b.active and b.number == operation)
            if cancelled:
                self._fail_locked("cancelled")
        self._pump()
        return cancelled

    def mark_observation_gap(self, profile_token: object) -> bool:
        """Reduce admission trust after a caller-observed gap; never prove a new attachment.

        This is a local, write-free transition. The transport still owns attachment evidence and
        incarnation binding. Known uncertainty, partial input and active work cannot be erased here.
        """
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            if (not self._managed or profile_token is not self._bootstrap_token or self._retired
                    or not self._synchronized or self._disconnect != "host_complete"
                    or self._pending is not None or self._wire_request is not None
                    or self._resync_required or self._writer_active or self._pump_owner is not None
                    or self._input_active or self._reset_pending or self._rx_bytes or self._payloads
                    or self._effects
                    or (self._bootstrap is not None and self._bootstrap.active)):
                return False
            # No framer lease exists and the state lock prevents a new one while inspected.
            if self._framer.buffered or self._text_buf:
                return False
            self._synchronized = self._publication = False
            # Keep a previous failed config's outcome. A successful view becomes explicitly stale.
            if self._state == "ready":
                self._state = "idle"
            if self._reason is None:
                self._reason = "observation_gap"
            self._status_dirty = True
            return True

    def _now(self):
        try:
            value = self._clock()
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("invalid monotonic clock")
            return value
        except BaseException:
            try:
                self.retire()
            except BaseException:
                # Cleanup must never replace the clock's original control/error object.
                pass
            raise

    @property
    def config_id(self):
        """Last allocated request ID, or the initial seed before the first request."""
        with self._lock:
            return _advance_id(self._wire.seed, max(0, self._wire.issued - 1))

    def start(self) -> ConfigRequest:
        return self.request_config()

    def request_config(self) -> ConfigRequest:
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            if self._retired:
                result = ConfigRequest(self._session_id, None, None, False, "retired")
            elif self._managed and (not self._synchronized
                                    or (self._bootstrap and self._bootstrap.active)):
                result = ConfigRequest(
                    self._session_id, None, self._wire_request, False, "bootstrap_required")
            elif self._pending is not None:
                p = self._pending
                result = ConfigRequest(self._session_id, p.number, p.request_id, True, "coalesced")
            elif self._wire_request is not None or self._writer_active or self._resync_required:
                result = ConfigRequest(self._session_id, None, self._wire_request, False, "busy_draining")
            elif self._wire.issued >= _ID_COUNT:
                self._state, self._reason, self._status_dirty = "sync_failed", "id_exhausted", True
                result = ConfigRequest(self._session_id, None, None, False, "id_exhausted")
            else:
                request_id = _advance_id(self._wire.seed, self._wire.issued)
                self._wire = replace(self._wire, issued=self._wire.issued + 1, outstanding_id=None)
                self._attempt_seq += 1
                p = _Attempt(self._attempt_seq, request_id, now + self._limits.deadline_seconds)
                self._pending = p
                self._last_attempt, self._last_request = p.number, request_id
                self._state, self._reason, self._status_dirty = "syncing", None, True
                result = ConfigRequest(self._session_id, p.number, request_id, True, "started")
        self._pump()
        return result

    def cancel_config(self, attempt_id: int) -> bool:
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            cancelled = type(attempt_id) is int and self._pending is not None and self._pending.number == attempt_id
            if cancelled:
                self._fail_locked("cancelled")
        self._pump()
        return cancelled

    def _fail_locked(self, reason):
        if self._bootstrap is not None and self._bootstrap.active:
            self._bootstrap.phase, self._bootstrap.reason = "failed", reason
        if self._pending is not None:
            if self._managed and self._pending.purpose == "inventory":
                self._last_config = ConfigOutcome(
                    self._session_id, self._pending.number, self._pending.request_id,
                    "sync_failed", reason)
            self._last_bytes, self._last_frames = self._pending.input_bytes, self._pending.input_frames
            self._pending = None
        self._state, self._reason, self._status_dirty = "sync_failed", reason, True

    def _expire_locked(self, now):
        if self._pending is not None and now >= self._pending.deadline:
            self._fail_locked("timeout")

    def _retire_locked(self, reason="retired"):
        if self._retired:
            return
        self._retired = True
        if self._pending is not None:
            self._fail_locked(reason)
        self._state, self._reason = "disconnected", reason
        if self._rx_bytes or self._payloads or self._input_active:
            self._resync_required = True
        self._rx.clear()
        self._rx_current = b""
        self._rx_offset = self._rx_bytes = 0
        self._payloads.clear()
        self._effects.clear()
        self._event_bytes = 0
        self._status_dirty = False
        self._reset_pending = True

    def retire(self) -> None:
        """Fence immediately; only the lease owner can reset mutable framing/debug buffers."""
        with self._lock:
            self._retire_locked()
        self._pump(cleanup_only=True)

    def export_wire_state(self) -> ConfigWireState:
        """Transfer only a retired, quiescent owner. Lost partial/input data requires clean recovery."""
        with self._lock:
            if not self._retired or self._pump_owner is not None or self._writer_active or self._reset_pending:
                raise RuntimeError("config owner has not quiesced")
            return replace(self._wire, outstanding_id=self._wire_request, resync_required=self._resync_required)

    def export_managed_state(self) -> ManagedWireState:
        with self._lock:
            if (not self._managed or not self._retired or self._pump_owner is not None
                    or self._writer_active or self._reset_pending):
                raise RuntimeError("managed owner has not quiesced")
            b = self._bootstrap
            return ManagedWireState(
                replace(self._wire, outstanding_id=self._wire_request,
                        resync_required=self._resync_required),
                self._synchronized, b.number if b else 0, b.sync_id if b else None,
                b.inventory_id if b else None, b.phase if b else "unverified_idle",
                b.reason if b else None,
                self._disconnect, self._wire_purpose if self._wire_request is not None else None,
                profile=MANAGED_PROFILE, scheme=MANAGED_STATE_SCHEME, last_config=self._last_config)

    def close(self) -> None:
        self.retire()
        if self._managed:
            return
        try:
            self._write_payload(mp.encode_disconnect())
        except Exception:
            log.debug("meshtastic: disconnect frame not sent", exc_info=True)

    def tick(self) -> None:
        self._pump()

    def mark_input_lost(self) -> None:
        """An owner-observed loss cannot be repaired by a later marker or a new object."""
        with self._lock:
            self._resync_required = True
            self._fail_locked("input_lost")
        self.retire()

    def feed_bytes(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("stream input must be bytes")
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            if self._retired:
                return
            if (len(data) > self._limits.rx_bytes or self._rx_bytes + len(data) > self._limits.rx_bytes
                    or len(self._rx) + bool(self._rx_current) >= self._limits.rx_chunks):
                self._resync_required = True
                self._fail_locked("capacity")
                self._rx.clear()
                self._rx_current = b""
                self._rx_offset = self._rx_bytes = 0
                self._payloads.clear()
                self._reset_pending = True
            elif data:
                self._rx.append(bytes(data))
                self._rx_bytes += len(data)
        self._pump()

    def _pump(self, *, cleanup_only=False):
        with self._lock:
            if self._pump_owner is not None:
                return
            self._pump_owner = threading.get_ident()
        primary = None
        writer_error = False
        callbacks = 0
        try:
            for _ in range(self._limits.pump_actions):
                if cleanup_only:
                    break
                now = self._now()
                with self._lock:
                    self._expire_locked(now)
                    if self._retired or self._reset_pending:
                        break
                    if self._payloads:
                        action = ("payload", self._payloads.popleft())
                    elif self._rx_current or self._rx:
                        if not self._rx_current:
                            self._rx_current, self._rx_offset = self._rx.popleft(), 0
                        end = min(len(self._rx_current), self._rx_offset + self._limits.parser_slice)
                        chunk = self._rx_current[self._rx_offset:end]
                        self._rx_offset = end
                        if end == len(self._rx_current):
                            # The immutable original retains its consumed prefix until this release.
                            # Charge all of that storage, separately from the bounded parser slice.
                            self._rx_bytes -= len(self._rx_current)
                            self._rx_current, self._rx_offset = b"", 0
                        self._input_active = True
                        action = ("input", chunk)
                    elif (self._pending is not None and self._pending.phase == "sync_barrier"
                          and not self._framer.buffered):
                        # This is an internal pump transition, never a completion subscriber.
                        # Every admitted payload/chunk and any old partial frame went first.
                        b = self._bootstrap
                        request_id = _advance_id(self._wire.seed, self._wire.issued)
                        self._wire = replace(
                            self._wire, issued=self._wire.issued + 1, outstanding_id=None)
                        self._synchronized = True
                        self._attempt_seq += 1
                        self._pending = _Attempt(
                            self._attempt_seq, request_id,
                            min(now + self._limits.deadline_seconds, b.deadline))
                        b.inventory_id, b.phase = request_id, "inventory_queued"
                        self._last_attempt, self._last_request = self._attempt_seq, request_id
                        action = ("transition", None)
                    elif (self._pending is not None and self._pending.phase == "disconnect_queued"
                          and not self._framer.buffered):
                        p = self._pending
                        p.phase = self._bootstrap.phase = "disconnect_writing"
                        self._disconnect = "writing"
                        self._writer_active = True
                        action = ("disconnect", p)
                    elif (self._pending is not None
                          and self._pending.phase in {"queued", "sync_queued"}
                          and not self._framer.buffered):
                        # An incomplete frame already admitted before this request still belongs to
                        # the preceding stream batch. Finish it before admitting a new config write.
                        p = self._pending
                        p.phase = "writing"
                        self._wire_request, self._writer_active = p.request_id, True
                        self._wire_purpose = p.purpose
                        if self._bootstrap is not None and self._bootstrap.active:
                            self._bootstrap.phase = (
                                "sync_writing" if p.purpose == "sync" else "inventory_writing")
                        action = ("write", p)
                    elif callbacks < self._limits.pump_callbacks and self._effects:
                        kind, data, size = self._effects.popleft()
                        self._event_bytes -= size
                        action = ("effect", (kind, data))
                    elif callbacks < self._limits.pump_callbacks and self._status_dirty:
                        self._status_dirty = False
                        action = ("effect", ("mesh_config_status", self._status_locked(now)))
                    else:
                        break
                kind, value = action
                if kind == "input":
                    before = self._framer.invalid_lengths
                    try:
                        payloads = self._framer.feed(value)
                    except BaseException:
                        with self._lock:
                            self._resync_required = True
                        raise
                    finally:
                        with self._lock:
                            self._input_active = False
                    with self._lock:
                        if self._retired or self._reset_pending:
                            continue
                        if (self._framer.invalid_lengths != before or before == 0xFFFFFFFF) and self._pending is not None:
                            self._fail_locked("malformed")
                        self._payloads.extend(payloads)
                elif kind == "payload":
                    self._handle_fromradio(value)
                elif kind == "transition":
                    continue
                elif kind == "disconnect":
                    try:
                        self._writer(StreamFramer.frame(mp.encode_disconnect()))
                    except BaseException as exc:
                        with self._lock:
                            known_zero = (type(exc) is ManagedWriteError
                                          and exc.definitely_not_written)
                            self._disconnect = "not_written" if known_zero else "uncertain"
                            self._resync_required |= not known_zero
                            self._fail_locked("write_failed")
                        writer_error = isinstance(exc, Exception)
                        raise
                    finally:
                        with self._lock:
                            self._writer_active = False
                    with self._lock:
                        self._disconnect = "host_complete"
                    now = self._now()
                    with self._lock:
                        self._expire_locked(now)
                        if self._pending is value and not self._retired:
                            value.phase = self._bootstrap.phase = "sync_queued"
                elif kind == "write":
                    try:
                        self._writer(StreamFramer.frame(mp.encode_want_config(value.request_id)))
                    except Exception:
                        writer_error = True
                        with self._lock:
                            if self._pending is value:
                                self._fail_locked("write_failed")
                        raise
                    finally:
                        with self._lock:
                            self._writer_active = False
                    now = self._now()
                    with self._lock:
                        self._expire_locked(now)
                        if self._pending is value and not self._retired:
                            value.phase = "collecting"
                            if self._bootstrap is not None and self._bootstrap.active:
                                self._bootstrap.phase = (
                                    "sync_collecting" if value.purpose == "sync"
                                    else "inventory_collecting")
                else:
                    callbacks += 1
                    self._deliver(*value)
        except BaseException as exc:
            primary = exc
            if not writer_error:
                with self._lock:
                    self._retire_locked()
            raise
        finally:
            try:
                with self._lock:
                    reset = self._reset_pending
                if reset:
                    partial = bool(self._framer.buffered)
                    self._framer.reset()
                    self._text_buf.clear()
                    with self._lock:
                        self._resync_required |= partial
                        self._reset_pending = False
            except BaseException:
                if primary is None:
                    raise
            finally:
                with self._lock:
                    self._pump_owner = None

    def _charge_locked(self, amount, frames=0):
        p = self._pending
        if p is None or (p.phase == "queued" and not self._managed):
            return
        p.input_bytes = min(self._limits.input_bytes + 1, p.input_bytes + amount)
        p.input_frames = min(self._limits.input_frames + 1, p.input_frames + frames)
        if p.input_bytes > self._limits.input_bytes or p.input_frames > self._limits.input_frames:
            self._fail_locked("capacity")

    def _handle_fromradio(self, payload: bytes) -> None:
        result = mp.decode_fromradio(payload)
        now = self._now()
        with self._lock:
            self._expire_locked(now)
            if self._retired or self._resync_required:
                return
            self._charge_locked(len(payload) + 4, 1)
            if result.malformed:
                if self._pending is not None:
                    self._fail_locked("malformed")
                return
            if self._managed and not self._publication:
                self._identified |= (result.kind in {"node_info", "channel", "text"}
                                     or result.kind == "my_info" and result.my_node_num is not None)
                if result.kind == "text":
                    self._discarded_text = min(0xFFFFFFFF, self._discarded_text + 1)
            if result.kind == "config_complete":
                if result.config_complete_id != self._wire_request or self._writer_active:
                    return
                self._wire_request = None
                self._wire_purpose = None
                p = self._pending
                if p is None or p.phase != "collecting":
                    return
                if p.purpose == "sync":
                    self._identified = True
                    p.phase = self._bootstrap.phase = "sync_barrier"
                    self._status_dirty = True
                    return
                if p.inventory.my_node_num is None:
                    self._fail_locked("missing_local")
                    return
                if self._revision == _REVISION_LIMIT:
                    self._retire_locked("revision_exhausted")
                    return
                if self._on_event is not None and not self._queue_effect_locked("mesh_config_complete", {
                        "config_id": p.request_id, "attempt_id": p.number,
                        "node_count": len(p.inventory.nodes), "channel_count": len(p.inventory.channels),
                        "session_id": self._session_id, "inventory_revision": self._revision + 1,
                        "inventory_confirmed": True}):
                    return
                self._inventory = p.inventory
                if self._managed:
                    self._publication = True
                    self._last_config = ConfigOutcome(
                        self._session_id, p.number, p.request_id, "ready", None)
                    if self._bootstrap is not None and self._bootstrap.active:
                        self._bootstrap.phase = "ready"
                self._confirmed_attempt = p.number
                self._last_bytes, self._last_frames = p.input_bytes, p.input_frames
                self._pending = None
                self._state, self._reason, self._changed = "ready", None, False
                self._revision += 1
                self._status_dirty = True
                return
            if self._managed and not self._publication:
                p = self._pending
                if (result.kind == "text" or p is None or p.purpose == "sync"
                        or p.phase != "collecting"):
                    return
            if result.kind == "text" and result.text is not None:
                t = result.text
                self._queue_event_locked("mesh_text", {
                    "from_num": t.from_num, "from_id": mp.node_id_str(t.from_num), "to_num": t.to_num,
                    "channel": t.channel, "text": t.text, "rx_snr": t.rx_snr,
                    "rx_rssi": t.rx_rssi, "packet_id": t.packet_id})
                return
            if result.kind == "my_info" and self._confirmed_attempt is not None:
                if result.my_node_num != self._inventory.my_node_num:
                    self._retire_locked("source_changed")
                    return
            p = self._pending
            collecting = p is not None and p.phase == "collecting"
            if self._wire_request is not None and not collecting:
                return
            if not collecting and self._revision == _REVISION_LIMIT:
                self._retire_locked("revision_exhausted")
                return
            inv = p.inventory if collecting else self._inventory
            event = self._admit_inventory_locked(inv, result)
            if event is False:
                self._fail_locked("capacity")
            elif event is not None:
                if collecting:
                    self._status_dirty = True
                else:
                    self._revision += 1
                    self._changed = self._confirmed_attempt is not None
                    self._queue_event_locked(*event)

    def _admit_inventory_locked(self, inv, res):
        """Check projected aggregate size before retaining a row; no config-row eviction."""
        kind = res.kind
        if kind == "node_info" and res.node is not None:
            row = replace(res.node, is_local=res.node.num == inv.my_node_num)
            key, rows, sizes = row.num, inv.nodes, inv.node_sizes
            if key not in rows and len(rows) >= self._limits.nodes:
                return False
            event = ("mesh_node", self._node_dict(row))
        elif kind == "channel" and res.channel is not None:
            row = replace(res.channel)
            if not 0 <= row.index <= 7:
                return False if self._pending is not None else None
            key, rows, sizes = row.index, inv.channels, inv.channel_sizes
            event = ("mesh_channel", {"index": row.index, "name": row.name,
                                       "role": row.role, "role_name": row.role_name})
        elif kind == "my_info":
            number = res.my_node_num
            changes = {key: replace(row, is_local=key == number) for key, row in inv.nodes.items()
                       if row.is_local != (key == number)}
            delta = _content_size(number) - _content_size(inv.my_node_num)
            new_sizes = {key: _content_size(asdict(row)) for key, row in changes.items()}
            delta += sum(size - inv.node_sizes[key] for key, size in new_sizes.items())
            if inv.size + delta > self._limits.inventory_bytes:
                return False
            inv.nodes.update(changes)
            inv.node_sizes.update(new_sizes)
            inv.my_node_num, inv.size = number, inv.size + delta
            return ("mesh_my_info", {"num": number, "node_id": mp.node_id_str(number) if number is not None else None})
        elif kind == "config" and res.config is not None:
            row = replace(res.config)
            size = _content_size(asdict(row))
            old = _content_size(asdict(inv.lora_config)) if inv.lora_config is not None else 4
            if inv.size - old + size > self._limits.inventory_bytes:
                return False
            inv.lora_config, inv.size = row, inv.size - old + size
            return ("mesh_config", {"region": row.region, "region_name": row.region_label,
                                     "modem_preset": row.modem_preset, "modem_preset_name": row.modem_preset_label,
                                     "use_preset": row.use_preset})
        else:
            return None
        size = _content_size(asdict(row))
        projected = inv.size - sizes.get(key, 0) + size + int(key not in rows and bool(rows))
        if projected > self._limits.inventory_bytes:
            return False
        rows[key], sizes[key], inv.size = row, size, projected
        return event

    def _queue_event_locked(self, kind, data):
        if self._on_event is None:
            return
        data = {**data, "session_id": self._session_id, "inventory_revision": self._revision,
                "inventory_confirmed": self._confirmed_attempt is not None}
        self._queue_effect_locked(kind, data)

    def _queue_effect_locked(self, kind, data):
        size = _content_size(data)
        key = data.get("num", data.get("index")) if isinstance(data, dict) else None
        if kind in ("mesh_node", "mesh_channel"):
            for index, (old_kind, old, old_size) in enumerate(self._effects):
                if old_kind == kind and old.get("num", old.get("index")) == key:
                    if self._event_bytes - old_size + size <= self._limits.event_bytes:
                        self._effects[index] = (kind, data, size)
                        self._event_bytes += size - old_size
                        return True
        if len(self._effects) >= self._limits.events or self._event_bytes + size > self._limits.event_bytes:
            self._dropped_events = min(0xFFFFFFFF, self._dropped_events + len(self._effects) + 1)
            self._effects.clear()
            self._event_bytes = 0
            self._resync_required = True
            self._fail_locked("capacity")
            return False
        self._effects.append((kind, data, size))
        self._event_bytes += size
        return True

    def _deliver(self, kind, data):
        with self._lock:
            if self._retired:
                return
        try:
            if kind == "debug":
                if self._on_text is not None:
                    self._on_text(data)
            elif self._on_event is not None:
                self._on_event(kind, {**data, "session_id": self._session_id})
        except Exception:
            log.debug("meshtastic: subscriber failed", exc_info=True)

    def _on_skipped_bytes(self, data: bytes):
        # Called only by the framer lease owner. Never invoke external sinks from inside framing.
        with self._lock:
            if self._retired:
                return
            self._charge_locked(len(data))
            if self._managed and not self._publication:
                return
        if self._on_text is None:
            return
        self._text_buf.extend(data)
        while b"\n" in self._text_buf:
            end = self._text_buf.index(b"\n")
            line = bytes(self._text_buf[:end]).decode("utf-8", "replace").rstrip("\r")
            del self._text_buf[:end + 1]
            if line.strip():
                with self._lock:
                    if not self._retired:
                        self._queue_effect_locked("debug", line)
        if len(self._text_buf) > _TEXT_BUF_CAP:
            self._text_buf.clear()

    def _status_locked(self, now):
        p = self._pending
        expired = p is not None and now >= p.deadline
        state = "sync_failed" if expired and not self._retired else self._state
        return {"state": state, "phase": "draining" if (self._wire_request is not None and (p is None or expired))
                else p.phase if p is not None and not expired else None,
                "attempt_id": self._last_attempt, "request_id": self._last_request,
                "reason": "timeout" if expired and not self._retired else self._reason,
                "inventory_revision": self._revision, "confirmed_attempt_id": self._confirmed_attempt,
                "inventory_stale": state != "ready", "inventory_confirmed": self._confirmed_attempt is not None,
                "changed_since_confirmation": self._changed,
                "remaining_seconds": max(0.0, p.deadline - now) if p is not None else 0.0,
                "node_count": len(self._inventory.nodes), "channel_count": len(self._inventory.channels),
                "inventory_bytes": self._inventory.size,
                "pending_nodes": len(p.inventory.nodes) if p is not None and not expired else 0,
                "pending_channels": len(p.inventory.channels) if p is not None and not expired else 0,
                "pending_bytes": p.inventory.size if p is not None and not expired else 0,
                "input_bytes": p.input_bytes if p is not None else self._last_bytes,
                "input_frames": p.input_frames if p is not None else self._last_frames,
                "queued_rx_bytes": self._rx_bytes, "queued_events": len(self._effects),
                "dropped_events": self._dropped_events, "resync_required": self._resync_required,
                **({"last_config_outcome": asdict(self._last_config)
                   if self._last_config is not None else None} if self._managed else {}),
                "cleanup_pending": self._pump_owner is not None or self._writer_active or self._reset_pending}

    def _bootstrap_status_locked(self, now):
        b, p = self._bootstrap, self._pending
        expired = b is not None and b.active and p is not None and now >= p.deadline
        phase = "failed" if expired else b.phase if b else "unverified_idle"
        admission = ("retired" if self._retired else
                     "blocked_uncertain" if self._resync_required else
                     "busy_draining" if self._wire_request and (p is None or expired) else
                     "active" if p is not None and not expired else
                     "unverified_idle" if not self._synchronized else
                     "ready" if self._state == "ready" and self._publication else
                     "synchronized_idle")
        return {"profile": MANAGED_PROFILE, "operation": b.number if b else None,
                "admission_state": admission,
                "phase": phase, "reason": "timeout" if expired else b.reason if b else self._reason,
                "sync_id": b.sync_id if b else None, "inventory_id": b.inventory_id if b else None,
                "synchronized": self._synchronized, "publication_admitted": self._publication,
                "identified": self._identified, "discarded_text": self._discarded_text,
                "disconnect": self._disconnect, "outstanding_purpose": self._wire_purpose,
                "wire_busy": self._wire_request is not None,
                "remaining_seconds": max(0.0, p.deadline - now) if p is not None else 0.0,
                "resync_required": self._resync_required}

    @property
    def session_id(self):
        return self._session_id

    def snapshot(self):
        now = self._now()
        with self._lock:
            status = self._status_locked(now)
            return {"session_id": self._session_id, "retired": self._retired,
                    "config_complete": status["state"] == "ready", "config_status": status,
                    **({"bootstrap_status": self._bootstrap_status_locked(now)}
                       if self._managed else {}),
                    **self._inventory.view()}

    @property
    def config_complete(self):
        return self.snapshot()["config_complete"]

    @property
    def nodes(self):
        with self._lock:
            return {key: replace(row) for key, row in self._inventory.nodes.items()}

    @property
    def channels(self):
        with self._lock:
            return {key: replace(row) for key, row in self._inventory.channels.items()}

    @property
    def my_node_num(self):
        with self._lock:
            return self._inventory.my_node_num

    @property
    def lora_config(self):
        with self._lock:
            return replace(self._inventory.lora_config) if self._inventory.lora_config is not None else None

    def node_list(self):
        return sorted(self.nodes.values(), key=lambda row: (not row.is_local, row.num))

    def active_channels(self):
        return sorted((row for row in self.channels.values() if row.role != 0), key=lambda row: row.index)

    def primary_channel(self):
        return next((row for row in self.channels.values() if row.role == 1), None)

    def send_text(self, text: str, channel: int = 0, dest: int = mp.BROADCAST_NUM):
        if self._managed:
            raise ManagedWriteUnavailable("managed non-transaction writes are not enabled")
        self._write_payload(mp.encode_text_message(text, channel=channel, dest=dest))

    def send_heartbeat(self):
        if self._managed:
            raise ManagedWriteUnavailable("managed non-transaction writes are not enabled")
        self._write_payload(mp.encode_heartbeat())

    def _write_payload(self, payload):
        if self._managed:
            raise ManagedWriteUnavailable("managed non-transaction writes are not enabled")
        self._writer(StreamFramer.frame(payload))

    @staticmethod
    def _node_dict(n):
        return {"num": n.num, "node_id": n.node_id, "long_name": n.long_name, "short_name": n.short_name,
                "hw_model": n.hw_model, "hw_model_name": n.hw_model_name, "snr": n.snr,
                "battery": n.battery, "last_heard": n.last_heard, "is_local": n.is_local}
