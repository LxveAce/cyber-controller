"""Regression guard for GhostESP-Revival's multi-line `scanap` output.

GhostESP-Revival streams each discovered AP as FOUR consecutive lines (SSID / BSSID / RSSI /
Channel) rather than the single pipe-delimited line the original ``_RE_AP`` pattern matched. The
old parser silently produced 0 ``ap_found`` events from real device output — the whole cross-comm
pipeline (TargetIngestor -> TargetPool -> OUI) went dark on this firmware.

The block below is a VERBATIM capture from real silicon: a GhostESP build flashed onto an ESP32 via
CC's FlashEngine (COM4), then ``scanap`` run. Feeding it back through the parser is the exact
verify-never-fake proof that the multi-line accumulator emits one ``ap_found`` per AP.
"""

from __future__ import annotations

from src.core.target_ingest import TargetIngestor
from src.protocols.ghost_esp import GhostESPProtocol

# ── verbatim COM4 capture (GhostESP flashed via CC FlashEngine, `scanap`) ────────────────────
_REAL_SCAN = """\
[0] SSID: ESP_1119AD,
BSSID: B4:BF:E9:11:19:AD,
RSSI: -21,
Channel: 1,
[1] SSID: SpectrumSetup-B566,
BSSID: 5C:FA:25:D6:21:D4,
RSSI: -43,
Channel: 11,
[2] SSID: KashPatels007,
BSSID: 90:D3:CF:3C:16:C1,
RSSI: -43,
Channel: 11,
[3] SSID: DIRECT-50-HP Smart Tank,
BSSID: 00:04:EA:7B:F7:EE,
RSSI: -48,
Channel: 6,
"""


def _parse_all(text: str):
    proto = GhostESPProtocol()
    events = []
    for line in text.splitlines():
        ev = proto.parse_line(line.strip())
        if ev is not None:
            events.append(ev)
    return events


def test_multiline_scan_emits_one_ap_found_per_ap() -> None:
    aps = [e for e in _parse_all(_REAL_SCAN) if e.event_type == "ap_found"]
    assert len(aps) == 4, "expected one ap_found per AP block in the real capture"


def test_multiline_fields_are_parsed_correctly() -> None:
    aps = [e for e in _parse_all(_REAL_SCAN) if e.event_type == "ap_found"]
    first = aps[0].data
    assert first["bssid"] == "B4:BF:E9:11:19:AD"
    assert first["ssid"] == "ESP_1119AD"
    assert first["channel"] == 1
    assert first["rssi"] == -21
    # the device's own [idx] is carried through for `select -a <idx>`
    assert first["index"] == 0
    assert aps[3].data["ssid"] == "DIRECT-50-HP Smart Tank"  # SSID with spaces survives intact
    assert aps[3].data["index"] == 3


def test_intermediate_lines_return_none() -> None:
    # BSSID / RSSI / Channel-without-a-record must not fall through to a bogus info/status event.
    proto = GhostESPProtocol()
    assert proto.parse_line("[0] SSID: Net,") is None
    assert proto.parse_line("BSSID: AA:BB:CC:DD:EE:FF,") is None
    assert proto.parse_line("RSSI: -30,") is None
    ev = proto.parse_line("Channel: 6,")  # closing line emits the record
    assert ev is not None and ev.event_type == "ap_found"


def test_record_without_bssid_does_not_emit() -> None:
    # A malformed block missing the BSSID line must be dropped, not emitted with an empty MAC.
    proto = GhostESPProtocol()
    proto.parse_line("[0] SSID: Broken,")
    proto.parse_line("RSSI: -40,")
    assert proto.parse_line("Channel: 3,") is None


def test_multiline_target_resolves_end_to_end() -> None:
    # real parser -> TargetIngestor._event_to_target: the multi-line AP must become a usable Target.
    ev = next(e for e in _parse_all(_REAL_SCAN) if e.event_type == "ap_found")
    t = TargetIngestor._event_to_target(ev, "COM4")
    assert t is not None
    assert t.mac == "B4:BF:E9:11:19:AD"
    assert t.extra.get("index") == 0
