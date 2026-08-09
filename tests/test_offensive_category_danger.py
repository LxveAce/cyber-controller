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


def _is_cease_subcommand(name: str) -> bool:
    """A ``<verb> stop|disable|clear`` cease form (e.g. esp32-div-serial's ``evilportal stop`` /
    ``capture stop``). ``safety._is_cease`` only matches the LEADING-prefix form (``stopportal``,
    ``stopattack``), so the subcommand-style CLIs (verb then a cease sub-token) slip its check even
    though they are genuine cease actions — and ``safety.classify`` already returns SAFE for them.
    We widen ONLY the test's exemption, NOT ``safety._is_cease`` itself: broadening the real helper
    would also flip ``ghost-esp:'karma stop'`` from lab-only → SAFE (un-gating a currently-gated
    command = weakening the floor) — a human safety call, surfaced to Atlas, not auto-applied."""
    toks = (name or "").strip().lower().split()
    return len(toks) >= 2 and toks[-1] in safety._CEASE_PREFIXES


def _is_gate_or_cease(name: str) -> bool:
    """arm/disarm (the gate) or any stop/clear/disable cease action. A cease verb that stops an
    attack (e.g. `stopscan`, `evilportal stop`) legitimately belongs in the Offensive group and is
    legitimately SAFE — safety.py never escalates a cease action, so the guard excepts it too."""
    return (name.strip().lower() in _GATE_VERBS or safety._is_cease(name)
            or _is_cease_subcommand(name))


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


def test_subcommand_cease_form_is_excepted():
    """The `<verb> stop` cease form (esp32-div-serial's `evilportal stop`) must be exempted like the
    leading-prefix ceases — it stops an attack (no TX) and classify already returns SAFE. Locks the
    CI-caught gap where the subcommand form slipped safety._is_cease's prefix-only check."""
    assert _is_gate_or_cease("evilportal stop")      # the exact CI-flagged command
    assert _is_gate_or_cease("capture stop")
    assert _is_gate_or_cease("settings -s key disable")
    assert not _is_gate_or_cease("startportal")      # an ACTIVE offensive verb is NOT a cease
    assert not _is_gate_or_cease("evilportal start")


def test_the_guard_actually_sees_the_offensive_category():
    """Vacuous-pass guard: the Offensive category must be non-empty (lxveos ships it today)."""
    found = [ci.name for _fw, ci in _offensive_commands()]
    assert found, "no Offensive-category commands found — enumeration or category name drifted"
