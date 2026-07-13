"""Regression — TargetsTab and CrossCommTab must COALESCE a scan burst into one table rebuild.

Root cause (UI-audit run wf_3f2a4b2c-b41): both tabs rebuilt the whole table (setRowCount + recreate
every cell, plus _apply_filter in TargetsTab) on EVERY target.* bus event. TargetPool.add publishes one
event per (re-)observed AP, so during a wardrive that is O(N^2) GUI-thread work per sweep and janks the
app. The sibling NetworkTab already coalesces the identical pattern with a single-shot 400ms debounce;
these two did not.

The fix mirrors NetworkTab: route target.* events through a single-shot timer whose timeout does one
rebuild, and skip the rebuild while the tab is hidden. This asserts a burst of N events schedules exactly
ONE rebuild rather than N. Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _targets_tab():
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.targets_tab import TargetsTab

    return TargetsTab(TargetPool(), EventBus())


def _cross_comm_tab():
    from src.core.cross_comm import AutoRouter, EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.ui.qt.cross_comm_tab import CrossCommTab

    bus = EventBus()
    return CrossCommTab(bus, TargetPool(), AutoRouter(bus, lambda port, cmd: None), DeviceManager())


def test_targets_tab_coalesces_burst_into_one_refresh(qapp):
    tab = _targets_tab()
    tab.show()
    qapp.processEvents()

    calls = {"n": 0}
    tab._refresh = lambda: calls.__setitem__("n", calls["n"] + 1)  # count rebuilds

    for _ in range(50):  # a scan-burst of 50 target events
        tab._bus_callback("target.updated", {})
    qapp.processEvents()  # deliver the queued _bridge.changed -> _refresh_timer.start

    assert calls["n"] == 0  # NOT rebuilt per event (was 50 before the fix)
    assert tab._refresh_timer.isActive()  # exactly one coalesced rebuild is pending

    tab._debounced_refresh()  # the timer fires once
    assert calls["n"] == 1


def test_targets_tab_defers_rebuild_while_hidden(qapp):
    tab = _targets_tab()
    tab.hide()
    calls = {"n": 0}
    tab._refresh = lambda: calls.__setitem__("n", calls["n"] + 1)

    tab._debounced_refresh()  # hidden -> defer, don't rebuild an off-screen table
    assert calls["n"] == 0
    assert tab._dirty is True


def test_cross_comm_tab_coalesces_burst_into_one_pool_refresh(qapp):
    tab = _cross_comm_tab()
    tab.show()
    qapp.processEvents()

    calls = {"n": 0}
    tab._refresh_pool = lambda: calls.__setitem__("n", calls["n"] + 1)

    for _ in range(50):
        tab._on_bus_event("target.updated", {"mac": "AA:BB", "ssid": "x"})

    assert calls["n"] == 0  # per-event rebuild removed (was 50 before the fix)
    assert tab._pool_refresh_timer.isActive()  # one coalesced rebuild pending

    tab._debounced_refresh_pool()
    assert calls["n"] == 1


def test_cross_comm_event_log_still_updates_per_event(qapp):
    # The debounce must only affect the pool TABLE — the event log should still record every event.
    tab = _cross_comm_tab()
    tab.show()
    qapp.processEvents()

    appended = {"n": 0}
    tab._append_event = lambda topic, payload: appended.__setitem__("n", appended["n"] + 1)

    for _ in range(5):
        tab._on_bus_event("target.updated", {"mac": "AA:BB"})

    assert appended["n"] == 5  # every event logged immediately, not coalesced
