"""Tests for ``src.protocols`` — the firmware serial-parser registry.

Covered (pure, no device, no heavy deps):
    * ``get_protocol`` returns the right class per firmware name, and an unknown
      name falls back to the generic passthrough;
    * each parser's ``parse_line`` on a representative sample line returns a
      ``ParsedEvent`` whose ``event_type`` matches the expected category.

The protocols package imports only the standard library, so no optional dep is
required; the ``importorskip`` is belt-and-suspenders.
"""

from __future__ import annotations

import pytest

protocols = pytest.importorskip("src.protocols")

from src.protocols.base import ParsedEvent  # noqa: E402  (after importorskip)
from src.protocols.marauder import MarauderProtocol  # noqa: E402
from src.protocols.ghost_esp import GhostESPProtocol  # noqa: E402
from src.protocols.bruce import BruceProtocol  # noqa: E402
from src.protocols.flipper import FlipperProtocol  # noqa: E402
from src.protocols.halehound import HaleHoundProtocol  # noqa: E402
from src.protocols.meshtastic import MeshtasticProtocol  # noqa: E402


# ── get_protocol routing ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "name, cls",
    [
        ("marauder", MarauderProtocol),
        ("ghost-esp", GhostESPProtocol),
        ("bruce", BruceProtocol),
        ("flipper", FlipperProtocol),
        ("halehound", HaleHoundProtocol),
        ("meshtastic", MeshtasticProtocol),
    ],
)
def test_get_protocol_returns_expected_class(name: str, cls: type) -> None:
    assert isinstance(protocols.get_protocol(name), cls)


def test_get_protocol_unknown_falls_back_to_generic() -> None:
    proto = protocols.get_protocol("no-such-firmware")
    assert isinstance(proto, protocols.GenericProtocol)
    assert proto.protocol_name == "generic"


def test_get_protocol_empty_falls_back_to_generic() -> None:
    assert isinstance(protocols.get_protocol(""), protocols.GenericProtocol)


def test_get_protocol_underscore_alias_resolves() -> None:
    # 'ghost_esp' should normalise to the 'ghost-esp' protocol.
    assert isinstance(protocols.get_protocol("ghost_esp"), GhostESPProtocol)


# ── parse_line on a representative sample per protocol ───────────────

# (firmware name, sample serial line, expected event_type)
_SAMPLES = [
    (
        "marauder",
        "AP: CoffeeShop BSSID: AA:BB:CC:DD:EE:FF Ch: 6 RSSI: -42",
        "ap_found",
    ),
    (
        "ghost-esp",
        "SSID: HomeNet | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -42",
        "ap_found",
    ),
    (
        "bruce",
        "[WIFI] AP: CoffeeShop | BSSID: AA:BB:CC:DD:EE:FF | CH: 1 | RSSI: -50 | AUTH: WPA2",
        "ap_found",
    ),
    (
        "flipper",
        "BT: Name: MyDevice | MAC: AA:BB:CC:DD:EE:FF | RSSI: -55",
        "ble_found",
    ),
    (
        "halehound",
        "[GUARDIAN] ROGUE AP: EvilTwin | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -30",
        "rogue_ap",
    ),
    (
        "meshtastic",
        "Node: !a1b2c3d4 | Name: BaseCamp | SNR: 9.5 | Battery: 92%",
        "info",
    ),
]


@pytest.mark.parametrize(
    "name, line, expected_type",
    _SAMPLES,
    ids=[s[0] for s in _SAMPLES],
)
def test_parse_line_event_type(name: str, line: str, expected_type: str) -> None:
    proto = protocols.get_protocol(name)
    event = proto.parse_line(line)
    assert isinstance(event, ParsedEvent)
    assert event.event_type == expected_type
    # The original line is always preserved on the event.
    assert event.raw == line


def test_parse_line_empty_returns_none() -> None:
    # Every parser treats blank input as noise (None), not an event.
    for name, _line, _t in _SAMPLES:
        assert protocols.get_protocol(name).parse_line("   ") is None


def test_generic_parse_line_emits_info() -> None:
    event = protocols.get_protocol("raw").parse_line("anything at all")
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "info"
