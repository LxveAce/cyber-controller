"""Global operating-posture gate — a master switch OVER the per-command safety floor.

The app shell carries a Recon/Defense <-> Offense posture toggle. This module is the security state
behind it: a process-global posture the command send paths consult IN ADDITION TO
:mod:`src.core.safety`. It never weakens safety.py — it only ADDS a gate:

    RECON (the default)  -> every offensive/TX verb is HARD-BLOCKED, even if the device is armed
    OFFENSE (host-gated) -> offensive verbs proceed to their NORMAL per-verb gate (classify +
                            tx_hard_block arm gate + the confirm dialog)

So the offensive path now needs TWO independent unlocks: a deliberate, host-authorized switch to
Offense (the shell logs + authorizes it) AND the existing per-command arm/confirm. A SAFE verb is
never affected. The default is the safe posture, and a construction with no shell wired (e.g. a
standalone tab in a test) stays RECON — fail-safe.

The shell owns the VISIBLE posture (``PageLayout``); the ``PageLayoutBinder`` mirrors every change
into here via ``set_posture`` so this global can't lie about what the toggle shows. These string
values are canonical; ``page_layout`` imports them from here so the two can't drift.
"""
from __future__ import annotations

POSTURE_RECON = "recon"      # default: passive recon / defence — offensive verbs blocked
POSTURE_OFFENSE = "offense"  # host-authorized: offensive ops allowed (still per-verb arm + confirm)

_VALID = (POSTURE_RECON, POSTURE_OFFENSE)
_POSTURE = POSTURE_RECON     # process global; the shell's binder mirrors the visible toggle here


def set_posture(posture: str) -> None:
    """Set the global posture. Any value but the two valid ones is ignored (stays fail-safe)."""
    global _POSTURE
    if posture in _VALID:
        _POSTURE = posture


def get_posture() -> str:
    """The current global posture (recon/offense)."""
    return _POSTURE


def offensive_blocked(danger: str, posture: str | None = None) -> bool:
    """Whether an offensive verb must be blocked by the posture gate.

    True when the verb is offensive (``danger`` non-empty, i.e. lab-only / illegal-tx per
    :func:`safety.classify`) AND the posture is not OFFENSE. A SAFE verb (empty ``danger``) is never
    blocked. ``posture`` defaults to the current global. Purely ADDITIVE over safety.py: it can only
    ever REFUSE a command, never permit one safety.py would refuse."""
    if not danger:
        return False
    return (posture if posture is not None else _POSTURE) != POSTURE_OFFENSE


def block_reason() -> str:
    """The honest, actionable message shown when the posture gate refuses an offensive verb."""
    return ("Blocked by Recon posture — switch the header posture toggle to Offense to enable "
            "offensive / TX operations. (They then still require the per-command arm + confirm.)")
