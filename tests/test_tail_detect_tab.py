"""TailDetectTab (src/ui/qt/tail_detect_tab.py) — the HUNT follower/tail-detection panel.

Verifies the panel over PersistenceTracker: it flags a device that keeps reappearing (persistence >=
threshold, strongest first), the threshold control filters, "mark as mine" drops a device into the
ignore list, it attaches read-only to the ingestor stream (no send path), and shutdown detaches.
Deterministic via a fixed now_fn + pre-seeded tracker. Offscreen Qt; no TX.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.tail_detect import PersistenceTracker  # noqa: E402
from src.ui.qt.tail_detect_tab import TailDetectTab  # noqa: E402

_NOW = 1_000_000.0
_W = 300   # window_seconds


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _seeded_tracker():
    """AA in 4 windows -> persistence 1.0; CC in 2 -> 0.5; EE in 1 -> 0.25."""
    tr = PersistenceTracker(window_seconds=_W, num_windows=4)
    for k in range(4):
        tr.observe("AA:BB", _NOW - k * _W, label="probe phone")
    for k in range(2):
        tr.observe("CC:DD", _NOW - k * _W, label="BLE watch")
    tr.observe("EE:FF", _NOW, label="client")
    return tr


def _panel(qapp, ingestor=None, tracker=None):
    return TailDetectTab(ingestor=ingestor, tracker=tracker or _seeded_tracker(),
                         now_fn=lambda: _NOW)


class _Ev:
    def __init__(self, event_type, data):
        self.event_type, self.data = event_type, data


class _FakeIngestor:
    def __init__(self):
        self.observers = []

    def add_event_observer(self, cb):
        self.observers.append(cb)

    def remove_event_observer(self, cb):
        self.observers.remove(cb)


def test_flags_persistent_devices_strongest_first(qapp):
    p = _panel(qapp)
    assert p._table.rowCount() == 2          # AA(1.0) + CC(0.5) >= 0.5; EE(0.25) excluded
    assert p._table.item(0, 0).text() == "AA:BB"          # strongest first
    assert p._table.item(0, 2).text() == "1.00"
    assert p._table.item(1, 0).text() == "CC:DD"


def test_threshold_filters(qapp):
    p = _panel(qapp)
    p._threshold_spin.setValue(0.8)                        # stalking-only
    assert p._table.rowCount() == 1                        # only AA (1.0) survives
    assert p._table.item(0, 0).text() == "AA:BB"


def test_mark_as_mine_ignores_device(qapp, monkeypatch):
    p = _panel(qapp)
    monkeypatch.setattr(p, "_save_ignore", lambda: None)   # don't touch real Settings
    p._table.selectRow(0)                                  # AA:BB
    p._mark_selected_ignored()
    assert "AA:BB" in p._tracker.ignore
    devices = {p._table.item(r, 0).text() for r in range(p._table.rowCount())}
    assert "AA:BB" not in devices                          # dropped from the flagged table


def test_attaches_read_only_to_ingestor(qapp):
    ing = _FakeIngestor()
    tr = PersistenceTracker(window_seconds=_W, num_windows=4)
    p = TailDetectTab(ingestor=ing, tracker=tr, now_fn=lambda: _NOW)
    assert len(ing.observers) == 1                         # attached exactly one read-only observer
    ing.observers[0](_Ev("probe_request", {"mac": "99:88"}), "COM3")   # feed a personal detection
    assert "99:88" in tr._seen                             # the tracker observed it
    assert not hasattr(p, "_send")                         # awareness-only: no send path


def test_shutdown_detaches_and_stops(qapp):
    ing = _FakeIngestor()
    p = TailDetectTab(ingestor=ing, tracker=_seeded_tracker(), now_fn=lambda: _NOW)
    assert len(ing.observers) == 1
    p.shutdown()
    assert ing.observers == []                             # observer removed
    assert not p._timer.isActive()


def test_model_tie_in_routes_alerts_only_when_wired(qapp, monkeypatch):
    """When a shared MetricsModel is wired, each refresh routes flagged tails to ALERT readings
    (Dashboard); with no model it never calls tails_to_alerts (Atlas's tie-in, cc d02c7c7)."""
    import src.ui.qt.tail_detect_tab as mod
    calls = []
    monkeypatch.setattr(mod, "tails_to_alerts",
                        lambda tracker, model, now, thr: calls.append((model, now, thr)))
    sentinel = object()
    TailDetectTab(tracker=_seeded_tracker(), now_fn=lambda: _NOW, model=sentinel)   # refresh runs
    assert calls and calls[-1][0] is sentinel and calls[-1][1] == _NOW
    calls.clear()
    TailDetectTab(tracker=_seeded_tracker(), now_fn=lambda: _NOW)                   # model=None
    assert calls == []
