"""Wardrive tabs must be stopped on app exit so the firmware isn't left scanning.

Regression for audit finding [10]: the main window's closeEvent joined _flash_tab / _software_tab /
_flock_heatmap but NOT _wardrive_tab / _wardrive_multi_tab, and neither wardrive tab had a shutdown()/stop
bridge — so quitting mid-capture left the ESP32 scanning (the firmware STOP verb was never sent) and skipped
the owner-aware port release + CSV close. Offscreen Qt; fixture mirrors test_terminal_clear.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


@pytest.fixture
def window(qapp, isolated_settings):
    from PyQt5.QtCore import QTimer

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()
    yield win
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    win.deleteLater()
    qapp.processEvents()


def test_wardrive_shutdown_sends_stop_when_capturing(window):
    tab = window._wardrive_tab
    calls = []

    class _FakeWorker:
        def stop(self):
            calls.append("stop")

        def isRunning(self):  # noqa: N802
            return False

    tab._worker = _FakeWorker()
    tab._btn_stop.setEnabled(True)  # simulate an active capture

    tab.shutdown()

    assert calls == ["stop"]              # firmware STOP verb sent (via _on_stop -> worker.stop)
    assert not tab._btn_stop.isEnabled()  # capture marked stopped


def test_wardrive_multi_shutdown_sends_stop_when_capturing(window):
    tab = window._wardrive_multi_tab
    calls = []

    class _FakeController:
        def stop(self):
            calls.append("stop")

        def snapshot(self):
            return {}

    tab._controller = _FakeController()
    tab._fh = None
    tab._btn_stop.setEnabled(True)

    tab.shutdown()

    assert calls == ["stop"]              # controller.stop() -> STOP verb per board


def test_closeevent_shuts_down_both_wardrive_tabs(window):
    win = window
    stopped = []
    win._wardrive_tab.shutdown = lambda: stopped.append("wardrive")
    win._wardrive_multi_tab.shutdown = lambda: stopped.append("wardrive_multi")

    win.close()  # fires closeEvent

    assert "wardrive" in stopped
    assert "wardrive_multi" in stopped
