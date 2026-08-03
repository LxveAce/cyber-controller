"""Offensive-completeness batch 1 (flipper + marauder) — every added offensive verb stays GATED.

Critical mechanic (safety.classify): it returns the CommandInfo's ``danger=`` verbatim and never widens
it, and category ``"Offensive"`` is NOT in ``_OFFENSIVE_CATEGORIES`` — so an offensive verb is protected
ONLY by its explicit ``danger=`` (or a name keyword like "attack"/"spam"). This test asserts every verb
added in this batch classifies ``lab-only`` (hits the confirm/arm gate), never SAFE, and that the
templated names emit the correct wire string through op_command.
"""
from __future__ import annotations

from src.core import safety
from src.core.op_spec import op_command
from src.protocols import get_protocol


def _by_name(fw):
    return {c.name: c for c in get_protocol(fw).get_commands()}


def _assert_lab_only(byname, names):
    for nm in names:
        assert nm in byname, f"missing offensive verb: {nm}"
        assert safety.classify(nm, byname[nm]) == "lab-only", f"{nm} classified as {safety.classify(nm, byname[nm])!r}, not lab-only"


def test_flipper_offensive_verbs_gated():
    b = _by_name("flipper")
    _assert_lab_only(b, [
        "subghz tx <key_hex> <freq_hz> <te_us> <repeat> <device>",
        "rfid emulate <key_type> <key_data>",
        "rfid write <key_type> <key_data>",
        "rfid raw_emulate <file>",
        "ir tx <protocol> <address> <command>",
        "ir universal <remote> <signal>",
    ])


def test_marauder_offensive_verbs_gated():
    b = _by_name("marauder")
    _assert_lab_only(b, ["attack -t funny", "findmy -t <idx>"])


def test_offensive_verbs_emit_correct_wire_string():
    fb = _by_name("flipper")
    assert op_command(fb["ir tx <protocol> <address> <command>"], "NEC 0x04 0x08") == "ir tx NEC 0x04 0x08"
    assert op_command(fb["rfid emulate <key_type> <key_data>"], "EM4100 1234567890") == "rfid emulate EM4100 1234567890"
    assert op_command(fb["subghz tx <key_hex> <freq_hz> <te_us> <repeat> <device>"],
                      "AABBCC 433920000 400 5 0") == "subghz tx AABBCC 433920000 400 5 0"
    mb = _by_name("marauder")
    assert op_command(mb["findmy -t <idx>"], "3") == "findmy -t 3"
    assert op_command(mb["attack -t funny"]) == "attack -t funny"  # no-arg → verbatim
