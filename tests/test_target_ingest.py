"""Full cross-comm loop test: a device's serial scan line -> protocol parser -> TargetIngestor ->
TargetPool -> 'target.added' -> AutoRouter -> a command routed to ANOTHER device. This closes the
"one device gets an AP, another executes on it" path with the REAL parser, pool, ingestor, and router.
"""
from __future__ import annotations

from src.core.cross_comm import AutoRouter, EventBus, RoutingRule, TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import TargetType
from src.protocols import get_protocol

# A representative Marauder AP scan line (matches src/protocols/marauder.py _RE_AP — note "Ch:" case).
_AP_LINE = "AP: MyLab BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42"


class _FakeConn:
    """Minimal stand-in for SerialConnection: records on_line callbacks and lets the test feed lines."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


def _wire():
    bus = EventBus()
    pool = TargetPool(bus)
    routed: list[tuple[str, str]] = []
    router = AutoRouter(bus, lambda port, cmd: routed.append((port, cmd)))
    router.add_rule(RoutingRule(
        name="ap-to-deviceB", target_type=TargetType.AP, ssid_pattern="lab", min_rssi=-90,
        command_template="attack {mac} ch {channel}", device_port="COM_B", cooldown=0.0, enabled=True,
    ))
    ingest = TargetIngestor(pool)
    conn = _FakeConn("COM_A")
    ingest.attach(conn, get_protocol("marauder"))
    return conn, pool, routed


def test_parser_line_actually_matches():
    # Guard: confirm the test fixture line parses to an ap_found (so a regex drift fails loudly here).
    ev = get_protocol("marauder").parse_line(_AP_LINE)
    assert ev is not None and ev.event_type == "ap_found", ev
    assert ev.data["bssid"] == "DE:AD:BE:EF:00:11" and ev.data["channel"] == 6


def test_full_loop_serial_to_cross_device_command():
    conn, pool, routed = _wire()
    conn.feed(_AP_LINE)  # device A (COM_A) reports an AP over serial
    assert len(routed) == 1, f"expected exactly one routed command, got {routed}"
    port, cmd = routed[0]
    assert port == "COM_B"
    assert "de:ad:be:ef:00:11" in cmd.lower() and "ch 6" in cmd.lower(), cmd


def test_ap_landed_in_pool():
    conn, pool, _ = _wire()
    conn.feed(_AP_LINE)
    macs = [t.mac.lower() for t in pool.all()]
    assert "de:ad:be:ef:00:11" in macs


def test_non_matching_line_is_ignored():
    conn, _, routed = _wire()
    conn.feed("Scan complete")        # not an AP/client line
    conn.feed("> ready")
    assert routed == []
