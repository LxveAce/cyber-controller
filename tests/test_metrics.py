"""Canonical metrics layer (``src/core/metrics.py``) — the shared reading vocabulary every firmware
normalizes up to, so one Dashboard renders any device's readings identically.

Two layers of coverage: the MetricsModel store + observers and the ``event_to_readings`` mapping are
unit-tested directly; ``attach_metrics`` is exercised END TO END by feeding REAL firmware serial
lines through a real ``TargetIngestor`` (the same ``_FakeConn`` pattern the event-observer test
uses), so the whole wire — parser -> ingestor observer -> model — is proven, not just the mapping.
"""
from __future__ import annotations

from src.core.cross_comm import EventBus, TargetPool
from src.core.metrics import (
    Medium,
    MetricsModel,
    Reading,
    ReadingKind,
    attach_metrics,
    event_to_readings,
)
from src.core.target_ingest import TargetIngestor
from src.models.target import TargetType
from src.protocols import get_protocol
from src.protocols.base import ParsedEvent

# Real firmware serial lines (mac-keyed Marauder, addr-keyed LxveOS) — both known-good parser input.
_MARAUDER_BLE = "BLE: 12:34:56:78:9a:bc Name: Fitbit RSSI: -40"
_LXVEOS_BLE = (
    "LXVEOS/1 ble addr=66:55:44:33:22:11 type=random rssi=-55 name=4d79 company=76 tracker=1"
)


class _FakeConn:
    """Minimal SerialConnection stand-in: records on_line callbacks, lets the test feed lines."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs: list = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


# ── MetricsModel: latest-wins store + observers ──────────────────────────────────────────────────

def test_model_latest_wins_and_stamps_monotonic_seq():
    m = MetricsModel()
    r1 = m.update(Reading(ReadingKind.RSSI, Medium.WIFI, -40, "dBm", "-40 dBm", "COM4"))
    r2 = m.update(Reading(ReadingKind.RSSI, Medium.WIFI, -55, "dBm", "-55 dBm", "COM4"))
    # Same (device, kind) -> the second replaces the first; seq is monotonic.
    assert m.latest("COM4", ReadingKind.RSSI).value == -55
    assert r2.seq > r1.seq
    assert len(m.readings_for("COM4")) == 1


def test_model_observer_fires_per_update_and_is_isolated():
    m = MetricsModel()
    seen: list = []
    m.subscribe(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))  # raises every time
    m.subscribe(lambda r: seen.append((r.kind, r.value)))               # must still run
    m.update(Reading(ReadingKind.BATTERY, Medium.SYSTEM, 88, "%", "88%", "COM4"))
    assert seen == [(ReadingKind.BATTERY, 88)]


def test_model_readings_for_is_kind_ordered_and_per_device():
    m = MetricsModel()
    m.update(Reading(ReadingKind.RSSI, Medium.WIFI, -40, "dBm", "", "COM4"))
    m.update(Reading(ReadingKind.DETECTION, Medium.WIFI, "AP home", "", "", "COM4"))
    m.update(Reading(ReadingKind.GPS_FIX, Medium.GPS, "1,2", "", "", "COM9"))
    kinds = [r.kind for r in m.readings_for("COM4")]
    # DETECTION is declared before RSSI in the enum -> stable tile order, not insertion order.
    assert kinds == [ReadingKind.DETECTION, ReadingKind.RSSI]
    assert [r.device_source for r in m.readings_for("COM9")] == ["COM9"]
    assert set(m.devices()) == {"COM4", "COM9"}


def test_model_clear_scopes_to_a_device():
    m = MetricsModel()
    m.update(Reading(ReadingKind.RSSI, Medium.WIFI, -40, "dBm", "", "COM4"))
    m.update(Reading(ReadingKind.RSSI, Medium.WIFI, -50, "dBm", "", "COM9"))
    m.clear("COM4")
    assert m.readings_for("COM4") == [] and len(m.readings_for("COM9")) == 1
    m.clear()
    assert m.all_latest() == []


# ── event_to_readings: the CANONICAL_EVENTS mapping ──────────────────────────────────────────────

def _ev(event_type: str, data: dict) -> ParsedEvent:
    return ParsedEvent(event_type=event_type, data=data, raw="")


def _by_kind(readings: list[Reading]) -> dict:
    return {r.kind: r for r in readings}


def test_detection_surfaces_detection_plus_rssi_and_channel():
    rs = event_to_readings(
        _ev("ap_found", {"bssid": "aa:bb", "ssid": "home", "rssi": -42, "channel": 6}), "COM4")
    by = _by_kind(rs)
    assert by[ReadingKind.DETECTION].medium is Medium.WIFI
    assert by[ReadingKind.DETECTION].value == "AP home"
    assert by[ReadingKind.RSSI].value == -42 and by[ReadingKind.RSSI].unit == "dBm"
    assert by[ReadingKind.CHANNEL].value == 6


def test_ble_detection_uses_ble_medium_and_name_or_addr():
    rs = event_to_readings(
        _ev("ble_found", {"addr": "66:55:44", "name": "Tile", "rssi": -55}), "COM4")
    by = _by_kind(rs)
    assert by[ReadingKind.DETECTION].medium is Medium.BLE
    assert by[ReadingKind.DETECTION].value == "BLE Tile"
    assert by[ReadingKind.RSSI].medium is Medium.BLE and by[ReadingKind.RSSI].value == -55


def test_gps_fix_maps_lat_lon():
    rs = event_to_readings(_ev("gps_fix", {"lat": 40.1, "lon": -74.2}), "COM4")
    assert len(rs) == 1 and rs[0].kind is ReadingKind.GPS_FIX and rs[0].medium is Medium.GPS
    assert rs[0].value == "40.1,-74.2" and "40.10000" in rs[0].label


def test_spectrum_maps_channel_and_rssi():
    by = _by_kind(event_to_readings(_ev("spectrum", {"channel": 11, "rssi": -70}), "COM4"))
    assert by[ReadingKind.SPECTRUM].value == -70
    assert by[ReadingKind.CHANNEL].value == 11 and by[ReadingKind.RSSI].value == -70


def test_device_info_maps_identity_caps_and_battery():
    rs = event_to_readings(_ev("device_info", {
        "board": "ESP32-S3", "fw": "1.2", "heap": 180,
        "caps_tokens": ["wifi", "ble", "gps"], "batt": 91,
    }), "COM4")
    by = _by_kind(rs)
    info = by[ReadingKind.DEVICE_INFO].value
    assert "ESP32-S3" in info and "fw 1.2" in info
    assert by[ReadingKind.CAPABILITIES].value == ["wifi", "ble", "gps"]
    assert by[ReadingKind.BATTERY].value == 91 and by[ReadingKind.BATTERY].unit == "%"


def test_link_state_maps_link_and_lifts_rssi():
    by = _by_kind(event_to_readings(_ev("link_state", {
        "tier": "LoRa", "rssi": -95, "snr": 7, "latency_ms": 120, "link_event": "tier",
    }), "COM4"))
    assert by[ReadingKind.LINK].value == "LoRa" and by[ReadingKind.LINK].medium is Medium.LORA
    assert "-95 dBm" in by[ReadingKind.LINK].label and "snr 7" in by[ReadingKind.LINK].label
    assert by[ReadingKind.RSSI].value == -95 and by[ReadingKind.RSSI].medium is Medium.LORA


def test_arm_state_and_alert_and_airspace_and_capture():
    arm = event_to_readings(_ev("arm_state", {"state": "armed"}), "COM4")[0]
    assert arm.kind is ReadingKind.ARM_STATE
    alert = event_to_readings(_ev("alert", {"grade": "A", "count": 3}), "COM4")[0]
    assert alert.kind is ReadingKind.ALERT and alert.value == "A"
    snap = event_to_readings(
        _ev("snapshot", {"aps": 12, "open": 2, "wps": 1, "bles": 5, "trackers": 0}), "COM4")[0]
    assert snap.kind is ReadingKind.AIRSPACE
    assert "APs 12" in snap.label and "trackers 0" in snap.label
    cap = event_to_readings(_ev("handshake_captured", {"bssid": "aa:bb"}), "COM4")[0]
    assert cap.kind is ReadingKind.CAPTURE and cap.value == "handshake"


def test_non_metric_events_map_to_nothing():
    for et in ("info", "status", "error", "version", "scan_complete", "save", "stopped"):
        assert event_to_readings(_ev(et, {"message": "x"}), "COM4") == []


def test_bool_flag_is_never_read_as_a_signal_value():
    # A ble_found carries tracker=1 / random flags; only real rssi becomes an RSSI reading, and a
    # detection with no rssi produces no RSSI reading at all (never a phantom 0/True).
    rs = event_to_readings(_ev("ble_found", {"addr": "aa", "name": "x", "tracker": True}), "COM4")
    assert ReadingKind.RSSI not in _by_kind(rs)


# ── attach_metrics: END-TO-END through a real TargetIngestor + real parsers ──────────────────────

def test_attach_metrics_end_to_end_marauder_ble():
    model = MetricsModel()
    ingest = TargetIngestor(TargetPool(EventBus()))
    attach_metrics(ingest, model)

    conn = _FakeConn("COM4")
    ingest.attach(conn, get_protocol("marauder"))
    conn.feed(_MARAUDER_BLE)

    # The real Marauder parser -> ble_found -> the model, with the RSSI lifted to its own kind.
    assert model.latest("COM4", ReadingKind.DETECTION).medium is Medium.BLE
    assert model.latest("COM4", ReadingKind.RSSI).value == -40


def test_attach_metrics_end_to_end_second_firmware_lxveos():
    model = MetricsModel()
    ingest = TargetIngestor(TargetPool(EventBus()))
    attach_metrics(ingest, model)

    conn = _FakeConn("COM23")
    ingest.attach(conn, get_protocol("lxveos"))
    conn.feed(_LXVEOS_BLE)

    # Same canonical readings from a DIFFERENT firmware's dialect — the whole point of the layer.
    assert model.latest("COM23", ReadingKind.DETECTION).medium is Medium.BLE
    assert model.latest("COM23", ReadingKind.RSSI).value == -55


def test_attach_metrics_is_read_only_and_detachable():
    pool = TargetPool(EventBus())
    ingest = TargetIngestor(pool)
    model = MetricsModel()
    cb = attach_metrics(ingest, model)

    conn = _FakeConn("COM4")
    ingest.attach(conn, get_protocol("marauder"))
    conn.feed(_MARAUDER_BLE)
    # Routing is unaffected: the BLE target still reached the pool alongside the metrics tap.
    assert any(t.target_type == TargetType.BLE for t in pool.all())

    # Detach stops further metrics without touching ingestion.
    ingest.remove_event_observer(cb)
    model.clear()
    conn.feed("BLE: aa:bb:cc:dd:ee:ff Name: Band RSSI: -60")
    assert model.all_latest() == []
    assert len([t for t in pool.all() if t.target_type == TargetType.BLE]) == 2
