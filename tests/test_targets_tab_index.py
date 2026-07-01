"""Targets-tab right-click must resolve actions against the POOLED target (which carries
extra['index']), not a row-reconstructed one. Regression: _target_from_row rebuilds a Target without
'extra', so index-gated actions (BW16 'Deauth (this index)') were silently dropped in this menu even
though the Network tab offered them. Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QMenu  # noqa: E402
from PyQt5.QtCore import QPoint  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_context_menu_resolves_against_pooled_target_with_index(qapp, monkeypatch):
    from src.core.cross_comm import TargetPool, EventBus
    from src.core.action_resolver import ActionResolver
    from src.core.device_manager import DeviceManager
    from src.models.target import Target, TargetType
    from src.ui.qt.targets_tab import TargetsTab

    dm = DeviceManager()
    bus = EventBus()
    pool = TargetPool(bus)
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Net",
                    channel=6, rssi=-40, device_source="COM8", extra={"index": 3}))

    tab = TargetsTab(pool, bus, dm, ActionResolver(dm))
    tab._refresh()  # populate the table from the pool
    assert tab._table.rowCount() >= 1

    captured = {}
    monkeypatch.setattr(tab._resolver, "resolve", lambda t: captured.setdefault("t", t) or {})
    # avoid fragile pixel geometry: itemAt -> the row-0 MAC cell
    mac_item = tab._table.item(0, 2)
    monkeypatch.setattr(tab._table, "itemAt", lambda pos: mac_item)
    # the menu is modal (exec_) — no-op it so the test doesn't block; resolve() ran before exec_
    monkeypatch.setattr(QMenu, "exec_", lambda *a, **k: None)

    tab._on_context_menu(QPoint(0, 0))

    assert captured.get("t") is not None, "resolver.resolve was not called"
    assert captured["t"].extra.get("index") == 3, "must resolve against the pooled target (with index)"
