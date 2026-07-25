"""Smoke test for the Operate Home embed into the app shell (CyberControllerWindow).

Verifies the dual-axis shell is wired into the ACTUAL app additively: the OPERATE HOME tab is
present, the existing top-level tabs are intact (nothing disrupted), the embedded OperateHome uses
the shell's FRESH analyzer centers (no reparenting), and those centers are fed from the event tap.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.ui.qt.main_window import CyberControllerWindow  # noqa: E402
from src.ui.qt.operate_home import OperateHome  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def window(qapp):
    return CyberControllerWindow(DeviceManager(), FlashEngine(), EventBus(), TargetPool())


def _tab_labels(win):
    return [win._tabs.tabText(i) for i in range(win._tabs.count())]


def test_operate_home_tab_is_embedded_additively(window):
    labels = _tab_labels(window)
    assert "Operate Home" in labels
    # every existing top-level tab is still present — the embed is additive, not a disruption
    for existing in ("Flash", "Connect", "Operate", "Survey", "Analyze", "Settings"):
        assert existing in labels


def test_embedded_operate_home_is_the_real_shell_with_fresh_centers(window):
    assert isinstance(window._operate_home, OperateHome)
    # the WiFi/BLE domain views use the shell's FRESH centers, not the parented analyzer instances
    assert window._operate_home.domain_view("wifi")._center is window._oh_wifi
    assert window._operate_home.domain_view("ble")._center is window._oh_ble
    assert window._oh_wifi is not window._wifi_analyzer   # a distinct instance (no reparenting)


def test_embedded_operate_home_routes_to_a_domain(window):
    window._operate_home._grid.domain_selected.emit("wifi")
    assert window._operate_home.current_domain() == "wifi"


def test_embedded_center_is_fed_by_the_shared_tap(window):
    sig = getattr(window, "_wifi_event_signal", None)
    if sig is None:
        pytest.skip("Wi-Fi analyzer tap not wired in this construction (no ingestor)")
    window._oh_wifi.set_clock(lambda: 5000.0)
    sig.wifi_event.emit("COM4", "ap_found",
                        {"bssid": "aa:bb:cc:dd:ee:09", "ssid": "Z", "rssi": -55,
                         "encryption": "WPA2"})
    window._oh_wifi._refresh()
    assert window._oh_wifi._table.rowCount() >= 1  # the fresh center folded in the tapped event
