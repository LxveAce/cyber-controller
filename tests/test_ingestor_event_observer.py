"""TargetIngestor.add_event_observer — the full-parsed-event tap the BLE-analyzer view feeds from.

The pool's ble_found->Target keeps only mac/name/rssi and reads the mac key, so it silently drops
LxveOS BLE adverts (keyed addr) and their tracker/company fields. The observer hook hands a view the
whole ParsedEvent instead, and must never let an observer's failure break serial ingestion.
"""
from __future__ import annotations

from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import TargetType
from src.protocols import get_protocol


class _FakeConn:
    """Minimal SerialConnection stand-in: records on_line callbacks, lets the test feed lines."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


# LxveOS bridge BLE event — addr-keyed, with tracker/company the pool Target drops.
_LXVEOS_BLE = (
    "LXVEOS/1 ble addr=66:55:44:33:22:11 type=random rssi=-55 name=4d79 company=76 tracker=1"
)
# Marauder BLE scan line — mac-keyed.
_MARAUDER_BLE = "BLE: 12:34:56:78:9a:bc Name: Fitbit RSSI: -40"


def test_observer_receives_full_parsed_events_incl_fields_pool_drops():
    bus = EventBus()
    ingest = TargetIngestor(TargetPool(bus))
    seen: list[tuple] = []
    ingest.add_event_observer(lambda ev, port: seen.append((ev.event_type, port, dict(ev.data))))

    lx = _FakeConn("COM23")
    ingest.attach(lx, get_protocol("lxveos"))
    lx.feed(_LXVEOS_BLE)

    ble = [(t, p, d) for (t, p, d) in seen if t == "ble_found"]
    assert len(ble) == 1
    _t, port, data = ble[0]
    assert port == "COM23"
    # The observer sees the FULL event: addr key + tracker + company — what the pool Target loses.
    # (The LxveOS parser types tracker/company as ints; the model stringifies company downstream.)
    assert data.get("addr") == "66:55:44:33:22:11"
    assert data.get("tracker") == 1 and data.get("company") == 76
    assert data.get("rssi") == -55 and data.get("name") == "My"  # 4d79 -> "My"


def test_observer_fires_for_a_second_firmware():
    bus = EventBus()
    ingest = TargetIngestor(TargetPool(bus))
    seen: list = []
    ingest.add_event_observer(lambda ev, port: seen.append(ev))

    conn = _FakeConn("COM4")
    ingest.attach(conn, get_protocol("marauder"))
    conn.feed(_MARAUDER_BLE)

    ble = [ev for ev in seen if ev.event_type == "ble_found"]
    assert len(ble) == 1 and ble[0].data.get("mac") == "12:34:56:78:9a:bc"
    assert ble[0].data.get("rssi") == -40


def test_observer_error_never_breaks_ingestion_or_routing():
    bus = EventBus()
    pool = TargetPool(bus)
    ingest = TargetIngestor(pool)
    good: list = []
    def _boom(ev, port):
        raise RuntimeError("boom")

    ingest.add_event_observer(_boom)                             # raises on every event
    ingest.add_event_observer(lambda ev, port: good.append(ev))  # must still run

    conn = _FakeConn("COM4")
    ingest.attach(conn, get_protocol("marauder"))
    conn.feed(_MARAUDER_BLE)

    # The raising observer is isolated: the second observer still fired...
    assert any(ev.event_type == "ble_found" for ev in good)
    # ...and routing was unaffected — the BLE target still reached the pool.
    assert any(t.target_type == TargetType.BLE for t in pool.all())
    # A later line is still ingested (the reader loop wasn't killed by the observer exception).
    conn.feed("BLE: aa:bb:cc:dd:ee:ff Name: Band RSSI: -60")
    assert len([ev for ev in good if ev.event_type == "ble_found"]) == 2


def test_remove_event_observer_stops_delivery():
    bus = EventBus()
    ingest = TargetIngestor(TargetPool(bus))
    seen: list = []
    cb = lambda ev, port: seen.append(ev)  # noqa: E731 — a named lambda is fine for a test handle
    ingest.add_event_observer(cb)
    ingest.remove_event_observer(cb)
    ingest.remove_event_observer(cb)  # second remove is a no-op, not an error

    conn = _FakeConn("COM4")
    ingest.attach(conn, get_protocol("marauder"))
    conn.feed(_MARAUDER_BLE)
    assert seen == []
