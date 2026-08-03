"""The conftest Qt-worker reaper joins a background QThread a test leaked (EXIT=127 guard).

A widget that starts a worker QThread at construction (clearest: ``FlashTab`` -> ``_VariantLoader``,
which hits the network for board variants) will, if a test builds it but never closes it, leave the
thread running until GC drops the widget — Qt then aborts ("QThread: Destroyed while thread is still
running") as a native EXIT=127 with no traceback: an order-dependent flake. The autouse
``reap_qt_workers`` joins the leak. This proves the mechanism deterministically — build a FlashTab
whose loader is still running, then reap it.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from conftest import reap_qt_workers  # noqa: E402  (the reaper under test)
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_reaper_joins_a_leaked_construction_worker(qapp, monkeypatch):
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt import flash_tab as FT

    # Make the variant load slow enough that the construction-started _VariantLoader is still
    # running right after __init__ returns (mirrors the real network fetch, without the network).
    monkeypatch.setattr(FlashEngine, "list_variants", lambda self, p: (time.sleep(0.4) or []))

    tab = FT.FlashTab(DeviceManager(), FlashEngine())   # __init__ -> _reload_variants starts loader
    running = [w for w in tab._bg_workers if w.isRunning()]
    assert running, "a construction-started _VariantLoader QThread should be running (the leak)"

    reap_qt_workers()   # what the autouse fixture calls after every test

    assert not any(w.isRunning() for w in running), "the reaper must JOIN the leaked worker thread"
    # tab was never explicitly closed — the reaper is the only thing that joined it.


def test_reaper_is_a_noop_without_worker_widgets(qapp):
    # A QApplication with no worker-bearing top-level widget: the reaper must run cleanly and fast.
    reap_qt_workers()   # no assertion needed — it must simply not raise
