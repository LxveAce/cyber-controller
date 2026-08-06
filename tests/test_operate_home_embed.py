"""Smoke test for the Operate Home embed into the app shell (CyberControllerWindow).

Verifies the dual-axis shell is wired into the ACTUAL app additively: the OPERATE HOME tab is
present, the existing top-level tabs are intact (nothing disrupted), and — Spade v2 P2c (D7 removed)
— Operate Home no longer embeds duplicate WiFi/BLE analyzers; wifi/ble are EXTERNAL tiles that
navigate to the ONE real analyzer, so there are no clones + no orphan taps to crash into.
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
    # P2.5: Operate Home is no longer a peer top-level tab — it is the launcher sub-view of the ONE OPERATE
    # verb surface (the double-Operate is gone). The 5 verb surfaces + the pinned Settings are all present.
    labels = _tab_labels(window)
    assert "OPERATE" in labels
    # Reform (P2): OPERATE opens on the Console (the merged Operate splitter); the tile Operate-Home is
    # retired from the surface (kept constructed + hidden).
    assert window._operate_surface.widget(0) is window._operate_action   # Console leads the OPERATE sub-views
    for existing in ("DEVICE", "HUNT", "OPERATE", "CRACK", "MAP", "Settings"):
        assert existing in labels


def test_operate_home_has_no_duplicate_analyzers(window):
    # D7 removed: wifi/ble are EXTERNAL (navigate) — no embedded clones, no _oh_wifi/_oh_ble attrs.
    assert isinstance(window._operate_home, OperateHome)
    assert not hasattr(window, "_oh_wifi") and not hasattr(window, "_oh_ble")
    assert window._operate_home.domain_view("wifi") is None   # external -> no embedded view
    assert window._operate_home.domain_view("ble") is None


def test_operate_home_wifi_tile_navigates_to_the_real_analyzer(window):
    # tapping the Wi-Fi tile routes to HUNT's ONE real analyzer (re-homed from Analyze), not an embedded dupe
    if window._wifi_analyzer is None:
        pytest.skip("no Wi-Fi analyzer in this build")
    window._operate_home._grid.domain_selected.emit("wifi")
    assert window._hunt_surface.currentWidget() is window._wifi_analyzer


def test_the_one_real_wifi_analyzer_is_fed_by_the_shared_tap(window):
    sig = getattr(window, "_wifi_event_signal", None)
    if sig is None or window._wifi_analyzer is None:
        pytest.skip("Wi-Fi analyzer tap not wired in this construction")
    window._wifi_analyzer.set_clock(lambda: 5000.0)
    sig.wifi_event.emit("COM4", "ap_found",
                        {"bssid": "aa:bb:cc:dd:ee:09", "ssid": "Z", "rssi": -55,
                         "encryption": "WPA2"})
    window._wifi_analyzer._refresh()
    assert window._wifi_analyzer._table.rowCount() >= 1  # the ONE real analyzer folds in the event
