"""Audit trail — tamper-evident logging with SHA-256 hash chain."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64  # Seed hash for the first entry


@dataclass
class AuditEntry:
    """A single audit-trail record.

    Attributes:
        timestamp: ISO-8601 UTC timestamp.
        action: Action category (e.g. 'flash', 'connect', 'mission_start').
        details: Arbitrary payload dict.
        prev_hash: SHA-256 hex digest of the previous entry.
        entry_hash: SHA-256 hex digest of this entry (computed at creation).
    """

    timestamp: str
    action: str
    details: dict[str, Any]
    prev_hash: str
    entry_hash: str = ""

    def __post_init__(self) -> None:
        if not self.entry_hash:
            self.entry_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """SHA-256 over the canonical content of this entry."""
        canonical = json.dumps(
            {
                "timestamp": self.timestamp,
                "action": self.action,
                "details": self.details,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        """Return True if the stored hash matches a fresh computation."""
        return self.entry_hash == self._compute_hash()


class AuditTrail:
    """Append-only, hash-chained audit log.

    Every entry includes the SHA-256 of the previous entry, forming an
    integrity chain.  Any tampering breaks the chain and is detectable
    via :meth:`verify_integrity`.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    # ── Public API ───────────────────────────────────────────────────

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    @property
    def length(self) -> int:
        return len(self._entries)

    def record(self, action: str, details: dict[str, Any] | None = None) -> AuditEntry:
        """Append a new audit entry and return it.

        Args:
            action: Action category string.
            details: Optional metadata dict.
        """
        prev_hash = self._entries[-1].entry_hash if self._entries else _GENESIS_HASH
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            details=details or {},
            prev_hash=prev_hash,
        )
        self._entries.append(entry)
        log.debug("Audit: %s — %s", action, entry.entry_hash[:16])
        return entry

    def verify_integrity(self) -> tuple[bool, int]:
        """Walk the full chain and verify every hash link.

        Returns:
            (is_valid, first_bad_index) — if valid, index is -1.
        """
        expected_prev = _GENESIS_HASH
        for idx, entry in enumerate(self._entries):
            if entry.prev_hash != expected_prev:
                log.warning("Audit chain broken at index %d (prev_hash mismatch)", idx)
                return False, idx
            if not entry.verify():
                log.warning("Audit chain broken at index %d (self-hash mismatch)", idx)
                return False, idx
            expected_prev = entry.entry_hash
        return True, -1

    # ── Persistence ──────────────────────────────────────────────────

    def save_to_file(self, path: str | Path) -> None:
        """Serialize the full trail to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self._entries]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("Audit trail saved: %s (%d entries)", path, len(data))

    def load_from_file(self, path: str | Path) -> None:
        """Deserialize a trail from a JSON file (replaces current entries)."""
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._entries = [AuditEntry(**e) for e in raw]
        log.info("Audit trail loaded: %s (%d entries)", path, len(self._entries))

    # ── Utilities ────────────────────────────────────────────────────

    def filter_by_action(self, action: str) -> list[AuditEntry]:
        """Return entries matching a specific action type."""
        return [e for e in self._entries if e.action == action]

    def tail(self, count: int = 10) -> list[AuditEntry]:
        """Return the last *count* entries."""
        return self._entries[-count:]

    def clear(self) -> None:
        """Remove all entries (destructive)."""
        self._entries.clear()
        log.info("Audit trail cleared")
