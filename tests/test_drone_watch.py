"""DroneWatchModel — the bounded, TTL-aware rollup of drone Remote-ID sightings (pure, no Qt/IO).

Feeds drone_found payloads (the shape DroneMeshProtocol emits) and asserts dedup by basic_id-or-mac,
the Pilot-follow-up merge, bounded eviction, TTL freshness, and the honest drop of a keyless event.
`now` is passed explicitly (monotonic seconds) so the test is deterministic.
"""
from __future__ import annotations

from src.core.drone_watch import DroneWatchModel


def _det(**kw):
    base = {"mac": "aa:bb:cc:dd:ee:ff", "basic_id": "DRONE-1", "rssi": -63,
            "drone_lat": 0.0, "drone_long": 0.0, "drone_altitude": 0,
            "pilot_lat": 0.0, "pilot_long": 0.0}
    base.update(kw)
    return base


def test_observe_creates_a_sighting():
    m = DroneWatchModel()
    d = m.observe(_det(drone_lat=37.77, drone_long=-122.41, drone_altitude=150), now=100.0)
    assert d is not None
    assert m.count == 1
    assert d.basic_id == "DRONE-1" and d.mac == "aa:bb:cc:dd:ee:ff"
    assert d.has_drone_location is True and d.drone_altitude == 150
    assert d.label == "DRONE-1" and d.times_seen == 1


def test_dedup_by_basic_id_bumps_times_seen():
    m = DroneWatchModel()
    m.observe(_det(rssi=-63), now=100.0)
    m.observe(_det(rssi=-55), now=101.0)   # same basic_id -> one row, newer rssi wins
    assert m.count == 1
    d = m.get("DRONE-1")
    assert d is not None and d.times_seen == 2 and d.rssi == -55


def test_pilot_followup_merges_onto_the_drone_row():
    m = DroneWatchModel()
    m.observe(_det(drone_lat=37.77, drone_long=-122.41), now=100.0)          # drone fix
    m.observe(_det(pilot_lat=37.78, pilot_long=-122.42), now=101.0)   # operator fix, same key
    assert m.count == 1
    d = m.get("DRONE-1")
    assert d is not None
    assert d.has_drone_location is True and d.has_pilot_location is True   # both kept
    assert d.drone_lat == 37.77 and d.pilot_lat == 37.78


def test_keyless_event_is_dropped_never_raises():
    m = DroneWatchModel()
    assert m.observe({"basic_id": "", "mac": ""}, now=100.0) is None
    assert m.observe("not a dict", now=100.0) is None
    assert m.count == 0


def test_mac_only_sighting_keys_on_mac():
    m = DroneWatchModel()
    d = m.observe({"mac": "11:22:33:44:55:66", "basic_id": "", "rssi": -70}, now=100.0)
    assert d is not None and d.key == "11:22:33:44:55:66" and d.label == "11:22:33:44:55:66"


def test_bounded_eviction_drops_the_stalest():
    m = DroneWatchModel(max_drones=2)
    m.observe(_det(basic_id="A"), now=100.0)
    m.observe(_det(basic_id="B"), now=101.0)
    m.observe(_det(basic_id="C"), now=102.0)   # at cap -> evicts the stalest (A)
    assert m.count == 2
    assert m.get("A") is None and m.get("B") is not None and m.get("C") is not None


def test_ttl_freshness_and_summary():
    m = DroneWatchModel()
    m.observe(_det(basic_id="A", drone_lat=1.0, drone_long=2.0), now=99.0)
    m.observe(_det(basic_id="B", pilot_lat=3.0, pilot_long=4.0), now=159.0)
    s = m.summary(now=160.0, ttl=60.0)
    assert s["total"] == 2
    assert s["fresh"] == 1                     # A is 61s old (stale at ttl=60), B is 1s old
    assert s["with_drone_location"] == 1 and s["with_pilot_location"] == 1
    assert m.drones(now=160.0, ttl=60.0, fresh_only=True) == [m.get("B")]
