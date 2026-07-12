"""Capture store — a thread-safe shared collection of captured handshakes / PMKIDs.

A direct structural mirror of :class:`src.core.cross_comm.TargetPool`: records are keyed by
:attr:`CaptureRecord.key` (``capture_type:bssid``), and adding a duplicate key upserts the existing
record (bumping ``times_seen`` / refreshing ``last_seen``) instead of spawning a duplicate row. An
:class:`~src.core.cross_comm.EventBus` broadcasts ``capture.added`` / ``capture.updated`` /
``capture.removed`` / ``capture.cleared`` / ``capture.cracked`` so the Captures list fills
in live — exactly the way the Targets tab rides ``target.*``.

Part of punch-list #2 (smarter deauth + exportable capture log), slice 1. The auto-register
ingest branch (slice 2), the Captures table + export (slices 3-4) and the crack wiring that calls
:meth:`CaptureStore.mark_cracked` (slice 4) land in later slices; this module is the store itself.
"""

from __future__ import annotations

import threading

from src.core.cross_comm import EventBus
from src.models.capture import CaptureRecord


class CaptureStore:
    """Thread-safe shared store of captured handshakes/PMKIDs (mirrors :class:`TargetPool`)."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._captures: dict[str, CaptureRecord] = {}
        self._lock = threading.Lock()
        self.bus = bus or EventBus()

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._captures)

    def all(self) -> list[CaptureRecord]:
        """Return a snapshot of all captures."""
        with self._lock:
            return list(self._captures.values())

    def get(self, key: str) -> CaptureRecord | None:
        with self._lock:
            return self._captures.get(key)

    # ── Mutation ─────────────────────────────────────────────────────

    def add(self, record: CaptureRecord) -> bool:
        """Add or upsert a capture.

        Returns:
            True if this is a new capture, False if it updated an existing one.
        """
        updated_payload: dict | None = None
        with self._lock:
            existing = self._captures.get(record.key)
            if existing is not None:
                existing.update_from(record)
                updated_payload = existing.to_dict()
            else:
                self._captures[record.key] = record
        # Publish OUTSIDE the lock: the non-reentrant lock must not be held across callbacks, or a
        # subscriber that reads the store would deadlock the ingest thread (mirrors TargetPool.add).
        if updated_payload is not None:
            self.bus.publish("capture.updated", updated_payload)
            return False
        self.bus.publish("capture.added", record.to_dict())
        return True

    def remove(self, key: str) -> CaptureRecord | None:
        with self._lock:
            rec = self._captures.pop(key, None)
        if rec is not None:
            self.bus.publish("capture.removed", rec.to_dict())
        return rec

    def clear(self) -> int:
        """Remove all captures, return the count removed."""
        with self._lock:
            n = len(self._captures)
            self._captures.clear()
        self.bus.publish("capture.cleared", {"count": n})
        return n

    def mark_cracked(self, key: str, password: str, detail: str = "", wordlist: str = "") -> bool:
        """Flip a capture to ``cracked`` with its recovered PSK and publish ``capture.cracked``.

        Returns True if the key was present. The crack slice calls this from its ``_on_done``
        so a solved capture updates its row in place (rather than appending a duplicate).
        """
        with self._lock:
            rec = self._captures.get(key)
            if rec is None:
                return False
            rec.crack_status = "cracked"
            rec.password = password
            rec.crack_detail = detail
            if wordlist:
                rec.wordlist = wordlist
            payload = rec.to_dict()
        self.bus.publish("capture.cracked", payload)
        return True
