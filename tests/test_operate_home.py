"""OPERATE HOME shell (src/ui/qt/operate_home.py) — the domain grid ⇄ per-domain screen routing.

Asserts the CLAIM, offscreen: it starts on the domain grid, tapping the Wi-Fi tile routes to the
real three-panel WifiDomainView, Home returns to the grid, and every domain has a stack screen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.ble_domain import BleDomainView  # noqa: E402
from src.ui.qt.operate_home import OperateHome  # noqa: E402
from src.ui.qt.wifi_domain import WifiDomainView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_starts_on_the_domain_grid(qapp):
    h = OperateHome()
    assert h.current_domain() is None
    assert h._stack.currentWidget() is h._grid


def test_selecting_wifi_routes_to_the_three_panel_view(qapp):
    h = OperateHome()
    shown = []
    h.domain_shown.connect(shown.append)
    h._grid.domain_selected.emit("wifi")   # a tile activation announces its key
    assert h.current_domain() == "wifi"
    assert isinstance(h._stack.currentWidget(), WifiDomainView)
    assert shown == ["wifi"]


def test_home_returns_to_the_grid(qapp):
    h = OperateHome()
    h.show_domain("ble")
    assert h.current_domain() == "ble"
    h.show_home()
    assert h.current_domain() is None
    assert h._stack.currentWidget() is h._grid


def test_every_domain_has_a_screen(qapp):
    h = OperateHome()
    for key in h._grid.domain_keys():
        assert h.domain_view(key) is not None
    # Wi-Fi and BLE are real domain views on the shared frame; the rest are placeholders.
    assert isinstance(h.domain_view("wifi"), WifiDomainView)
    assert isinstance(h.domain_view("ble"), BleDomainView)
    assert not isinstance(h.domain_view("nfc"), WifiDomainView)


def test_selecting_ble_routes_to_the_ble_domain(qapp):
    h = OperateHome()
    h._grid.domain_selected.emit("ble")
    assert h.current_domain() == "ble"
    assert isinstance(h._stack.currentWidget(), BleDomainView)
