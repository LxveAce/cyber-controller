"""Beat 274 - macro arm-gate classifier divergence (cc-deep-audit-12 / hub twin S3, HIGH).

`is_offensive_macro` (which the engine-level `play()` arm gate consults) matched a NARROW set of
`_ATTACK_PREFIXES` with `cmd.startswith(...)`. The authoritative classifier `safety.classify()`
uses a SUBSTRING scan, so the two disagreed: an offensive command whose danger keyword is NOT the
leading token -- `wifi deauth 5`, `run attack`, `flood 2400`, `rf_jam`, `brute` -- was flagged by
`safety.classify()` but MISSED by `is_offensive_macro`, so a macro built from it replayed via the
arm-gated `play()` with NO arm prompt (the gate was bypassed for a genuinely offensive macro).

Fix: `is_offensive_macro` now flags a step offensive when `safety.classify(cmd)` is non-SAFE OR it
matches `_ATTACK_PREFIXES`. The union keeps ONE source of truth with the terminal danger classifier
while retaining the curated prefix list as an additive floor for verbs whose danger lives only in
CommandInfo metadata (`probe`, `startportal`, `subghz tx`, `nfc/rfid emulate`) a bare macro step
string can't hand to `classify()`. It stays a WARN/arm gate (never a hard block).

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_nonleading_offensive_verbs_now_gated (parametrized): non-leading offensive verbs -> True.
  - test_offensive_step_refuses_play_without_arm: a `flood 2400` macro won't transmit un-armed.
  - test_is_offensive_agrees_with_safety_classify: gate flags everything safety.classify does.
Guards (pass on both HEAD and the fix):
  - test_benign_macro_not_offensive: a recon macro is not offensive (no over-gating of safe steps).
  - test_metadata_only_verbs_still_gated: probe/startportal/nfc emulate stay gated (no regression).
"""
from __future__ import annotations

import pytest

from src.core import safety
from src.core.macro_recorder import Macro, MacroRecorder, MacroStep, is_offensive_macro


def _macro(*cmds: str, name: str = "M", protocol: str = "marauder") -> Macro:
    return Macro(name=name, device_protocol=protocol,
                 steps=[MacroStep(command=c) for c in cmds])


@pytest.mark.parametrize("cmd", ["flood 2400", "run attack", "wifi deauth 5", "rf_jam", "brute"])
def test_nonleading_offensive_verbs_now_gated(cmd):
    """A command safety.classify() flags offensive must make the macro offensive (arm-gated)."""
    assert safety.classify(cmd), f"precondition: safety must flag {cmd!r} offensive"
    assert is_offensive_macro(_macro(cmd)) is True, f"{cmd!r} must be arm-gated"


def test_offensive_step_refuses_play_without_arm(tmp_path):
    """play() must refuse an un-armed macro whose step is a non-leading attack."""
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    rec.play(_macro("flood 2400"), send_command=sent.append,
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert sent == [], "an un-armed offensive macro must NOT transmit"
    assert done and done[0][0] is False and "not armed" in done[0][1].lower()


def test_offensive_step_plays_when_armed(tmp_path):
    """Retention rule: armed, the same offensive macro DOES play (never a hard block)."""
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    rec.play(_macro("flood 2400"), send_command=sent.append, armed=True, async_=False)
    assert sent == ["flood 2400"], "an armed offensive macro plays after the arm prompt"


@pytest.mark.parametrize("cmd", [
    "flood 2400", "run attack", "wifi deauth 5", "rf_jam", "brute", "deauth", "jam", "beacon spam",
])
def test_is_offensive_agrees_with_safety_classify(cmd):
    """Broadened invariant: anything safety.classify() flags, the macro arm gate also flags."""
    if safety.classify(cmd):
        assert is_offensive_macro(_macro(cmd)) is True, f"gate must not disagree-low on {cmd!r}"


def test_benign_macro_not_offensive():
    """Guard: a recon macro with no danger keyword is not arm-gated (no over-gating)."""
    assert is_offensive_macro(_macro("scanap", "scan -t ap")) is False


@pytest.mark.parametrize("cmd", ["probe", "startportal", "evilportal", "nfc emulate"])
def test_metadata_only_verbs_still_gated(cmd):
    """Guard: prefix-list-floor verbs (metadata-only danger) stay gated -- no regression."""
    assert is_offensive_macro(_macro(cmd)) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
