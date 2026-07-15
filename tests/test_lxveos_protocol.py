"""LxveOSProtocol — parse LxveOS's headless esp_console identity output.

LxveOS's ``status`` command emits ONE machine-readable line the Cyber Controller host parses to
identify a unit (the M1 CC-bridge seed); ``info`` prints a four-line summary. Both blocks below
are VERBATIM captures from the live LxveOS board on COM23 (LxveOS 0.1.0-m0, bare_esp32_headless),
cross-checked against the firmware source (``components/lxveos_cli/src/lxveos_cli.c``).
"""

from __future__ import annotations

import types

from src.core.handshake import DEFAULT_PROBE_COMMANDS, detect_firmware, learn_vocabulary
from src.protocols import get_protocol, resolve_protocol_name
from src.protocols.lxveos import LxveOSProtocol, _decode_caps

# verbatim COM23 captures
_STATUS = (
    "LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 "
    "panel=none caps=0x007 ops=12/3/6 heap=184988"
)
_INFO = [
    "fw    : LxveOS 0.1.0-m0", "board : bare_esp32_headless", "chip  : esp32", "ui    : headless",
]

# A representative slice of the real COM23 reply to the default probe command (`help`): the command
# summaries LxveOS prints, followed by its linenoise prompt. Captured live in beat 224.
_HELP_REPLY = [
    "help", "help  [<string>] [-v <0|1>]",
    "Print the summary of all registered commands if no arguments are given,",
    "agree", "Accept the authorized-use terms to unlock commands",
    "info", "status", "One machine-readable status line (Cyber Controller bridge format)",
    "caps", "sysinfo", "loglevel", "nvs", "reboot",
    "lxveos>",
]


def test_registry_resolves_lxveos():
    assert get_protocol("lxveos").protocol_name == "lxveos"
    assert resolve_protocol_name("LxveOS") == "lxveos"
    assert resolve_protocol_name("lxveos") == "lxveos"


def test_status_bridge_line_parses_with_typed_fields():
    ev = LxveOSProtocol().parse_line(_STATUS)
    assert ev is not None and ev.event_type == "device_info"
    d = ev.data
    assert d["source"] == "status_line" and d["proto_version"] == 1
    assert d["board"] == "bare_esp32_headless" and d["chip"] == "esp32" and d["ui"] == "headless"
    assert d["fw"] == "0.1.0-m0" and d["panel"] == "none"
    assert d["caps"] == 0x007 and isinstance(d["caps"], int)  # hex bitmask -> int
    assert d["caps_tokens"] == ["wifi", "ble", "bt_classic"]  # decoded from the live COM23 mask
    assert d["ops"] == {"ready": 12, "planned": 3, "unavailable": 6}
    assert d["heap"] == 184988 and isinstance(d["heap"], int)


def test_caps_bitmask_decodes_in_firmware_bit_order():
    # Bit order is the firmware's lxveos_cap_t enum (lxveos_caps): wifi=0 ble=1 bt_classic=2
    # display=3 storage=4 gps=5 ir=6 subghz=7 nrf24=8 nfc=9. Locked so a bit-order drift is caught.
    assert _decode_caps(0x000) == []
    assert _decode_caps(0x007) == ["wifi", "ble", "bt_classic"]
    assert _decode_caps(0x0a0) == ["gps", "subghz"]
    assert _decode_caps(0x3ff) == [
        "wifi", "ble", "bt_classic", "display", "storage", "gps", "ir", "subghz", "nrf24", "nfc",
    ]


def test_caps_decode_surfaces_unknown_future_bits():
    # A set bit beyond the known M0 range must be surfaced (capN), not dropped — an M1 capability
    # firmware reports can't silently vanish from the operator's view.
    assert _decode_caps(0x400) == ["cap10"]
    assert _decode_caps(0x401) == ["wifi", "cap10"]


def test_status_tolerates_unknown_future_keys():
    # LxveOS may append fields (comment in cmd_status says older hosts ignore them); an unknown key
    # must land in the event as a raw string, never crash or get dropped.
    ev = LxveOSProtocol().parse_line(_STATUS + " region=us newfield=abc123")
    assert ev.data["region"] == "us" and ev.data["newfield"] == "abc123"


def test_info_block_accumulates_to_one_device_info():
    p = LxveOSProtocol()
    assert p.parse_line(_INFO[0]) is None  # fw  -> start record
    assert p.parse_line(_INFO[1]) is None  # board
    assert p.parse_line(_INFO[2]) is None  # chip
    ev = p.parse_line(_INFO[3])            # ui -> emit
    assert ev is not None and ev.event_type == "device_info"
    assert ev.data == {
        "fw": "0.1.0-m0", "source": "info_cmd",
        "board": "bare_esp32_headless", "chip": "esp32", "ui": "headless",
    }


def test_stray_info_line_without_record_is_benign_info():
    # A board/chip/ui line with no in-progress `fw :` must NOT emit a half-built identity — it's
    # surfaced as plain info instead.
    p = LxveOSProtocol()
    ev = p.parse_line("ui    : headless")
    assert ev is not None and ev.event_type == "info"
    assert ev.data["message"] == "ui    : headless"


def test_prompt_is_a_readiness_status_not_noise():
    ev = LxveOSProtocol().parse_line("lxveos>")
    assert ev is not None and ev.event_type == "status" and ev.data == {"prompt": True}


def test_identify_matches_lxveos_output_only():
    p = LxveOSProtocol()
    assert p.identify(_STATUS) is True
    assert p.identify("lxveos>") is True
    assert p.identify("fw    : LxveOS 0.1.0-m0") is True
    assert p.identify("I (27) boot: ESP-IDF v6.0.2 2nd stage bootloader") is False
    assert p.identify("-22 Ch: 1 b4:bf:e9:11:19:ad ESSID: ESP_1119AD") is False  # Marauder AP line


def test_command_catalog_full_surface_with_danger_flags():
    # The full LxveOS surface (LXVEOS-CC-CONTROL-SPEC §5) is now exposed. Offensive ops are lab-only;
    # LxveOS ships no interference emitter, so nothing is illegal-tx; recon/defense stay passive.
    cmds = {c.name: c for c in LxveOSProtocol().get_commands()}
    assert {
        "help", "agree", "info", "status", "bridge", "caps", "features", "sysinfo",
        "scan", "sniff", "stations", "probes", "capture", "wardrive",
        "blescan", "subghz", "nrf24", "nfc", "ir",
        "defend", "eviltwin", "apaudit", "bleflood", "btracker", "blehid",
        "arm", "disarm", "evilportal", "badble",
    } <= set(cmds)
    assert cmds["evilportal"].danger == "lab-only"
    assert cmds["badble"].danger == "lab-only"
    assert all(c.danger in ("", "lab-only") for c in cmds.values())
    assert not any(c.danger == "illegal-tx" for c in cmds.values())  # no emitter shipped
    assert cmds["scan"].danger == "" and cmds["defend"].danger == "" and cmds["blescan"].danger == ""
    assert cmds["arm"].danger == ""  # the gate itself transmits nothing


# ── event-line parsing (bridge on -> LXVEOS/1 <type> k=v events) ──

def test_ap_event_parses_to_ap_found_with_decoded_ssid():
    ev = LxveOSProtocol().parse_line(
        "LXVEOS/1 ap bssid=de:ad:be:ef:00:01 ssid=4d794e6574 ch=6 rssi=-42 auth=wpa2"
    )
    assert ev is not None and ev.event_type == "ap_found"
    d = ev.data
    assert d["bssid"] == "de:ad:be:ef:00:01"
    assert d["ssid"] == "MyNet" and d["ssid_hex"] == "4d794e6574"  # hex decoded back to text + raw kept
    assert d["ch"] == 6 and d["rssi"] == -42 and d["auth"] == "wpa2"


def test_hidden_ssid_ap_event_has_empty_ssid():
    ev = LxveOSProtocol().parse_line(
        "LXVEOS/1 ap bssid=aa:bb:cc:dd:ee:ff ssid= ch=1 rssi=-70 auth=open"
    )
    assert ev.event_type == "ap_found" and ev.data["ssid"] == "" and ev.data["ssid_hex"] == ""


def test_bridge_and_done_events():
    p = LxveOSProtocol()
    ev = p.parse_line("LXVEOS/1 bridge state=on")
    assert ev.event_type == "bridge_state" and ev.data["state"] == "on"
    ev = p.parse_line("LXVEOS/1 done of=scan n=5")
    assert ev.event_type == "batch_done" and ev.data["of"] == "scan" and ev.data["n"] == 5


def test_ble_and_handshake_events():
    ev = LxveOSProtocol().parse_line("LXVEOS/1 ble addr=11:22:33:44:55:66 name=4d79 rssi=-55")
    assert ev.event_type == "ble_found" and ev.data["name"] == "My" and ev.data["rssi"] == -55
    ev = LxveOSProtocol().parse_line("LXVEOS/1 hs kind=pmkid bssid=de:ad:be:ef:00:01 essid=4e6574")
    assert ev.event_type == "handshake_captured"
    assert ev.data["kind"] == "pmkid" and ev.data["essid"] == "Net"


def test_unknown_event_type_is_forward_compat_info():
    ev = LxveOSProtocol().parse_line("LXVEOS/1 futurething x=1 y=2")
    assert ev.event_type == "info" and ev.data["lxveos_event"] == "futurething"
    assert ev.data["fields"] == {"x": "1", "y": "2"}


def test_arm_state_from_structured_event_and_from_prose():
    p = LxveOSProtocol()
    # structured event (bridge on)
    ev = p.parse_line("LXVEOS/1 arm state=pending token=123 window=30")
    assert ev.event_type == "arm_state" and ev.data["state"] == "pending" and ev.data["token"] == 123
    # prose fallback (spec §4 replies)
    ev = p.parse_line("arm requested. Confirm within 30s:  arm 3735928559")
    assert ev.event_type == "arm_state" and ev.data["state"] == "pending" and ev.data["token"] == 3735928559
    ev = p.parse_line("ARMED - offensive-TX ops permitted until 'disarm' or inactivity timeout.")
    assert ev.event_type == "arm_state" and ev.data["state"] == "armed"
    ev = p.parse_line("arm state: safe")
    assert ev.event_type == "arm_state" and ev.data["state"] == "safe"
    ev = p.parse_line("offensive TX is compiled OUT of this build ... nothing to arm.")
    assert ev.event_type == "arm_state" and ev.data["state"] == "tx_disabled"


# ── auto-detect integration (handshake.detect_firmware / learn_vocabulary) ──

def test_default_probe_reply_auto_detects_lxveos():
    # CC probes an unknown text-CLI device with `help` (DEFAULT_PROBE_COMMANDS). LxveOS's reply +
    # `lxveos>` prompt must resolve to the lxveos protocol, not fall back to generic — HW-confirmed
    # on the live COM23 board (beat 224).
    assert DEFAULT_PROBE_COMMANDS == ("help",)
    assert detect_firmware(_HELP_REPLY) == "lxveos"


def test_status_line_alone_auto_detects_lxveos():
    # Even a lone bridge line (e.g. the M1 framed boot identity) identifies the unit.
    assert detect_firmware([_STATUS]) == "lxveos"


def test_learn_vocabulary_confirms_lxveos_commands_from_help():
    # The `help` reply advertises LxveOS's real command names; learn_vocabulary must confirm them
    # against get_commands(), so command drift would surface instead of silently mis-sending.
    dev = types.SimpleNamespace(firmware="lxveos", driver_type="text-cli")
    vocab = learn_vocabulary(_HELP_REPLY, dev)
    assert {"info", "status", "caps", "sysinfo", "reboot"} <= vocab
