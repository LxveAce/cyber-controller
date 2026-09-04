"""Regression guard for the 2026-09-03 red-team finding: ~65 offensive verbs whose danger lives ONLY in
CommandInfo metadata (a bare token like ``START`` / ``dhcpstarve start`` carries no danger keyword) evaded
the server-side consent gate on /api/broadcast, /api/macros/run and /api/rules because those gates classified
the command STRING ONLY and discarded the authoritative ``CommandInfo.danger``.

The floor here: for EVERY registered protocol, every command that classifies as dangerous WITH its
CommandInfo must be caught by the info-aware gates. This iterates the real registry, so a new dangerous verb
added to any protocol can't silently reopen the gap.
"""
from __future__ import annotations

import pytest

from src.core import safety
from src.core.broadcast import command_info_for
from src.core.macro_recorder import Macro, MacroStep, is_offensive_macro
from src.protocols import PROTOCOLS


def _dangerous_commands():
    """(firmware, command_name) for every registered command that is dangerous WITH its CommandInfo."""
    out = []
    for fw, cls in PROTOCOLS.items():
        if fw in ("generic", "raw"):
            continue
        try:
            cmds = cls().get_commands()
        except Exception:  # noqa: BLE001 — a protocol with no command surface is fine to skip
            continue
        for ci in cmds:
            name = getattr(ci, "name", "")
            if name and safety.classify(name, ci):
                out.append((fw, name))
    return out


_DANGEROUS = _dangerous_commands()


def test_registry_has_metadata_danger_verbs():
    # Sanity: the corpus is non-empty and includes at least one verb whose BARE string is NOT dangerous
    # (i.e. the danger is metadata-only — exactly the class the old string-only gate missed).
    assert _DANGEROUS, "expected dangerous commands in the protocol registry"
    metadata_only = [(fw, n) for fw, n in _DANGEROUS if not safety.classify(n)]
    assert metadata_only, "expected at least one metadata-only-danger verb (e.g. bluestress START)"


@pytest.mark.parametrize("firmware,command", _DANGEROUS)
def test_offensive_macro_is_info_aware(firmware, command):
    # THE gate: an offensive verb, under its own firmware, must be flagged so the play-time arm gate fires.
    macro = Macro(name="probe", device_protocol=firmware, steps=[MacroStep(command=command)])
    assert is_offensive_macro(macro) is True, (
        f"{firmware}:{command!r} is dangerous with its CommandInfo but the macro gate missed it"
    )


def test_command_info_for_resolves_metadata_danger():
    # The shared resolver the web gates use must surface the authoritative danger for a metadata-only verb.
    info = command_info_for("bluestress", "START")
    assert info is not None and safety.classify("START", info) == "illegal-tx"
    assert safety.classify("START") == ""  # proves the bare string alone was NOT enough (the bug)


def test_benign_verb_not_overflagged():
    # Info-awareness must not over-flag: a benign verb under a firmware where it's safe stays un-gated.
    macro = Macro(name="scan", device_protocol="marauder", steps=[MacroStep(command="scanall")])
    assert is_offensive_macro(macro) is False
