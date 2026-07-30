"""OPERATE HOME launcher (src/ui/qt/operate_home.py) — the domain tile grid as a pure launcher.

Spade D6c: OperateHome is no longer a browser. Every tile NAVIGATES to its real surface
(``navigate_requested``) or is a greyed P4 roadmap tile (gps/subghz); there is no in-place
``DomainDetailView`` browser. These assert that launcher behavior, offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.operate_home import OperateHome, build_operate_home  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_starts_on_the_domain_grid(qapp):
    h = OperateHome()
    assert h.current_domain() is None            # a launcher opens nothing in-place
    assert h._grid is not None                   # the grid is the content


def test_selecting_wifi_navigates_to_the_real_surface(qapp):
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("wifi")         # a tile activation announces its key
    assert seen == ["wifi"]                       # navigates to the real analyzer
    assert h.current_domain() is None             # stays a launcher; the host does the navigation
    assert h.domain_view("wifi") is None          # never an embedded browser


def test_selecting_ble_navigates_to_the_real_surface(qapp):
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("ble")
    assert seen == ["ble"]
    assert h.domain_view("ble") is None


def test_tools_and_settings_navigate(qapp):
    # Tools + Settings live in real tabs, so their tiles navigate — never a "coming soon"
    # placeholder that lies about a shipped feature.
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("tools")
    h._grid.domain_selected.emit("settings")
    assert seen == ["tools", "settings"]
    assert h.domain_view("tools") is None and h.domain_view("settings") is None


def test_gps_and_subghz_are_greyed_roadmap_tiles(qapp):
    # Spade D6c (Atlas call): GPS + Sub-GHz land in MAP at P4, so they are greyed, non-activating
    # roadmap tiles — no browser, no navigate-to-nothing. Their cards are disabled (Qt blocks
    # the click) so they never emit navigate_requested, and they build no in-place screen.
    h = OperateHome()
    assert set(h._grid.roadmap_keys()) == {"gps", "subghz"}
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    for key in ("gps", "subghz"):
        assert not h._grid._cards[key].isEnabled()     # greyed, non-activating tile
        assert h.domain_view(key) is None              # no in-place screen built
    # Even a forced show_domain on a roadmap key is a no-op — roadmap tiles never navigate.
    h.show_domain("gps")
    assert seen == []


def test_build_operate_home_returns_a_single_operate_home(qapp):
    home = build_operate_home()
    assert isinstance(home, OperateHome)


def test_build_operate_home_navigates_wifi_ble(qapp):
    # No duplicate analyzers: wifi/ble build no in-place view — tapping navigates to the ONE real
    # analyzer, not a transmit-nothing clone double-fed from the event taps.
    home = build_operate_home()
    assert home.domain_view("wifi") is None and home.domain_view("ble") is None
    seen: list[str] = []
    home.navigate_requested.connect(seen.append)
    home._grid.domain_selected.emit("wifi")
    assert seen == ["wifi"]
    assert home.current_domain() is None
