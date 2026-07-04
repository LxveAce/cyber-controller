"""FlockSession (FL F2) — GPS-fused ALPR detection log. Pure Python, deterministic, no Qt/hardware/network.

Mirrors tests/test_wardrive.py: synthetic NMEA fixes + Flock-You serial lines -> located, deduped cameras ->
a portable GeoJSON FeatureCollection.
"""
from __future__ import annotations

import io
import json

from src.core.flock import CameraDetection, FlockSession

# Proven NMEA sentences (parse_nmea ignores the checksum, so *00 is fine).
FIX_A = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"   # ~48.1173, 11.51667
FIX_B = "$GPGGA,123520,4808.000,N,01132.000,E,1,08,0.9,545.4,M,46.9,M,,*00"   # ~48.1333, 11.53333
NO_FIX = "$GPGGA,123521,,,,,0,00,,,M,,M,,*00"                                  # fix quality 0 -> has_fix False

# Flock-You detection lines (JSON + the two proven human-mirror shapes from test_flock_you_protocol.py).
DET_WEAK = '{"event":"detection","mac_address":"AA:BB:CC:DD:EE:FF","ssid":"Flock","rssi":-70,"channel":6,"oui":"AABBCC","detection_method":"oui","frequency":2437}'
DET_STRONG = '{"event":"detection","mac_address":"AA:BB:CC:DD:EE:FF","ssid":"FlockCam2","rssi":-40,"channel":6,"oui":"AABBCC","detection_method":"oui","frequency":2437}'
DET_OTHER = '{"event":"detection","mac_address":"11:22:33:44:55:66","rssi":-50,"channel":1}'
HUMAN_OUI = "[flockyou] DETECT-OUI mac=DE:AD:BE:EF:00:11 oui=DEADBE rssi=-70 ch=1 addr=addr2 count=1"


def test_no_fix_drops_detection():
    s = FlockSession()
    assert s.observe(DET_WEAK) is False        # no GPS fix yet -> unlocatable, dropped
    assert s.camera_count == 0


def test_located_detection_recorded():
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.has_fix
    assert s.observe(DET_WEAK, now="2026-07-04 00:00:00") is True
    assert s.camera_count == 1
    cam = s.cameras["AA:BB:CC:DD:EE:FF"]
    assert abs(cam.lat - 48.1173) < 1e-3 and abs(cam.lon - 11.51667) < 1e-3
    assert cam.rssi == -70 and cam.count == 1 and cam.oui == "AABBCC"
    assert cam.utc == "123519" and cam.first_seen == "2026-07-04 00:00:00"


def test_dedup_by_mac_keeps_strongest_and_relocates():
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe(DET_WEAK) is True                 # first sighting at A (rssi -70)
    assert s.observe(DET_WEAK) is False                # same/weaker -> no relocate, but counted
    s.update_gps(FIX_B)
    assert s.observe(DET_STRONG) is True               # stronger (-40) -> relocate to B + refresh identity
    s.update_gps(FIX_A)
    assert s.observe(DET_WEAK) is False                # weaker again -> stays at B
    cam = s.cameras["AA:BB:CC:DD:EE:FF"]
    assert cam.rssi == -40                             # strongest kept
    assert abs(cam.lat - 48.1333) < 1e-3 and abs(cam.lon - 11.53333) < 1e-3   # located at the stronger fix
    assert cam.ssid == "FlockCam2"                     # identity refreshed from the stronger line
    assert cam.count == 4                              # every sighting counted
    assert s.camera_count == 1                         # still one unique camera


def test_human_line_detection_recorded():
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe(HUMAN_OUI) is True
    assert "DE:AD:BE:EF:00:11" in s.cameras
    assert s.cameras["DE:AD:BE:EF:00:11"].rssi == -70


def test_empty_and_nested_mac_dropped():
    s = FlockSession()
    s.update_gps(FIX_A)
    # a drifted JSON with a NESTED mac_address must not manufacture a phantom camera (parser yields mac="")
    assert s.observe('{"event":"detection","mac_address":{"x":1},"rssi":-61}') is False
    assert s.observe('{"event":"detection","mac_address":"","rssi":-50}') is False
    assert s.camera_count == 0


def test_status_and_noise_ignored():
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe("[flockyou] booting v1.0") is False     # status line -> info, not a detection
    assert s.observe("random serial noise") is False
    assert s.observe("") is False
    assert s.camera_count == 0


def test_detection_without_valid_fix_dropped():
    s = FlockSession()
    s.update_gps(NO_FIX)                    # a sentence parsed, but fix quality 0
    assert not s.has_fix
    assert s.observe(DET_WEAK) is False
    assert s.camera_count == 0


def test_geojson_shape_and_writer():
    s = FlockSession()
    s.update_gps(FIX_A)
    s.observe(DET_WEAK)
    s.observe(DET_OTHER)
    gj = s.to_geojson()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 2
    macs = [f["properties"]["mac"] for f in gj["features"]]
    assert macs == sorted(macs)                          # deterministic, sorted by MAC
    feat = gj["features"][macs.index("AA:BB:CC:DD:EE:FF")]
    assert feat["type"] == "Feature" and feat["geometry"]["type"] == "Point"
    lon, lat = feat["geometry"]["coordinates"]           # GeoJSON order is [lon, lat]
    assert abs(lon - 11.51667) < 1e-3 and abs(lat - 48.1173) < 1e-3
    assert feat["properties"]["rssi"] == -70 and feat["properties"]["count"] == 1

    buf = io.StringIO()
    n = s.write_geojson(buf)
    assert n == 2
    parsed = json.loads(buf.getvalue())                  # valid, portable GeoJSON
    assert parsed["type"] == "FeatureCollection" and len(parsed["features"]) == 2


def test_camera_detection_feature_is_lon_lat():
    c = CameraDetection(mac="X", lat=48.0, lon=11.0, rssi=-30, count=1)
    coords = c.to_feature()["geometry"]["coordinates"]
    assert coords == [11.0, 48.0]                         # [lon, lat], not [lat, lon]


DET_NO_RSSI = '{"event":"detection","mac_address":"AA:BB:CC:DD:EE:FF","ssid":"drift","channel":6}'  # rssi omitted -> 0


def test_no_rssi_sentinel_does_not_hijack_location():
    """A drifted detection that omits rssi (parses to 0) must NOT beat a real (negative) reading and drag the
    camera to the wrong fix — 0 is the 'unknown' sentinel, ranked below any real signal."""
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe(DET_STRONG) is True                 # real -40 at A
    s.update_gps(FIX_B)
    assert s.observe(DET_NO_RSSI) is False               # no-rssi(->0) at B must NOT relocate
    cam = s.cameras["AA:BB:CC:DD:EE:FF"]
    assert cam.rssi == -40                               # real reading kept
    assert abs(cam.lat - 48.1173) < 1e-3                 # still at A, not hijacked to B


def test_real_reading_replaces_unknown_first_sighting():
    """If the FIRST sighting had no rssi (0/unknown), a later REAL reading should win and relocate."""
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe(DET_NO_RSSI) is True                 # unknown-rssi first, at A
    assert s.cameras["AA:BB:CC:DD:EE:FF"].rssi == 0
    s.update_gps(FIX_B)
    assert s.observe(DET_WEAK) is True                    # real -70 beats unknown -> relocate to B
    cam = s.cameras["AA:BB:CC:DD:EE:FF"]
    assert cam.rssi == -70 and abs(cam.lat - 48.1333) < 1e-3


def test_first_seen_immutable_last_seen_advances():
    s = FlockSession()
    s.update_gps(FIX_A)
    s.observe(DET_WEAK, now="2026-07-04 00:00:00")
    s.observe(DET_WEAK, now="2026-07-04 00:05:00")       # weaker/equal re-sighting, later time
    cam = s.cameras["AA:BB:CC:DD:EE:FF"]
    assert cam.first_seen == "2026-07-04 00:00:00"       # pinned to the first sighting
    assert cam.last_seen == "2026-07-04 00:05:00"        # advances
    assert cam.count == 2


def test_equal_rssi_at_new_fix_does_not_relocate():
    s = FlockSession()
    s.update_gps(FIX_A)
    s.observe(DET_WEAK)                                   # -70 at A
    s.update_gps(FIX_B)
    assert s.observe(DET_WEAK) is False                  # SAME -70 at B must not move the camera
    assert abs(s.cameras["AA:BB:CC:DD:EE:FF"].lat - 48.1173) < 1e-3   # stays at A


def test_multi_camera_independence():
    s = FlockSession()
    s.update_gps(FIX_A)
    s.observe(DET_WEAK)                                   # camera AA... at A
    s.observe(DET_OTHER)                                  # camera 11... at A
    s.update_gps(FIX_B)
    s.observe(DET_STRONG)                                 # relocate ONLY AA... to B
    assert abs(s.cameras["AA:BB:CC:DD:EE:FF"].lat - 48.1333) < 1e-3   # moved
    assert abs(s.cameras["11:22:33:44:55:66"].lat - 48.1173) < 1e-3   # untouched
    assert s.camera_count == 2


def test_float_rssi_through_observe():
    s = FlockSession()
    s.update_gps(FIX_A)
    assert s.observe('{"event":"detection","mac_address":"AA:BB:CC:11:22:33","rssi":-61.5,"channel":6}') is True
    assert s.cameras["AA:BB:CC:11:22:33"].rssi == -61    # float truncated by the parser, ingested cleanly
