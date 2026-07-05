"""Tests for FlockSession crash-safe persistence (F5 piece 5a).

checkpoint() writes the drive's cameras atomically after each add; from_checkpoint()
resumes them after a restart. Between them a mid-drive crash can't lose the run.
"""
import json

from src.core.flock import CameraDetection, FlockSession


def _session_with(*cams: CameraDetection) -> FlockSession:
    s = FlockSession()
    for c in cams:
        s.cameras[c.mac] = c
    return s


def test_checkpoint_writes_valid_geojson_atomically(tmp_path):
    s = _session_with(
        CameraDetection(mac="AA:BB:CC:DD:EE:01", lat=40.7128, lon=-74.006, ssid="cam1", rssi=-50, count=3),
        CameraDetection(mac="AA:BB:CC:DD:EE:02", lat=34.0522, lon=-118.2437, rssi=-60, count=1),
    )
    p = tmp_path / "drive" / "flock.geojson"  # parent dir is created for us
    assert s.checkpoint(p) == 2
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2
    assert list((tmp_path / "drive").glob("*.tmp")) == []  # no temp file left behind


def test_checkpoint_round_trips_all_fields(tmp_path):
    orig = _session_with(
        CameraDetection(
            mac="AA:BB:CC:DD:EE:01", lat=40.7128, lon=-74.006, ssid="cam1", oui="AA:BB:CC",
            detection_method="ble", rssi=-50, channel=6, frequency=2437,
            utc="2026-07-04 23:00:00", first_seen="t1", last_seen="t2", count=3),
    )
    p = tmp_path / "flock.geojson"
    orig.checkpoint(p)
    loaded = FlockSession.from_checkpoint(p)
    assert loaded.camera_count == 1
    c = loaded.cameras["AA:BB:CC:DD:EE:01"]
    assert (round(c.lat, 6), round(c.lon, 6)) == (40.7128, -74.006)
    assert c.ssid == "cam1" and c.oui == "AA:BB:CC" and c.detection_method == "ble"
    assert c.rssi == -50 and c.channel == 6 and c.frequency == 2437 and c.count == 3
    assert c.utc == "2026-07-04 23:00:00"


def test_from_checkpoint_missing_or_corrupt_is_empty(tmp_path):
    assert FlockSession.from_checkpoint(tmp_path / "nope.geojson").camera_count == 0
    bad = tmp_path / "bad.geojson"
    bad.write_text("{ not json", encoding="utf-8")
    assert FlockSession.from_checkpoint(bad).camera_count == 0


def test_from_checkpoint_skips_malformed_features(tmp_path):
    p = tmp_path / "mixed.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"coordinates": [-74.0, 40.0]}, "properties": {"mac": "AA:BB:CC:DD:EE:01"}},
        {"properties": {"mac": "no-geometry"}},                     # missing geometry -> skipped
        {"geometry": {"coordinates": [1.0, 2.0]}, "properties": {}},  # no mac -> skipped
    ]}), encoding="utf-8")
    s = FlockSession.from_checkpoint(p)
    assert s.camera_count == 1 and "AA:BB:CC:DD:EE:01" in s.cameras


def test_checkpoint_reflects_latest_state_each_call(tmp_path):
    s = FlockSession()
    p = tmp_path / "f.geojson"
    s.cameras["A"] = CameraDetection(mac="A", lat=1.0, lon=2.0)
    s.checkpoint(p)
    assert len(json.loads(p.read_text(encoding="utf-8"))["features"]) == 1
    s.cameras["B"] = CameraDetection(mac="B", lat=3.0, lon=4.0)
    s.checkpoint(p)
    assert len(json.loads(p.read_text(encoding="utf-8"))["features"]) == 2
