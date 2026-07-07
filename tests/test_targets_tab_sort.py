"""Targets-tab numeric columns (Ch, RSSI) must sort as numbers, not as text.

Regression: _refresh() stored the Channel/RSSI cells as strings (QTableWidgetItem(str(...))) while
user sorting was enabled, so QTableWidget compared them lexicographically — channels ordered
1, 10, 2 and RSSI ordered "-5" before "-60". Storing an int in the DisplayRole restores numeric
ordering. Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab():
    from src.core.cross_comm import TargetPool, EventBus
    from src.core.action_resolver import ActionResolver
    from src.core.device_manager import DeviceManager
    from src.models.target import Target, TargetType

    dm = DeviceManager()
    bus = EventBus()
    pool = TargetPool(bus)
    # channels chosen so string sort (1, 10, 2) differs from numeric sort (1, 2, 10);
    # rssi chosen so string sort ("-40" < "-5" < "-60") differs from numeric (-60 < -40 < -5).
    pool.add(Target(mac="AA:00:00:00:00:01", target_type=TargetType.AP, ssid="a",
                    channel=1, rssi=-40, device_source="COM8"))
    pool.add(Target(mac="AA:00:00:00:00:02", target_type=TargetType.AP, ssid="b",
                    channel=10, rssi=-5, device_source="COM8"))
    pool.add(Target(mac="AA:00:00:00:00:03", target_type=TargetType.AP, ssid="c",
                    channel=2, rssi=-60, device_source="COM8"))

    from src.ui.qt.targets_tab import TargetsTab
    tab = TargetsTab(pool, bus, dm, ActionResolver(dm))
    tab._refresh()
    return tab


def _column_order(tab, col):
    return [int(tab._table.item(r, col).text()) for r in range(tab._table.rowCount())]


def test_channel_column_sorts_numerically(qapp):
    tab = _make_tab()
    assert tab._table.rowCount() == 3
    tab._table.sortItems(4, Qt.AscendingOrder)   # Ch column
    assert _column_order(tab, 4) == [1, 2, 10]


def test_rssi_column_sorts_numerically(qapp):
    tab = _make_tab()
    assert tab._table.rowCount() == 3
    tab._table.sortItems(3, Qt.AscendingOrder)   # RSSI column
    assert _column_order(tab, 3) == [-60, -40, -5]
