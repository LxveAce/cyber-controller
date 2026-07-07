"""Regression guard: malformed, attacker-controllable serial coords must not crash the parser.

A device fully controls its own serial output, so a malicious or buggy GhostESP-/Flipper-class
device can stream a line whose numeric field is *shaped* like a number but is not a valid float
("1.2.3", ".", "-", "-."). The parsers used over-permissive regex character classes ([\\d.\\-] /
-?[\\d.]) that matched these and then fed them straight into an unguarded float()/int(float()),
raising ValueError out of ``parse_line``. Tightening the captures to a real float shape
(-?\\d+(?:\\.\\d+)?) makes malformed input fall through gracefully instead of raising — no crash,
no per-line traceback amplification in the ingest log.
"""

from __future__ import annotations

import pytest

from src.protocols.base import ParsedEvent
from src.protocols.ghost_esp import GhostESPProtocol
from src.protocols.flipper import FlipperProtocol


# ── GhostESP GPS ─────────────────────────────────────────────────────

_MALFORMED_GPS = [
    "GPS: Lat=1.2.3 Lon=0",   # multi-dot
    "GPS: Lat=. Lon=.",       # dot only
    "GPS: Lat=- Lon=-",       # sign only
    "GPS: Lat=1-2 Lon=3",     # embedded sign
]


@pytest.mark.parametrize("line", _MALFORMED_GPS)
def test_malformed_gps_does_not_raise(line: str) -> None:
    # Pre-fix this raised ValueError out of parse_line (float("1.2.3") etc.).
    ev = GhostESPProtocol().parse_line(line)
    assert isinstance(ev, ParsedEvent)
    # A non-float coord no longer matches the GPS pattern, so it degrades to a generic info event
    # rather than being emitted as a (broken) gps_fix or crashing the read path.
    assert ev.event_type != "gps_fix"
    assert ev.event_type == "info"


def test_valid_gps_still_parses_to_floats() -> None:
    ev = GhostESPProtocol().parse_line("GPS: Lat=37.7749 Lon=-122.4194")
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type == "gps_fix"
    assert ev.data == {"lat": 37.7749, "lon": -122.4194}


# ── Flipper SubGHz RSSI ──────────────────────────────────────────────

_SUBGHZ_BASE = "SubGhz: Protocol: Princeton | Bit: 24 | Key: 0x001234 | Freq: 433.92 MHz | RSSI: "

_MALFORMED_RSSI = ["1.2.3", ".", "-."]


@pytest.mark.parametrize("rssi", _MALFORMED_RSSI)
def test_malformed_subghz_rssi_does_not_raise(rssi: str) -> None:
    # Pre-fix this raised ValueError out of parse_line (int(float("1.2.3")) etc.).
    ev = FlipperProtocol().parse_line(_SUBGHZ_BASE + rssi)
    assert isinstance(ev, ParsedEvent)
    # The signal is still reported; only the unparseable RSSI degrades. Any rssi that does survive
    # must be a real int (never a crash, never a raw string).
    assert ev.event_type == "subghz_found"
    if "rssi" in ev.data:
        assert isinstance(ev.data["rssi"], int)


def test_valid_subghz_rssi_still_parses() -> None:
    ev = FlipperProtocol().parse_line(_SUBGHZ_BASE + "-40.5")
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type == "subghz_found"
    assert ev.data["protocol"] == "Princeton"
    assert ev.data["rssi"] == -40
