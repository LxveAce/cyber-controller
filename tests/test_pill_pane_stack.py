"""PillPaneStack (reform W1) — a destination surface: pill sub-nav over stacked panes.

The drop-in for the inner QTabWidget strips: pills switch the panes, the first is shown, a None pane
is skipped (honest-empty), and pane_changed fires on both a user pill click and a programmatic
select. Structure only — no send path. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from src.ui.qt.pill_pane_stack import PillPaneStack  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_builds_panes_and_shows_first(qapp):
    s = PillPaneStack()
    a, b = QLabel("A"), QLabel("B")
    s.set_panes([("dashboard", "Dashboard", a), ("mesh", "Mesh", b)])
    assert s.keys() == ["dashboard", "mesh"]
    assert s.current() == "dashboard"
    assert s._stack.currentWidget() is a


def test_none_pane_is_skipped(qapp):
    s = PillPaneStack()
    s.set_panes([("wifi", "Wi-Fi", QLabel("w")), ("ble", "BLE", None),
                 ("targets", "Targets", QLabel("t"))])
    assert s.keys() == ["wifi", "targets"]        # the None BLE pane never becomes a blank pane


def test_select_switches_pane_and_emits(qapp):
    s = PillPaneStack()
    a, b = QLabel("A"), QLabel("B")
    s.set_panes([("console", "Console", a), ("macros", "Macros", b)])
    got: list[str] = []
    s.pane_changed.connect(got.append)
    s.select("macros")
    assert s._stack.currentWidget() is b and s.current() == "macros" and got == ["macros"]
    s.select("nope")                               # unknown key -> no-op, no emit
    assert got == ["macros"]


def test_user_pill_click_switches_pane(qapp):
    s = PillPaneStack()
    a, b = QLabel("A"), QLabel("B")
    s.set_panes([("console", "Console", a), ("macros", "Macros", b)])
    got: list[str] = []
    s.pane_changed.connect(got.append)
    s._pills._pills[1][1].click()                  # click the Macros pill
    assert s._stack.currentWidget() is b and got == ["macros"]
