"""CaptureStore + CaptureRecord (punch-list #2, slice 1): dedup by ``capture_type:bssid``, the
``capture.added`` / ``updated`` / ``removed`` / ``cleared`` / ``cracked`` EventBus topics mirroring
``target.*``, latest-wins upsert, and ``to_dict`` / ``from_dict`` round-trips. No Qt / no
hardware — pure model + store."""

from __future__ import annotations

from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus
from src.models.capture import CaptureRecord


def _bus_recorder(bus: EventBus) -> list[tuple[str, dict]]:
    """Record every capture.* event the store publishes, as (topic, payload)."""
    events: list[tuple[str, dict]] = []
    for topic in ("capture.added", "capture.updated", "capture.removed",
                  "capture.cleared", "capture.cracked"):
        bus.subscribe(topic, lambda t, p: events.append((t, p)))
    return events


def test_record_key_is_type_and_lowercased_bssid():
    r = CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="pmkid")
    assert r.key == "pmkid:aa:bb:cc:dd:ee:ff"


def test_record_roundtrips_through_dict():
    r = CaptureRecord(
        bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol", ssid="HomeNet", channel=6,
        sta_mac="11:22:33:44:55:66", key_version=2, rssi=-40, gps_lat=45.5, gps_lon=-122.6,
        device_source="COM7", firmware="marauder", pcap_path="/sd/hs.pcapng", hashes_extracted=1,
    )
    d = r.to_dict()
    r2 = CaptureRecord.from_dict(d)
    assert r2.to_dict() == d                       # lossless round-trip
    assert r2.captured_at == r.captured_at and r2.last_seen == r.last_seen
    assert r2.key == r.key


def test_add_new_publishes_capture_added_once():
    bus = EventBus()
    events = _bus_recorder(bus)
    store = CaptureStore(bus)
    assert store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol")) is True
    assert store.count == 1
    added = [e for e in events if e[0] == "capture.added"]
    assert len(added) == 1 and added[0][1]["bssid"] == "aa:bb:cc:dd:ee:ff"


def test_duplicate_key_upserts_not_duplicates():
    bus = EventBus()
    events = _bus_recorder(bus)
    store = CaptureStore(bus)
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol"))   # mixed case
    # Same key (case-insensitive), now carrying the SSID + an on-SD pcap path.
    is_new = store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol",
                                     ssid="HomeNet", pcap_path="/sd/hs.pcapng"))
    assert is_new is False                          # updated, not added
    assert store.count == 1                         # one row, not two
    rec = store.get("eapol:aa:bb:cc:dd:ee:ff")
    assert rec.times_seen == 2                      # bumped
    assert rec.ssid == "HomeNet"                    # latest-wins on a newly-known field
    assert rec.pcap_path == "/sd/hs.pcapng"
    assert [e[0] for e in events] == ["capture.added", "capture.updated"]


def test_update_never_clobbers_known_value_with_empty():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol",
                            ssid="HomeNet", channel=6))
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol"))   # bare re-observe
    rec = store.get("eapol:aa:bb:cc:dd:ee:ff")
    assert rec.ssid == "HomeNet" and rec.channel == 6   # known values retained


def test_different_capture_types_same_bssid_are_distinct():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="pmkid"))
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol"))
    assert store.count == 2   # a PMKID and an EAPOL capture of the same AP are separate rows


def test_remove_and_clear_publish_and_empty():
    bus = EventBus()
    events = _bus_recorder(bus)
    store = CaptureStore(bus)
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol"))
    assert store.remove("eapol:aa:bb:cc:dd:ee:ff") is not None and store.count == 0
    store.add(CaptureRecord(bssid="11:22:33:44:55:66", capture_type="pmkid"))
    assert store.clear() == 1 and store.count == 0
    topics = [e[0] for e in events]
    assert "capture.removed" in topics and "capture.cleared" in topics


def test_mark_cracked_flips_and_publishes():
    bus = EventBus()
    events = _bus_recorder(bus)
    store = CaptureStore(bus)
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="pmkid"))
    assert store.mark_cracked("pmkid:aa:bb:cc:dd:ee:ff", "hunter2", detail="in wordlist") is True
    rec = store.get("pmkid:aa:bb:cc:dd:ee:ff")
    assert rec.crack_status == "cracked" and rec.password == "hunter2"
    cracked = [e for e in events if e[0] == "capture.cracked"]
    assert len(cracked) == 1 and cracked[0][1]["password"] == "hunter2"
    assert store.mark_cracked("nope:00:00:00:00:00:00", "x") is False   # unknown key -> False


def test_all_returns_snapshot_copy():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol"))
    snap = store.all()
    snap.clear()                       # mutating the returned list must not affect the store
    assert store.count == 1


def test_attach_file_sets_path_without_bumping_times_seen():
    # Red-team fix (#2 slice-5 review): a pcap file-attach is bookkeeping, not a re-observation, so
    # it must set the path but NOT bump times_seen (a full add() upsert would over-count).
    store = CaptureStore()
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol"))
    key = "eapol:aa:bb:cc:dd:ee:ff"
    events = _bus_recorder(store.bus)
    assert store.attach_file(key, pcap_path="/sd/hs.pcapng") is True
    rec = store.get(key)
    assert rec.pcap_path == "/sd/hs.pcapng" and rec.times_seen == 1
    assert [t for t, _ in events] == ["capture.updated"]   # repaints the row, no new-row event


def test_attach_file_missing_key_returns_false():
    store = CaptureStore()
    assert store.attach_file("eapol:zz", pcap_path="/x") is False
