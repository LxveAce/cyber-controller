"""Regression guard for the GhostESP command-verb fixes.

GhostESP-Revival firmware uses a specific set of CLI verbs that differ from the
placeholder names the controller originally shipped. This test locks in the
source-verified verbs across all three command surfaces — the flat command
palette (``get_commands``), the per-target action map (``TARGET_ACTIONS``) and
the unified-broadcast capability map (``BROADCAST_CAPABILITIES``) — and asserts
the old/wrong verbs are gone.
"""

from __future__ import annotations

from src.core.broadcast import BroadcastVerb
from src.models.target import TargetType
from src.protocols.ghost_esp import (
    BROADCAST_CAPABILITIES,
    TARGET_ACTIONS,
    GhostESPProtocol,
)


# ── helpers ──────────────────────────────────────────────────────────

def _command_names() -> set[str]:
    return {c.name for c in GhostESPProtocol().get_commands()}


def _action_templates() -> set[str]:
    out: set[str] = set()
    for actions in TARGET_ACTIONS.values():
        for a in actions:
            out.add(a.command_template)
    return out


def _broadcast_commands() -> set[str]:
    return {cmd for _pre, cmd in BROADCAST_CAPABILITIES.values()}


def _broadcast_precommands() -> set[str]:
    out: set[str] = set()
    for pre, _cmd in BROADCAST_CAPABILITIES.values():
        out.update(pre)
    return out


# ── new (correct) verbs are present ──────────────────────────────────

def test_renamed_command_verbs_present() -> None:
    names = _command_names()
    for verb in (
        "attack -d",        # was "deauth"
        "beaconspam -r",    # was "beacon"
        "beaconspam -rr",   # was "rickroll"
        "startportal",      # was "portal start"
        "stopportal",       # was "portal stop"
        "capture -eapol",   # was "capture start"
        "capture -stop",    # was "capture stop"
        "startwd",          # was "wardrive start"
        "startwd -s",       # was "wardrive stop"
        "list -a",          # was "list ap"
        "list -s",          # was "list sta"
        "chipinfo",         # was "info" / "version"
        "blescan -s",       # was "blestop"
        "stop",             # was "stopattack"
    ):
        assert verb in names, f"expected renamed verb {verb!r} in get_commands()"


def test_already_correct_verbs_left_intact() -> None:
    names = _command_names()
    for verb in ("scanap", "scansta", "stopscan", "blescan", "settings", "help", "reboot"):
        assert verb in names, f"correct verb {verb!r} must remain"


# ── old (wrong) verbs are gone everywhere ────────────────────────────

_OLD_VERBS = (
    "deauth",
    "beacon",
    "rickroll",
    "portal start",
    "portal stop",
    "capture start",
    "capture stop",
    "wardrive start",
    "wardrive stop",
    "list ap",
    "list sta",
    "info",
    "version",
    "blestop",
    "stopattack",
)


def test_old_command_verbs_gone() -> None:
    names = _command_names()
    for verb in _OLD_VERBS:
        assert verb not in names, f"old verb {verb!r} still present in get_commands()"


def test_old_verbs_gone_from_target_actions() -> None:
    templates = _action_templates()
    for verb in _OLD_VERBS:
        assert verb not in templates, f"old verb {verb!r} still in TARGET_ACTIONS"


def test_old_verbs_gone_from_broadcast() -> None:
    cmds = _broadcast_commands()
    for verb in _OLD_VERBS:
        assert verb not in cmds, f"old verb {verb!r} still in BROADCAST_CAPABILITIES"


# ── dropped phantom entries ──────────────────────────────────────────

def test_blespam_uses_real_documented_form() -> None:
    # The command-surface audit (2026-07-15, cross-checked vs docs.ghostesp.net) confirms
    # `blespam [mode|-s]` is a REAL GhostESP verb, so it now ships in the palette. What was dropped
    # was the OLD phantom `blespam all` form / its TargetAction / the BLE_SPAM broadcast verb — those
    # stay gone (CC exposes blespam via the command palette only, not as a per-target/broadcast action).
    names = _command_names()
    assert "blespam" in names, "real 'blespam' verb missing from palette"
    assert "blespam -s" in names, "real 'blespam -s' stop verb missing from palette"
    assert "blespam all" not in names, "old phantom 'blespam all' form must stay gone"
    assert "blespam all" not in _action_templates()
    assert BroadcastVerb.BLE_SPAM not in BROADCAST_CAPABILITIES


def test_phantom_portal_and_capture_extras_dropped() -> None:
    names = _command_names()
    assert "portal sethtml <path>" not in names
    assert "portal creds" not in names
    assert "capture save" not in names


# ── renamed verbs propagate into the action / broadcast surfaces ─────

def test_target_actions_use_new_verbs() -> None:
    templates = _action_templates()
    assert "attack -d" in templates       # Deauth AP / Deauth Client
    assert "beaconspam -r" in templates   # Beacon Spam
    assert "startportal" in templates     # Evil Portal
    assert "capture -eapol" in templates  # Capture Traffic


def test_deauth_actions_require_prior_select() -> None:
    # GhostESP deauth needs a prior `select -a`, like Marauder.
    deauths = [
        a
        for actions in TARGET_ACTIONS.values()
        for a in actions
        if a.command_template == "attack -d"
    ]
    assert deauths, "no deauth (attack -d) action found"
    for a in deauths:
        assert a.requires_selection is True
        assert a.pre_commands == ["select -a {index}"]


def test_broadcast_deauth_uses_new_verb_and_select() -> None:
    pre, cmd = BROADCAST_CAPABILITIES[BroadcastVerb.DEAUTH_ALL]
    assert cmd == "attack -d"
    assert tuple(pre) == ("select -a all",)


def test_broadcast_renamed_verbs() -> None:
    assert BROADCAST_CAPABILITIES[BroadcastVerb.BEACON_SPAM][1] == "beaconspam -r"
    assert BROADCAST_CAPABILITIES[BroadcastVerb.CAPTURE_HANDSHAKES][1] == "capture -eapol"
    # untouched verbs stay put
    assert BROADCAST_CAPABILITIES[BroadcastVerb.FIND_APS][1] == "scanap"
    # STOP ALL must be the universal `stop` (not scan-only `stopscan`), so it halts deauth/beacon too.
    assert BROADCAST_CAPABILITIES[BroadcastVerb.STOP_ALL][1] == "stop"
