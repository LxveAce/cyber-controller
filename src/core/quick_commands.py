"""Quick-command catalog for touch-first surfaces (MB — the mobile Remote home).

Sourced ENTIRELY from the real per-firmware protocol command registries (`protocol.get_commands()`), so a
button can never fire a phantom command the firmware doesn't have (the Bruce lesson). Only **one-tap,
argument-free** commands are surfaced — anything needing an index/channel/SSID belongs in the terminal, not a
tap grid. Each command is tagged with its :mod:`src.core.safety` danger level so the UI can *label* (never
block) the dangerous ones, keeping the "Yes, proceed" escape hatch the owner requires.

Pure Python (no Qt, no Flask, no serial) — fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from src.core import safety
from src.protocols import get_protocol


@dataclass(frozen=True)
class QuickCommand:
    command: str        # the exact string sent to the device (a real protocol command)
    label: str          # human label (the command's description, or the command itself)
    category: str       # grouping category from the protocol
    danger: str         # safety level: "" (safe) | "lab-only" | "illegal-tx"


def _is_one_tap(ci) -> bool:
    """True for an argument-free command: no ``args`` description and no inline ``<placeholder>``."""
    if (getattr(ci, "args", "") or "").strip():
        return False
    return "<" not in getattr(ci, "name", "")


def quick_commands_for(firmware: str) -> List[QuickCommand]:
    """The one-tap quick commands for *firmware*, sourced from its protocol. Empty list if the firmware is
    unknown or exposes no command registry — never raises."""
    try:
        proto = get_protocol(firmware)
    except Exception:  # noqa: BLE001 — unknown firmware must degrade to an empty catalog, not crash the page
        return []
    get_commands = getattr(proto, "get_commands", None)
    if not callable(get_commands):
        return []
    try:
        infos = get_commands()
    except Exception:  # noqa: BLE001
        return []
    out: List[QuickCommand] = []
    for ci in infos or []:
        name = getattr(ci, "name", "")
        if not name or not _is_one_tap(ci):
            continue
        out.append(QuickCommand(
            command=name,
            label=(getattr(ci, "description", "") or name),
            category=(getattr(ci, "category", "") or "General"),
            # safety.classify now folds in the CommandInfo's description + category (see src/core/safety.py),
            # so the offensive-but-benignly-named commands are labelled without a local workaround here.
            danger=safety.classify(name, ci),
        ))
    return out


def grouped_quick_commands(firmware: str) -> List[Tuple[str, List[QuickCommand]]]:
    """`quick_commands_for` grouped by category, preserving first-seen category order."""
    order: List[str] = []
    by_cat: dict[str, List[QuickCommand]] = {}
    for qc in quick_commands_for(firmware):
        if qc.category not in by_cat:
            by_cat[qc.category] = []
            order.append(qc.category)
        by_cat[qc.category].append(qc)
    return [(cat, by_cat[cat]) for cat in order]


# ── canonical grouping (A16): fold the many firmware-native categories into the console's three
# mockup buckets — Scanning / Attack / Network — plus Other for anything that fits none. This is a
# COSMETIC re-labelling for the OPERATE grid only; it never changes gating. safety.py's danger
# classification stays the floor, and every dangerous command is forced into Attack so a
# benignly-named offensive verb can't hide under Scanning/Network.
CANONICAL_GROUPS: Tuple[str, ...] = ("Scanning", "Attack", "Network", "Other")

# Keyword sets matched against the command + its native category (lowercased, whole-substring).
# Order of evaluation in canonical_group() is what resolves overlaps — danger and Attack win first.
_ATTACK_WORDS = (
    "deauth", "disassoc", "jam", "beacon", "spam", "flood", "evil", "portal", "attack", "karma",
    "pwn", "rickroll", "badusb", "ducky", "inject", "spoof", "clone", "replay", "bruteforce",
)
_SCAN_WORDS = (
    "scan", "sniff", "list", "find", "probe", "wardrive", "detect", "monitor", "recon", "discover",
    "survey", "search", "capture", "target", "station", "enum", "watch", "analyz",
)
_NETWORK_WORDS = (
    "connect", "join", "ap ", "apstart", "startap", "sta", "dhcp", "dns", "web", "server", "http",
    "telnet", "ping", "ip", "channel", "tcp", "udp", "mac", "hostname", "gateway", "route",
)


def canonical_group(command: str, category: str = "", danger: str = "") -> str:
    """Map a command to one of :data:`CANONICAL_GROUPS`. Dangerous commands (danger truthy) are
    ALWAYS Attack. Otherwise keyword-match command+category: Attack > Scanning > Network > Other."""
    hay = f"{command} {category}".lower()
    if danger:
        return "Attack"
    if any(w in hay for w in _ATTACK_WORDS):
        return "Attack"
    if any(w in hay for w in _SCAN_WORDS):
        return "Scanning"
    if any(w in hay for w in _NETWORK_WORDS):
        return "Network"
    return "Other"


def canonical_grouped_quick_commands(firmware: str) -> List[Tuple[str, List[QuickCommand]]]:
    """`quick_commands_for` folded into the console's canonical buckets, in CANONICAL_GROUPS order.
    Empty buckets are dropped so the grid only shows groups that actually have commands."""
    by_group: dict[str, List[QuickCommand]] = {g: [] for g in CANONICAL_GROUPS}
    for qc in quick_commands_for(firmware):
        by_group[canonical_group(qc.command, qc.category, qc.danger)].append(qc)
    return [(g, by_group[g]) for g in CANONICAL_GROUPS if by_group[g]]
