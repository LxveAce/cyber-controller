"""A bounded observation log must not confer a device address or action identity."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.protocols.base import ParsedEvent
from src.protocols.marauder import MarauderProtocol


class FakeConnection:
    def __init__(self, port):
        self.port = port
        self.callbacks = []

    def on_line(self, callback):
        self.callbacks.append(callback)

    def remove_line_callback(self, callback):
        self.callbacks.remove(callback)

    def feed(self, line):
        for callback in list(self.callbacks):
            callback(line)


def wired():
    pool = TargetPool(EventBus())
    ingestor = TargetIngestor(pool)
    connection = FakeConnection("SYNTHETIC_A")
    ingestor.attach(connection, MarauderProtocol())
    return ingestor, connection, pool


def test_repeated_names_and_mac_shaped_names_are_distinct_nonaddressable_observations():
    ingestor, connection, pool = wired()
    for line in ("-60 Device: Watch", "-61 Device: Watch", "-62 Device: 02:00:00:00:00:01"):
        connection.feed(line)
    rows = ingestor.ble_observations()
    assert len(rows) == 3 and len({row["observation_id"] for row in rows}) == 3
    assert [row["label"] for row in rows] == ["Watch", "Watch", "02:00:00:00:00:01"]
    assert all(row["addressable"] is False for row in rows)
    assert all("mac" not in row and "addr" not in row for row in rows)
    assert pool.count == 0


def test_snapshot_is_detached_and_bounded_to_latest_200_records():
    ingestor, connection, pool = wired()
    for index in range(213):
        connection.feed(f"-60 Device: Label {index}")
    first = ingestor.ble_observations()
    assert len(first) == 200
    assert first[0]["label"] == "Label 13" and first[-1]["label"] == "Label 212"
    first[0]["label"] = "mutated outside"
    first.clear()
    assert ingestor.ble_observations()[0]["label"] == "Label 13"
    assert pool.count == 0


def test_connection_and_command_epochs_are_provenance_only_and_stale_callback_drops():
    ingestor, connection, pool = wired()
    old_callback = connection.callbacks[0]
    connection.feed("[0][RSSI:-60] Watch")
    ingestor.note_command_sent(connection.port, "sniffbt")
    connection.feed("[0][RSSI:-61] Watch")
    rows = ingestor.ble_observations()
    assert rows[0]["connection_epoch"] == rows[1]["connection_epoch"]
    assert rows[0]["scan_epoch"] != rows[1]["scan_epoch"]
    assert rows[0]["reported_index"] == rows[1]["reported_index"] == 0
    ingestor.detach(connection)
    ingestor.attach(connection, MarauderProtocol())
    old_callback("[0][RSSI:-62] stale callback")
    connection.feed("[0][RSSI:-63] Watch")
    rows = ingestor.ble_observations()
    assert len(rows) == 3
    assert rows[-1]["connection_epoch"] != rows[1]["connection_epoch"]
    assert rows[-1]["scan_epoch"] != rows[1]["scan_epoch"]
    assert pool.count == 0


@pytest.mark.parametrize("command", ["clearlist -b", "reboot", "sniffbt -t airtag"])
def test_relevant_sent_command_advances_local_observation_epoch(command):
    ingestor, connection, _ = wired()
    connection.feed("-60 Device: Watch")
    before = ingestor.ble_observations()[-1]
    ingestor.note_command_sent(connection.port, command)
    connection.feed("-61 Device: Watch")
    after = ingestor.ble_observations()[-1]
    assert before["scan_epoch"] != after["scan_epoch"]
    assert before["connection_epoch"] == after["connection_epoch"]


@pytest.mark.parametrize("command", ["list -b", "stopscan", "clearlist -a"])
def test_unrelated_or_read_only_commands_do_not_invent_new_scan_epochs(command):
    ingestor, connection, _ = wired()
    connection.feed("-60 Device: Watch")
    before = ingestor.ble_observations()[-1]["scan_epoch"]
    ingestor.note_command_sent(connection.port, command)
    connection.feed("-61 Device: Watch")
    assert ingestor.ble_observations()[-1]["scan_epoch"] == before


def test_explicit_address_still_pools_and_observation_payload_cannot_smuggle_address():
    ingestor, connection, pool = wired()
    connection.feed("BLE: 02:00:00:00:00:01 Name: Watch RSSI: -60")
    assert pool.count == 1 and pool.all()[0].mac == "02:00:00:00:00:01"
    assert ingestor.ble_observations() == []
    event = ParsedEvent("ble_observation", {
        "mac": "02:00:00:00:00:02", "name": "injected", "rssi": -60,
    })
    assert ingestor._event_to_target(event, connection.port) is None
    assert ingestor._event_to_capture(event, connection.port) is None


def test_parallel_sources_produce_unique_bounded_observations_without_merging():
    ingestor, first, pool = wired()
    second = FakeConnection("SYNTHETIC_B")
    ingestor.attach(second, MarauderProtocol())
    ready = threading.Barrier(2)

    def feed(connection):
        ready.wait(timeout=3)
        for _ in range(125):
            connection.feed("[0][RSSI:-60] Same Name")

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(feed, connection) for connection in (first, second)]
        for future in futures:
            future.result(timeout=5)
    rows = ingestor.ble_observations()
    assert len(rows) == 200 and len({row["observation_id"] for row in rows}) == 200
    assert pool.count == 0


def test_observer_mutation_cannot_change_already_retained_observation():
    ingestor, connection, _ = wired()
    ingestor.add_event_observer(lambda ev, port: ev.data.update(label="external mutation"))
    connection.feed("-60 Device: Original")
    assert ingestor.ble_observations()[0]["label"] == "Original"


def test_replacement_connection_ignores_old_detach_and_old_observation_callback():
    ingestor, original, pool = wired()
    old_callback = original.callbacks[0]
    replacement = FakeConnection(original.port)
    ingestor.attach(replacement, MarauderProtocol())
    assert original.callbacks == []
    observed = []
    ingestor.add_event_observer(lambda event, port: observed.append(event.data["label"]))
    ingestor.detach(original)
    old_callback("-60 Device: Old")
    replacement.feed("-61 Device: New")
    assert observed == ["New"]
    assert ingestor.ble_observations()[0]["label"] == "New"
    assert ingestor.parser_for(replacement.port) is not None
    assert pool.count == 0


@pytest.mark.parametrize("field,value", [
    ("label", "x" * 257), ("label", []), ("rssi", True), ("rssi", -129),
    ("rssi", float("nan")), ("addressable", 0), ("reported_index", True),
    ("format", "other"), ("label_truncated", 1),
])
def test_malformed_observation_is_not_retained_or_forwarded(field, value):
    pool = TargetPool(EventBus())
    ingestor = TargetIngestor(pool)
    connection = FakeConnection("SYNTHETIC_BAD")
    data = {"label": "Label", "rssi": -60, "reported_index": None, "format": "live",
            "label_truncated": False, "addressable": False}
    data[field] = value

    class FixtureProtocol:
        def parse_line(self, line):
            return ParsedEvent("ble_observation", data)

    ingestor.attach(connection, FixtureProtocol())
    observed = []
    ingestor.add_event_observer(lambda *args: observed.append(args))
    connection.feed("synthetic input")
    assert ingestor.ble_observations() == [] and observed == [] and pool.count == 0
