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


def test_status_tx_and_arm_fields_typed():
    # current firmware appends `arm=<state>` (token) + `tx=<0|1>` (offensive-TX compiled in). tx is typed
    # to a bool so the TX-lockout UI can tell a TX-capable-but-SAFE unit from one that can never arm.
    p = LxveOSProtocol()
    d = p.parse_line(_STATUS + " arm=safe tx=1").data
    assert d["arm"] == "safe" and d["tx"] is True
    d = p.parse_line(_STATUS + " arm=safe tx=0").data
    assert d["tx"] is False
    # a malformed tx value is left as a raw string, never silently coerced to a wrong bool
    d = p.parse_line(_STATUS + " tx=x").data
    assert d["tx"] == "x"


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


def test_sta_event_parses_client_with_typed_fields():
    # firmware `stations` emits: mac/ap (MAC strings), rssi (int), frames (uint), essid (hex).
    ev = LxveOSProtocol().parse_line(
        "LXVEOS/1 sta mac=aa:bb:cc:00:11:22 ap=de:ad:be:ef:00:01 rssi=-58 frames=42 essid=4d794e6574"
    )
    assert ev is not None and ev.event_type == "client_found"
    d = ev.data
    assert d["mac"] == "aa:bb:cc:00:11:22" and d["ap"] == "de:ad:be:ef:00:01"
    assert d["rssi"] == -58 and d["frames"] == 42
    assert d["essid"] == "MyNet" and d["essid_hex"] == "4d794e6574"


def test_probe_event_parses_directed_ssid_with_typed_fields():
    # firmware `probes` emits: ssid (hex), seen (uint), rssi (int) — no client MAC (aggregated by SSID).
    ev = LxveOSProtocol().parse_line("LXVEOS/1 probe ssid=4d794e6574 seen=7 rssi=-63")
    assert ev is not None and ev.event_type == "probe_request"
    d = ev.data
    assert d["ssid"] == "MyNet" and d["ssid_hex"] == "4d794e6574"
    assert d["seen"] == 7 and d["rssi"] == -63
    assert "mac" not in d  # the passive probe scan carries no per-device MAC


def test_done_markers_for_stations_and_probes():
    p = LxveOSProtocol()
    ev = p.parse_line("LXVEOS/1 done of=stations n=3")
    assert ev.event_type == "batch_done" and ev.data["of"] == "stations" and ev.data["n"] == 3
    ev = p.parse_line("LXVEOS/1 done of=probes n=0")
    assert ev.event_type == "batch_done" and ev.data["of"] == "probes" and ev.data["n"] == 0


def test_bridge_and_done_events():
    p = LxveOSProtocol()
    ev = p.parse_line("LXVEOS/1 bridge state=on")
    assert ev.event_type == "bridge_state" and ev.data["state"] == "on"
    ev = p.parse_line("LXVEOS/1 done of=scan n=5")
    assert ev.event_type == "batch_done" and ev.data["of"] == "scan" and ev.data["n"] == 5


def test_alert_events_from_all_six_detectors():
    p = LxveOSProtocol()
    # defend -> deauth (counts typed int, busiest source mac kept as string)
    d = p.parse_line("LXVEOS/1 alert kind=deauth bssid=de:ad:be:ef:00:01 count=27 deauth=20 disassoc=7").data
    assert d["kind"] == "deauth" and d["bssid"] == "de:ad:be:ef:00:01"
    assert d["count"] == 27 and d["deauth"] == 20 and d["disassoc"] == 7
    # eviltwin -> ssid hex-decoded, bssid/open/enc counts int
    d = p.parse_line("LXVEOS/1 alert kind=eviltwin ssid=4d794e6574 bssids=2 open=1 enc=1").data
    assert d["kind"] == "eviltwin" and d["ssid"] == "MyNet" and d["bssids"] == 2 and d["open"] == 1 and d["enc"] == 1
    # apaudit -> weak / wps, grade int
    d = p.parse_line("LXVEOS/1 alert kind=weak bssid=aa:bb:cc:00:11:22 ssid=4d794e6574 grade=0").data
    assert d["kind"] == "weak" and d["grade"] == 0 and d["ssid"] == "MyNet"
    d = p.parse_line("LXVEOS/1 alert kind=wps bssid=aa:bb:cc:00:11:22 ssid=4d794e6574 grade=3 wps=1").data
    assert d["kind"] == "wps" and d["grade"] == 3 and d["wps"] == 1
    # bleflood -> rate/uniq int, vendor token
    d = p.parse_line("LXVEOS/1 alert kind=bleflood rate=15 uniq=92 vendor=Apple").data
    assert d["kind"] == "bleflood" and d["rate"] == 15 and d["uniq"] == 92 and d["vendor"] == "Apple"
    # btracker -> tracker, addr string, vendor token, name hex, rssi int
    d = p.parse_line("LXVEOS/1 alert kind=tracker addr=11:22:33:44:55:66 vendor=AirTag rssi=-40 name=4d79").data
    assert d["kind"] == "tracker" and d["vendor"] == "AirTag" and d["rssi"] == -40 and d["name"] == "My"
    # blehid -> addr string, rssi int, name hex
    d = p.parse_line("LXVEOS/1 alert kind=blehid addr=11:22:33:44:55:66 rssi=-50 name=4b6579").data
    assert d["kind"] == "blehid" and d["rssi"] == -50 and d["name"] == "Key"
    # watch (target watchlist hit) -> mac/band pass through as strings, rssi typed int. The generic alert
    # row needs no new field types: `kind`/`mac`/`band` land in the else-branch, `rssi` in the int set.
    d = p.parse_line("LXVEOS/1 alert kind=watch mac=de:ad:be:ef:00:01 rssi=-42 band=wifi").data
    assert d["kind"] == "watch" and d["mac"] == "de:ad:be:ef:00:01" and d["rssi"] == -42 and d["band"] == "wifi"
    d = p.parse_line("LXVEOS/1 alert kind=watch mac=11:22:33:44:55:66 rssi=-70 band=ble").data
    assert d["kind"] == "watch" and d["band"] == "ble" and d["rssi"] == -70


def test_snapshot_airspace_summary_event():
    # the `airspace` custom command emits one occupancy summary: AP count (+ open/WPS-exposed splits) and
    # BLE advertiser count (+ known-tracker count). All counts typed to int for the CC dashboard.
    d = LxveOSProtocol().parse_line("LXVEOS/1 snapshot aps=14 open=3 wps=2 bles=8 trackers=1").data
    assert d["aps"] == 14 and d["open"] == 3 and d["wps"] == 2
    assert d["bles"] == 8 and d["trackers"] == 1


def test_ble_event_full_fields():
    # firmware `blescan`: addr (reversed to MSB-first), type, rssi always; name/company/fp/appr/tracker
    # only when the advert carried them. company is the numeric Bluetooth-SIG ID; tracker the item class.
    ev = LxveOSProtocol().parse_line(
        "LXVEOS/1 ble addr=aa:bb:cc:dd:ee:ff type=random rssi=-55 name=4d79 company=76 fp=1 appr=64 tracker=1"
    )
    assert ev.event_type == "ble_found"
    d = ev.data
    assert d["addr"] == "aa:bb:cc:dd:ee:ff" and d["type"] == "random" and d["rssi"] == -55
    assert d["name"] == "My" and d["name_hex"] == "4d79"
    assert d["company"] == 76 and d["fp"] == 1 and d["appr"] == 64 and d["tracker"] == 1


def test_ble_event_minimal_has_no_optional_fields():
    ev = LxveOSProtocol().parse_line("LXVEOS/1 ble addr=11:22:33:44:55:66 type=public rssi=-40")
    assert ev.event_type == "ble_found" and ev.data["rssi"] == -40
    for absent in ("name", "company", "fp", "appr", "tracker"):
        assert absent not in ev.data


def test_handshake_event_keeps_hashcat_line_and_extracts_essid():
    # firmware `capture` forwards the raw hashcat-22000 artifact as `line=`; the parser keeps it verbatim
    # for Crack Lab and lifts the ESSID (field 5, hex) out for a display name.
    line = "WPA*01*0102030405060708090a0b0c0d0e0f10*deadbeef0001*aabbcc001122*4d794e6574***"
    ev = LxveOSProtocol().parse_line(f"LXVEOS/1 hs kind=pmkid line={line}")
    assert ev.event_type == "handshake_captured"
    assert ev.data["kind"] == "pmkid"
    assert ev.data["line"] == line  # kept byte-for-byte for the crack pipeline
    assert ev.data["essid"] == "MyNet" and ev.data["essid_hex"] == "4d794e6574"
    # WPA*02 EAPOL handshake -> kind eapol
    ev = LxveOSProtocol().parse_line(
        "LXVEOS/1 hs kind=eapol line=WPA*02*aabb*deadbeef0001*aabbcc001122*4e6574*cc*dd*00"
    )
    assert ev.data["kind"] == "eapol" and ev.data["essid"] == "Net"


def test_unknown_event_type_is_forward_compat_info():
    ev = LxveOSProtocol().parse_line("LXVEOS/1 futurething x=1 y=2")
    assert ev.event_type == "info" and ev.data["lxveos_event"] == "futurething"
    assert ev.data["fields"] == {"x": "1", "y": "2"}


def test_arm_state_from_structured_event_and_from_prose():
    p = LxveOSProtocol()
    # structured events (bridge on) — the firmware emits one at every transition
    ev = p.parse_line("LXVEOS/1 arm state=pending token=123 window=30")
    assert ev.event_type == "arm_state" and ev.data["state"] == "pending" and ev.data["token"] == 123
    for state in ("armed", "safe", "tx_disabled"):
        ev = p.parse_line(f"LXVEOS/1 arm state={state}")
        assert ev.event_type == "arm_state" and ev.data["state"] == state
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
