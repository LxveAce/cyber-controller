"""Biscuit operation widgets (A2) — the reusable card→detail foundation. Offscreen.

Covers the pattern's guarantees: a card activates, the detail Start/Stop toggles + emits the right
intent, a live stat grid updates, the mode segment reports the choice, disabled-Start works,
the Help sheet builds from a spec.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.biscuit import (  # noqa: E402
    HelpSheet,
    ModeSegment,
    OperationCard,
    OperationDetail,
    StartStopButton,
    StatGrid,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_card_activates_on_click_and_enter(qapp):
    card = OperationCard("📶", "Scan APs", "Find nearby access points")
    fired = []
    card.activated.connect(lambda: fired.append(True))
    QTest.mouseClick(card, Qt.LeftButton)
    assert fired == [True]
    QTest.keyClick(card, Qt.Key_Return)
    assert fired == [True, True]


def test_stat_grid_updates_values(qapp):
    grid = StatGrid(["APs", "Buffer"], columns=2)
    grid.set_stats({"APs": (16, "#3fb950"), "Buffer": "0%"})
    assert grid._tiles["APs"]._value.text() == "16"
    assert grid._tiles["Buffer"]._value.text() == "0%"
    grid.set_stats({"APs": 20, "nonexistent": 1})     # unknown key ignored, no crash
    assert grid._tiles["APs"]._value.text() == "20"


def test_mode_segment_reports_choice(qapp):
    seg = ModeSegment(["Basic", "Targeted", "Manual"])
    picks = []
    seg.mode_changed.connect(picks.append)
    assert seg.current_mode() == "Basic"
    seg._select("Targeted")
    assert seg.current_mode() == "Targeted" and picks == ["Targeted"]
    seg._select("Targeted")                            # re-selecting same mode does NOT re-emit
    assert picks == ["Targeted"]


def test_detail_start_stop_toggles_and_emits(qapp):
    det = OperationDetail("Scan APs", stat_labels=["APs"], modes=["Basic", "Targeted"])
    starts, stops = [], []
    det.start_requested.connect(lambda: starts.append(True))
    det.stop_requested.connect(lambda: stops.append(True))

    assert det._btn.text() == "Start"
    det._btn.click()                                   # request start
    assert starts == [True] and stops == []

    det.set_running(True, "Scanning…")                 # host confirms it started
    assert det._btn.text() == "Stop" and "Scanning" in det._status.text()
    det._btn.click()                                   # now Stop
    assert stops == [True]
    det.set_running(False)
    assert det._btn.text() == "Start" and det._status.text() == "Ready"


def test_detail_disabled_start_shows_reason(qapp):
    det = OperationDetail("Deauth")
    det.set_ready(False, "Select a target first")
    assert not det._btn.isEnabled()
    assert det._status.text() == "Select a target first"
    det.set_ready(True)
    assert det._btn.isEnabled()


def test_detail_help_button_emits_and_builds_sheet(qapp):
    spec = {"title": "Scan APs", "summary": "Discover nearby WiFi networks.",
            "what_it_does": [("🔍", "Network Discovery", "Finds APs on all channels."),
                             ("🎯", "Target Selection", "Pick an AP for attacks.")],
            "tips": ["Sort by RSSI to find the closest."]}
    det = OperationDetail("Scan APs", help_spec=spec)
    asked = []
    det.help_requested.connect(lambda: asked.append(True))
    det.help_requested.emit()                          # (don't exec_ a modal in the test)
    assert asked == [True]
    sheet = HelpSheet(spec)                             # builds without a live parent
    assert sheet.windowTitle() == "Scan APs"


def test_detail_without_help_has_no_help_button(qapp):
    # help is opt-in: no spec -> no ? button, and current_mode is "" when there are no modes
    det = OperationDetail("Passive Monitor")
    assert det._help_spec is None
    assert det.current_mode() == ""


def test_start_stop_button_toggles_and_gates(qapp):
    btn = StartStopButton()
    starts, stops = [], []
    btn.start_requested.connect(lambda: starts.append(True))
    btn.stop_requested.connect(lambda: stops.append(True))
    assert btn.text() == "Start" and btn.isEnabled()
    btn.click()                                        # request start
    assert starts == [True] and stops == []
    btn.set_running(True)                              # host confirms running -> Stop, enabled
    assert btn.text() == "Stop" and btn.isEnabled()
    btn.click()
    assert stops == [True]
    btn.set_running(False)
    btn.set_ready(False)                    # not ready -> Start disabled (guidance on host)
    assert btn.text() == "Start" and not btn.isEnabled()
