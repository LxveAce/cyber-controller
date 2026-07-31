"""P3 flow-spine Slice B (handshake→CRACK) — the Wi-Fi analyzer hands a captured handshake to CRACK.

The emitter (`WifiAnalyzerTab.crack_capture_requested`) + `main_window._on_crack_capture_requested`
resolve/dispatch, on Atlas's Slice A `FlowIntent` substrate. LOAD-only: the hand-off navigates to
CRACK + loads the capture into the cracker; it never starts a crack (the per-run consent gate stays
the single arming point) and never touches the guarded send path. Offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

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
def win(qapp, tmp_path, monkeypatch):
    # Isolate the capture-store persist path to a temp dir so this test never writes the real
    # ~/.cyber-controller/captures/captures.json (window tests otherwise pollute the app store).
    import src.core.install as _install
    monkeypatch.setattr(_install, "captures_dir", lambda: tmp_path)
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


def _add_capture(win, bssid, pcap="/tmp/hs.pcap"):
    from src.models.capture import CaptureRecord
    rec = CaptureRecord(bssid=bssid, capture_type="eapol", ssid="TargetNet")
    rec.pcap_path = pcap
    win._hub.captures.add(rec)
    return rec


def test_slice_b_sends_capture_to_crack_lab(win):
    # End-to-end: a capture exists for an AP -> crack_capture_requested fires -> main_window
    # resolves the CaptureRecord + dispatches -> the CRACK surface is shown + Crack Lab loaded it.
    if win._wifi_analyzer is None:
        pytest.skip("WifiAnalyzerTab unavailable")
    bssid = "AA:BB:CC:DD:EE:FF"
    rec = _add_capture(win, bssid)
    win._wifi_analyzer.crack_capture_requested.emit(bssid)
    # navigated to the CRACK surface + the Crack Lab sub-view
    assert win._tabs.currentWidget() is win._crack_surface
    assert win._crack_surface.currentWidget() is win._crack_lab_tab
    # LOAD-only: the capture path + key + BSSID are staged into the cracker (no crack started)
    assert win._crack_lab_tab._capture_edit.text() == "/tmp/hs.pcap"
    assert win._crack_lab_tab._active_capture_key == rec.key
    assert win._crack_lab_tab._bssid_edit.text() == bssid


def test_slice_b_prefers_a_crackable_record(win):
    # If a BSSID has multiple capture records, the resolve picks one with a crackable artifact.
    if win._wifi_analyzer is None:
        pytest.skip("WifiAnalyzerTab unavailable")
    from src.models.capture import CaptureRecord
    bssid = "00:11:22:33:44:55"
    bare = CaptureRecord(bssid=bssid, capture_type="pmkid", ssid="N")   # not crackable (no file)
    win._hub.captures.add(bare)
    good = _add_capture(win, bssid, pcap="/tmp/good.pcap")              # eapol with a pcap
    win._wifi_analyzer.crack_capture_requested.emit(bssid)
    assert win._crack_lab_tab._active_capture_key == good.key
    assert win._crack_lab_tab._capture_edit.text() == "/tmp/good.pcap"


def test_slice_b_no_capture_for_bssid_is_a_safe_noop(win):
    # A BSSID with no capture in the store -> no crash + no nav hijack (the emitter only offers the
    # action for an AP that has a capture; this guards the resolve path defensively).
    if win._wifi_analyzer is None:
        pytest.skip("WifiAnalyzerTab unavailable")
    before = win._tabs.currentWidget()
    win._wifi_analyzer.crack_capture_requested.emit("66:66:66:66:66:66")   # not in the store
    assert win._tabs.currentWidget() is before   # unchanged, no crash
