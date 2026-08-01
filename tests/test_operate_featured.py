"""Operate-Home curation (`operate_featured.featured_actions`) — pure, no Qt.

Grounds against real catalogs + a lightweight fixture that returns the RAW command list (not a mock
handing back a pre-featured list). featured_actions does the scoring; it picks which verbs show.
It never changes HOW they send; every tap rides the same guarded path. Dangerous verbs are INCLUDED
(owner call 2026-08-01) — selection is breadth-first across intents so an attack verb earns a slot.
"""
from __future__ import annotations

from src.protocols.base import CommandInfo
from src.ui.qt.operate_featured import featured_actions


class _Proto:
    """Lightweight protocol stub: hands back a raw command list; featured_actions scores it."""
    def __init__(self, commands):
        self._cmds = commands

    def cached_commands(self):
        return self._cmds


def test_empty_catalog_yields_nothing():
    assert featured_actions(_Proto([])) == []     # no CLI / stock-DIV / no catalog -> honest empty


def test_no_cached_commands_never_raises():
    class _Bad:
        def cached_commands(self):
            raise RuntimeError("no catalog")
    assert featured_actions(_Bad()) == []              # tolerant


def test_caps_at_max_n_and_dedupes():
    cmds = [CommandInfo(f"scan{i}", "WiFi") for i in range(10)]
    out = featured_actions(_Proto(cmds), max_n=5)
    assert len(out) == 5 and len({c.name for c in out}) == 5


def test_breadth_first_one_per_intent():
    # one verb per intent bucket (status/scan/capture/attack/control) surfaces before a 2nd of any
    cmds = [
        CommandInfo("status", "Device"),
        CommandInfo("scanall", "WiFi"),
        CommandInfo("capture -eapol", "WiFi"),
        CommandInfo("attack -t deauth", "Offensive", danger="lab-only"),
        CommandInfo("channel", "WiFi"),
        CommandInfo("scansta", "WiFi"),           # a 2nd scan verb - must NOT crowd out the rest
    ]
    names = [c.name for c in featured_actions(_Proto(cmds), max_n=5)]
    assert set(names) == {"status", "scanall", "capture -eapol", "attack -t deauth", "channel"}


def test_dangerous_verbs_are_included_owner_optin():
    # Ace opted in: an offensive verb IS featured (danger-labeled + gated in the UI, not here)
    cmds = [CommandInfo("scanall", "WiFi"), CommandInfo("info", "Device"),
            CommandInfo("attack -t deauth", "Offensive", danger="lab-only")]
    names = [c.name for c in featured_actions(_Proto(cmds), max_n=3)]
    assert "attack -t deauth" in names            # earns a slot, not crowded out by recon verbs


def test_featured_flag_takes_precedence():
    cmds = [CommandInfo("scanall", "WiFi"), CommandInfo("my primary", "X", featured=True),
            CommandInfo("status", "Device")]
    assert [c.name for c in featured_actions(_Proto(cmds))] == ["my primary"]


def test_only_intent_verbs_no_noise():
    cmds = [CommandInfo("frobnicate", "Misc"), CommandInfo("wibble", "Misc")]
    assert featured_actions(_Proto(cmds)) == []        # no intent bucket hit -> nothing featured


def test_real_marauder_catalog_surfaces_scan_and_attack():
    # ground against the real catalog: Marauder's home should offer recon AND its signature attack
    from src.protocols import get_protocol
    out = featured_actions(get_protocol("marauder"))
    assert 0 < len(out) <= 5
    joined = " ".join(c.name for c in out).lower()
    assert "scan" in joined and "attack" in joined     # balanced recon + offensive
