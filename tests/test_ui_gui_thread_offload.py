"""UI-audit Batch UI-3 (LOW offloads): blocking work must not run on the Qt GUI thread.

* main_window._on_sidebar_scan ran serial.tools.list_ports.comports() (blocking SetupAPI/registry I/O,
  seconds with many virtual COM ports) inline on every F5 / Scan-Ports press — now a _PortScanWorker QThread.
* flock_heatmap_tab._on_load parsed + reprojected a (potentially huge) cameras.geojson on the GUI thread;
  the scene build must stay on the GUI thread, so the fix gives visible feedback (WaitCursor) + blocks
  re-entry (disable Load) around the load. Here we assert the cursor + button are always restored.
"""

from __future__ import annotations

import json
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── main_window: Scan Ports runs comports() off the GUI thread ────────────────────────────────────

def test_port_scan_worker_runs_off_gui_thread(qapp):
    from types import SimpleNamespace

    from src.ui.qt.main_window import _PortScanWorker

    gui_ident = threading.get_ident()
    ran: dict = {}

    class _FakeDM:
        def scan_ports(self):
            ran["ident"] = threading.get_ident()
            return [SimpleNamespace(port="COM7", name="ESP32")]

    got: dict = {}
    worker = _PortScanWorker(_FakeDM())
    worker.done.connect(lambda devs: got.__setitem__("devs", devs))
    worker.start()
    worker.wait()
    qapp.processEvents()

    assert ran.get("ident") is not None
    assert ran["ident"] != gui_ident  # comports() ran off the GUI thread
    assert got["devs"][0].port == "COM7"  # result delivered via the queued signal


def test_port_scan_worker_swallows_errors(qapp):
    from src.ui.qt.main_window import _PortScanWorker

    class _BoomDM:
        def scan_ports(self):
            raise OSError("enumeration failed")

    got: dict = {}
    worker = _PortScanWorker(_BoomDM())
    worker.done.connect(lambda devs: got.__setitem__("devs", devs))
    worker.start()
    worker.wait()
    qapp.processEvents()

    assert got["devs"] == []  # a scan failure yields an empty list, never crashes the UI


# ── flock: loading a geojson always restores the busy cursor + Load button ────────────────────────

def test_load_geojson_restores_cursor_and_button(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab

    p = tmp_path / "cams.geojson"
    p.write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.3, 47.6]},
                 "properties": {}},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(p), "")))

    tab = FlockHeatmapTab()
    assert QApplication.overrideCursor() is None
    tab._on_load()

    assert tab._btn_load.isEnabled()  # re-enabled in the finally block
    assert QApplication.overrideCursor() is None  # busy cursor restored, not left stuck
    assert tab.camera_count == 1


def test_load_geojson_restores_cursor_even_on_bad_file(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab

    bad = tmp_path / "bad.geojson"
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))

    tab = FlockHeatmapTab()
    tab._on_load()  # load_geojson_file swallows the parse error; the finally must still restore state

    assert tab._btn_load.isEnabled()
    assert QApplication.overrideCursor() is None
    assert tab.camera_count == 0
