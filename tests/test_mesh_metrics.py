"""Meshtastic telemetry -> canonical metrics bridge (``src/core/mesh_metrics.py``).

Uses the REAL ``MeshNode`` dataclass (pure protobuf model, no serial), so the mapping is checked
against the actual node shape the stream produces. A node's present telemetry becomes readings; a
field it never reported yields no reading (no phantom values).
"""
from __future__ import annotations

from src.core.mesh_metrics import feed_mesh_node, node_to_readings
from src.core.metrics import MetricsModel, ReadingKind
from src.protocols.meshtastic_proto import MeshNode


def _by_kind(readings):
    return {r.kind: r for r in readings}


def test_full_node_maps_identity_battery_gps_link():
    n = MeshNode(num=0x043ae298, node_id="!043ae298", long_name="Base", snr=6.5, battery=85,
                 voltage=3.9, latitude=40.1, longitude=-74.2, altitude=12, hops_away=2,
                 channel_util=15.0)
    rs = node_to_readings(n)
    by = _by_kind(rs)
    assert by[ReadingKind.DEVICE_INFO].device_source == "mesh:!043ae298"
    info = by[ReadingKind.DEVICE_INFO].value
    assert "Base" in info and "2 hops" in info
    assert by[ReadingKind.BATTERY].value == 85 and by[ReadingKind.BATTERY].extra["voltage"] == 3.9
    assert by[ReadingKind.GPS_FIX].value == "40.1,-74.2"
    assert by[ReadingKind.LINK].value == 6.5 and by[ReadingKind.LINK].extra["hops_away"] == 2
    assert all(r.device_source == "mesh:!043ae298" for r in rs)   # every reading keyed to the node


def test_external_power_sentinel_is_labelled_not_a_number():
    n = MeshNode(num=1, node_id="!01", battery=101)   # 101 = externally powered
    batt = _by_kind(node_to_readings(n))[ReadingKind.BATTERY]
    assert batt.value == "external" and "external" in batt.label


def test_sparse_node_yields_only_identity_no_phantom_readings():
    n = MeshNode(num=5, node_id="!05")   # reported nothing but its identity
    kinds = {r.kind for r in node_to_readings(n)}
    assert kinds == {ReadingKind.DEVICE_INFO}


def test_position_only_maps_when_both_lat_and_lon_present():
    lat_only = {r.kind for r in node_to_readings(MeshNode(num=1, latitude=40.1))}
    assert ReadingKind.GPS_FIX not in lat_only
    got = {r.kind for r in node_to_readings(MeshNode(num=1, latitude=40.1, longitude=-74.2))}
    assert ReadingKind.GPS_FIX in got


def test_feed_mesh_node_stores_into_the_model_keyed_per_node():
    m = MetricsModel()
    feed_mesh_node(m, MeshNode(num=1, node_id="!01", battery=90, snr=5.0))
    assert m.latest("mesh:!01", ReadingKind.BATTERY).value == 90
    assert m.latest("mesh:!01", ReadingKind.LINK).value == 5.0
    assert {r.device_source for r in m.readings_for("mesh:!01")} == {"mesh:!01"}


def test_device_source_override_is_honored():
    rs = node_to_readings(MeshNode(num=1, node_id="!01", battery=50), device_source="COM7")
    assert all(r.device_source == "COM7" for r in rs)
