"""Tests for FlockSession.merge — combine camera sets by the strongest-RSSI dedup rule (F5 5b).

The same rule observe() uses, applied to whole sets: merge two saved drives, or reconcile a
server/peer set on wifi sync, keeping the location seen at the strongest signal per MAC.
"""
from src.core.flock import CameraDetection, FlockSession


def _cam(mac, lat, lon, rssi=-60, count=1, first="t1", last="t1", **kw):
    return CameraDetection(mac=mac, lat=lat, lon=lon, rssi=rssi, count=count,
                           first_seen=first, last_seen=last, **kw)


def _sess(*cams):
    s = FlockSession()
    for c in cams:
        s.cameras[c.mac] = c
    return s


def test_merge_adds_new_cameras():
    s = _sess(_cam("A", 1.0, 1.0))
    changed = s.merge(_sess(_cam("B", 2.0, 2.0)))
    assert changed == 1 and s.camera_count == 2 and "B" in s.cameras


def test_merge_stronger_rssi_relocates_and_sums_count():
    s = _sess(_cam("A", 1.0, 1.0, rssi=-70, count=2, first="t2", last="t5"))
    s.merge(_sess(_cam("A", 9.0, 9.0, rssi=-40, count=3, first="t1", last="t9")))
    a = s.cameras["A"]
    assert (a.lat, a.lon, a.rssi) == (9.0, 9.0, -40)     # relocated to the stronger fix
    assert a.count == 5                                   # 2 + 3
    assert a.first_seen == "t1" and a.last_seen == "t9"   # window widened both ways


def test_merge_weaker_rssi_keeps_location_but_sums_count():
    s = _sess(_cam("A", 1.0, 1.0, rssi=-40, count=2))
    s.merge(_sess(_cam("A", 9.0, 9.0, rssi=-70, count=1)))
    a = s.cameras["A"]
    assert (a.lat, a.lon, a.rssi) == (1.0, 1.0, -40) and a.count == 3


def test_merge_zero_rssi_sentinel_does_not_hijack_location():
    # rssi 0 is the missing sentinel; a real (negative) reading must win the location.
    s = _sess(_cam("A", 1.0, 1.0, rssi=-80))
    s.merge(_sess(_cam("A", 9.0, 9.0, rssi=0)))
    a = s.cameras["A"]
    assert (a.lat, a.lon) == (1.0, 1.0)   # kept the real reading, not the 0-sentinel


def test_merge_location_is_symmetric():
    ab = _sess(_cam("A", 1.0, 1.0, rssi=-70))
    ab.merge(_sess(_cam("A", 9.0, 9.0, rssi=-40)))
    ba = _sess(_cam("A", 9.0, 9.0, rssi=-40))
    ba.merge(_sess(_cam("A", 1.0, 1.0, rssi=-70)))
    x, y = ab.cameras["A"], ba.cameras["A"]
    assert (x.lat, x.lon, x.rssi, x.count) == (y.lat, y.lon, y.rssi, y.count)


def test_merge_accepts_a_dict_and_copies_in():
    src = _cam("A", 2.0, 2.0, rssi=-50)
    s = FlockSession()
    assert s.merge({"A": src}) == 1 and s.camera_count == 1
    s.cameras["A"].lat = 99.0          # merged-in camera is a copy...
    assert src.lat == 2.0              # ...so the source object is untouched
