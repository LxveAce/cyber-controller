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
    for sig in ("status", "updated", "location", "line", "stopped"):
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


def test_shutdown_stops_and_waits_live_worker(qapp):
    """closeEvent calls FlockHeatmapTab.shutdown(); unlike hideEvent (which keeps the worker running on
    purpose), it must stop AND wait the live QThread so run()'s finally-block closes both serial ports
    before the wrapper is destroyed on exit."""
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    tab = FlockHeatmapTab()

    class _W:
        def __init__(self) -> None:
            self.stopped = False
            self.waited = False
            self._run = True

        def stop(self) -> None:
            self.stopped = True

        def isRunning(self) -> bool:  # noqa: N802
            return self._run

        def wait(self, *_a) -> bool:
            self.waited = True
            self._run = False
            return True

    w = _W()
    tab._live_worker = w
    tab.shutdown()
    assert w.stopped, "shutdown must ask the live scan worker to stop"
    assert w.waited, "shutdown must wait for the worker to close its serial ports before teardown"


def test_shutdown_without_worker_is_safe(qapp):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    tab = FlockHeatmapTab()
    assert tab._live_worker is None
    tab.shutdown()  # no live scan running -> a clean no-op, must not raise


def test_main_window_closeevent_joins_tab_workers(qapp, tmp_path, monkeypatch):
    """End-to-end: the real MainWindow.closeEvent must stop-wait the SoftwareTab OS-flash worker and the
    FlockHeatmapTab live-scan worker, so neither unparented QThread is destroyed mid-run on exit."""
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)

    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))

    class _SoftW:
        def __init__(self) -> None:
            self.waited = False
            self._run = True

        def isRunning(self) -> bool:  # noqa: N802
            return self._run

        def wait(self, *_a) -> bool:
            self.waited = True
            self._run = False
            return True

    class _LiveW:
        def __init__(self) -> None:
            self.stopped = False
            self.waited = False
            self._run = True

        def stop(self) -> None:
            self.stopped = True

        def isRunning(self) -> bool:  # noqa: N802
            return self._run

        def wait(self, *_a) -> bool:
            self.waited = True
            self._run = False
            return True

    sw_worker = _SoftW()
    live_worker = _LiveW()
    win._software_tab._worker = sw_worker
    win._flock_heatmap._live_worker = live_worker

    win.close()  # -> closeEvent

    assert sw_worker.waited, "closeEvent must wait for the SoftwareTab OS-flash worker"
    assert live_worker.stopped and live_worker.waited, "closeEvent must stop-wait the live Flock scan"


def test_live_scan_line_signal_is_surfaced(qapp, monkeypatch):
    """Regression: the worker's `line` signal (start/stop notices AND the failure paths — pyserial
    missing, busy/denied COM port) was emitted but never connected, so scan errors were silently
    swallowed. _toggle_live must wire `line` to a visible surface. We stub the worker with real Qt
    signals (no thread / no serial) and assert an emitted diagnostic reaches the live-log pane."""
    from PyQt5.QtCore import QObject, pyqtSignal

    from src.ui.qt import flock_heatmap_tab as fht

    class _FakeWorker(QObject):
        status = pyqtSignal(str, int)
        updated = pyqtSignal(dict)
        location = pyqtSignal(float, float, bool)   # matches _FlockWorker (drives the "you are here" marker)
        line = pyqtSignal(str)
        stopped = pyqtSignal()

        def __init__(self, *_a, **_k) -> None:
            super().__init__()
            self.started = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            pass

    monkeypatch.setattr(fht, "_FlockWorker", _FakeWorker)
    tab = fht.FlockHeatmapTab()
    tab._dev_combo.addItem("COM_FAKE")
    tab._dev_combo.setCurrentText("COM_FAKE")

    assert tab._live_log.toPlainText() == ""
    tab._toggle_live()
    assert isinstance(tab._live_worker, _FakeWorker) and tab._live_worker.started

    # an emitted diagnostic (e.g. a busy/denied port) must land on the log surface — before the fix the
    # `line` signal was connected to nothing, so this text vanished and the operator saw only "Idle".
    tab._live_worker.line.emit("flock scan error: [Errno 13] Access is denied")
    assert "flock scan error" in tab._live_log.toPlainText()


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
