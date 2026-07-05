"""Tests for the F5 live driving loop.

`_flock_pump` is the per-iteration capture step (Qt/serial-free, so it's unit-testable directly);
`_FlockWorker` is the QThread that reads the serial ports and drives it. We test the pump against real
GPS + Flock-You line samples, and smoke-test that the worker constructs and stops cleanly.
"""
import json

import pytest
from PyQt5.QtWidgets import QApplication

from src.core.flock import FlockSession
from src.ui.qt.flock_heatmap_tab import _flock_pump


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])

FIX_A = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"   # quality 1 -> has_fix
NO_FIX = "$GPGGA,123521,,,,,0,00,,,M,,M,,*00"                                 # quality 0 -> no fix
DET = ('{"event":"detection","mac_address":"AA:BB:CC:DD:EE:FF","ssid":"Flock",'
       '"rssi":-70,"channel":6,"oui":"AABBCC","detection_method":"oui","frequency":2437}')


def test_pump_adds_camera_with_fix_and_checkpoints(tmp_path):
    s = FlockSession()
    p = tmp_path / "drive" / "flock.geojson"                 # parent dir created by checkpoint()
    assert _flock_pump(s, FIX_A, DET, str(p)) is True        # fix + detection -> located camera
    assert s.camera_count == 1
    assert json.loads(p.read_text(encoding="utf-8"))["features"], "checkpoint written on add"


def test_pump_no_fix_drops_detection(tmp_path):
    s = FlockSession()
    p = tmp_path / "f.geojson"
    assert _flock_pump(s, NO_FIX, DET, str(p)) is False      # unlocatable without a fix
    assert s.camera_count == 0
    assert not p.exists(), "nothing added -> no checkpoint written"


def test_pump_gps_only_updates_fix_without_adding():
    s = FlockSession()
    assert _flock_pump(s, FIX_A, "") is False                # a GPS line alone adds no camera
    assert s.has_fix and s.camera_count == 0


def test_pump_empty_lines_are_safe():
    s = FlockSession()
    assert _flock_pump(s, "", "") is False                   # nothing in, nothing happens, no crash
    assert s.camera_count == 0


def test_pump_add_without_checkpoint_path_is_ok():
    s = FlockSession()
    assert _flock_pump(s, FIX_A, DET) is True                # default checkpoint_path "" -> no write, still True
    assert s.camera_count == 1


def test_worker_constructs_and_stops(qapp):
    from src.ui.qt.flock_heatmap_tab import _FlockWorker
    w = _FlockWorker("", 9600, "COM_DOES_NOT_EXIST", 115200, "")
    assert w.session.camera_count == 0 and w._stop is False
    for sig in ("status", "updated", "line", "stopped"):
        assert hasattr(w, sig), f"missing signal {sig}"
    w.stop()
    assert w._stop is True


def _gj(n):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(i), float(i)]},
         "properties": {"mac": f"AA:{i:02d}", "count": 1}} for i in range(n)]}


def test_tab_live_controls_and_port_guard(qapp):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    tab = FlockHeatmapTab()
    for attr in ("_gps_combo", "_dev_combo", "_btn_live", "_live_status"):
        assert hasattr(tab, attr), f"missing control {attr}"
    assert tab._live_worker is None
    tab._toggle_live()                                   # no device port picked -> guarded, no worker started
    assert tab._live_worker is None
    assert "device port" in tab._live_status.text().lower()


def test_tab_record_render_split(qapp):
    # While hidden, live updates must be RECORDED (latest kept) but NOT rendered; showEvent catches up.
    from PyQt5.QtGui import QHideEvent, QShowEvent

    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    tab = FlockHeatmapTab()
    tab.showEvent(QShowEvent())                          # visible
    tab._on_live_update(_gj(1))
    assert tab.camera_count == 1                         # rendered live
    tab.hideEvent(QHideEvent())                          # backgrounded
    tab._on_live_update(_gj(2))
    assert tab.camera_count == 1                         # not repainted while hidden...
    assert tab._latest_gj is not None                   # ...but the newest data is retained
    tab.showEvent(QShowEvent())                          # wake -> replay the latest
    assert tab.camera_count == 2
