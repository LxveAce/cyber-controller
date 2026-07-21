"""ESP32-DIV parser fix (firmware-comms): the deauth line's target MAC was never captured.

_RE_DEAUTH made the trailing MAC group OPTIONAL after a lazy '.*?', so the engine matched with the group
empty and group(1) was always None -> deauth_sent's 'target' was always "". The fix keeps the whole tail
optional (a target-less broadcast deauth still registers) but the MAC group itself is required WHEN it
matches, so a present MAC is captured. Mirrors the sibling ghost_esp / handshake parsers.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "line,expected_mac",
    [
        ("Deauth sent AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
        ("Deauth frame to 11:22:33:44:55:66", "11:22:33:44:55:66"),
        ("DEAUTH attack on AA:BB:CC:DD:EE:99", "AA:BB:CC:DD:EE:99"),
    ],
)
def test_div_deauth_captures_target_mac(line, expected_mac):
    """A deauth line WITH a target MAC now yields it in data['target'] (was always '')."""
    from src.protocols import get_protocol

    ev = get_protocol("esp32-div").parse_line(line)
    assert ev.event_type == "deauth_sent"
    assert ev.data["target"] == expected_mac


def test_div_deauth_without_mac_still_registers():
    """A target-less deauth (broadcast) still emits deauth_sent with an empty target — no regression."""
    from src.protocols import get_protocol

    ev = get_protocol("esp32-div").parse_line("Deauth sent")
    assert ev.event_type == "deauth_sent"
    assert ev.data["target"] == ""


# ── danger annotations: DIV offensive verbs must classify authoritatively ─────

def test_div_offensive_verbs_carry_explicit_danger():
    """The DIV attack/BLE-spam verbs must be flagged lab-only and 'nrf jam' illegal-tx, so the Operate
    grid buckets and gates them deterministically (info.danger wins over keyword heuristics)."""
    from src.core import safety
    from src.protocols import get_protocol

    cmds = {ci.name: ci for ci in get_protocol("esp32-div").cached_commands()}
    for verb in ("deauth", "deauth all", "beacon", "beacon target", "probe", "rickroll",
                 "blespam", "blespam apple"):
        assert safety.classify(cmds[verb].name, cmds[verb]) == safety.LAB_ONLY, verb
    assert safety.classify(cmds["nrf jam"].name, cmds["nrf jam"]) == safety.ILLEGAL_TX
    # Genuinely passive verbs stay safe (no false danger on scans / sniffs / captures).
    # (Note: "stopattack" trips classify's "attack" keyword heuristic — a harmless pre-existing
    # false-positive that just adds a confirm to a cease command; not part of this annotation change.)
    for verb in ("scanwifi", "nrf sniff", "sniff", "handshake", "pmkid"):
        assert safety.classify(cmds[verb].name, cmds[verb]) == safety.SAFE, verb
