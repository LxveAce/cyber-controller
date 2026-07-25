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

import json
import os
import threading

from src.core.cross_comm import EventBus
from src.models.capture import CaptureRecord


class CaptureStore:
    """Thread-safe shared store of captured handshakes/PMKIDs (mirrors :class:`TargetPool`)."""

    def __init__(self, bus: EventBus | None = None, persist_path: str | None = None) -> None:
        self._captures: dict[str, CaptureRecord] = {}
        self._lock = threading.Lock()
        self.bus = bus or EventBus()
        # Opt-in durability: given a path, the library survives across sessions — loaded on
        # construction and rewritten atomically after every mutation. Left None (the default, and
        # what every isolated in-memory test uses) it stays purely in RAM, no disk touched.
        self._persist_path = persist_path or ""
        if self._persist_path:
            self._load()

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
        added_payload: dict | None = None
        with self._lock:
            existing = self._captures.get(record.key)
            if existing is not None:
                existing.update_from(record)
                updated_payload = existing.to_dict()
            else:
                self._captures[record.key] = record
                # Snapshot the added payload INSIDE the lock too: a concurrent same-key add() could
                # otherwise mutate this record (update_from) mid-serialization, tearing the payload.
                added_payload = record.to_dict()
        # Publish OUTSIDE the lock: the non-reentrant lock must not be held across callbacks, or a
        # subscriber that reads the store would deadlock the ingest thread (mirrors TargetPool.add).
        if updated_payload is not None:
            self.bus.publish("capture.updated", updated_payload)
            self._autosave()
            return False
        self.bus.publish("capture.added", added_payload)
        self._autosave()
        return True

    def attach_file(self, key: str, pcap_path: str = "", hc22000_path: str = "") -> bool:
        """Attach a written capture file to an existing record WITHOUT counting a re-observation.

        A ``pcap_saved`` line is bookkeeping for a capture already logged, not a fresh sighting, so
        it must not bump ``times_seen`` the way a full :meth:`add` upsert would. Returns True if the
        key existed. Publishes ``capture.updated`` so the Captures list repaints with the file path.
        """
        with self._lock:
            rec = self._captures.get(key)
            if rec is None:
                return False
            if pcap_path:
                rec.pcap_path = pcap_path
            if hc22000_path:
                rec.hc22000_path = hc22000_path
            payload = rec.to_dict()
        self.bus.publish("capture.updated", payload)
        self._autosave()
        return True

    def remove(self, key: str) -> CaptureRecord | None:
        with self._lock:
            rec = self._captures.pop(key, None)
        if rec is not None:
            self.bus.publish("capture.removed", rec.to_dict())
            self._autosave()
        return rec

    def clear(self) -> int:
        """Remove all captures, return the count removed."""
        with self._lock:
            n = len(self._captures)
            self._captures.clear()
        self.bus.publish("capture.cleared", {"count": n})
        self._autosave()
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
        self._autosave()
        return True

    # ── Persistence (opt-in) ─────────────────────────────────────────

    def _load(self) -> None:
        """Best-effort load of the persisted capture library on construction. A missing or corrupt
        file simply starts empty — a broken cache must never stop the app from capturing anew."""
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        with self._lock:
            for d in data:
                try:
                    rec = CaptureRecord.from_dict(d)
                except (TypeError, ValueError, KeyError):
                    continue   # skip a single malformed row, keep the rest
                self._captures[rec.key] = rec

    def _autosave(self) -> None:
        """Rewrite the whole library to disk atomically (temp file + os.replace). No-op when
        persistence is off. Best-effort: a write failure must never break a live capture, so it is
        swallowed — the in-memory store stays authoritative for the session."""
        if not self._persist_path:
            return
        with self._lock:
            payload = [r.to_dict() for r in self._captures.values()]
        tmp = self._persist_path + ".tmp"
        try:
            parent = os.path.dirname(self._persist_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._persist_path)
        except OSError:
            pass
