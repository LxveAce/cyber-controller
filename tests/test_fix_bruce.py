"""Regression tests for the Bruce serial-protocol fix.

Bruce's serial shell is a Flipper-CLI-style shell:
    COMMAND: <x>
    ... output ...
    [CLI] Result: TRUE|FALSE
    #

The prior implementation carried fabricated WiFi/BLE/NFC serial commands and
'[WIFI]/[BLE]/[SUBGHZ]/[NFC]/[IR]' tag regexes that never match real output.
These tests pin (a) the corrected verbs, (b) the absence of the fabricated
commands, and (c) the conservative CLI parser surfacing status/info events.
"""

from __future__ import annotations

from src.core.broadcast import BroadcastVerb
from src.models.target import TargetType
from src.protocols.base import ParsedEvent
from src.protocols.bruce import (
    BROADCAST_CAPABILITIES,
    TARGET_ACTIONS,
    BruceProtocol,
)

# Target event types a *correct* Bruce parser must NEVER emit (it has no target discovery).
_TARGET_EVENTS = {
    "ap_found", "client_found", "ble_found", "subghz_found", "nfc_found", "ir_found", "rogue_ap",
}


def _command_names() -> set[str]:
    return {ci.name for ci in BruceProtocol().get_commands()}


# ── get_commands(): corrected verbs present, fabricated/stale verbs gone ──────

def test_corrected_verbs_present():
    names = _command_names()
    assert "ir rx" in names
    assert "ir tx" in names
    assert "subghz rx" in names
    assert "subghz tx" in names
    assert "subghz tx_from_file" in names
    assert any(n.startswith("badusb run_from_file") for n in names)


def test_kept_system_commands_present():
    names = _command_names()
    for keep in ("info", "free", "uptime", "reboot"):
        assert keep in names, f"expected real system command missing: {keep}"


def test_fabricated_serial_commands_removed():
    names = _command_names()
    for phantom in ("wifi scan", "wifi deauth", "wifi beacon",
                    "ble scan", "ble spam", "nfc read", "nfc emulate"):
        assert phantom not in names, f"fabricated command still present: {phantom}"


def test_old_verb_names_replaced():
    # Pre-fix verb names must be gone (renamed), not lingering beside the new ones.
    names = _command_names()
    for stale in ("ir receive", "ir send", "subghz scan", "subghz send",
                  "subghz replay", "badusb run <script>"):
        assert stale not in names, f"stale verb still present: {stale}"


# ── TARGET_ACTIONS: only the renamed SubGHz actions survive ──────────────────

def test_target_actions_fabricated_types_removed():
    for gone in (TargetType.AP, TargetType.CLIENT, TargetType.BLE, TargetType.NFC):
        assert not TARGET_ACTIONS.get(gone), f"fabricated target actions remain for {gone}"


def test_target_actions_subghz_renamed():
    cmds = {a.command_template for a in TARGET_ACTIONS.get(TargetType.SUBGHZ, [])}
    assert "subghz tx_from_file" in cmds
    assert "subghz rx" in cmds
    assert "subghz replay" not in cmds
    assert "subghz scan" not in cmds


# ── BROADCAST_CAPABILITIES: fabricated verbs dropped, SubGHz renamed ─────────

def test_broadcast_caps_corrected():
    cmds = {cmd for _pre, cmd in BROADCAST_CAPABILITIES.values()}
    assert "subghz rx" in cmds
    for phantom in ("wifi scan", "ble scan", "wifi deauth", "wifi beacon", "ble spam"):
        assert phantom not in cmds
    assert BroadcastVerb.SUBGHZ_SCAN in BROADCAST_CAPABILITIES
    for gone in (BroadcastVerb.FIND_APS, BroadcastVerb.BLE_SCAN, BroadcastVerb.DEAUTH_ALL,
                 BroadcastVerb.BEACON_SPAM, BroadcastVerb.BLE_SPAM):
        assert gone not in BROADCAST_CAPABILITIES


# ── parser: real CLI lines -> conservative status/info events (no fake targets) ──

def test_cli_result_false_parses_to_non_target_status():
    ev = BruceProtocol().parse_line("[CLI] Result: FALSE")
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type not in _TARGET_EVENTS
    assert ev.event_type in {"status", "info"}
    assert ev.data.get("success") is False


def test_cli_result_true_parses_to_status():
    ev = BruceProtocol().parse_line("[CLI] Result: TRUE")
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type not in _TARGET_EVENTS
    assert ev.data.get("success") is True


def test_command_echo_is_non_target():
    ev = BruceProtocol().parse_line("COMMAND: info")
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type not in _TARGET_EVENTS
    assert ev.data.get("command") == "info"


def test_fabricated_wifi_tag_no_longer_yields_target():
    # The old fabricated "[WIFI] ..." line must no longer parse into an ap_found target.
    ev = BruceProtocol().parse_line(
        "[WIFI] AP: CoffeeShop | BSSID: AA:BB:CC:DD:EE:FF | CH: 1 | RSSI: -50 | AUTH: WPA2"
    )
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type not in _TARGET_EVENTS
