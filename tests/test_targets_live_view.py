"""Live-view toggle on the Targets tab (1.7.0).

The tab defaults to showing the WHOLE session; toggling "Live view" filters the table down to targets
seen within ``_LIVE_VIEW_WINDOW_S`` seconds ("currently in range") without touching the shared pool.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.models.target import Target, TargetType  # noqa: E402
from src.ui.qt.targets_tab import TargetsTab, _LIVE_VIEW_WINDOW_S  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app


def _mk(mac: str, ssid: str, age_s: float) -> Target:
    """A Target last seen ``age_s`` seconds ago (0 = right now)."""
    seen = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return Target(
        target_type=TargetType.AP, mac=mac, ssid=ssid, rssi=-50, channel=6,
        timestamp=seen, last_seen=seen,
    )


def _visible(tab: TargetsTab) -> set[str]:
    return {
        tab._table.item(r, 1).text()
        for r in range(tab._table.rowCount())
        if not tab._table.isRowHidden(r)
    }


def test_live_view_toggle_filters_by_freshness(_app):
    bus = EventBus()
    pool = TargetPool(bus)
    tab = TargetsTab(pool, bus)

    pool.add(_mk("AA:AA:AA:AA:AA:01", "FreshNet", age_s=1))
    pool.add(_mk("AA:AA:AA:AA:AA:02", "StaleNet", age_s=_LIVE_VIEW_WINDOW_S + 60))
    tab._refresh()

    # Default OFF → the whole session is visible.
    assert not tab._live_view.isChecked()
    assert _visible(tab) == {"FreshNet", "StaleNet"}

    # ON → only the in-range (fresh) target survives.
    tab._live_view.setChecked(True)
    assert _visible(tab) == {"FreshNet"}

    # OFF again → whole session restored (pool never lost the stale target).
    tab._live_view.setChecked(False)
    assert _visible(tab) == {"FreshNet", "StaleNet"}


def test_live_view_combines_with_search(_app):
    bus = EventBus()
    pool = TargetPool(bus)
    tab = TargetsTab(pool, bus)

    pool.add(_mk("BB:BB:BB:BB:BB:01", "AlphaFresh", age_s=1))
    pool.add(_mk("BB:BB:BB:BB:BB:02", "BravoFresh", age_s=1))
    pool.add(_mk("BB:BB:BB:BB:BB:03", "AlphaStale", age_s=_LIVE_VIEW_WINDOW_S + 60))
    tab._refresh()

    tab._live_view.setChecked(True)
    tab._search_input.setText("alpha")  # fires textChanged → _apply_filter

    # "alpha" matches AlphaFresh + AlphaStale; live view drops the stale one.
    assert _visible(tab) == {"AlphaFresh"}


def test_epoch_helper_handles_types():
    now = datetime.now(timezone.utc)
    assert abs(TargetsTab._epoch(now) - now.timestamp()) < 0.001
    assert TargetsTab._epoch(1234.5) == 1234.5
    assert TargetsTab._epoch(None) is None
    assert TargetsTab._epoch("not-a-time") is None
