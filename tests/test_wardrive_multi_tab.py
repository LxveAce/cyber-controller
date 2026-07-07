"""Offscreen tests for the multi-device wardrive Qt tab (F1 slice 4b).

The widget is thin — capture lives in MultiWardriveController — so these cover the Qt-side logic: it lists
the connected boards, honours the checkboxes, and renders a controller snapshot into the status table
without needing a live controller or any serial ports.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.wardrive_multi_tab import WardriveMultiTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Dev:
    def __init__(self, port: str, fw: str) -> None:
        self.port = port
        self.firmware = fw


class _FakeDM:
    def __init__(self, devs) -> None:
        self._devs = [_Dev(p, f) for p, f in devs]

    def list_connected(self):
        return list(self._devs)


class _MutableDM(_FakeDM):
    """Device manager whose connected-board list can change between refreshes."""

    def set(self, devs) -> None:
        self._devs = [_Dev(p, f) for p, f in devs]


def test_lists_connected_boards(qapp):
    tab = WardriveMultiTab(device_manager=_FakeDM([("COM3", "marauder"), ("COM4", "")]))
    assert tab._board_list.count() == 2                    # populated from dm.list_connected()
    assert tab._checked_boards() == [("COM3", "marauder"), ("COM4", "")]   # default: all ticked


def test_checkboxes_filter_selection(qapp):
    tab = WardriveMultiTab(device_manager=_FakeDM([("COM3", "marauder"), ("COM4", "")]))
    tab._board_list.item(1).setCheckState(Qt.Unchecked)    # drop the second board
    assert tab._checked_boards() == [("COM3", "marauder")]


def test_apply_snapshot_renders_status_table(qapp):
    tab = WardriveMultiTab(device_manager=_FakeDM([]))
    snap = {"running": True, "fix": "48.07, 11.51", "total_aps": 3,
            "boards": [{"port": "COM3", "firmware": "marauder", "aps": 2, "started": True},
                       {"port": "COM4", "firmware": "", "aps": 1, "started": True}]}
    tab._apply_snapshot(snap)
    t = tab._status_table
    assert t.rowCount() == 2
    assert t.item(0, 0).text() == "COM3" and t.item(0, 2).text() == "2"
    assert t.item(1, 1).text() == "(auto)"                 # blank firmware shown as auto
    assert "Total APs: 3" in tab._total_label.text()
    assert "48.07, 11.51" in tab._total_label.text()


def test_start_guards_when_no_board_selected(qapp):
    tab = WardriveMultiTab(device_manager=_FakeDM([("COM3", "marauder")]))
    tab._board_list.item(0).setCheckState(Qt.Unchecked)    # nothing ticked
    tab._on_start()
    assert tab._controller is None                          # guarded — no run started, no file opened
    assert "at least one board" in tab._total_label.text().lower()


def test_no_device_manager_is_safe(qapp):
    tab = WardriveMultiTab(device_manager=None)
    assert tab._board_list.count() == 0                    # nothing to list, no crash
    tab._on_start()
    assert tab._controller is None


def test_new_board_defaults_checked_after_refresh(qapp):
    # Two boards connected and auto-checked, then a brand-new board is plugged in.
    dm = _MutableDM([("COM3", "marauder"), ("COM4", "")])
    tab = WardriveMultiTab(device_manager=dm)
    assert tab._checked_boards() == [("COM3", "marauder"), ("COM4", "")]  # both auto-ticked

    dm.set([("COM3", "marauder"), ("COM4", ""), ("COM5", "ghostesp")])
    tab._refresh_boards()
    assert tab._board_list.count() == 3
    # The never-seen board must default to checked even though others are already ticked.
    assert ("COM5", "ghostesp") in tab._checked_boards()


def test_unchecked_board_stays_unchecked_across_refresh(qapp):
    # A deliberately-unticked board must not be silently re-checked by a refresh.
    dm = _MutableDM([("COM3", "marauder"), ("COM4", "")])
    tab = WardriveMultiTab(device_manager=dm)
    tab._board_list.item(1).setCheckState(Qt.Unchecked)     # operator drops COM4
    assert tab._checked_boards() == [("COM3", "marauder")]

    tab._refresh_boards()                                    # no topology change
    assert tab._checked_boards() == [("COM3", "marauder")]  # untick preserved, not new
