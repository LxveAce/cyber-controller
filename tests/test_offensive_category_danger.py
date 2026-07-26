"""Every "Offensive"-category command MUST engage the arm/confirm safety gate.

safety.classify does NOT auto-escalate on the Offensive category name: _OFFENSIVE_CATEGORIES
holds "attack"/"portal"/"jam"/... but deliberately NOT "offensive" — adding it would deadlock
arm/disarm, which live in the Offensive category (escalating arm to lab-only would mean arm
itself requires ARMED). So an Offensive verb relies on its OWN explicit danger= (or a danger
keyword / description) to reach classify != SAFE. This guard fails the moment an Offensive
command would slip through SAFE and skip the arm+confirm gate — the trap the operability audit
flagged before the per-firmware Offensive rollout. arm/disarm are the gate itself and excepted.
"""
from __future__ import annotations

from src.core import safety
from src.protocols import PROTOCOLS, get_protocol

# The gate mechanism itself: arm ENABLES offensive TX, disarm CEASES to SAFE. Neither transmits,
# so neither must classify dangerous (escalating arm would deadlock; disarm is a cease action).
_GATE_VERBS = {"arm", "disarm"}


def _is_gate_or_cease(name: str) -> bool:
    """arm/disarm (the gate) or any stop/clear/disable cease action. A cease verb that stops an
    attack (e.g. `stopscan`) legitimately belongs in the Offensive group and is legitimately SAFE —
    safety.py never escalates a cease action, so the guard excepts it too, not just arm/disarm."""
    return name.strip().lower() in _GATE_VERBS or safety._is_cease(name)


def _offensive_commands():
    """Yield (firmware, CommandInfo) for every command in an "Offensive" category."""
    for fw in sorted(PROTOCOLS):
        for ci in get_protocol(fw).get_commands():
            if (ci.category or "").strip().lower() == "offensive":
                yield fw, ci


def test_every_offensive_category_command_engages_the_gate():
    """No Offensive verb (bar arm/disarm) may classify SAFE — it would send with no confirm."""
    slipped = [
        f"{fw}:{ci.name!r} (danger={ci.danger!r})"
        for fw, ci in _offensive_commands()
        if not _is_gate_or_cease(ci.name)
        and safety.classify(ci.name, ci) == safety.SAFE
    ]
    assert not slipped, (
        "Offensive-category command(s) classify SAFE and would SKIP the arm+confirm gate — "
        "give each an explicit danger= (_OFFENSIVE_CATEGORIES omits 'offensive' by design, "
        f"since arm/disarm live there): {slipped}"
    )


def test_offensive_cease_action_is_excepted():
    """A stop/clear cease action in the Offensive group (e.g. marauder's per-group `stopscan`) is
    legitimately SAFE — stopping an attack is never itself dangerous — so the guard excepts it like
    arm/disarm rather than flagging it. This locks the exception the marauder rollout relies on."""
    from src.protocols.base import CommandInfo

    stop = CommandInfo("stopscan", "Offensive", "Stop the current offensive op")
    assert safety.classify(stop.name, stop) == safety.SAFE  # a cease action is SAFE by design
    assert _is_gate_or_cease(stop.name)  # ...and the guard excepts it, so it is never "slipped"


def test_the_guard_actually_sees_the_offensive_category():
    """Vacuous-pass guard: the Offensive category must be non-empty (lxveos ships it today)."""
    found = [ci.name for _fw, ci in _offensive_commands()]
    assert found, "no Offensive-category commands found — enumeration or category name drifted"
