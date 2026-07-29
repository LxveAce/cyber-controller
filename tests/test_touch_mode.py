"""Touch-mode resolution + wiring: the responsive layout's touch paths are no longer dead code.

Every tab's ``_relayout_*`` used to pass a hard-coded ``touch=False``, so the touch axis (bigger hit
targets, Nodes' 1-wide stack, Crack stacking) never activated. Now they pass ``touch_active()`` — a
user override (Settings "Touch mode") over runtime detection, applied at startup and persisted.
Offscreen Qt; the override is a process global, so each test resets it.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt import touch_mode as TM  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_touch_mode():
    TM.set_touch_mode("auto")
    yield
    TM.set_touch_mode("auto")


def test_override_forces_touch_active():
    TM.set_touch_mode("on")
    assert TM.touch_active() is True
    TM.set_touch_mode("off")
    assert TM.touch_active() is False


def test_bogus_mode_is_ignored():
    TM.set_touch_mode("on")
    TM.set_touch_mode("nonsense")
    assert TM.get_touch_mode() == "on"     # unchanged


def test_auto_falls_back_to_detection():
    TM.set_touch_mode("auto")
    # On this (non-touch) CI/dev box detection returns False; the point is auto DELEGATES to it.
    assert TM.touch_active() == TM._has_touch_device()


def _maxcol(tab) -> int:
    g = tab._btn_grid
    return max((g.getItemPosition(j)[1] for j in range(g.count())), default=-1) + 1


def test_a_tab_relayout_uses_touch_active(qapp):
    # The wiring: Nodes at a compact width lays out 1-wide under touch, 2-wide under pointer —
    # driven by the touch override. (Reset the size-class debounce between; width is unchanged.)
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab()
    TM.set_touch_mode("on")
    tab._last_nodes_size = None
    tab.resize(480, 800)
    qapp.processEvents()
    tab._relayout_nodes()
    assert _maxcol(tab) == 1
    TM.set_touch_mode("off")
    tab._last_nodes_size = None
    tab.resize(480, 800)
    qapp.processEvents()
    tab._relayout_nodes()
    assert _maxcol(tab) == 2


def test_settings_control_loads_and_persists_touch_mode(qapp):
    from src.ui.qt.settings_tab import SettingsTab
    tab = SettingsTab()
    # load reflects the setting into the combo
    tab._load_into_ui({"interface": {"mode": "pro", "touch_mode": "on"}})
    assert tab._touch_mode_combo.currentData() == "on"
    # a user change applies live to the module global AND persists via _gather
    tab._touch_mode_combo.setCurrentIndex(tab._touch_mode_combo.findData("off"))
    assert TM.get_touch_mode() == "off"                       # applied live
    gathered = tab._gather()
    assert gathered["interface"]["touch_mode"] == "off"       # persisted
    assert "mode" in gathered["interface"]              # Simple/Pro carried forward, not wiped
