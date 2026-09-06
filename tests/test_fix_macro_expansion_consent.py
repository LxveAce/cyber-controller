"""Regression guards: the variable-expansion consent gap in macro playback.

Before this fix the play-time arm gate (and the Qt/Tk callers) classified the RAW macro template,
so a benign-looking ``{{ACTION}}`` step passed the gate and then expanded to ``attack -d -t all``
at send time — an offensive command transmitted with NO arm confirmation. The engine now resolves
variables into a detached snapshot BEFORE the gate and classifies/sends the EXACT expanded strings;
``resolve_macro`` is the same helper the Qt/Tk play paths now use to classify and confirm.

Scope: pure engine logic plus the ``resolve_macro`` / ``is_offensive_macro`` helpers. No serial,
GPS, radio, or host-shell commands run; no Qt/Tk event loop or hardware. Send is a fake list
``append`` callback, so "transmit" means "appended to a Python list", never a real device write.
"""
from __future__ import annotations

from src.core.macro_recorder import (
    Macro,
    MacroRecorder,
    MacroStep,
    is_offensive_macro,
    resolve_macro,
)


def _template(command: str = "{{ACTION}}", name: str = "Variable command") -> Macro:
    return Macro(name=name, steps=[MacroStep(command)])


# 1. Expanded-unsafe command: zero sends unless explicitly armed ────────────────────────────────────

def test_expanded_offensive_refused_without_arm(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    rec.play(
        _template(), send_command=sent.append,
        variables={"ACTION": "attack -d -t all"}, armed=False, async_=False,
        complete_callback=lambda ok, msg: done.append((ok, msg)),
    )
    assert sent == [], "a template that expands to an attack must NOT transmit while un-armed"
    assert done and done[0][0] is False
    assert "not armed" in done[0][1].lower()


def test_expanded_offensive_plays_only_when_armed(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    rec.play(
        _template(), send_command=sent.append,
        variables={"ACTION": "attack -d -t all"}, armed=True, async_=False,
    )
    assert sent == ["attack -d -t all"], "explicitly armed => the expanded attack is sent verbatim"


# 2. Benign expansion still succeeds (recon macros stay usable) ─────────────────────────────────────

def test_benign_expansion_plays_unarmed(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    rec.play(
        _template(), send_command=sent.append,
        variables={"ACTION": "scan -t ap"}, armed=False, async_=False,
        complete_callback=lambda ok, msg: done.append((ok, msg)),
    )
    assert sent == ["scan -t ap"], "a benign expansion plays with no arm needed"
    assert done and done[0][0] is True


# 3. The confirm/classify decision targets the EXPANDED command (Qt/Tk caller logic) ────────────────

def test_confirmation_decision_uses_expanded_command():
    macro = _template()
    # The raw template alone looks benign — that was the whole trap.
    assert is_offensive_macro(macro) is False
    resolved = resolve_macro(macro, {"ACTION": "attack -d -t all"})
    # The UIs now classify/confirm the resolved snapshot, so the prompt fires on the real command.
    assert is_offensive_macro(resolved) is True
    assert resolved.steps[0].command == "attack -d -t all"


# 4. Resolving does not mutate the caller's macro or variables dict ─────────────────────────────────

def test_resolve_does_not_mutate_caller_inputs():
    macro = _template()
    variables = {"ACTION": "attack -d -t all"}
    resolved = resolve_macro(macro, variables)
    assert macro.steps[0].command == "{{ACTION}}", "the caller's template macro must be untouched"
    assert variables == {"ACTION": "attack -d -t all"}, "the caller's variables dict must be untouched"
    assert resolved is not macro and resolved.steps[0] is not macro.steps[0]
    # Metadata that classification depends on is preserved on the snapshot.
    assert resolved.name == macro.name
    assert resolved.device_protocol == macro.device_protocol


# 5. A post-gate variable mutation cannot change the bytes actually sent ────────────────────────────

def test_post_gate_variable_mutation_cannot_change_sent_bytes(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    variables = {"ACTION": "scan -t ap"}
    sent: list[str] = []

    def _send(cmd: str) -> None:
        # A racing caller flips the variable to an attack AFTER the snapshot was frozen at play().
        variables["ACTION"] = "attack -d -t all"
        sent.append(cmd)

    macro = Macro(name="two step", steps=[MacroStep("{{ACTION}}"), MacroStep("{{ACTION}}")])
    rec.play(macro, send_command=_send, variables=variables, armed=False, async_=False)
    assert sent == ["scan -t ap", "scan -t ap"], (
        "both steps transmit the frozen snapshot; a mid-play dict mutation cannot inject an attack"
    )


# 6. An existing LITERAL unsafe macro (no variables) is still blocked ───────────────────────────────

def test_literal_offensive_macro_still_blocked_unarmed(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    macro = Macro(name="Deauth", steps=[MacroStep("attack -d -t all")])
    rec.play(
        macro, send_command=sent.append, armed=False, async_=False,
        complete_callback=lambda ok, msg: done.append((ok, msg)),
    )
    assert sent == []
    assert done and done[0][0] is False
    assert "not armed" in done[0][1].lower()


# 7. Malformed (non-string) substitution values are coerced, not fatal to the thread ────────────────

def test_malformed_variable_value_does_not_kill_playback(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    # A caller slipping a non-string value in must not raise TypeError inside re.sub (which, on the
    # daemon playback path, would be a silent thread death). It is coerced with str().
    rec.play(
        _template(), send_command=sent.append,
        variables={"ACTION": 1234}, armed=False, async_=False,  # type: ignore[dict-item]
        complete_callback=lambda ok, msg: done.append((ok, msg)),
    )
    assert sent == ["1234"], "a non-string value is coerced to str and sent, not dropped"
    assert done and done[0][0] is True


def test_unknown_placeholder_is_left_verbatim_and_not_offensive(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    # No value supplied for ACTION: the placeholder stays literal (not silently blanked), and a bare
    # ``{{ACTION}}`` is not an attack keyword, so it plays as the benign string it is.
    rec.play(_template(), send_command=sent.append, variables={}, armed=False, async_=False)
    assert sent == ["{{ACTION}}"]


# 8. Web-style call shape (no variables passed) stays compatible ────────────────────────────────────

def test_web_style_no_variables_offensive_refused_then_armed_plays(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    macro = Macro(name="Deauth", steps=[MacroStep("attack -d -t all")])
    # Web computes `offensive` from the macro (no substitutions) and arms only on consent.
    offensive = is_offensive_macro(resolve_macro(macro, None))
    assert offensive is True

    refused: list[str] = []
    rec.play(macro, send_command=refused.append, armed=(offensive and False), async_=False)
    assert refused == [], "no consent => no send (web parity)"

    played: list[str] = []
    rec.play(macro, send_command=played.append, armed=(offensive and True), async_=False)
    assert played == ["attack -d -t all"], "consent => armed => the macro plays"
