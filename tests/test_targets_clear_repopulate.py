"""Regression: Targets 'Clear' must NOT persist across a rescan (owner-reported 2026-07-13).

The owner asked whether a Targets 'Clear All' that "persists" is intentional. Investigation showed the
data path is correct — Clear is a deliberate session-wipe of the shared pool, and a fresh scan re-adds
every re-observed AP (the pool key IS the dedup, and clear() empties it; nothing keeps a persistent
"seen" set that would drop a re-observation). These tests lock that in end-to-end and cover the
Clear-aware empty-state hint that makes the wipe read as intentional in the UI.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.target_ingest import TargetIngestor  # noqa: E402
from src.models.target import Target, TargetType  # noqa: E402
from src.protocols.marauder import MarauderProtocol  # noqa: E402
from src.ui.qt.targets_tab import TargetsTab  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeConn:
    """Minimal serial-connection stand-in the ingestor attaches its on_line handler to."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs: list = []

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def remove_line_callback(self, cb) -> None:
        if cb in self._cbs:
            self._cbs.remove(cb)

    def feed(self, *lines: str) -> None:
        for ln in lines:
            for cb in list(self._cbs):
                cb(ln)


# Marauder v1.12.3 multi-line AP form: ESSID -> BSSID -> Ch -> RSSI, two distinct APs.
_SCAN = (
    "ESSID: HomeNet", "BSSID: aa:bb:cc:11:22:33", "Ch: 6", "RSSI: -50",
    "ESSID: CoffeeShop", "BSSID: dd:ee:ff:44:55:66", "Ch: 11", "RSSI: -72",
)


def test_rescan_after_clear_repopulates_pool():
    """Data path: feed a scan -> Clear -> feed the SAME scan again -> the pool repopulates."""
    bus = EventBus()
    pool = TargetPool(bus)
    ingestor = TargetIngestor(pool)
    conn = _FakeConn("COM_TEST")
    ingestor.attach(conn, MarauderProtocol())

    conn.feed(*_SCAN)
    assert pool.count == 2  # initial scan populated

    assert pool.clear() == 2
    assert pool.count == 0  # Clear wiped the shared pool

    conn.feed(*_SCAN)  # a fresh scan re-observes the same APs
    assert pool.count == 2, "rescan after Clear must repopulate — Clear is not allowed to 'persist'"


def test_targets_tab_clear_hint_is_intentional_then_repopulates(_app):
    """UI: after Clear the table is empty with the Clear-aware hint; a rescan repopulates the table and
    drops back to the normal hint."""
    bus = EventBus()
    pool = TargetPool(bus)
    tab = TargetsTab(pool, bus)

    def _ap(mac: str, ssid: str) -> Target:
        return Target(target_type=TargetType.AP, mac=mac, ssid=ssid, rssi=-50, channel=6)

    # Fresh session: normal "how to scan" hint.
    assert tab._empty_hint.text() == tab._HINT_NEVER_SCANNED
    assert not tab._cleared_empty

    pool.add(_ap("AA:AA:AA:AA:AA:01", "HomeNet"))
    tab._refresh()
    assert tab._table.rowCount() == 1
    # isHidden() reflects the setVisible() call regardless of top-level show state (offscreen).
    assert tab._empty_hint.isHidden()

    # Clear (bus event flips the flag synchronously) -> empty + intentional-wipe hint.
    pool.clear()
    assert tab._cleared_empty
    tab._refresh()
    assert tab._table.rowCount() == 0
    assert not tab._empty_hint.isHidden()
    assert tab._empty_hint.text() == tab._HINT_AFTER_CLEAR

    # Rescan -> a target.added flips the flag back, table repopulates, hint hidden.
    pool.add(_ap("AA:AA:AA:AA:AA:01", "HomeNet"))
    assert not tab._cleared_empty
    tab._refresh()
    assert tab._table.rowCount() == 1
    assert tab._empty_hint.isHidden()
