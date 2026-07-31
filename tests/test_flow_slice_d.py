"""P3 flow-spine Slice D (->MAP): open a saved Flock scan on the MAP / Flock canvas.

The emitter (the Tools "Open Flock scan on the map..." action) delegates to
main_window._open_flock_scan_on_map(path), which dispatches a FlowIntent through Atlas's Slice A
substrate to FlockHeatmapTab.load_geojson_file. Awareness-only: the Flock Map renders WHERE ALPR
cameras were seen and drives no device; nothing is armed or sent. Offscreen.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    import src.core.install as _install
    monkeypatch.setattr(_install, "captures_dir", lambda: tmp_path)   # isolate the app store
    w = _make_window()
    try:
        w._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in w.findChildren(QTimer):
        t.stop()
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


def _cameras_geojson(path, n=2):
    feats = [
        {"type": "Feature", "properties": {"count": 1},
         "geometry": {"type": "Point", "coordinates": [-122.4 + i * 0.01, 37.7 + i * 0.01]}}
        for i in range(n)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(path)


def test_slice_d_opens_a_saved_scan_on_the_map(win, tmp_path):
    # A saved cameras.geojson -> "Open Flock scan on the map" -> MAP shown + Flock canvas loaded it.
    scan = _cameras_geojson(tmp_path / "cameras.geojson", n=2)
    win._show_subtab(win._crack_surface, win._crack_lab_tab)   # start on a DIFFERENT surface
    assert win._tabs.currentWidget() is win._crack_surface
    ok = win._open_flock_scan_on_map(scan)
    assert ok is True
    # navigated to the MAP surface + the Flock sub-view
    assert win._tabs.currentWidget() is win._map_surface
    assert win._map_surface.currentWidget() is win._flock_heatmap
    # LOAD-only: the two cameras are now on the awareness map (no device touched)
    assert win._flock_heatmap.camera_count == 2


def test_slice_d_empty_path_does_not_dispatch(win):
    # No path chosen in the dialog -> no-op (no nav, no crash), the flow never fires.
    before = win._tabs.currentWidget()
    assert win._open_flock_scan_on_map("") is False
    assert win._tabs.currentWidget() is before


def test_slice_d_bad_file_navigates_but_loads_nothing(win, tmp_path):
    # A missing/corrupt scan -> load_geojson_file returns 0 (safe); the flow still routes (True) and
    # the canvas simply holds no cameras. Never crashes.
    missing = str(tmp_path / "nope.geojson")
    assert win._open_flock_scan_on_map(missing) is True
    assert win._map_surface.currentWidget() is win._flock_heatmap
    assert win._flock_heatmap.camera_count == 0
