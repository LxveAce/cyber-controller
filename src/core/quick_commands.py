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


# Categories whose commands are inherently offensive (active TX / attack / phishing). Used only to ESCALATE
# toward a warning — never to downgrade — so a one-tap send is always labelled.
_OFFENSIVE_CATEGORIES = frozenset({"attack", "attacks", "portal", "evil portal", "jam", "jamming", "spam"})


def _is_one_tap(ci) -> bool:
    """True for an argument-free command: no ``args`` description and no inline ``<placeholder>``."""
    if (getattr(ci, "args", "") or "").strip():
        return False
    return "<" not in getattr(ci, "name", "")


def _danger_of(ci) -> str:
    """Danger level for a quick command, failing TOWARD a warning.

    :func:`safety.classify` scans only the command NAME (plus an explicit ``ci.danger``). That misses a
    command whose offensive nature lives in its DESCRIPTION or CATEGORY — e.g. ``probe`` ("probe request
    flood"), ``iot_recon`` ("credential brute force"), ``sniffpwn`` ("sniff-then-deauth"), ``startportal``
    ("evil portal") — which would otherwise present as a plain safe button and transmit on one tap with no
    confirmation. We broaden the scan here (name + description + offensive category) and take the worst, so
    the "label" half of label-never-block can't fail open on this surface.
    """
    name_level = safety.classify(getattr(ci, "name", ""), ci)
    desc_level = safety.classify(getattr(ci, "description", "") or "")
    cat = (getattr(ci, "category", "") or "").strip().lower()
    cat_level = safety.LAB_ONLY if cat in _OFFENSIVE_CATEGORIES else safety.SAFE
    return safety.worst_of(name_level, desc_level, cat_level)


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
            danger=_danger_of(ci),
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
