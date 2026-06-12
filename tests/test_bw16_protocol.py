"""Tests for ``src.protocols.bw16`` — the BW16 (RTL8720DN) serial parser.

All tests are pure: the module imports only the standard library plus the
``BaseProtocol`` contract, so no hardware, pyserial, PyQt5 or esptool is needed.
There is no subprocess or serial I/O in this protocol to mock — parsing and
formatting are deterministic pure functions over strings.

Covered:
    * ``format_command`` AT+ convention (verbatim vs. ``=value``, empty args);
    * ``parse_line`` on both supported scan-list layouts, including optional
      BSSID / channel / RSSI fields;
    * ``OK`` / ``ERROR`` acknowledgements -> ``status`` events;
    * boot / SDK noise + blank lines;
    * ``get_commands`` shape and danger annotations;
    * ``identify`` markers;
    * conformance to the ``BaseProtocol`` contract.
"""

from __future__ import annotations

import pytest

# The protocols package is stdlib-only; importorskip is belt-and-suspenders.
pytest.importorskip("src.protocols")

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent  # noqa: E402
from src.protocols.bw16 import BW16Protocol  # noqa: E402


@pytest.fixture()
def proto() -> BW16Protocol:
    return BW16Protocol()


# ── Identity / contract ──────────────────────────────────────────────

def test_is_base_protocol(proto: BW16Protocol) -> None:
    assert isinstance(proto, BaseProtocol)


def test_protocol_name(proto: BW16Protocol) -> None:
    assert proto.protocol_name == "bw16"


# ── format_command (AT+ convention) ──────────────────────────────────

def test_format_no_args_is_verbatim(proto: BW16Protocol) -> None:
    assert proto.format_command("AT+SCAN") == "AT+SCAN"


def test_format_none_args_is_verbatim(proto: BW16Protocol) -> None:
    assert proto.format_command("AT+STOP", None) == "AT+STOP"


def test_format_empty_dict_is_verbatim(proto: BW16Protocol) -> None:
    assert proto.format_command("AT+SCAN", {}) == "AT+SCAN"


def test_format_with_value_appends_equals(proto: BW16Protocol) -> None:
    assert proto.format_command("AT+DEAUTHIDX", {"idx": "ALL"}) == "AT+DEAUTHIDX=ALL"


def test_format_numeric_value(proto: BW16Protocol) -> None:
    # The spec example: AT+BEACONRANDOM=<n>.
    assert proto.format_command("AT+BEACONRANDOM", {"count": 5}) == "AT+BEACONRANDOM=5"


def test_format_deauth_index(proto: BW16Protocol) -> None:
    assert proto.format_command("AT+DEAUTHIDX", {"idx": 3}) == "AT+DEAUTHIDX=3"


def test_format_uses_first_value_only(proto: BW16Protocol) -> None:
    # Only the first arg value is consumed for the AT+ ``=value`` suffix.
    out = proto.format_command("AT+DEAUTHIDX", {"idx": "ALL", "ignored": "x"})
    assert out == "AT+DEAUTHIDX=ALL"


def test_format_blank_value_falls_back_to_verbatim(proto: BW16Protocol) -> None:
    # An empty / whitespace value must not produce a dangling "AT+SCAN=".
    assert proto.format_command("AT+SCAN", {"idx": "   "}) == "AT+SCAN"


# ── parse_line: scan-list AP entries ─────────────────────────────────

def test_parse_bracket_format_full(proto: BW16Protocol) -> None:
    line = "[0] MySSID  ch:6  -42dBm  AA:BB:CC:DD:EE:FF"
    event = proto.parse_line(line)
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "ap_found"
    assert event.raw == line
    assert event.data == {
        "index": 0,
        "ssid": "MySSID",
        "channel": 6,
        "rssi": -42,
        "bssid": "AA:BB:CC:DD:EE:FF",
    }


def test_parse_vampire_scan_line(proto: BW16Protocol) -> None:
    # CONFIRMED format captured from a real RTL8720DN (Vampire Deauther, 115200).
    event = proto.parse_line("0: KashPatels007 (CH 1, RSSI -42)")
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "ap_found"
    assert event.data["index"] == 0
    assert event.data["ssid"] == "KashPatels007"
    assert event.data["channel"] == 1
    assert event.data["rssi"] == -42
    # The Vampire scan prints no BSSID.
    assert "bssid" not in event.data


def test_parse_bracket_minimal_index_and_ssid_only(proto: BW16Protocol) -> None:
    # A sparse firmware that prints only "[n] ssid" still parses.
    event = proto.parse_line("[12] OpenNet")
    assert event is not None
    assert event.event_type == "ap_found"
    assert event.data == {"index": 12, "ssid": "OpenNet"}


def test_parse_bracket_double_digit_index(proto: BW16Protocol) -> None:
    event = proto.parse_line("[10] FiveGigNet  ch:149  -67dBm  11:22:33:44:55:66")
    assert event is not None
    assert event.data["index"] == 10
    assert event.data["channel"] == 149  # 5 GHz channel
    assert event.data["rssi"] == -67
    assert event.data["bssid"] == "11:22:33:44:55:66"


def test_parse_ssid_with_spaces(proto: BW16Protocol) -> None:
    event = proto.parse_line("[3] My Home Network  ch:1  -55dBm")
    assert event is not None
    assert event.data["ssid"] == "My Home Network"
    assert event.data["channel"] == 1
    assert event.data["rssi"] == -55
    assert "bssid" not in event.data


def test_parse_vampire_ssid_with_spaces_and_5ghz(proto: BW16Protocol) -> None:
    # SSID with spaces + a 5 GHz channel, real capture.
    event = proto.parse_line("3: DIRECT-50-HP Smart Tank 5100 (CH 44, RSSI -46)")
    assert event is not None
    assert event.event_type == "ap_found"
    assert event.data["index"] == 3
    assert event.data["ssid"] == "DIRECT-50-HP Smart Tank 5100"
    assert event.data["channel"] == 44  # 5 GHz
    assert event.data["rssi"] == -46


def test_parse_vampire_empty_ssid_hidden_network(proto: BW16Protocol) -> None:
    # Hidden network prints an empty SSID: "14:  (CH 136, RSSI -77)".
    event = proto.parse_line("14:  (CH 136, RSSI -77)")
    assert event is not None
    assert event.event_type == "ap_found"
    assert event.data["index"] == 14
    assert event.data["ssid"] == ""
    assert event.data["channel"] == 136
    assert event.data["rssi"] == -77


def test_parse_bracket_tag_error_is_status(proto: BW16Protocol) -> None:
    # "[ERROR] Unknown command: AT" -> status (ok=False) with the message.
    event = proto.parse_line("[ERROR] Unknown command: AT")
    assert event is not None
    assert event.event_type == "status"
    assert event.data["ok"] is False
    assert event.data["message"] == "Unknown command: AT"


def test_parse_bracket_tag_scan_is_info(proto: BW16Protocol) -> None:
    # "[SCAN] Starting..." -> info, tag preserved.
    event = proto.parse_line("[SCAN] Starting...")
    assert event is not None
    assert event.event_type == "info"
    assert event.data["tag"] == "SCAN"
    assert event.data["message"] == "Starting..."


def test_parsed_fields_have_correct_types(proto: BW16Protocol) -> None:
    event = proto.parse_line("[0] MySSID  ch:6  -42dBm  AA:BB:CC:DD:EE:FF")
    assert event is not None
    assert isinstance(event.data["index"], int)
    assert isinstance(event.data["channel"], int)
    assert isinstance(event.data["rssi"], int)
    assert isinstance(event.data["ssid"], str)
    assert isinstance(event.data["bssid"], str)


# ── parse_line: AT status acknowledgements ───────────────────────────

def test_parse_ok_status(proto: BW16Protocol) -> None:
    event = proto.parse_line("OK")
    assert event is not None
    assert event.event_type == "status"
    assert event.data["ok"] is True


def test_parse_error_status(proto: BW16Protocol) -> None:
    event = proto.parse_line("ERROR")
    assert event is not None
    assert event.event_type == "status"
    assert event.data["ok"] is False


def test_parse_error_with_detail(proto: BW16Protocol) -> None:
    event = proto.parse_line("ERROR: invalid index")
    assert event is not None
    assert event.event_type == "status"
    assert event.data["ok"] is False
    assert event.data["message"] == "invalid index"


# ── parse_line: noise / info / blank ─────────────────────────────────

@pytest.mark.parametrize(
    "line",
    [
        "RTL_HalBleMacInit: done",
        "rltk_wlan_init",
        "hci_read_rom_check pass",
        "AmebaD SDK v1.0",
        "random unrecognised banner text",
    ],
)
def test_parse_boot_noise_is_info(proto: BW16Protocol, line: str) -> None:
    event = proto.parse_line(line)
    assert event is not None
    assert event.event_type == "info"
    assert event.data["message"] == line
    assert event.raw == line


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "  \r\n"])
def test_parse_blank_returns_none(proto: BW16Protocol, blank: str) -> None:
    assert proto.parse_line(blank) is None


def test_every_nonblank_line_yields_an_event(proto: BW16Protocol) -> None:
    # Reliability: a non-blank line must never silently vanish.
    for line in ["[0] X", "OK", "noise", "AT+SCAN"]:
        assert proto.parse_line(line) is not None


# ── get_commands ─────────────────────────────────────────────────────

def test_get_commands_returns_commandinfo_list(proto: BW16Protocol) -> None:
    cmds = proto.get_commands()
    assert isinstance(cmds, list)
    assert cmds
    assert all(isinstance(c, CommandInfo) for c in cmds)


def test_get_commands_covers_confirmed_set(proto: BW16Protocol) -> None:
    names = {c.name for c in proto.get_commands()}
    assert {
        "AT+SCAN",
        "AT+DEAUTHIDX",
        "AT+DEAUTHIDX=ALL",
        "AT+BEACONRANDOM",
        "AT+STOP",
    } <= names


def test_scan_and_stop_are_safe(proto: BW16Protocol) -> None:
    by_name = {c.name: c for c in proto.get_commands()}
    assert by_name["AT+SCAN"].danger == ""
    assert by_name["AT+STOP"].danger == ""


@pytest.mark.parametrize(
    "name",
    ["AT+DEAUTHIDX", "AT+DEAUTHIDX=ALL", "AT+BEACONRANDOM"],
)
def test_transmit_commands_are_lab_only(proto: BW16Protocol, name: str) -> None:
    by_name = {c.name: c for c in proto.get_commands()}
    assert by_name[name].danger == "lab-only"


def test_danger_values_are_valid(proto: BW16Protocol) -> None:
    valid = {"", "lab-only", "illegal-tx"}
    assert all(c.danger in valid for c in proto.get_commands())


# ── identify ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "line",
    [
        "AT+SCAN",
        "AT+DEAUTHIDX=ALL",
        "RTL_HalBleMacInit",
        "rltk_wlan_init: ok",
        "hci_read_rom_check",
        "Booting AmebaD...",
    ],
)
def test_identify_true_for_bw16_markers(proto: BW16Protocol, line: str) -> None:
    assert proto.identify(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "Marauder v1.0",
        "[GUARDIAN] ROGUE AP: EvilTwin",
        "SSID: HomeNet | BSSID: AA:BB:CC:DD:EE:FF",
        "",
        "just some text",
    ],
)
def test_identify_false_for_other_firmware(proto: BW16Protocol, line: str) -> None:
    assert proto.identify(line) is False
