"""Tests for ``src.protocols.esp32_div`` — the ESP32-DIV inbound parser + catalog.

ESP32-DIV (cifertech/ESP32-DIV) is a touch/button-driven multi-tool with NO
serial command interface (verified on hardware). These tests therefore exercise:

    * ``parse_line`` as an INBOUND-only parser over the captured v1.1.0 boot
      output (banner / Version line / Button events / ESP-IDF error logs) and
      the fall-through behaviour for misc / empty lines;
    * ``get_commands`` as a documented CATALOG: presence of expected tools and
      correct danger-flag annotations (safe / lab-only / illegal-tx);
    * ``identify`` auto-detection on banner markers;
    * ``format_command`` (display-only; DIV ignores serial input).

The module imports only the standard library plus the base/protocol contracts,
so no optional dependency (pyserial / PyQt5 / esptool) is required and nothing
here touches hardware. ``importorskip`` is belt-and-suspenders only.
"""

from __future__ import annotations

import pytest

esp32_div = pytest.importorskip("src.protocols.esp32_div")

from src.protocols.base import (  # noqa: E402  (after importorskip)
    BaseProtocol,
    CommandInfo,
    ParsedEvent,
)
from src.protocols.esp32_div import Esp32DivProtocol  # noqa: E402


# Captured v1.1.0 boot output (verbatim from a bare board).
_BOOT_BANNER = [
    "==================================",
    "ESP32-DIV",
    "Developed by: CiferTech",
    "Version:      1.1.0",
    "Contact:      cifertech@gmail.com",
    "GitHub:       github.com/cifertech",
    "==================================",
]


@pytest.fixture()
def proto() -> Esp32DivProtocol:
    return Esp32DivProtocol()


# ── Identity / contract ──────────────────────────────────────────────

def test_is_base_protocol_subclass(proto: Esp32DivProtocol) -> None:
    assert isinstance(proto, BaseProtocol)


def test_protocol_name(proto: Esp32DivProtocol) -> None:
    assert proto.protocol_name == "esp32-div"


# ── parse_line: Version line -> device_info ──────────────────────────

def test_version_line_returns_device_info(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Version:      1.1.0")
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "device_info"
    assert event.data == {"firmware": "esp32-div", "version": "1.1.0"}
    assert event.raw == "Version:      1.1.0"


def test_version_line_with_suffix(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Version: 1.2.0-rc1")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data["version"] == "1.2.0-rc1"
    assert event.data["firmware"] == "esp32-div"


def test_version_line_case_insensitive(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("version:   2.0.0")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data["version"] == "2.0.0"


# ── parse_line: Button events -> button ──────────────────────────────

@pytest.mark.parametrize("index", list(range(8)))
def test_button_pressed_all_indices(proto: Esp32DivProtocol, index: int) -> None:
    # On a bare board every Button N reads "Pressed".
    line = f"Button {index}: Pressed"
    event = proto.parse_line(line)
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "button"
    assert event.data == {"index": index, "state": "Pressed"}
    assert event.raw == line


def test_button_released(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Button 3: Released")
    assert event is not None
    assert event.event_type == "button"
    assert event.data == {"index": 3, "state": "Released"}


def test_button_state_normalised_capitalisation(proto: Esp32DivProtocol) -> None:
    # Whatever casing the firmware uses, state comes back capitalised.
    event = proto.parse_line("Button 2: pressed")
    assert event is not None
    assert event.data["state"] == "Pressed"


def test_button_index_is_int(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Button 7: Pressed")
    assert event is not None
    assert isinstance(event.data["index"], int)


# ── parse_line: ESP-IDF logs -> error ────────────────────────────────

def test_esp_idf_error_line(proto: Esp32DivProtocol) -> None:
    line = "E (622) ADC: adc1_lock_release(419): adc lock release failed"
    event = proto.parse_line(line)
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "error"
    assert event.data["tag"] == "ADC"
    assert event.data["level"] == "E"
    assert event.data["ticks"] == 622
    assert event.data["message"] == "adc1_lock_release(419): adc lock release failed"
    assert event.raw == line


def test_esp_idf_warning_is_error_event(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("W (100) wifi: something is off")
    assert event is not None
    assert event.event_type == "error"
    assert event.data["level"] == "W"
    assert event.data["tag"] == "wifi"


def test_esp_idf_info_level_is_info_event(proto: Esp32DivProtocol) -> None:
    # Informational ESP-IDF logs (I/D/V) must NOT be flagged as errors.
    event = proto.parse_line("I (50) boot: chip revision: v3.0")
    assert event is not None
    assert event.event_type == "info"
    assert event.data["level"] == "I"
    assert event.data["tag"] == "boot"
    assert event.data["ticks"] == 50


# ── parse_line: banner identity + decoration ─────────────────────────

def test_product_banner_line(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("ESP32-DIV")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data == {"field": "product", "value": "ESP32-DIV"}


def test_developed_by_line(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Developed by: CiferTech")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data == {"field": "developer", "value": "CiferTech"}


def test_contact_line(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Contact:      cifertech@gmail.com")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data == {"field": "contact", "value": "cifertech@gmail.com"}


def test_github_line(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("GitHub:       github.com/cifertech")
    assert event is not None
    assert event.event_type == "device_info"
    assert event.data == {"field": "github", "value": "github.com/cifertech"}


def test_banner_rule_line_is_noise(proto: Esp32DivProtocol) -> None:
    # The "====..." decoration line is dropped (None), like an empty line.
    assert proto.parse_line("==================================") is None


# ── parse_line: fall-through + empties ───────────────────────────────

def test_empty_line_returns_none(proto: Esp32DivProtocol) -> None:
    assert proto.parse_line("") is None
    assert proto.parse_line("   ") is None
    assert proto.parse_line("\t\n") is None


def test_unknown_line_is_info(proto: Esp32DivProtocol) -> None:
    event = proto.parse_line("Booting on-device menu...")
    assert event is not None
    assert event.event_type == "info"
    assert event.data == {"message": "Booting on-device menu..."}


def test_generic_error_wording_is_error(proto: Esp32DivProtocol) -> None:
    # An error-worded line not in strict ESP-IDF format still flags as error.
    event = proto.parse_line("PN532 init failed")
    assert event is not None
    assert event.event_type == "error"
    assert event.data["message"] == "PN532 init failed"


def test_full_boot_banner_sequence(proto: Esp32DivProtocol) -> None:
    # Parse the captured banner end-to-end; collect the meaningful events.
    events = [proto.parse_line(line) for line in _BOOT_BANNER]
    types = [e.event_type for e in events if e is not None]
    # Two rule lines drop to None; the rest are all device_info.
    assert events[0] is None  # leading "===" rule
    assert events[-1] is None  # trailing "===" rule
    assert types == ["device_info"] * 5
    # Exactly one of them is the authoritative firmware/version event.
    version_events = [
        e for e in events
        if e is not None and e.data.get("firmware") == "esp32-div"
    ]
    assert len(version_events) == 1
    assert version_events[0].data["version"] == "1.1.0"


# ── identify ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "line",
    [
        "ESP32-DIV",
        "Developed by: CiferTech",
        "GitHub:       github.com/cifertech",
        "some prefix ESP32-DIV some suffix",
    ],
)
def test_identify_true(proto: Esp32DivProtocol, line: str) -> None:
    assert proto.identify(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "Marauder v0.13",
        "[GUARDIAN] ROGUE AP: x",
        "Button 0: Pressed",  # button events are not unique enough to claim
        "",
    ],
)
def test_identify_false(proto: Esp32DivProtocol, line: str) -> None:
    assert proto.identify(line) is False


# ── get_commands: catalog completeness + danger flags ────────────────

def test_get_commands_returns_command_info(proto: Esp32DivProtocol) -> None:
    cmds = proto.get_commands()
    assert len(cmds) > 0
    assert all(isinstance(c, CommandInfo) for c in cmds)


def test_command_names_unique(proto: Esp32DivProtocol) -> None:
    names = [c.name for c in proto.get_commands()]
    assert len(names) == len(set(names)), "catalog has duplicate command ids"


def test_every_command_has_name_category_description(proto: Esp32DivProtocol) -> None:
    for c in proto.get_commands():
        assert c.name, "command missing name"
        assert c.category, f"{c.name} missing category"
        assert c.description, f"{c.name} missing description"


def test_danger_values_are_valid(proto: Esp32DivProtocol) -> None:
    valid = {"", "lab-only", "illegal-tx"}
    for c in proto.get_commands():
        assert c.danger in valid, f"{c.name} has invalid danger {c.danger!r}"


def _danger_for(proto: Esp32DivProtocol, name: str) -> str:
    for c in proto.get_commands():
        if c.name == name:
            return c.danger
    raise AssertionError(f"command {name!r} not found in catalog")


# Expected danger flag per the device-tool spec.
_EXPECTED_DANGER = {
    # WiFi
    "packet_monitor": "",
    "wifi_scanner": "",
    "beacon_spam": "lab-only",
    "deauth": "lab-only",
    "deauth_detector": "",
    "captive_portal": "lab-only",
    "probe_flood": "lab-only",
    # BLE
    "ble_scanner": "",
    "ble_sniffer": "",
    "ble_spoofer": "lab-only",
    "sour_apple": "lab-only",
    "ble_jammer": "illegal-tx",
    "ble_rubber_ducky": "lab-only",
    # RF24
    "scanner_2g4": "",
    "protokill": "illegal-tx",
    # SubGHz
    "subghz_replay": "lab-only",
    "subghz_jammer": "illegal-tx",
    "subghz_profiles": "",
    # IR
    "ir_replay": "",
    "ir_saved": "",
    "ir_universal": "",
    # NFC
    "card_reader": "",
    "card_clone": "lab-only",
    "nfc_dump": "",
    "decode_access": "",
    "nfc_erase": "lab-only",
    "jam_reader": "illegal-tx",
    "tag_disrupt": "illegal-tx",
    "disrupt_emulate": "lab-only",
    # GPS
    "wardriver": "",
    "satellite_scanner": "",
    # System
    "serial_monitor": "",
    "sd_file_manager": "",
    "update_firmware": "",
    "touch_calibrate": "",
    "settings": "",
}


def test_catalog_contains_all_expected_tools(proto: Esp32DivProtocol) -> None:
    names = {c.name for c in proto.get_commands()}
    missing = set(_EXPECTED_DANGER) - names
    assert not missing, f"catalog missing tools: {sorted(missing)}"


@pytest.mark.parametrize("name, danger", sorted(_EXPECTED_DANGER.items()))
def test_each_tool_danger_flag(proto: Esp32DivProtocol, name: str, danger: str) -> None:
    assert _danger_for(proto, name) == danger


def test_illegal_tx_jammers_flagged(proto: Esp32DivProtocol) -> None:
    # Every jam/disrupt transmit tool must carry the strongest flag.
    illegal = {
        c.name for c in proto.get_commands() if c.danger == "illegal-tx"
    }
    assert {"ble_jammer", "protokill", "subghz_jammer", "jam_reader", "tag_disrupt"} <= illegal


def test_categories_cover_all_domains(proto: Esp32DivProtocol) -> None:
    cats = {c.category for c in proto.get_commands()}
    assert cats == {
        "WiFi", "BLE", "2.4GHz", "SubGHz", "IR", "NFC", "GPS", "System",
    }


# ── format_command (display-only; DIV ignores serial input) ──────────

def test_format_command_plain(proto: Esp32DivProtocol) -> None:
    assert proto.format_command("wifi_scanner") == "wifi_scanner"


def test_format_command_with_args(proto: Esp32DivProtocol) -> None:
    out = proto.format_command("subghz_profiles", {"freq": "433"})
    assert out == "subghz_profiles 433"


def test_format_command_no_args_dict_empty(proto: Esp32DivProtocol) -> None:
    # An empty args dict behaves like no args (no trailing space).
    assert proto.format_command("settings", {}) == "settings"
