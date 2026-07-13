"""Passive target-freshness (staleness) summary over the live pool.

Read-only analytics over already-scanned targets: how many were last observed recently versus a
while ago. Every Target stamps ``last_seen`` (UTC) each time it is re-observed, so partitioning the
pool by "seconds since last seen" tells the operator what is *currently* in range versus what is a
stale left-over from earlier in the session — and, when nothing is fresh, that the capture has gone
quiet (device unplugged / out of range). No transmission, no probing: it only reads the live pool.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Two thresholds partition the pool by how long ago each target was last observed.
_FRESH_SEC = 30      # seen within the last 30 s  -> live, currently in range
_RECENT_SEC = 120    # 30 s – 2 min ago           -> probably still around, going stale
#                      older than 2 min            -> stale left-over from earlier


def _last_seen_age(target: Any, now: datetime) -> float | None:
    """Seconds since *target* was last observed, or None if it carries no usable ``last_seen``."""
    ls = getattr(target, "last_seen", None)
    if not isinstance(ls, datetime):
        return None
    # Defensive: the model stamps UTC, but never subtract a naive datetime from an aware one.
    if ls.tzinfo is None:
        ls = ls.replace(tzinfo=timezone.utc)
    return (now - ls).total_seconds()


def summarize_freshness(targets: Any, now: datetime | None = None) -> dict:
    """Bucket the pool by staleness — how long since each target was last observed.

    *targets* is any iterable of objects with a ``last_seen`` datetime; *now* defaults to the
    current UTC time (injectable for deterministic tests). Returns a JSON-friendly dict: ``total``
    counted, ``fresh`` (≤ 30 s), ``recent`` (30 s–2 min), and ``stale`` (> 2 min) counts, the two
    thresholds used, and ``newest_age_sec`` / ``oldest_age_sec`` (the min/max staleness in the pool,
    None when empty). A target with no ``last_seen`` is skipped rather than guessed
    (verify-never-fake).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    fresh = recent = stale = counted = 0
    newest = oldest = None
    for t in targets:
        age = _last_seen_age(t, now)
        if age is None:
            continue
        age = max(age, 0.0)               # clock skew: last_seen barely ahead -> clamp to 0
        counted += 1
        if age <= _FRESH_SEC:
            fresh += 1
        elif age <= _RECENT_SEC:
            recent += 1
        else:
            stale += 1
        newest = age if newest is None else min(newest, age)
        oldest = age if oldest is None else max(oldest, age)

    return {
        "total": counted,
        "fresh": fresh,
        "recent": recent,
        "stale": stale,
        "fresh_within_sec": _FRESH_SEC,
        "recent_within_sec": _RECENT_SEC,
        "newest_age_sec": None if newest is None else round(newest, 1),
        "oldest_age_sec": None if oldest is None else round(oldest, 1),
    }
