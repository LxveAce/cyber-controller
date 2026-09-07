"""Bounded memory-only BLE-history web-runtime owner + read adapter (runtime slice 2).

Owns an optional in-memory ``BleJournal`` for an already-decided BLE-history policy. A
journal is constructed ONLY for an explicit ``memory`` selection, started before the sink
is exposed, and closed via the core's bounded close. It does no filesystem, directory,
ACL, settings-load, env, path, or writer-thread work and never claims disk durability.
``persistent`` is reported unavailable with no fallback; a malformed/disabled/absent policy
creates no journal. The read side only reads the journal -- never starting, stopping,
flushing, clearing, or exporting it, and never making a row a target or action. It is
transport-agnostic: it returns finite outcomes/bodies and the route maps them to HTTP.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from src.core.ble_history_policy import (
    MODE_DISABLED,
    MODE_MEMORY,
    MODE_PERSISTENT,
    BleHistoryPolicy,
)
from src.core.ble_journal import BleJournal, Cursor, JournalLimits

# Read-API bounds (independent of the core's limits; the fixed page budget exceeds max row bytes).
_DEFAULT_LIMIT = 128
_MAX_LIMIT = 256
_PAGE_BYTES = 256 * 1024
_MAX_CURSOR_LEN = 128
# Canonical memory cursor wire form: ordering 0, 16-lowerhex run id, decimal seq, discarding 0.
_CURSOR_RE = re.compile(r"\A0:([0-9a-f]{16}):([0-9]{1,18}):0\Z")
_LIMIT_RE = re.compile(r"\A[0-9]{1,3}\Z")

# Finite read outcomes (the route maps these to HTTP status).
OUTCOME_OK = "ok"                     # 200 with rows
OUTCOME_DISABLED = "disabled"         # 200, empty rows, disabled status
OUTCOME_UNAVAILABLE = "unavailable"   # 503, no data
OUTCOME_BAD_REQUEST = "bad_request"   # 400, before touching the journal
OUTCOME_EXPIRED = "expired"           # 410, old run / evicted position

# Finite status reasons (never a path, raw settings, or exception text).
_REASON_DISABLED = "disabled"
_REASON_PERSISTENT = "persistent_not_implemented"
_REASON_START_FAILED = "start_failed"
_REASON_UNAVAILABLE = "unavailable"
# Bounded admission-counter keys surfaced in status (observations, never durability).
_COUNTER_KEYS = (
    "admitted", "confirmed", "invalid", "queue_full", "degraded_rejected", "closing_rejected",
)


@dataclass(frozen=True)
class HistoryReadResult:
    """A finite read outcome; ``body`` is a JSON-safe dict for the route to serialize."""

    outcome: str
    body: dict


class BleHistoryRuntime:
    """Owns the optional memory journal for one web-runtime lifetime. Construction is
    single-threaded: the factory builds/starts it before any subscription can deliver."""

    def __init__(self, decision: BleHistoryPolicy):
        self._decision = decision
        self._journal: Optional[BleJournal] = None
        self._started = False
        self._start_failed = False
        self._run_id: Optional[str] = None

    # ── lifecycle ──────────────────────────────
    def start(self) -> None:
        """Construct + start a memory journal only for an explicit ``memory`` selection. An ordinary
        start failure keeps the failed journal attached and records ``start_failed`` (no
        raise/retry/replacement); a ``BaseException`` propagates. Other selections create no
        journal. Call once."""
        if self._decision.selected != MODE_MEMORY:
            return
        journal = BleJournal(persist_path=None, limits=JournalLimits())
        self._journal = journal   # retained BEFORE start(), so a failed start stays owned
        try:
            journal.start()
        except Exception:   # noqa: BLE001 -- ordinary start failure is owned + reported, not raised
            self._start_failed = True
            return
        self._run_id = journal.counters().get("run_id")
        self._started = True

    @property
    def sink(self) -> Optional[BleJournal]:
        """The started memory journal for the ingestor to submit into, or None. Set after
        a successful start."""
        return self._journal if self._started else None

    def close(self, timeout: float = 5.0):
        """Close the memory journal via the core's bounded close if one exists; return its
        ``CloseResult`` (or None). ``resolved and lock_released`` means done; else this stage
        is retried."""
        if self._journal is None:
            return None
        return self._journal.close(timeout)

    # ── status ─────────────────────────────────
    def _status_metadata(self) -> dict:
        """Finite JSON-safe status from CACHED fields ONLY -- never a journal call. Every rejection,
        unavailable, disabled and retained-start-failure path uses this so syntax validation stays
        independent of the journal (a failing/locked ``counters()`` can never prevent a finite
        response)."""
        requested = self._decision.requested
        if self._started:
            effective, storage, available, reason = MODE_MEMORY, "memory", True, None
        elif self._start_failed:
            effective, storage, available, reason = MODE_MEMORY, "none", False, _REASON_START_FAILED
        elif self._decision.selected == MODE_DISABLED:
            effective, storage, available, reason = MODE_DISABLED, "none", False, _REASON_DISABLED
        elif self._decision.selected == MODE_PERSISTENT:
            effective, storage, available, reason = (
                MODE_PERSISTENT, "none", False, _REASON_PERSISTENT)
        else:
            # selected is None (malformed/refused): surface the finite policy reason if present.
            effective, storage, available = None, "none", False
            reason = self._decision.reason or _REASON_UNAVAILABLE
        status: dict[str, Any] = {
            "requested_mode": requested, "effective_mode": effective, "storage": storage,
            "available": available, "reason": reason, "durable": False,
            "policy_lifetime": "restart", "run_id": self._run_id,
        }
        return status

    def status(self) -> dict:
        """Full status for a live/successful read: finite metadata plus the STARTED journal's
        bounded admission counters. Counters are a live journal read, so a rejection path uses
        ``_status_metadata()`` instead (no journal access)."""
        status = self._status_metadata()
        if self._started and self._journal is not None:
            counts = self._journal.counters()
            status["counters"] = {k: counts.get(k, 0) for k in _COUNTER_KEYS}
        return status

    # ── read ───────────────────────────────────
    def read_page(
        self, *, cursor: Optional[str] = None, limit: Optional[str] = None,
    ) -> HistoryReadResult:
        """Validate the raw query values then read one bounded page; all validation precedes
        any journal access. ``cursor``/``limit`` are the raw request strings (or None)."""
        # Disabled / persistent / malformed / not-started: finite status, read nothing.
        if not self._started:
            if self._decision.selected == MODE_DISABLED and not self._start_failed:
                body = {"status": self._status_metadata(), "rows": [], "cursor": None,
                        "has_more": False, "earliest_seq": None}
                return HistoryReadResult(OUTCOME_DISABLED, body)
            return HistoryReadResult(OUTCOME_UNAVAILABLE, {"status": self._status_metadata()})
        journal = self._journal
        if journal is None:   # invariant: started implies a journal; defensive
            return HistoryReadResult(OUTCOME_UNAVAILABLE, {"status": self._status_metadata()})

        # limit
        if limit is None:
            max_rows = _DEFAULT_LIMIT
        elif _LIMIT_RE.fullmatch(limit):
            value = int(limit)
            if 1 <= value <= _MAX_LIMIT:
                max_rows = value
            else:
                return self._bad_request("limit_out_of_range")
        else:
            return self._bad_request("limit_malformed")

        # cursor -- validate the exact wire form here; never leak Cursor.parse's raw exception.
        parsed: Optional[Cursor] = None
        if cursor is not None:
            if len(cursor) > _MAX_CURSOR_LEN:
                return self._bad_request("cursor_too_long")
            match = _CURSOR_RE.fullmatch(cursor)
            if match is None:
                return self._bad_request("cursor_malformed")
            cur_run, req_seq = match.group(1), int(match.group(2))
            if cur_run != self._run_id:   # run identity first: an old lifetime is expired
                return self._expired()
            watermark = max(0, journal.counters().get("confirmed_seq", -1))
            if req_seq > watermark:
                return self._bad_request("cursor_future_offset")
            parsed = Cursor(0, self._run_id, req_seq)

        page = journal.read(parsed, max_rows=max_rows, max_bytes=_PAGE_BYTES)
        if page.expired:
            return self._expired(earliest_seq=page.earliest_seq)
        if page.budget_too_small:   # impossible while page budget > max row, but a finite outcome
            return HistoryReadResult(
                OUTCOME_UNAVAILABLE,
                {"status": self._status_metadata(), "reason": "page_budget_too_small"})

        # A response cursor advances ONLY through rows actually returned -- never a sampled total.
        if page.rows:
            resume = f"0:{self._run_id}:{page.rows[-1]['seq']}:0"
        elif cursor is not None:
            resume = cursor   # empty page: retain the validated input cursor
        else:
            resume = f"0:{self._run_id}:0:0"   # fresh empty run
        body = {"status": self.status(), "rows": page.rows, "cursor": resume,
                "has_more": page.next_cursor is not None, "earliest_seq": page.earliest_seq}
        return HistoryReadResult(OUTCOME_OK, body)

    def _bad_request(self, reason: str) -> HistoryReadResult:
        # Syntax rejection: finite metadata only, never a journal call (validate before access).
        return HistoryReadResult(
            OUTCOME_BAD_REQUEST, {"status": self._status_metadata(), "reason": reason})

    def _expired(self, earliest_seq: Optional[int] = None) -> HistoryReadResult:
        return HistoryReadResult(
            OUTCOME_EXPIRED,
            {"status": self._status_metadata(), "reason": "cursor_expired",
             "earliest_seq": earliest_seq})
