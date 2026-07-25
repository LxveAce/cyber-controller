"""Wardrive channel-coverage planner — the offline, pure-analysis half of Thread C.

Given a WiGLE CSV (as written by :class:`~src.core.wardrive.WardriveSession`), answer three questions:
which Wi-Fi channels carry the most distinct networks, how few channels cover most of them (the
diminishing-returns curve), and — if several radios/nodes run at once — which channels to assign them
to. The channel set is derived ENTIRELY from the observed data; there is NO hardcoded {1, 6, 11} 2.4 GHz
prior, so a 5 GHz-heavy or region-specific capture plans correctly from its own numbers.

LAWFUL, OWNER-AUTHORIZED USE ONLY — this only reads a CSV of already-captured broadcast metadata; it
transmits nothing and touches no hardware. Pure + unit-tested (tests/test_wardrive_planner.py).
"""
from __future__ import annotations

import csv
import io
import os
from typing import Dict, List, Tuple

from src.core.wardrive import _MAC_RE  # reuse the WiGLE MAC-row validator


def channel_yield(csv_text: str) -> Dict[int, int]:
    """Count the DISTINCT networks (BSSIDs) seen on each Wi-Fi channel.

    The WiGLE file is append-only — one BSSID can own several rows (a fresh row each time it is re-seen
    stronger) — so a raw-row count over-reports. De-dup by BSSID first (a network's channel is stable, so
    the first sighting's channel wins), then tally unique BSSIDs per channel. Tolerant, mirroring
    :func:`~src.core.wardrive.summarize_wigle_csv`: the ``WigleWifi`` pre-header, the column header, and any
    short/garbled row are skipped (only rows whose first field is a real MAC and whose channel parses).
    Returns ``{channel: distinct_network_count}``; channel <= 0 (a missing/unknown reading) is excluded.
    """
    channel_of: Dict[str, int] = {}
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 14 or not _MAC_RE.fullmatch(row[0].strip()):
            continue  # pre-header, the "MAC,..." header, or a non-data row
        try:
            ch = int(row[4])  # WIGLE_HEADER: MAC,SSID,AuthMode,FirstSeen,Channel,...
        except (ValueError, IndexError):
            continue
        if ch <= 0:
            continue  # 0 / negative = no real channel reading
        channel_of.setdefault(row[0].strip().upper(), ch)
    counts: Dict[int, int] = {}
    for ch in channel_of.values():
        counts[ch] = counts.get(ch, 0) + 1
    return counts


def _ranked(yields: Dict[int, int]) -> List[Tuple[int, int]]:
    """Channels as ``(channel, count)`` sorted by count desc, ties broken by ascending channel number
    (so the result is deterministic — never dependent on dict/order or a {1,6,11} assumption)."""
    return sorted(yields.items(), key=lambda kv: (-kv[1], kv[0]))


def cumulative_coverage(yields: Dict[int, int]) -> List[Tuple[int, int, float]]:
    """The coverage curve: for channels ranked busiest-first, each ``(channel, cumulative_networks,
    cumulative_percent)`` — i.e. "the top-N busiest channels together cover X% of all networks seen". This
    is the diminishing-returns curve for deciding how many channels/radios are worth running. Percent is of
    the total distinct networks; an empty input returns ``[]``.
    """
    total = sum(yields.values())
    out: List[Tuple[int, int, float]] = []
    running = 0
    for ch, n in _ranked(yields):
        running += n
        out.append((ch, running, (running / total * 100.0) if total else 0.0))
    return out


def assign_nodes(yields: Dict[int, int], n: int) -> List[int]:
    """Pick the ``n`` highest-yield channels to assign ``n`` simultaneous radios/nodes to, so a fixed
    number of listeners covers the most networks. Returns channels busiest-first (ties by ascending
    channel). ``n <= 0`` -> ``[]``; ``n`` >= the number of observed channels -> every channel. The set
    comes from the data, never a {1, 6, 11} assumption.
    """
    if n <= 0:
        return []
    return [ch for ch, _ in _ranked(yields)[:n]]


def format_plan(csv_text: str, nodes: int = 3) -> str:
    """Render a read-only, ASCII-only wardrive channel plan from a WiGLE CSV: the per-channel yield, the
    coverage curve, and the top-``nodes`` channel assignment. Pure (no I/O) so it is unit-testable.
    """
    yields = channel_yield(csv_text)
    total = sum(yields.values())
    lines = [f"wardrive channel plan - {total} distinct network(s) across {len(yields)} channel(s)"]
    if not yields:
        lines.append("  (no channelled networks found - is this a WiGLE CSV?)")
        return "\n".join(lines)
    lines.append("  yield (distinct networks per channel, busiest first):")
    for ch, running, pct in cumulative_coverage(yields):
        lines.append(f"    ch{ch:<3} {yields[ch]:>4} net  ->  cumulative {running:>4} ({pct:5.1f}%)")
    picks = assign_nodes(yields, nodes)
    if picks:
        covered = sum(yields[ch] for ch in picks)
        pct = covered / total * 100.0 if total else 0.0
        lines.append(f"  assign {nodes} node(s) -> channels {picks}  (covers {covered}/{total} = {pct:.1f}%)")
    return "\n".join(lines)


def wardrive_plan_cli(csv_path: str, nodes: int = 3) -> int:
    """CLI for ``--wardrive-plan``: print the channel-coverage plan for a WiGLE CSV, then exit (0 on
    success, 1 if the file is missing). Read-only, ASCII-only output for console safety.
    """
    if not os.path.isfile(csv_path):
        print(f"[wardrive] no such file: {csv_path}")
        return 1
    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    print(f"[wardrive] {csv_path}")
    print(format_plan(text, nodes))
    return 0
