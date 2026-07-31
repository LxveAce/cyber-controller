"""P3 flow B fix — "Send capture to Crack Lab" must never SILENTLY no-op.

A network-wide (BSSID-less) handshake ticks "HS" on every AP sharing the ESSID but is logged under
ONE BSSID. Clicking a sibling AP row must resolve the capture by SSID; if nothing matches at all,
the operator is told (a toast), never a silent no-op on a tick that promised a capture. Offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


@pytest.fixture
def win(qapp):
    w = _make_window()
    try:
        w._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in w.findChildren(QTimer):
        t.stop()
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


def test_no_matching_capture_toasts_instead_of_silent_noop(win):
    # A BSSID with no capture and no analyzer AP must NOT silently return: the surface is unchanged
    # (no bogus nav) AND the operator is told via a toast (the honest-functionality fix).
    win._tabs.setCurrentWidget(win._settings_tab)
    before = win._tabs.currentWidget()
    win._app_shell._toast_label.clear()
    win._on_crack_capture_requested("de:ad:be:ef:00:99")
    assert win._tabs.currentWidget() is before                 # no navigation on a miss
    assert win._app_shell._toast_label.text() != ""            # but NOT silent — the operator is told


def test_ssid_fallback_resolves_a_sibling_ap_capture(win):
    # Capture logged under BSSID-A (ssid "SharedNet"); the operator clicks a SIBLING AP (BSSID-B, same
    # ssid, seen by the analyzer). It must resolve by SSID -> CRACK shown + the record loaded.
    if win._wifi_analyzer is None:
        pytest.skip("no Wi-Fi analyzer in this build")
    from src.models.capture import CaptureRecord
    rec = CaptureRecord(bssid="aa:aa:aa:aa:aa:a1", capture_type="eapol",
                        ssid="SharedNet", pcap_path="/tmp/shared.pcap")
    win._hub.captures.add(rec)
    # Teach the analyzer model a sibling AP (BSSID-B, same ssid) so _ssid_for_bssid resolves it.
    win._wifi_analyzer.model.observe(
        "ap_found", {"bssid": "aa:aa:aa:aa:aa:b2", "ssid": "SharedNet", "rssi": -50}, time.monotonic())
    assert win._ssid_for_bssid("aa:aa:aa:aa:aa:b2") == "SharedNet"   # the fallback source works

    win._tabs.setCurrentWidget(win._settings_tab)
    win._on_crack_capture_requested("aa:aa:aa:aa:aa:b2")        # the SIBLING bssid (no record of its own)
    assert win._tabs.currentWidget() is win._crack_surface     # resolved by ssid -> navigated
    assert win._crack_lab_tab._active_capture_key == rec.key   # ...to the right record
