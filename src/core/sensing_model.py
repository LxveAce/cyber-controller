"""Sensing model — a bounded, TTL-aware rollup of Wi-Fi CSI presence/motion verdicts.

Folds ``sensing_verdict`` events (from :class:`~src.protocols.csi_sensor.CsiSensorProtocol`, which
parses a sensing node's ``csi presence=… motion=… conf=…`` lines via :mod:`src.core.sensing`) into
one current-state row per node. A direct structural mirror of
:class:`~src.core.drone_watch.DroneWatchModel` and :class:`~src.core.ble_analyzer.BleAnalyzerModel`:
pure (no Qt, no I/O), bounded (the stalest node is evicted at the cap), TTL-aware (a node's reading
fades as it ages), and clock-injected (``now`` is passed in) so it is deterministic under test.

This is the data layer a "Sense" view renders later; it never authors RF and never routes a
person into the Target pool. Only the PROVEN tier (presence + motion) is real on commodity 2.4 GHz
Wi-Fi CSI — the honesty tiers live in :data:`src.core.sensing.SENSING_TIERS`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.core.sensing import PROVEN

_MAX_NODES = 256          # stalest node is evicted when a new one arrives at the cap
_DEFAULT_TTL = 15.0       # seconds since last_seen after which a node's reading is considered stale
_MOTION_HISTORY = 64      # bounded per-node motion ring for a future trend sparkline


@dataclass
class NodeSensing:
    """One sensing node's current, aged room state — latest verdict + a bounded motion trend."""

    node_id: str
    presence: bool = False
    motion: float = 0.0
    confidence: float = 0.0
    tier: str = PROVEN
    first_seen: float = 0.0
    last_seen: float = 0.0
    verdicts: int = 0
    motion_history: list[float] = field(default_factory=list)

    def age(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def is_fresh(self, now: float, ttl: float = _DEFAULT_TTL) -> bool:
        return self.age(now) <= ttl

    def freshness(self, now: float, ttl: float = _DEFAULT_TTL) -> float:
        """1.0 just-seen -> 0.0 at/after ttl — a stale row fades by this factor in the view."""
        if ttl <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.age(now) / ttl))

    def occupied(self, now: float, ttl: float = _DEFAULT_TTL) -> bool:
        """A node reports the room occupied only while its reading is still fresh — a stale
        presence=True (the node went quiet) must not be shown as a live occupancy."""
        return self.presence and self.is_fresh(now, ttl)


class SensingModel:
    """Bounded, TTL-aware rollup of CSI sensing verdicts (mirrors ``DroneWatchModel``)."""

    def __init__(self, max_nodes: int = _MAX_NODES) -> None:
        self._nodes: dict[str, NodeSensing] = {}
        self._max_nodes = max(1, int(max_nodes))

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def count(self) -> int:
        return len(self._nodes)

    def observe(self, data: object, now: float) -> "NodeSensing | None":
        """Fold one ``sensing_verdict`` payload in, returning the new/updated node row.

        Returns None only if *data* isn't a dict — a verdict with no node id is still valid (a
        single unnamed node deployment), so it keys under ``"node"`` rather than being dropped.
        """
        if not isinstance(data, dict):
            return None
        node_id = str(data.get("node_id", "") or "").strip() or "node"
        n = self._nodes.get(node_id)
        if n is None:
            if len(self._nodes) >= self._max_nodes:
                self._evict_stalest()
            n = NodeSensing(node_id=node_id, first_seen=now)
            self._nodes[node_id] = n
        # Current-state fields: latest verdict wins (presence/motion/confidence are a live reading).
        n.presence = bool(data.get("presence"))
        n.motion = _clamp01(_num(data.get("motion"), 0.0))
        n.confidence = _clamp01(_num(data.get("confidence"), 0.0))
        tier = data.get("tier")
        if tier:
            n.tier = str(tier)
        n.motion_history.append(n.motion)
        if len(n.motion_history) > _MOTION_HISTORY:
            del n.motion_history[: len(n.motion_history) - _MOTION_HISTORY]
        n.last_seen = now
        n.verdicts += 1
        return n

    def _evict_stalest(self) -> None:
        """Drop the least-recently-seen node — bounds memory across many transient nodes."""
        if not self._nodes:
            return
        stalest = min(self._nodes.values(), key=lambda n: n.last_seen)
        self._nodes.pop(stalest.node_id, None)

    def get(self, node_id: str) -> "NodeSensing | None":
        return self._nodes.get(node_id.strip()) if isinstance(node_id, str) else None

    def nodes(self, now: float | None = None, ttl: float = _DEFAULT_TTL,
              fresh_only: bool = False) -> "list[NodeSensing]":
        """Snapshot of node rows, most-recently-seen first. fresh_only drops rows older than ttl."""
        rows = list(self._nodes.values())
        if fresh_only and now is not None:
            rows = [n for n in rows if n.is_fresh(now, ttl)]
        rows.sort(key=lambda n: n.last_seen, reverse=True)
        return rows

    def summary(self, now: float, ttl: float = _DEFAULT_TTL) -> dict:
        """Header counts: total nodes tracked, how many are fresh, and how many report the room
        occupied RIGHT NOW (fresh + presence). ``any_occupied`` drives a room-occupied indicator."""
        rows = list(self._nodes.values())
        occupied = sum(1 for n in rows if n.occupied(now, ttl))
        return {
            "total": len(rows),
            "fresh": sum(1 for n in rows if n.is_fresh(now, ttl)),
            "occupied": occupied,
            "any_occupied": occupied > 0,
        }


def _num(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v
