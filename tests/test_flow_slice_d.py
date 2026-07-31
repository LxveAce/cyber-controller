"""P3 flow-spine Slice D (wardrive -> MAP): a finished wardrive's Wi-Fi APs open on ONE map as an
additive layer over the Flock cameras.

Two halves, both on Atlas's Slice A substrate:
- FlockHeatmapTab.load_wardrive_csv builds a second _CameraLayer (green AP dots) in the world_px
  plane as the cameras (projection math untouched), toggled by "Wi-Fi APs".
- WardriveTab "View on map" (explicit tap, no surprise surface-jump) emits the finished CSV path;
  main_window._on_view_wardrive_on_map routes a FlowIntent to load_wardrive_csv.
Awareness-only: the map plots located APs, drives no device; nothing is armed or sent. Offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

_TWO_CAMS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]},
     "properties": {"count": 3}},
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.1, 48.1]},
     "properties": {"count": 5}},
]}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wardrive_csv(path, n=2):
    from src.core import wardrive as wd
    rows = "".join(
        f"AA:BB:CC:DD:EE:{i:02X},AP{i},[WPA2][ESS],t,6,2437,-50,48.{i},11.{i},0.0,0,,,WIFI\n"
        for i in range(1, n + 1)
    )
    path.write_text(wd.WIGLE_HEADER + "\n" + rows)
    return str(path)


# ── the receive method: a second layer on the one Flock canvas ──────────

def test_slice_d_load_wardrive_csv_adds_an_ap_layer(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    n = w.load_wardrive_csv(_wardrive_csv(tmp_path / "w.csv", n=2))
    assert n == 2 and w.wardrive_count == 2
    assert w._wardrive_layer is not None and len(w._wardrive_layer._dots) == 2   # one AP dot each
    assert w._chk_wardrive.isChecked()                             # loaded APs are visible


def test_slice_d_one_map_holds_both_cameras_and_aps(qapp, tmp_path):
    # Done-gate: ONE map renders BOTH the Flock cameras and the wardrive AP layer.
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    w.set_geojson(_TWO_CAMS)                                     # 2 cameras
    w.load_wardrive_csv(_wardrive_csv(tmp_path / "w.csv", n=2))  # + 2 APs on the same canvas
    assert w.camera_count == 2 and w.wardrive_count == 2
    assert w._camera_layer is not None and w._wardrive_layer is not None
    assert len(w._camera_layer._dots) == 2 and len(w._wardrive_layer._dots) == 2
    w.reset_view()                                              # frame both -> must not crash


def test_slice_d_toggle_hides_and_restores_the_ap_layer(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    w.load_wardrive_csv(_wardrive_csv(tmp_path / "w.csv", n=2))
    assert w._wardrive_layer is not None
    w._chk_wardrive.setChecked(False)                           # hide APs
    assert w._wardrive_layer is None                            # layer dropped on rebuild...
    assert w.wardrive_count == 2                                # ...but parsed points are retained
    w._chk_wardrive.setChecked(True)
    assert w._wardrive_layer is not None                        # ...and restored


def test_slice_d_flock_toggle_gives_wifi_only(qapp, tmp_path):
    # owner-call #3: a Wi-Fi / Flock / both control. Hiding Flock cameras leaves only the AP layer.
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    w.set_geojson(_TWO_CAMS)
    w.load_wardrive_csv(_wardrive_csv(tmp_path / "w.csv", n=2))
    assert w._camera_layer is not None and w._wardrive_layer is not None   # both (default)
    w._chk_flock.setChecked(False)                             # Wi-Fi only
    assert w._camera_layer is None and w._wardrive_layer is not None
    assert w.camera_count == 2                                 # cameras retained, just hidden
    w._chk_flock.setChecked(True)
    assert w._camera_layer is not None                         # ...and restored


def test_slice_d_bad_wardrive_csv_is_safe(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    assert w.load_wardrive_csv(str(tmp_path / "nope.csv")) == 0   # missing file -> 0, no crash
    assert w._wardrive_layer is None and w.wardrive_count == 0


# ── the emitter -> dispatch -> load flow, through the real window ───────

def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


@pytest.fixture
def win(qapp):
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


def test_slice_d_view_on_map_emitter_opens_the_map(win, tmp_path):
    # The explicit "View on map" tap -> MAP shown + the Flock canvas holds this drive's APs.
    csv = _wardrive_csv(tmp_path / "drive.csv", n=2)
    win._show_subtab(win._crack_surface, win._crack_lab_tab)    # start on a DIFFERENT surface
    assert win._tabs.currentWidget() is win._crack_surface
    win._wardrive_tab._last_csv_path = csv                      # a finished drive left a CSV
    win._wardrive_tab._emit_view_on_map()
    assert win._tabs.currentWidget() is win._map_surface
    assert win._map_surface.currentWidget() is win._flock_heatmap
    assert win._flock_heatmap.wardrive_count == 2


def test_slice_d_view_on_map_no_path_is_a_noop(win):
    # No finished drive (empty path) -> the button emits nothing; no nav, no crash.
    before = win._tabs.currentWidget()
    win._wardrive_tab._last_csv_path = ""
    win._wardrive_tab._emit_view_on_map()
    assert win._tabs.currentWidget() is before


# ── S1 file-import: load_wardrive_log dispatches WiGLE CSV + Kismet netxml onto the AP layer ──

_NETXML = (
    '<?xml version="1.0"?>\n<detection-run kismet-version="2016.01.R1" start-time="x">\n'
    '  <wireless-network number="1" type="infrastructure">\n'
    '    <SSID><essid cloaked="false">KismetNet</essid></SSID>\n'
    "    <BSSID>00:11:22:33:44:01</BSSID><channel>6</channel>\n"
    "    <gps-info><avg-lat>47.62</avg-lat><avg-lon>-122.35</avg-lon></gps-info>\n"
    "  </wireless-network>\n</detection-run>\n"
)


def test_import_wardrive_csv_onto_map(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    n = w.load_wardrive_log(_wardrive_csv(tmp_path / "d.csv", n=2))   # dispatcher -> WiGLE parser
    assert n == 2 and w.wardrive_count == 2
    assert w._wardrive_layer is not None


def test_import_wardrive_netxml_onto_map(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    p = tmp_path / "scan.netxml"
    p.write_text(_NETXML)
    w = FlockHeatmapTab()
    n = w.load_wardrive_log(str(p))                       # dispatcher sniffs XML -> netxml parser
    assert n == 1 and w.wardrive_count == 1              # the net with a real avg-fix plots
    assert w._wardrive_layer is not None


def test_import_wardrive_bad_file_is_safe(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    assert w.load_wardrive_log(str(tmp_path / "nope.netxml")) == 0   # missing -> 0, no crash
    assert w.wardrive_count == 0


def test_import_oversized_field_is_safe_not_a_crash(qapp, tmp_path):
    # A field over csv's 128 KB limit makes csv.reader raise -- the import must CATCH it (return 0),
    # not let it escape the clicked slot and abort the app (regression guard for 04e9aaa).
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    p = tmp_path / "huge.csv"
    p.write_text("AA:BB:CC:DD:EE:FF," + "x" * 200000)
    w = FlockHeatmapTab()
    assert w.load_wardrive_log(str(p)) == 0        # dispatcher/CSV parse error caught, not crashed
    assert w.load_wardrive_csv(str(p)) == 0        # WiGLE parse error caught, not crashed
