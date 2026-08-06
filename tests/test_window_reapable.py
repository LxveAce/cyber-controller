"""Regression: a built-but-never-closed CyberControllerWindow is REAPABLE.

Window-building tests that never call win.close() used to leave the HealthMonitor poll thread and
child QThread workers (e.g. FlashTab's construction-time _VariantLoader) running; they accumulated
across the suite and eventually crashed a teardown processEvents() on a dangling cross-thread queued
signal (an intermittent SIGSEGV — the full suite was a ~75% coin-flip). CyberControllerWindow now
exposes shutdown() (the worker-teardown half of closeEvent), so conftest.reap_qt_workers() joins
them after every test. Offscreen Qt; drives no serial, authors no TX.
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


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def test_window_exposes_shutdown(qapp):
    win = _make_window()
    try:
        assert callable(getattr(win, "shutdown", None))   # reapable == has shutdown()
    finally:
        win.close()


def test_reaper_stops_unclosed_windows_health_thread(qapp):
    """A window built and NEVER closed still has its HealthMonitor thread stopped by the reaper, so
    it can't accumulate across the suite + crash a later teardown's processEvents()."""
    from conftest import reap_qt_workers
    win = _make_window()
    assert win._health.is_running          # the poll thread runs from construction
    reap_qt_workers()    # the autouse teardown reaper finds the window's shutdown() and joins it
    assert not win._health.is_running      # ...and stopped it
    win.close()


def test_shutdown_is_idempotent(qapp):
    win = _make_window()
    win.shutdown()
    win.shutdown()                         # second call is a no-op, must not raise
    win.close()                            # close after shutdown still works
