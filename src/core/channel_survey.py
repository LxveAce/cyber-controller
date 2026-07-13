"""Passive Wi-Fi channel-occupancy survey.

Read-only analytics over already-scanned APs: how many access points sit on each channel, the
2.4 vs 5 GHz split, and a recommended clear 2.4 GHz channel from the non-overlapping {1, 6, 11}
set — weighting each candidate by the APs that overlap it (2.4 GHz 20 MHz channels overlap when
their numbers differ by 4 or less). No transmission, no probing: it only tallies the live pool.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# Classic non-overlapping 2.4 GHz channels. A 20 MHz channel spans ±2 channels, so two whose numbers
# differ by 5+ do not overlap; |a - b| <= 4 means their masks collide.
_NON_OVERLAP_24 = (1, 6, 11)
_OVERLAP_SPAN = 4


def _is_ap(target: Any) -> bool:
    tt = getattr(target, "target_type", None)
    return getattr(tt, "value", tt) == "ap"


def _overlap_load(per: Counter, candidate: int) -> int:
    """Sum the APs on 2.4 GHz channels that overlap *candidate* (|Δ| ≤ 4)."""
    return sum(n for ch, n in per.items() if ch <= 14 and abs(ch - candidate) <= _OVERLAP_SPAN)


def survey_channels(targets: Any) -> dict:
    """Tally AP occupancy per channel and recommend a clear 2.4 GHz channel.

    *targets* is any iterable of objects with ``.channel`` (int) and ``.target_type``; only APs on a
    real channel (> 0) are counted. Returns a JSON-friendly dict: ``per_channel`` {channel: count}
    ascending, ``band_24`` / ``band_5`` (5 GHz = ch > 14, the tool's channel-based convention),
    ``busiest`` up to five (channel, count) pairs most-first, ``total_aps``, and ``recommend_24`` —
    the least-congested of {1, 6, 11} by overlap-weighted load (None if no 2.4 GHz APs are seen);
    ``recommend_24_load`` is the winning candidate's overlapping-AP count.
    """
    per: Counter = Counter()
    band_24 = band_5 = 0
    for t in targets:
        if not _is_ap(t):
            continue
        ch = int(getattr(t, "channel", 0) or 0)
        if ch <= 0:
            continue  # unknown channel — don't invent one
        per[ch] += 1
        if ch <= 14:
            band_24 += 1
        else:
            band_5 += 1

    recommend = load = None
    if band_24:
        # Fewest overlapping APs wins; min() breaks ties toward the lowest channel number.
        load, recommend = min((_overlap_load(per, cand), cand) for cand in _NON_OVERLAP_24)

    return {
        "per_channel": dict(sorted(per.items())),
        "band_24": band_24,
        "band_5": band_5,
        "busiest": per.most_common(5),
        "total_aps": band_24 + band_5,
        "recommend_24": recommend,
        "recommend_24_load": load,
    }
