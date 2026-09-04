"""Per-firmware curated one-tap actions for Operate Home — pure, no Qt, unit-testable.

The home surface's QuickActionsStrip shows a small curated set of the connected firmware's most
relevant verbs. The catalog has no `featured`/`rank` field, so the set is derived by scoring each
`cached_commands()` verb on operator INTENT (status / scan / capture / attack / control). Dangerous
verbs ARE included (owner call) - curation only picks WHICH verbs show; every tap still
rides the same guarded `_send` (classify → tx_hard_block → confirm → write) and is danger-labeled +
readiness-gated in the strip. STOP / safe-state is handled by the strip, not here. Honest-empty: a
no-CLI / stock-DIV / no-catalog firmware yields `[]` (the strip shows the honest hint), not invented
tiles. A protocol may declare its own primaries via `CommandInfo.featured=True`, taking precedence.
"""
from __future__ import annotations

from typing import Any

# Intent buckets, highest-value first. Case-insensitive substring on ``ci.name`` then ``category``.
# A verb's FIRST matching bucket sets its rank; ties keep first-seen (catalog) order, like the grid.
_INTENT: "tuple[tuple[str, tuple[str, ...]], ...]" = (
    ("status",  ("status", "info", "chipinfo")),                      # near-universal "is it alive"
    ("scan",    ("scan", "sniff", "list", "discover", "find", "nearby")),
    ("capture", ("capture", "pcap", "handshake", "pmkid", "eapol", "record", "wardriv")),
    ("attack",  ("attack", "deauth", "spam", "beacon", "rickroll", "portal", "jam", "karma")),
    ("control", ("start", "stop", "reboot", "channel", "gps")),
)


def _score(ci: Any) -> "tuple[int, int]":
    """(intent_rank, is_stream) for *ci* - higher rank = more featured; stream breaks ties."""
    name = (getattr(ci, "name", "") or "").lower()
    cat = (getattr(ci, "category", "") or "").lower()
    stream = 1 if getattr(ci, "stream", False) else 0
    for i, (_bucket, needles) in enumerate(_INTENT):
        if any(n in name for n in needles) or any(n in cat for n in needles):
            return (len(_INTENT) - i, stream)
    return (0, stream)


def featured_actions(proto: Any, max_n: int = 5) -> "list[Any]":
    """Up to *max_n* curated CommandInfo verbs for *proto*'s Operate-Home strip.

    Derived from ``proto.cached_commands()`` by intent score (status/scan/capture/attack/control) +
    a stream tie-break; dangerous verbs are NOT excluded (owner call - shown danger-labeled/gated).
    If any command declares ``featured=True`` those win (a protocol names its own primaries).
    Honest-empty: no catalog -> ``[]`` (the strip shows the honest hint). Pure; never raises.
    """
    try:
        commands = list(proto.cached_commands())
    except Exception:  # noqa: BLE001 — a protocol with no cached catalog features nothing, never raises
        return []
    if not commands:
        return []
    # A protocol may opt in by flagging its own primaries; honor those first (capped, first-seen).
    declared = [c for c in commands if getattr(c, "featured", False)]
    if declared:
        return declared[:max_n]
    # Otherwise: bucket each intent-hitting verb by its rank, then pick BREADTH-FIRST across buckets
    # (one of each intent - status, scan, capture, attack, control - in rank order) before a 2nd
    # of any. This balances the strip (not just 5 recon verbs), so a dangerous verb earns a
    # slot (owner opt-in). Within a bucket: stream verbs first (live feedback), then catalog order.
    from collections import defaultdict
    buckets: "dict[int, list]" = defaultdict(list)
    for idx, c in enumerate(commands):
        rank, stream = _score(c)
        if rank > 0:
            buckets[rank].append((-stream, idx, c))
    ranks = sorted(buckets, reverse=True)          # high-rank buckets first (status > scan > ... )
    for r in ranks:
        buckets[r].sort()                          # stream-first, then first-seen (idx)
    out: "list[Any]" = []
    depth = 0
    while len(out) < max_n and any(len(buckets[r]) > depth for r in ranks):
        for r in ranks:
            if depth < len(buckets[r]):
                out.append(buckets[r][depth][2])
                if len(out) >= max_n:
                    break
        depth += 1
    return out
