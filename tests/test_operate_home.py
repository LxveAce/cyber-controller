"""OPERATE HOME shell (src/ui/qt/operate_home.py) — the domain grid ⇄ per-domain screen routing.

Asserts the CLAIM, offscreen: it starts on the domain grid, tapping the Wi-Fi tile routes to the
real three-panel WifiDomainView, Home returns to the grid, each in-place domain has a stack screen,
and an external domain (Tools/Settings) emits navigate_requested instead of a "coming soon" lie.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.ble_domain import BleDomainView  # noqa: E402
from src.ui.qt.operate_home import OperateHome, build_operate_home  # noqa: E402
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
    # Wi-Fi/BLE (and GPS/Sub-GHz) are real domain views; 2.4 GHz + NFC are honest placeholders.
    assert isinstance(h.domain_view("wifi"), WifiDomainView)
    assert isinstance(h.domain_view("ble"), BleDomainView)
    assert not isinstance(h.domain_view("nfc"), WifiDomainView)


def test_external_domains_navigate_instead_of_showing_a_placeholder(qapp):
    # Tools/Settings live in real tabs, so Operate Home ROUTES to them — no "coming soon" lie.
    seen: list[str] = []
    h = OperateHome(external_domains={"tools", "settings"})
    h.navigate_requested.connect(seen.append)
    assert h.domain_view("tools") is None        # no in-place screen built for an external domain
    assert h.domain_view("settings") is None
    h._grid.domain_selected.emit("tools")        # tapping the tile
    assert seen == ["tools"]                      # asks the host to open the real tab
    assert h.current_domain() is None             # and stays on the grid (no placeholder swap)
    assert h._stack.currentWidget() is h._grid
    # A radio with genuinely no screen yet stays an honest in-place placeholder.
    assert h.domain_view("nrf") is not None


def test_selecting_ble_routes_to_the_ble_domain(qapp):
    h = OperateHome()
    h._grid.domain_selected.emit("ble")
    assert h.current_domain() == "ble"
    assert isinstance(h._stack.currentWidget(), BleDomainView)


def test_selecting_gps_routes_to_the_gps_domain(qapp):
    from src.ui.qt.gps_domain import GpsDomainView
    h = OperateHome()
    h._grid.domain_selected.emit("gps")
    assert h.current_domain() == "gps"
    assert isinstance(h._stack.currentWidget(), GpsDomainView)


def test_selecting_subghz_routes_to_the_subghz_domain(qapp):
    from src.ui.qt.subghz_domain import SubGhzDomainView
    h = OperateHome()
    h._grid.domain_selected.emit("subghz")
    assert h.current_domain() == "subghz"
    assert isinstance(h._stack.currentWidget(), SubGhzDomainView)


# ── build_operate_home factory: the app-shell embed with FRESH analyzer centers (no reparenting) ──
def test_build_operate_home_uses_fresh_live_centers(qapp):
    from src.ui.qt.ble_analyzer_tab import BleAnalyzerTab
    from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
    home, wifi_center, ble_center = build_operate_home()
    assert isinstance(home, OperateHome)
    assert isinstance(wifi_center, WifiAnalyzerTab) and isinstance(ble_center, BleAnalyzerTab)
    # the WiFi/BLE domain views use THESE fresh centers (not the shell's parented analyzers)
    assert home.domain_view("wifi")._center is wifi_center
    assert home.domain_view("ble")._center is ble_center


def test_build_operate_home_center_is_live_when_fed(qapp):
    # feeding the fresh WiFi center an event (as the shared tap would) fills its table — a live view
    home, wifi_center, _ble = build_operate_home()
    wifi_center.set_clock(lambda: 1000.0)
    wifi_center.on_wifi_event("COM4", "ap_found",
                              {"bssid": "aa:bb:cc:dd:ee:01", "ssid": "Net", "rssi": -50,
                               "encryption": "WPA2"})
    wifi_center._refresh()
    assert wifi_center._table.rowCount() >= 1


def test_build_operate_home_routes_to_domains(qapp):
    home, _wifi, _ble = build_operate_home()
    home._grid.domain_selected.emit("ble")
    assert home.current_domain() == "ble"
    assert isinstance(home._stack.currentWidget(), BleDomainView)
