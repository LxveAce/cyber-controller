"""Audit trail — integrity-chained logging (SHA-256 hash chain).

Honest guarantee: the chain detects ACCIDENTAL corruption, truncation, and reordering, plus a naive
partial edit that doesn't recompute the downstream hashes. It is NOT tamper-proof against a determined
LOCAL writer — the chain is an UNKEYED, public SHA-256 over public content, so anyone who can edit the
file can recompute every following entry_hash and forge a fully-consistent chain. Real tamper-EVIDENCE
would need a keyed HMAC (a secret the writer can't read) plus an off-box anchor of the head hash; that
redesign is owner-gated (SEC-C1). So a passing verify_integrity() means "not corrupted/truncated", not
"authentic".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64  # Seed hash for the first entry


def _atomic_write_text(path: Path, data: str) -> None:
    """Write *data* to *path* atomically (temp sibling -> fsync -> os.replace), so a crash mid-write
    can never truncate/lose the durable chain (the discipline the sibling secret files use). O_CREAT
    0o600 keeps the temp owner-only; it is removed on any failure, and the existing file is left
    untouched until the atomic replace."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    """Append-only (by API), hash-chained audit log.

    Every entry includes the SHA-256 of the previous entry, forming an integrity chain. A broken link
    (via :meth:`verify_integrity`) reliably flags ACCIDENTAL corruption, truncation, reordering, or a
    naive edit that didn't recompute the downstream hashes. It does NOT prove authenticity: the chain
    is UNKEYED, so a local writer with the code can edit an entry and recompute every subsequent
    entry_hash into a consistent chain (see the module docstring — a keyed HMAC + off-box anchor is the
    owner-gated fix). A passing verify_integrity() means "not accidentally corrupted/truncated", not
    "not tampered with".

    Durability (audit L-2): pass ``persist_path`` to make the trail survive process exit. The
    existing chain is loaded + verified on construction, and every :meth:`record` append is
    flushed to an owner-only JSONL file as it happens — so the auth-fail / flash / serial-command
    records this tool produces aren't lost on a crash or the documented Windows single-instance
    exit. Persistence failures are logged, never raised (auditing must never break the app).
    """

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._entries: list[AuditEntry] = []
        # One AuditTrail is shared by the Qt UI and the threaded web remote (SocketIO
        # async_mode="threading" + the flash worker thread), so record() must be serialized:
        # concurrent appends would otherwise read the SAME last entry and mint two entries with an
        # identical prev_hash, breaking the chain that verify_integrity() promises. Reentrant so the
        # construction-time _load_jsonl → verify_integrity path can nest safely.
        self._lock = threading.RLock()
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        if self._persist_path is not None:
            self._init_persistence()

    # ── Durable persistence (append-only JSONL) ──────────────────────

    @staticmethod
    def _harden_perms(path: Path) -> None:
        """POSIX 0600 fallback for the owner-only guarantee (a no-op on Windows).

        The owner-only promise in the class docstring is enforced two ways, mirroring every sibling
        secret file (encrypted_storage.save, vault._save_hdr, web_auth): restrict_to_current_user()
        sets the explicit NTFS ACL on Windows, and this chmod is the POSIX side. win_acl is a
        documented no-op off Windows (win_acl.py:143), so WITHOUT this the persisted trail — which
        holds web auth-fail/ok usernames plus flash/connect/serial-command records — is created
        world-readable (umask 022 → 0644) and any second local account can read it.
        """
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms (Windows)

    def _init_persistence(self) -> None:
        """Load + verify any existing chain, then ensure the file is owner-only."""
        path = self._persist_path
        assert path is not None
        try:
            from src.security.win_acl import restrict_to_current_user, secure_dir

            secure_dir(path.parent)
            if path.exists():
                self._load_jsonl(path)
                ok, bad = self.verify_integrity()
                if not ok:
                    log.warning(
                        "Audit chain failed verification at index %d on load from %s — "
                        "the on-disk trail is corrupted, truncated, or was edited without "
                        "recomputing the chain.",
                        bad, path,
                    )
                else:
                    log.info("Audit trail loaded + verified: %s (%d entries)", path, len(self._entries))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self._harden_perms(path)        # POSIX 0600 (also fixes a legacy 0644 file on reload)
            restrict_to_current_user(path)  # Windows owner-only NTFS ACL
        except Exception:
            # Never let a persistence problem prevent the app from running.
            log.exception("Audit persistence init failed for %s; continuing in-memory only", path)
            self._persist_path = None

    def _load_jsonl(self, path: Path) -> None:
        """Load entries from an append-only JSONL file (one entry per line).

        Parses DEFENSIVELY: a torn/malformed line (e.g. the partial trailing line left by an unclean
        exit or power loss mid-append) is skipped with a warning instead of aborting the whole load.
        A single bad line must never abort the load — that would bubble to _init_persistence's broad
        except, set persist_path=None, and silently disable ALL durable auditing for the session
        (defeating the exact L-2 crash-durability guarantee this file exists for).
        """
        entries: list[AuditEntry] = []
        torn = False
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(AuditEntry(**json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                torn = True
                log.warning("Skipping unparseable audit line in %s: %s", path, exc)
        with self._lock:
            self._entries = entries
        if torn:
            # Rewrite the file from the clean in-memory chain so the next append lands on a well-formed
            # boundary (drops the torn tail) instead of gluing onto a newline-less partial line.
            data = "".join(json.dumps(asdict(e), separators=(",", ":")) + "\n" for e in entries)
            _atomic_write_text(path, data)  # atomic: a crash mid-repair can't truncate the chain
            self._harden_perms(path)  # re-assert 0600 after rewriting the sensitive chain

    def _append_jsonl(self, entry: AuditEntry) -> None:
        if self._persist_path is None:
            return
        try:
            line = json.dumps(asdict(entry), separators=(",", ":"))
            # O_CREAT with mode 0o600 so if the file is (re)created here it is owner-only from the
            # first byte on POSIX; the mode is ignored when the file already exists. Mirrors
            # encrypted_storage.save's os.open(...,0o600) — win_acl is a no-op off Windows.
            fd = os.open(str(self._persist_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # shrink the torn-write window on a crash/power-loss
        except Exception:
            log.exception("Failed to append audit entry to %s", self._persist_path)

    # ── Public API ───────────────────────────────────────────────────

    @property
    def entries(self) -> list[AuditEntry]:
        with self._lock:
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
        # Hold the lock across read-last → build → append → flush so two threads can never read the
        # same predecessor (identical prev_hash) and so the JSONL line order matches memory order.
        with self._lock:
            prev_hash = self._entries[-1].entry_hash if self._entries else _GENESIS_HASH
            entry = AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                action=action,
                details=details or {},
                prev_hash=prev_hash,
            )
            self._entries.append(entry)
            self._append_jsonl(entry)  # L-2: durably flush as the event happens
            log.debug("Audit: %s — %s", action, entry.entry_hash[:16])
            return entry

    def verify_integrity(self) -> tuple[bool, int]:
        """Walk the full chain and verify every hash link.

        A False result reliably flags a broken chain (accidental corruption, truncation, reordering,
        or a naive edit that didn't recompute downstream hashes). A True result does NOT prove
        authenticity — the chain is unkeyed, so a local writer with the code could have recomputed it
        into a consistent chain (see the module docstring for the owner-gated keyed-HMAC redesign).

        Returns:
            (is_valid, first_bad_index) — if valid, index is -1.
        """
        with self._lock:
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
        _atomic_write_text(path, json.dumps(data, indent=2))
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
