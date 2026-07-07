"""Marauder v1.12.3 fixes (firmware-comms): corrected command tokens + a stateful multi-line AP parser so
the Targets tab actually populates. (Completes the fix whose agent landed the code but not this test.)"""

from __future__ import annotations


def test_marauder_tokens_corrected():
    from src.protocols import get_protocol
    names = [c.name for c in get_protocol("marauder").get_commands()]
    # v1.12.3 renamed the scan + channel-set verbs
    assert "scanall" in names
    assert "scanap" not in names and "scansta" not in names
    assert "channel -s <ch>" in names and "channel <ch>" not in names
    # BLE: real verbs are sniffbt / sniffskim (blescan/bletrack/bleskimmer don't exist)
    assert any("sniffbt" in n for n in names)
    assert "blescan" not in names and "bletrack" not in names
    # blespam token fixes
    assert any("sourapple" in n for n in names) and not any("blespam -t apple" == n for n in names)


def test_marauder_multiline_ap_parse():
    """v1.12.3 prints each AP across separate ESSID/BSSID/RSSI lines; the parser accumulates them into one
    ap_found (only once a BSSID + RSSI are seen). Leading whitespace from the firmware is tolerated."""
    from src.protocols import get_protocol
    p = get_protocol("marauder")
    assert p.parse_line("   ESSID: MyNet").event_type == "info"          # starts a record
    assert p.parse_line("   BSSID: aa:bb:cc:dd:ee:ff").event_type == "info"  # incomplete (no RSSI yet)
    ev = p.parse_line("    RSSI: -52")                                    # completes -> ap_found
    assert ev.event_type == "ap_found"
    assert ev.data["ssid"] == "MyNet"
    assert ev.data["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert ev.data["rssi"] == -52


def test_marauder_livescan_line_is_not_ap_found():
    """The live-scan one-liner has no BSSID, so it must NOT become an ap_found (Targets need a BSSID)."""
    from src.protocols import get_protocol
    p = get_protocol("marauder")
    ev = p.parse_line(" Ch: 6  RSSI: -50  ESSID: MyNet")
    assert ev is None or ev.event_type != "ap_found"


def test_marauder_legacy_singleline_still_parses():
    """The legacy single-line 'AP: .. BSSID: .. Ch: .. RSSI: ..' form is kept for back-compat / older builds."""
    from src.protocols import get_protocol
    ev = get_protocol("marauder").parse_line("AP: CoffeeShop BSSID: AA:BB:CC:DD:EE:FF Ch: 6 RSSI: -42")
    assert ev.event_type == "ap_found"


def test_marauder_identify_rejects_foreign_firmware_tokens():
    """Marauder.identify() must fingerprint only Marauder-specific output. The old markers 'scanap' (a
    GhostESP command), 'BSSID:' and 'Deauth sent' (both printed verbatim by GhostESP / ESP32-DIV) made it
    claim a sibling firmware's lines during auto-detect."""
    from src.protocols import get_protocol

    m = get_protocol("marauder")
    # These lines come from OTHER firmwares and must NOT be claimed as Marauder.
    assert not m.identify("scanap - Scan for access points")          # GhostESP command
    assert not m.identify("SSID: Net | BSSID: aa:bb:cc:dd:ee:ff | CH: 6 | RSSI: -40")  # GhostESP AP line
    assert not m.identify("Deauth sent AA:BB:CC:DD:EE:FF")            # ESP32-DIV deauth line
    # Genuine Marauder tokens still identify.
    assert m.identify("ESP32 Marauder v1.12.3")
    assert m.identify("  scanall")
    assert m.identify("  sniffpmkid")


def test_ghostesp_help_not_misdetected_as_marauder():
    """detect_firmware over a GhostESP 'help' reply must NOT resolve to Marauder. Previously Marauder's
    'scanap' marker matched GhostESP's own 'scanap' command line and (as first-registered protocol) won."""
    from src.core.handshake import detect_firmware

    ghost_help = [
        "GhostESP v1.0.0",
        "  scanap - Scan for access points",
        "  blescan - Scan BLE",
        "  stopscan",
    ]
    assert detect_firmware(ghost_help) == "ghost-esp"
    # And a command-only dump (no banner) must not flip to Marauder either.
    assert detect_firmware(["scanap", "blescan", "stopscan"]) != "marauder"


def test_marauder_ap_actions_use_documented_grammar():
    # bug-hunt #12: the AP actions appended flags that aren't in the firmware grammar (attack -t beacon -s,
    # attack -t probe -s, sniffpmkid -c). They must use only the documented forms.
    from src.protocols.marauder import TARGET_ACTIONS
    from src.models.target import TargetType
    by_name = {a.name: a for a in TARGET_ACTIONS[TargetType.AP]}
    # Beacon Clone: add the SSID to the list, then beacon from the list (no invalid -s on attack)
    assert by_name["Beacon Clone"].command_template == "attack -t beacon -l"
    assert by_name["Beacon Clone"].pre_commands == ["ssid -a -n {ssid}"]
    # Probe Flood: bare attack -t probe (no -s)
    assert by_name["Probe Flood"].command_template == "attack -t probe"
    # Sniff PMKID: set channel via the documented channel -s, then bare sniffpmkid
    assert by_name["Sniff PMKID"].command_template == "sniffpmkid"
    assert by_name["Sniff PMKID"].pre_commands == ["channel -s {channel}"]
    # No AP action may carry the bogus -s on an attack verb
    assert not any("attack -t beacon -s" in a.command_template or "attack -t probe -s" in a.command_template
                   for a in TARGET_ACTIONS[TargetType.AP])
