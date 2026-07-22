"""Per-target attack actions must actually be offered for ESP32-DIV and GhostESP scans.

Both firmwares' scan streams print no per-entry index, so their `select ... {index}`-gated actions
(Deauth AP / Capture / Deauth Client) used to be dropped by the ActionResolver — the headline attack
menu was inert on supported firmware. The parsers now assign a discovery-order ordinal (deduped by MAC,
matching each firmware's own list position), exactly like the Marauder parser, so the actions resolve.

End-to-end: real parser -> TargetIngestor._event_to_target -> ActionResolver.resolve.
"""

from __future__ import annotations

import types

from src.core.action_resolver import ActionResolver
from src.core.target_ingest import TargetIngestor
from src.protocols import get_protocol


def _target(firmware: str, port: str, line: str):
    ev = get_protocol(firmware).parse_line(line)
    return TargetIngestor._event_to_target(ev, port)


def _resolve(port: str, firmware: str, target):
    dev = types.SimpleNamespace(port=port, firmware=firmware, name=firmware)
    dm = types.SimpleNamespace(list_connected=lambda: [dev])
    return ActionResolver(dm).resolve(target)


def test_div_ap_gets_index_but_offers_no_serial_actions():
    # Stock ESP32-DIV is touch-only: its parser still assigns a scan index (target pool), but it
    # offers NO serial target actions — deauth/capture live on the serial fork's command palette.
    t = _target("esp32-div", "COM3", "AP: SSID=HomeNet BSSID=AA:BB:CC:DD:EE:FF CH=6 RSSI=-40")
    assert t.extra.get("index") == 0
    actions = _resolve("COM3", "esp32-div", t).get("COM3", [])
    assert all(a.name not in ("Deauth AP", "Capture Handshake") for a in actions)  # none offered


def test_div_client_gets_station_index_but_offers_no_serial_actions():
    t = _target("esp32-div", "COM3", "STA: MAC=11:22:33:44:55:66 BSSID=AA:BB:CC:DD:EE:FF RSSI=-50")
    assert t.extra.get("index") == 0
    actions = _resolve("COM3", "esp32-div", t).get("COM3", [])
    assert all(a.name != "Deauth Client" for a in actions)


def test_div_index_is_stable_per_mac():
    proto = get_protocol("esp32-div")
    a = proto.parse_line("AP: SSID=Net1 BSSID=AA:AA:AA:AA:AA:AA CH=1 RSSI=-30")
    b = proto.parse_line("AP: SSID=Net2 BSSID=BB:BB:BB:BB:BB:BB CH=6 RSSI=-40")
    a_again = proto.parse_line("AP: SSID=Net1 BSSID=AA:AA:AA:AA:AA:AA CH=1 RSSI=-31")
    assert a.data["index"] == 0 and b.data["index"] == 1 and a_again.data["index"] == 0


def test_ghostesp_ap_gets_index_and_offers_deauth():
    t = _target("ghost-esp", "COM4", "SSID: CoffeeShop | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -40")
    assert t.extra.get("index") == 0
    actions = _resolve("COM4", "ghost-esp", t)["COM4"]
    deauth = next(a for a in actions if a.name == "Deauth AP")
    assert deauth.pre_commands == ["select -a 0"]
