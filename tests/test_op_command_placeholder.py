"""op_command <...> placeholder resolution (op_spec.py).

A catalog verb whose NAME carries placeholders (`ir tx_from_file <path>`, `add -c -b <mac> -ap`)
must resolve to the byte-identical line the Devices terminal sends, so an op fires the same from the
Operate console and the Devices terminal.

Regression for: Operate's OpPanel used to APPEND the arg (`op_command` = `f"{name} {arg}"`), so a
templated verb went out malformed — the literal `<path>` on the wire, or the args shoved past a
mid-string placeholder (`add -c -b -ap AA:BB 3`). op_command now substitutes in place.
"""
from __future__ import annotations

from src.core.op_spec import op_command
from src.core.placeholders import sanitize_arg, substitute_tokens


class _CI:
    """Minimal CommandInfo stand-in — op_command reads only the name (tokens live in the name)."""
    def __init__(self, name, args=""):
        self.name = name
        self.args = args


# ── no placeholders: the original append model is preserved verbatim ──
def test_plain_verb_unchanged():
    assert op_command(_CI("deauth")) == "deauth"                        # bare verb
    assert op_command(_CI("deauth"), "-t AA:BB") == "deauth -t AA:BB"   # verb + arg
    assert op_command(_CI("deauth"), "deauth -t AA:BB") == "deauth -t AA:BB"  # verbatim
    assert op_command(_CI("scan"), "  ") == "scan"                      # blank arg -> bare verb


# ── single placeholder: substituted in place, NOT appended ──
def test_single_placeholder_resolves():
    # the exact bug Atlas flagged: was "ir tx_from_file <path> capture.ir"
    assert op_command(_CI("ir tx_from_file <path>"), "capture.ir") == "ir tx_from_file capture.ir"


def test_single_placeholder_takes_the_whole_arg():
    # one field -> a value with spaces survives (mirrors the Devices single field)
    assert op_command(_CI("loader open <app>"), "My App") == "loader open My App"


# ── multi placeholder mid-string: each value lands in its own slot, in order ──
def test_multi_placeholder_midstring():
    # was "add -c -b -ap AA:BB 3" under the old append model
    assert op_command(_CI("add -c -b <mac> -ap <idx>"), "AA:BB 3") == "add -c -b AA:BB -ap 3"


def test_multi_placeholder_last_absorbs_remainder():
    assert op_command(_CI("subghz scan <start> <stop>"), "433 434") == "subghz scan 433 434"


def test_repeated_token():
    assert op_command(_CI("led -r <v> -g <v> -b <v>"), "10 20 30") == "led -r 10 -g 20 -b 30"


# ── incomplete input -> "" (not sent), mirroring the terminal's blank-field cancel ──
# op_command's sole caller (op_panel -> operate_tab._send) no-ops on an empty string, so returning
# "" suppresses the send exactly as the Devices terminal cancels on a blank field. An incomplete
# templated verb must NEVER go on the wire as a literal "<path>" or a dangling half-command.
def test_no_arg_on_templated_verb_sends_nothing():
    assert op_command(_CI("ir tx_from_file <path>")) == ""
    assert op_command(_CI("add -c -b <mac> -ap <idx>")) == ""


def test_under_filled_arg_sends_nothing_like_terminal_cancel():
    # 2 tokens, 1 value -> incomplete -> "" (was a malformed "gpio set 5 " before the DEBUG fix)
    assert op_command(_CI("gpio set <pin> <0/1>"), "5") == ""
    assert op_command(_CI("join -a <idx> -p <pwd>"), "3") == ""       # no empty-password send
    assert op_command(_CI("loader open <app>"), "  ") == ""           # blank value -> nothing


def test_never_leaves_a_literal_placeholder_on_the_wire():
    for name, arg in [("ir tx_from_file <path>", ""), ("gpio set <pin> <0/1>", "5"),
                      ("add -c -b <mac> -ap <idx>", "AA:BB"), ("loader open <app>", "<>")]:
        out = op_command(_CI(name), arg)
        assert "<" not in out and ">" not in out     # never a literal token, filled or empty


# ── sanitization mirrors the terminal: angle brackets stripped so a value can't smuggle a token ──
def test_value_is_sanitized():
    assert op_command(_CI("loader open <app>"), "<evil>") == "loader open evil"


# ── THE INVARIANT: Operate resolves the byte-identical string the Devices terminal builds ──
def test_cross_surface_equality_with_devices_resolution():
    name = "add -c -b <mac> -ap <idx>"
    # The Devices terminal path: one sanitized value per token field, substituted in place.
    devices = substitute_tokens(name, [sanitize_arg("AA:BB"), sanitize_arg("3")])
    operate = op_command(_CI(name), "AA:BB 3")
    assert operate == devices == "add -c -b AA:BB -ap 3"


def test_device_tab_delegates_to_the_same_primitives():
    # The terminal's helpers ARE the shared primitives (single source -> op_command can't drift).
    import pytest
    pytest.importorskip("PyQt5.QtWidgets")
    from src.core.placeholders import placeholder_tokens
    from src.ui.qt.device_tab import DeviceTab
    assert DeviceTab._substitute_tokens is substitute_tokens
    assert DeviceTab._sanitize_arg is sanitize_arg
    assert DeviceTab._placeholder_tokens is placeholder_tokens
