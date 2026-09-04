"""Global operating-posture — a VISIBLE display/convenience state (it gates nothing).

The app shell carries a Recon/Defense <-> Offense posture toggle. This module is the process-global
state behind it, mirrored from the shell so any surface can read the current posture for display.

**It does NOT gate command usage** (owner decision): CC is universally usable out of the
box — offensive verbs are reachable by default. The safety model is (1) a one-time first-run
authorized-use consent and (2) the per-command pre-execution confirm (``safety.should_confirm``),
plus the arm gate where a firmware implements it (``safety.tx_hard_block``). safety.py stays the
untouched floor. An earlier build made Recon hard-block offensive verbs until a switch to Offense;
that forced-switch was removed per the owner — the toggle is a visible indicator, never a blocker.

These string values are canonical; ``page_layout`` imports them from here so they can't drift, and
``PageLayoutBinder`` mirrors the visible toggle into here via ``set_posture``.
"""
from __future__ import annotations

POSTURE_RECON = "recon"      # passive recon / defence (the default)
POSTURE_OFFENSE = "offense"  # active/offensive posture (a visible indicator only — gates nothing)

_VALID = (POSTURE_RECON, POSTURE_OFFENSE)
_POSTURE = POSTURE_RECON     # process global; the shell's binder mirrors the visible toggle here


def set_posture(posture: str) -> None:
    """Set the global posture indicator. Any value but the two valid ones is ignored."""
    global _POSTURE
    if posture in _VALID:
        _POSTURE = posture


def get_posture() -> str:
    """The current global posture indicator (recon/offense) — for display, not gating."""
    return _POSTURE
