"""BLE address identity and latest-report provenance, with inert discovery lines."""

from datetime import datetime, timedelta, timezone

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import Target, TargetType
from src.protocols import get_protocol
from src.protocols.base import ParsedEvent

UPPER = "02:AA:BB:CC:DD:EE"
LOWER = UPPER.lower()


class InertConnection:
    def __init__(self, port):
        self.port = port
        self.is_connected = True
        self.lines = []

    def on_line(self, callback):
        self.lines.append(callback)

    def remove_line_callback(self, callback):
        self.lines.remove(callback)

    def feed(self, line):
        for callback in tuple(self.lines):
            callback(line)

    def write(self, *_args, **_kwargs):
        pytest.fail("this test must not write to a device")


def test_two_sources_merge_address_casing_and_latest_signal():
    pool = TargetPool()
    ingestor = TargetIngestor(pool)
    a, b = InertConnection("INERT_A"), InertConnection("INERT_B")
    ingestor.attach(a, get_protocol("marauder"))
    ingestor.attach(b, get_protocol("marauder"))
    try:
        a.feed(f"BLE: {UPPER} Name: Watch RSSI: -80")
        b.feed(f"BLE: {LOWER} Name: Watch RSSI: -40")
        assert pool.count == 1
        row = pool.all()[0].to_dict()
        assert (row["mac"], row["rssi"], row["device_source"]) == (LOWER, -40, "INERT_B")
    finally:
        ingestor.close()


def test_case_normalized_model_roundtrip_and_pool_lookup_removal():
    target = Target(mac=UPPER, target_type=TargetType.BLE)
    assert target.mac == LOWER and target.key == "ble:" + LOWER
    restored = Target.from_dict(target.to_dict())
    assert restored.mac == LOWER and restored.key == target.key
    pool = TargetPool()
    pool.add(restored)
    assert pool.get("ble:" + UPPER).key == target.key
    assert pool.remove("ble:" + UPPER).mac == LOWER
    assert pool.count == 0


@pytest.mark.parametrize(
    "value",
    [
        "name-like",
        "02-AA-BB-CC-DD-EE",
        "02:AA:BB:CC:DD:E",
        "02:AA:BB:CC:DD:GG",
        " " + UPPER,
        UPPER + "\n",
    ],
)
def test_noncanonical_address_shapes_are_not_reinterpreted(value):
    target = Target(mac=value, target_type=TargetType.BLE)
    assert target.mac == value and target.key == "ble:" + value


@pytest.mark.parametrize("target_type", [TargetType.AP, TargetType.CLIENT, TargetType.NFC])
def test_other_target_identity_spelling_is_unchanged(target_type):
    target = Target(mac=UPPER, target_type=target_type)
    assert target.mac == UPPER and target.key == target_type.value + ":" + UPPER
    pool = TargetPool()
    pool.add(target)
    assert pool.get(target.key) is target
    assert pool.get(target_type.value + ":" + LOWER) is None


@pytest.mark.parametrize("latest_rssi", [-40, 0])
def test_latest_tuple_and_held_reference_are_coherent(latest_rssi):
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = first + timedelta(seconds=10)
    bus = EventBus()
    payloads = []
    bus.subscribe("target.updated", lambda _topic, payload: payloads.append(payload))
    pool = TargetPool(bus)
    pool.add(
        Target(
            mac=UPPER,
            target_type=TargetType.BLE,
            ssid="Remembered name",
            rssi=-80,
            device_source="INERT_A",
            timestamp=first,
            last_seen=first,
            extra={"index": 8, "address_type": "random"},
        )
    )
    held = pool.all()[0]
    incoming = Target(
        mac=UPPER,
        target_type=TargetType.BLE,
        rssi=latest_rssi,
        device_source="INERT_B",
        timestamp=later,
        last_seen=later,
        extra={"metadata": {"reported": "fresh"}},
    )
    assert not pool.add(incoming)
    latest = pool.get(incoming.key)
    assert (latest.rssi, latest.device_source, latest.last_seen) == (latest_rssi, "INERT_B", later)
    assert latest.timestamp == first and latest.ssid == "Remembered name"
    assert "index" not in latest.extra and "address_type" not in latest.extra
    assert (held.rssi, held.device_source, held.last_seen) == (-80, "INERT_A", first)
    incoming.extra["metadata"]["reported"] = "candidate changed"
    assert latest.extra["metadata"]["reported"] == "fresh"
    payloads[0]["extra"]["metadata"]["reported"] = "event changed"
    assert latest.extra["metadata"]["reported"] == "fresh"
    latest.to_dict()["extra"]["metadata"]["reported"] = "export changed"
    assert latest.extra["metadata"]["reported"] == "fresh"


def test_new_ble_record_and_event_do_not_share_candidate_metadata():
    pool = TargetPool()
    events = []
    pool.bus.subscribe("target.added", lambda _topic, data: events.append(data))
    candidate = Target(mac=UPPER, target_type=TargetType.BLE, extra={"nested": [1]})
    assert pool.add(candidate)
    candidate.extra["nested"].append(2)
    events[0]["extra"]["nested"].append(3)
    assert pool.get(candidate.key).extra == {"nested": [1]}


@pytest.mark.parametrize("target_type", [TargetType.AP, TargetType.CLIENT])
def test_indexed_source_pair_survives_unindexed_reports_and_changes_with_index(target_type):
    pool = TargetPool()
    key = target_type.value + ":" + UPPER
    pool.add(
        Target(
            mac=UPPER,
            target_type=target_type,
            rssi=-80,
            device_source="INERT_A",
            extra={"index": 3},
        )
    )
    held = pool.get(key)
    pool.add(Target(mac=UPPER, target_type=target_type, rssi=-40, device_source="INERT_B"))
    assert pool.get(key) is held
    assert (held.device_source, held.extra["index"], held.rssi) == ("INERT_A", 3, -40)
    pool.add(
        Target(
            mac=UPPER, target_type=target_type, rssi=0, device_source="INERT_C", extra={"index": 0}
        )
    )
    assert (held.device_source, held.extra["index"], held.rssi) == ("INERT_C", 0, -40)
    assert pool.invalidate_index("INERT_C", target_type) == 1
    assert "index" not in held.extra


@pytest.mark.parametrize(
    "address_type,retained",
    [
        ("random", True),
        ("public", True),
        ("x" * 32, True),
        ("x" * 33, False),
        ("", False),
        ("bad\nvalue", False),
        (None, False),
        ({"mutable": "random"}, False),
    ],
)
def test_only_explicit_bounded_address_type_metadata_is_retained(address_type, retained):
    event = ParsedEvent(
        "ble_found", {"addr": UPPER, "type": address_type, "name": "Watch", "rssi": -50}
    )
    target = TargetIngestor._event_to_target(event, "INERT")
    assert target.mac == LOWER
    assert target.extra == ({"address_type": address_type} if retained else {})


def test_lxveos_reported_type_clears_on_next_untyped_observation():
    pool = TargetPool()
    ingestor = TargetIngestor(pool)
    a, b = InertConnection("INERT_A"), InertConnection("INERT_B")
    ingestor.attach(a, get_protocol("lxveos"))
    ingestor.attach(b, get_protocol("marauder"))
    try:
        a.feed(f"LXVEOS/1 ble addr={UPPER} type=random rssi=-55 name=5761746368")
        assert pool.all()[0].extra == {"address_type": "random"}
        b.feed(f"BLE: {LOWER} Name: Watch RSSI: -60")
        assert pool.count == 1 and pool.all()[0].extra == {}
    finally:
        ingestor.close()


def test_mac_shaped_addressless_names_remain_unaddressable_reports():
    pool = TargetPool()
    ingestor = TargetIngestor(pool)
    wire = InertConnection("INERT")
    ingestor.attach(wire, get_protocol("marauder"))
    try:
        wire.feed(f"-60 Device: {UPPER}")
        wire.feed(f"[0][RSSI:-50] {LOWER}")
        reports = ingestor.ble_observations()
        assert pool.count == 0 and len(reports) == 2
        assert [report["label"] for report in reports] == [UPPER, LOWER]
        assert all(report["addressable"] is False for report in reports)
    finally:
        ingestor.close()
