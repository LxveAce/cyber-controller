"""GhostESP-Revival station (client) scan — `scansta` / `list -s` — parser coverage.

GhostESP streams each associated STATION as five consecutive lines, exactly like its multi-line AP
scan, but the parser previously had no station handler so every station GhostESP found fell through
to a generic `info` event (the client never reached TargetIngestor -> TargetPool). This adds the
`client_found` parity Marauder already has.

The format is grounded in the firmware's OWN source, not guessed:
  * `main/scans/wifi/station_scan.c` glog:
        "[%d] Station MAC: %s,\n  Station Vendor: %s,\n  Associated AP: %s,\n"
        "  AP BSSID: %s,\n  AP Vendor: %s\n"   (non-indexed live form: "Station:" / "STA Vendor:")
  * `webui/src/parsers.js` STATION_* patterns (the firmware's own web UI consumer).
Field names + line order below match that source verbatim; leading indentation is `.strip()`'d by
parse_line, the same as the AP multi-line path.
"""

from __future__ import annotations

from src.core.target_ingest import TargetIngestor
from src.protocols.ghost_esp import GhostESPProtocol

# ── station block in the firmware's exact five-line shape (indexed `list -s` form) ────────────
_STA_SCAN = """\
[0] Station MAC: AA:BB:CC:DD:EE:F0,
     Station Vendor: Apple,
     Associated AP: HomeNet,
     AP BSSID: B4:BF:E9:11:19:AD,
     AP Vendor: Espressif
[1] Station MAC: AA:BB:CC:DD:EE:F1,
     Station Vendor: Samsung,
     Associated AP: HomeNet,
     AP BSSID: B4:BF:E9:11:19:AD,
     AP Vendor: Espressif
"""


def _parse_all(text: str):
    proto = GhostESPProtocol()
    events = []
    for line in text.splitlines():
        ev = proto.parse_line(line.strip())
        if ev is not None:
            events.append(ev)
    return events


def test_station_scan_emits_one_client_found_per_station() -> None:
    clients = [e for e in _parse_all(_STA_SCAN) if e.event_type == "client_found"]
    assert len(clients) == 2, "expected one client_found per station block"


def test_station_fields_are_parsed_correctly() -> None:
    clients = [e for e in _parse_all(_STA_SCAN) if e.event_type == "client_found"]
    first = clients[0].data
    assert first["client_mac"] == "AA:BB:CC:DD:EE:F0"
    assert first["vendor"] == "Apple"
    assert first["ap_ssid"] == "HomeNet"
    assert first["ap_mac"] == "B4:BF:E9:11:19:AD"   # Marauder-compatible key for downstream ingest
    assert first["ap_vendor"] == "Espressif"
    assert first["index"] == 0                      # the device's own [idx]
    assert clients[1].data["client_mac"] == "AA:BB:CC:DD:EE:F1"
    assert clients[1].data["index"] == 1


def test_intermediate_lines_return_none_emit_on_closing_ap_vendor() -> None:
    # Only the closing "AP Vendor" line emits; the four earlier lines must not fall through to info.
    proto = GhostESPProtocol()
    assert proto.parse_line("[0] Station MAC: AA:BB:CC:DD:EE:F0,") is None
    assert proto.parse_line("Station Vendor: Apple,") is None
    assert proto.parse_line("Associated AP: HomeNet,") is None
    assert proto.parse_line("AP BSSID: B4:BF:E9:11:19:AD,") is None
    ev = proto.parse_line("AP Vendor: Espressif")
    assert ev is not None and ev.event_type == "client_found"
    assert ev.data["ap_mac"] == "B4:BF:E9:11:19:AD"


def test_non_indexed_live_station_form() -> None:
    # The live-discovery form has no [idx] and uses "Station:" / "STA Vendor:".
    proto = GhostESPProtocol()
    assert proto.parse_line("Station: 12:34:56:78:9A:BC,") is None
    assert proto.parse_line("STA Vendor: Google,") is None
    assert proto.parse_line("Associated AP: Cafe,") is None
    assert proto.parse_line("AP BSSID: 00:11:22:33:44:55,") is None
    ev = proto.parse_line("AP Vendor: Cisco")
    assert ev is not None and ev.event_type == "client_found"
    assert ev.data["client_mac"] == "12:34:56:78:9A:BC"
    assert "index" not in ev.data  # no fabricated ordinal when the device gave none


def test_station_does_not_collide_with_ap_multiline() -> None:
    # A station block and an AP block back-to-back must each emit their own event type — the two
    # accumulators key off distinct line shapes ("Station MAC:"/"AP BSSID:" vs "^SSID:"/"^BSSID:").
    proto = GhostESPProtocol()
    mixed = [
        "[0] SSID: MyNet,", "BSSID: B4:BF:E9:11:19:AD,", "RSSI: -21,", "Channel: 1,",
        "[0] Station MAC: AA:BB:CC:DD:EE:F0,", "Station Vendor: Apple,",
        "Associated AP: MyNet,", "AP BSSID: B4:BF:E9:11:19:AD,", "AP Vendor: Espressif",
    ]
    kinds = [ev.event_type for ln in mixed if (ev := proto.parse_line(ln)) is not None]
    assert kinds == ["ap_found", "client_found"]


def test_station_resolves_end_to_end_to_a_client_target() -> None:
    # real parser -> TargetIngestor._event_to_target: the station must become a CLIENT Target.
    ev = next(e for e in _parse_all(_STA_SCAN) if e.event_type == "client_found")
    t = TargetIngestor._event_to_target(ev, "COM4")
    assert t is not None
    assert t.mac == "AA:BB:CC:DD:EE:F0"
    assert t.extra.get("index") == 0
