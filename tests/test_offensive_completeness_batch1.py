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


def test_ghost_esp_offensive_verbs_gated():
    b = _by_name("ghost_esp")
    _assert_lab_only(b, [
        "blespam -random", "blespam -apple", "blespam -samsung", "blespam -google", "blespam -ms",
        "attack -hsd",
        "aerialspoof <id> <lat> <lon> <alt>",
        "ir send <path> <button>", "ir inline", "ir universals send <index>",
        "ir universals sendall <file> <button> <delay>", "ir dazzler",
        "nfc emulate uid <uid>", "nfc emulate ndef url <url>", "nfc emulate file <path>",
        "nfc hardnested known <blk> <ab> <key> target <tblk> <tab>", "nfctest",
        "ethpoison start",
        "chameleon emulator",
    ])


def test_ghost_esp_chameleon_label_is_per_verb():
    # The critic's finding 1: chameleon emulator is protected ONLY by its explicit label (category "NFC"
    # is not offensive, name has no keyword). Its sibling reads must classify SAFE — proving the gate is
    # the explicit danger=, not the category.
    b = _by_name("ghost_esp")
    assert safety.classify("chameleon emulator", b["chameleon emulator"]) == "lab-only"
    assert safety.classify("chameleon connect", b["chameleon connect"]) == ""
    assert safety.classify("ir dazzler stop", b["ir dazzler stop"]) == ""


def test_esp_at_offensive_verbs_gated():
    b = _by_name("esp_at")
    _assert_lab_only(b, [
        'AT+CWSAP="<ssid>","<pwd>",<chl>,<ecn>',
        'AT+BLEADVDATA="<hex>"',
        "AT+BLEADVSTART",
        'AT+BLEADVDATAEX="<name>","<uuid>","<mfr_hex>",<incl_pwr>',
        'AT+BLESCANRSPDATA="<hex>"',
        'AT+BLEADDR=<type>,"<addr>"',
        'AT+BLENAME="<name>"',
        "AT+BLEADVPARAM=<int_min>,<int_max>,<adv_type>,<own_addr>,<chan_map>",
    ])


def test_esp_at_wire_strings():
    b = _by_name("esp_at")
    assert op_command(b['AT+CWSAP="<ssid>","<pwd>",<chl>,<ecn>'], "evil pass123 6 3") == 'AT+CWSAP="evil","pass123",6,3'
    assert op_command(b['AT+BLENAME="<name>"'], "FakeTag") == 'AT+BLENAME="FakeTag"'
    assert op_command(b["AT+BLEADVPARAM=<int_min>,<int_max>,<adv_type>,<own_addr>,<chan_map>"],
                      "100 200 0 0 7") == "AT+BLEADVPARAM=100,200,0,0,7"


def test_esp32_div_offensive_verbs_gated():
    b = _by_name("esp32_div_serial")
    _assert_lab_only(b, ["evilportal start", "evilportal clone <n>", "blespam sourapple"])
    assert safety.classify("evilportal stop", b["evilportal stop"]) == ""  # stop is SAFE


def test_offensive_verbs_emit_correct_wire_string():
    gb = _by_name("ghost_esp")
    assert op_command(gb["aerialspoof <id> <lat> <lon> <alt>"], "1 45.0 -70.0 100") == "aerialspoof 1 45.0 -70.0 100"
    assert op_command(gb["ir send <path> <button>"], "/ir/tv.ir POWER") == "ir send /ir/tv.ir POWER"
    assert op_command(gb["nfc emulate uid <uid>"], "04AABBCC") == "nfc emulate uid 04AABBCC"
    assert op_command(gb["attack -hsd"]) == "attack -hsd"
    fb = _by_name("flipper")
    assert op_command(fb["ir tx <protocol> <address> <command>"], "NEC 0x04 0x08") == "ir tx NEC 0x04 0x08"
    assert op_command(fb["rfid emulate <key_type> <key_data>"], "EM4100 1234567890") == "rfid emulate EM4100 1234567890"
    assert op_command(fb["subghz tx <key_hex> <freq_hz> <te_us> <repeat> <device>"],
                      "AABBCC 433920000 400 5 0") == "subghz tx AABBCC 433920000 400 5 0"
    mb = _by_name("marauder")
    assert op_command(mb["findmy -t <idx>"], "3") == "findmy -t 3"
    assert op_command(mb["attack -t funny"]) == "attack -t funny"  # no-arg → verbatim
