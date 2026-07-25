"""GhostESP handshake capture — `Handshake found!` → `handshake_captured` (Marauder parity).

On a successful EAPOL capture GhostESP's `main/core/callbacks.c` prints a three-line record:

    Handshake found!
    AP=<bssid>
    Pair=<pairwise>/<group>

The parser previously had no handler, so all three lines fell through to the generic `info` event —
the crackable-material capture never reached the shared CaptureStore. Adds the `handshake_captured`
parity Marauder already emits (grounded in the firmware's own callbacks.c glog + webui/parsers.js
`handshake(text)` patterns: /Handshake found/i + /AP=([0-9A-Fa-f:]{17})/i + /Pair=(\\S+)/i).
"""

from __future__ import annotations

from src.core.target_ingest import TargetIngestor
from src.protocols.ghost_esp import GhostESPProtocol

_HANDSHAKE = "Handshake found!\nAP=B4:BF:E9:11:19:AD\nPair=CCMP/CCMP\n"


def _parse_all(text: str):
    proto = GhostESPProtocol()
    return [ev for line in text.splitlines() if (ev := proto.parse_line(line.strip())) is not None]


def test_handshake_emits_one_event_on_the_ap_line() -> None:
    proto = GhostESPProtocol()
    assert proto.parse_line("Handshake found!") is None          # trigger — nothing yet
    ev = proto.parse_line("AP=B4:BF:E9:11:19:AD")                 # AP line — emits
    assert ev is not None and ev.event_type == "handshake_captured"
    assert ev.data["bssid"] == "B4:BF:E9:11:19:AD"
    assert ev.data["ap_mac"] == "B4:BF:E9:11:19:AD"
    assert proto.parse_line("Pair=CCMP/CCMP") is None             # trailing Pair line — swallowed


def test_handshake_does_not_leak_bogus_info_events() -> None:
    # All three lines must collapse to exactly ONE handshake_captured — no `info` pollution.
    events = _parse_all(_HANDSHAKE)
    assert [e.event_type for e in events] == ["handshake_captured"]


def test_stray_ap_line_without_trigger_is_not_a_handshake() -> None:
    # "AP=..." on its own (no preceding "Handshake found!") must NOT fabricate a handshake.
    proto = GhostESPProtocol()
    ev = proto.parse_line("AP=B4:BF:E9:11:19:AD")
    assert ev is not None and ev.event_type != "handshake_captured"


def test_handshake_abandons_cleanly_on_an_unexpected_line() -> None:
    # A trigger not followed by an AP line must not wedge the stage machine — a later capture works.
    proto = GhostESPProtocol()
    proto.parse_line("Handshake found!")
    proto.parse_line("...capture aborted...")                     # unexpected — abandon the stage
    proto.parse_line("Handshake found!")
    ev = proto.parse_line("AP=00:11:22:33:44:55")
    assert ev is not None and ev.event_type == "handshake_captured"
    assert ev.data["bssid"] == "00:11:22:33:44:55"


def test_handshake_resolves_end_to_end_to_an_eapol_capture_record() -> None:
    # real parser -> TargetIngestor._event_to_capture: a handshake becomes an EAPOL CaptureRecord.
    ev = next(e for e in _parse_all(_HANDSHAKE) if e.event_type == "handshake_captured")
    rec = TargetIngestor(pool=None)._event_to_capture(ev, "COM4")
    assert rec is not None
    assert rec.bssid == "B4:BF:E9:11:19:AD"
    assert rec.capture_type == "eapol"
