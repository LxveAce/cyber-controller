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
    # Deferred-emit AP path: each AP flushes on the NEXT `[n] SSID` (or next non-AP line); the LAST
    # AP has nothing after it in a bare capture, so flush() at end-of-stream emits it.
    final = proto.flush()
    if final is not None:
        events.append(final)
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


def test_intermediate_lines_return_none_and_channel_defers() -> None:
    # Every line returns None — including Channel, which used to emit but now DEFERS so the trailing
    # Security/Band/Vendor lines can land. The AP emits on flush() (or the next SSID).
    proto = GhostESPProtocol()
    assert proto.parse_line("[0] SSID: Net,") is None
    assert proto.parse_line("BSSID: AA:BB:CC:DD:EE:FF,") is None
    assert proto.parse_line("RSSI: -30,") is None
    assert proto.parse_line("Channel: 6,") is None   # deferred — Channel no longer emits itself
    ev = proto.flush()                               # end-of-stream flush emits the record
    assert ev is not None and ev.event_type == "ap_found"
    assert ev.data["bssid"] == "AA:BB:CC:DD:EE:FF"


def test_record_without_bssid_does_not_emit() -> None:
    # A malformed block missing the BSSID line must be dropped, not emitted empty — even at flush()
    # (the deferred path's end-of-stream drain).
    proto = GhostESPProtocol()
    proto.parse_line("[0] SSID: Broken,")
    proto.parse_line("RSSI: -40,")
    assert proto.parse_line("Channel: 3,") is None
    assert proto.flush() is None


def test_device_indexed_multiline_does_not_touch_the_ordinal_state() -> None:
    # GHOSTESP-MLINE-INDEX-0713: the device supplies its own [idx], so parsing a multi-line AP must
    # NOT call the mutating _assign_ap_index fallback. Emitting index=5 while _ap_index quietly
    # advanced to 1 and _ap_indices got a hub ordinal is a select -a mis-bind hazard.
    proto = GhostESPProtocol()
    proto.parse_line("[5] SSID: DeviceIndexed,")
    proto.parse_line("BSSID: B4:BF:E9:11:19:AD,")
    proto.parse_line("RSSI: -30,")
    assert proto.parse_line("Channel: 6,") is None   # deferred
    ev = proto.flush()
    assert ev is not None and ev.data["index"] == 5  # the device's own [idx], carried through
    assert proto._ap_index == 0, "fallback ordinal counter must not advance for device-indexed APs"
    assert proto._ap_indices == {}, "no bssid->ordinal pollution when the device gave an index"


def test_multiline_target_resolves_end_to_end() -> None:
    # real parser -> TargetIngestor._event_to_target: the multi-line AP must become a usable Target.
    ev = next(e for e in _parse_all(_REAL_SCAN) if e.event_type == "ap_found")
    t = TargetIngestor._event_to_target(ev, "COM4")
    assert t is not None
    assert t.mac == "B4:BF:E9:11:19:AD"
    assert t.extra.get("index") == 0


def test_plain_ap_content_is_unchanged_no_trailing_fields() -> None:
    # GUARDRAIL 1 (no regression): the plain-ESP32 path (SSID/BSSID/RSSI/Channel, no trailing fields)
    # emits the SAME ap_found CONTENT it always did — only the emit TIMING deferred. Two APs -> two
    # events; the second flushes via flush().
    proto = GhostESPProtocol()
    evs = []
    for ln in ["[0] SSID: A,", "BSSID: 11:11:11:11:11:11,", "RSSI: -20,", "Channel: 1,",
               "[1] SSID: B,", "BSSID: 22:22:22:22:22:22,", "RSSI: -30,", "Channel: 6,"]:
        e = proto.parse_line(ln)
        if e:
            evs.append(e)
    e = proto.flush()
    if e:
        evs.append(e)
    assert [x.event_type for x in evs] == ["ap_found", "ap_found"]
    assert evs[0].data == {"ssid": "A", "bssid": "11:11:11:11:11:11", "channel": 1, "rssi": -20, "index": 0}
    assert evs[1].data == {"ssid": "B", "bssid": "22:22:22:22:22:22", "channel": 6, "rssi": -30, "index": 1}
    assert "encryption" not in evs[0].data   # no trailing fields -> no extra keys (content byte-identical)


def test_ap_security_and_trailing_fields_captured() -> None:
    # GhostESP-Revival prints Security/PMF/Band/Vendor AFTER the Channel line (ap_scan.c glog). The
    # deferred accumulator now captures them onto the ap_found. GROUNDED IN FIRMWARE SOURCE, UNVERIFIED
    # AGAINST HARDWARE (Security is C5/C6-only in the main path; final HIL is owner/hardware-gated).
    proto = GhostESPProtocol()
    for ln in ["[0] SSID: SecureNet,", "BSSID: B4:BF:E9:11:19:AD,", "RSSI: -40,", "Channel: 6,",
               "Band: 2.4GHz,", "Security: WPA2", "PMF: Optional", "Vendor: Espressif"]:
        assert proto.parse_line(ln) is None   # every line deferred / captured, none leaks a bogus info
    ev = proto.flush()
    assert ev is not None and ev.event_type == "ap_found"
    d = ev.data
    assert d["bssid"] == "B4:BF:E9:11:19:AD" and d["channel"] == 6 and d["rssi"] == -40  # unchanged
    assert d["encryption"] == "WPA2"   # the whole point: the AP encryption now reaches ap_found
    assert d["band"] == "2.4GHz" and d["pmf"] == "Optional" and d["vendor"] == "Espressif"


def test_ap_security_feeds_the_wifi_analyzer_grade() -> None:
    # end-to-end: the encryption on the ap_found feeds the WiFi analyzer's security_grade (WPA2 -> strong),
    # which is the whole reason to capture it.
    from src.core.wifi_analyzer import security_grade
    proto = GhostESPProtocol()
    for ln in ["[0] SSID: Sec,", "BSSID: AA:AA:AA:AA:AA:AA,", "RSSI: -50,", "Channel: 1,", "Security: WPA2"]:
        proto.parse_line(ln)
    ev = proto.flush()
    assert security_grade(ev.data["encryption"]) == "strong"
