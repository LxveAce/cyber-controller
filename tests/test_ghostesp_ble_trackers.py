"""GhostESP BLE tracker scans — Flipper Zero (`blescan -f`) + Apple AirTag (`aerialscan`).

GhostESP has the tracker-scan COMMANDS (blescan -f / aerialscan / listairtags) but the parser had no
handler for their output, so every detected Flipper / AirTag fell through to a generic `info` and
never reached the target pool. Both now surface as `ble_found` (a Flipper/AirTag IS a BLE device),
with a `kind` discriminator, flowing end-to-end into a BLE target.

The formats are grounded in the FIRMWARE's own source, not guessed:
  * `flipper_scan.c`: glog("[%d] %s Flipper Found:\n  MAC: %s,\n  Name: %s,\n  RSSI: %d dBm\n")
  * `airtag_scan.c`:  glog("[%d] AirTag Found (Total: %d)\n  MAC: %s,\n  RSSI: %d dBm (%s),\n")
  * `webui/src/parsers.js` FLIPPER_*/AIRTAG_* patterns (the firmware's own web-UI consumer).
Both records close on the "RSSI: N dBm" line; the leading indentation is `.strip()`'d by parse_line.
"""

from __future__ import annotations

from src.core.target_ingest import TargetIngestor
from src.protocols.ghost_esp import GhostESPProtocol

_FLIPPER = """\
[0] White Flipper Found:
     MAC: AA:BB:CC:DD:EE:F0,
     Name: Flipper Zynq,
     RSSI: -60 dBm
"""

_AIRTAG = """\
[0] AirTag Found (Total: 3)
     MAC: AA:BB:CC:DD:EE:F1,
     RSSI: -55 dBm (Near),
"""


def _parse_all(text: str):
    proto = GhostESPProtocol()
    return [ev for line in text.splitlines() if (ev := proto.parse_line(line.strip())) is not None]


def test_flipper_scan_emits_one_ble_found_with_kind() -> None:
    events = _parse_all(_FLIPPER)
    assert [e.event_type for e in events] == ["ble_found"]   # one event, no `info` pollution
    d = events[0].data
    assert d["kind"] == "flipper"
    assert d["flipper_type"] == "White"
    assert d["mac"] == "AA:BB:CC:DD:EE:F0"
    assert d["name"] == "Flipper Zynq"                       # the Flipper's advertised name
    assert d["rssi"] == -60
    assert d["index"] == 0


def test_airtag_scan_emits_one_ble_found_with_kind() -> None:
    events = _parse_all(_AIRTAG)
    assert [e.event_type for e in events] == ["ble_found"]
    d = events[0].data
    assert d["kind"] == "airtag"
    assert d["total"] == 3
    assert d["mac"] == "AA:BB:CC:DD:EE:F1"
    assert d["rssi"] == -55
    assert "name" not in d  # an AirTag advertises no name — not faked


def test_tracker_intermediate_lines_return_none() -> None:
    # Only the closing "RSSI: N dBm" line emits; the earlier lines must not become bogus `info`.
    proto = GhostESPProtocol()
    assert proto.parse_line("[0] Black Flipper Found:") is None
    assert proto.parse_line("MAC: AA:BB:CC:DD:EE:F0,") is None
    assert proto.parse_line("Name: Zero,") is None
    ev = proto.parse_line("RSSI: -70 dBm")
    assert ev is not None and ev.event_type == "ble_found" and ev.data["flipper_type"] == "Black"


def test_tracker_without_mac_does_not_emit() -> None:
    # A truncated record missing the MAC line must be dropped, not emitted as an empty BLE target.
    proto = GhostESPProtocol()
    proto.parse_line("[0] AirTag Found (Total: 1)")
    assert proto.parse_line("RSSI: -40 dBm (Far),") is None


def test_tracker_resolves_end_to_end_to_a_ble_target() -> None:
    # real parser -> TargetIngestor._event_to_target: a detected Flipper becomes a BLE Target.
    ev = _parse_all(_FLIPPER)[0]
    t = TargetIngestor._event_to_target(ev, "COM4")
    assert t is not None
    assert t.mac == "AA:BB:CC:DD:EE:F0"
    assert t.ssid == "Flipper Zynq"      # the advertised name carries onto the target
    assert t.rssi == -60


def test_normal_ble_device_still_parses_and_trackers_dont_shadow_it() -> None:
    # The pre-existing "BLE Device: <mac> Name: <n> RSSI: <r>" path is untouched by the trackers.
    proto = GhostESPProtocol()
    ev = proto.parse_line("BLE Device: 11:22:33:44:55:66 Name: Watch RSSI: -48")
    assert ev is not None and ev.event_type == "ble_found"
    assert ev.data["mac"] == "11:22:33:44:55:66" and "kind" not in ev.data
