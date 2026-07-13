"""BlueJammer control/STOP panel in the Devices tab (src/ui/qt/device_tab.py).

When a BlueJammer is the active firmware, a prominent control/stop panel appears and the (inert) serial
send affordances are disabled — the stock firmware has no serial command channel, so the real control is
its web UI. Uses isHidden() (the widget's own visibility request) since the tab isn't shown. Offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _tab():
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab
    return DeviceTab(DeviceManager())


def _combo_index(combo, needle):
    for i in range(combo.count()):
        if needle in combo.itemText(i).lower():
            return i
    return -1


def _drain_bj(tab, qapp):
    """STOP/arm now run the (blocking, up-to-4s) controller HTTP call on a QThread so the GUI never
    freezes; the result lands in _bj_status via a queued signal. Wait for the worker + pump the event
    loop so the final status is observable in a test."""
    for w in list(tab._bj_workers):
        w.wait()
    qapp.processEvents()


def test_panel_hidden_by_default(qapp):
    tab = _tab()
    assert tab._bj_panel.isHidden()


def test_panel_shows_and_disables_send_for_bluejammer(qapp):
    tab = _tab()
    idx = _combo_index(tab._firmware_combo, "jammer")
    assert idx >= 0, "BlueJammer should be a firmware choice"
    tab._firmware_combo.setCurrentIndex(idx)  # fires _update_bj_panel
    assert not tab._bj_panel.isHidden()
    assert not tab._btn_send.isEnabled()      # no serial command channel
    assert not tab._cmd_input.isEnabled()
    assert not tab._cmd_palette.isEnabled()


def test_panel_hides_for_other_firmware(qapp):
    tab = _tab()
    bj = _combo_index(tab._firmware_combo, "jammer")
    tab._firmware_combo.setCurrentIndex(bj)
    assert not tab._bj_panel.isHidden()
    mar = _combo_index(tab._firmware_combo, "marauder")
    assert mar >= 0
    tab._firmware_combo.setCurrentIndex(mar)
    assert tab._bj_panel.isHidden()
    assert tab._cmd_input.isEnabled()
    assert tab._cmd_palette.isEnabled()


def test_open_webui_does_not_raise(qapp, monkeypatch):
    tab = _tab()
    called = {}
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: called.setdefault("url", url))
    tab._open_bj_webui()
    assert called.get("url") == "http://192.168.1.1"


# ── full remote control surface ──────────────────────────────────────

def test_full_control_surface_present(qapp):
    """STOP, the four arm-mode buttons, and the RF-shielded attestation are all present; arming is
    disabled until the attestation is checked."""
    tab = _tab()
    assert tab._bj_stop_btn is not None
    assert len(tab._bj_arm_btns) == 4
    assert not tab._bj_attest.isChecked()
    assert all(not b.isEnabled() for b in tab._bj_arm_btns)  # arm disabled by default


def test_attestation_enables_arming(qapp):
    tab = _tab()
    tab._bj_attest.setChecked(True)  # fires _bj_attest_changed
    assert all(b.isEnabled() for b in tab._bj_arm_btns)
    tab._bj_attest.setChecked(False)
    assert all(not b.isEnabled() for b in tab._bj_arm_btns)


def test_stop_without_map_is_safe_and_guides(qapp):
    """STOP with no validated control map must NOT raise — it surfaces the fail-safe guidance."""
    tab = _tab()
    tab._bj_stop()  # must not raise
    _drain_bj(tab, qapp)
    assert "unavailable" in tab._bj_status.text().lower()
    assert "web ui" in tab._bj_status.text().lower()


def test_arm_blocked_without_attestation(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from src.core.bluejammer_control import Mode
    tab = _tab()
    # confirm dialog should never even be reached without attestation
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail("must not confirm un-attested"))
    tab._bj_set_mode(Mode.WIFI)
    assert "confirmation" in tab._bj_status.text().lower()


def test_arm_unavailable_without_validated_map(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from src.core.bluejammer_control import Mode
    tab = _tab()
    tab._bj_attest.setChecked(True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    tab._bj_set_mode(Mode.WIFI)  # attested + confirmed, but no validated map -> fail-safe
    _drain_bj(tab, qapp)
    assert "unavailable" in tab._bj_status.text().lower()


def test_parse_map_file_roundtrip(qapp, tmp_path):
    from src.core.bluejammer_control import Mode
    import json
    p = tmp_path / "map.json"
    p.write_text(json.dumps({
        "validated": True,
        "http_calls": {"Idle": ["POST", "/mode", "idle"], "WiFi": ["POST", "/mode", "wifi"]},
    }), encoding="utf-8")
    from src.ui.qt.device_tab import DeviceTab
    cmap = DeviceTab._bj_parse_map_file(str(p))
    assert cmap.validated
    assert cmap.http_calls[Mode.IDLE] == ("POST", "/mode", "idle")
    assert cmap.has_http(Mode.WIFI)


def test_parse_map_without_validated_key_defaults_unvalidated(qapp, tmp_path):
    """Fail-safe: a control map that omits the 'validated' key must be treated as NOT validated, so it
    can never silently send guessed frames or make STOP a no-op that reports success."""
    from src.ui.qt.device_tab import DeviceTab
    import json
    p = tmp_path / "map.json"
    p.write_text(json.dumps({
        "http_calls": {"Idle": ["POST", "/mode", "idle"]},  # NOTE: no "validated" key
    }), encoding="utf-8")
    cmap = DeviceTab._bj_parse_map_file(str(p))
    assert cmap.validated is False


def test_shipped_scaffolding_is_inert(qapp):
    """Invariant: as shipped (no user-loaded control map) the controller CANNOT transmit — the arm/STOP
    scaffolding is present but the activator carries no frames. Cyber Controller ships none."""
    tab = _tab()
    tab._bj_build_controller()
    assert tab._bj_controller is not None
    assert tab._bj_controller.available is False  # no validated Idle/arm frame -> nothing can be sent
    assert not tab._bj_map.validated
    assert not tab._bj_map.uart_frames and not tab._bj_map.http_calls


def test_loaded_validated_map_sends_stop(qapp, monkeypatch):
    """With a validated map loaded, STOP actually dispatches over the (mocked) web-UI transport."""
    from src.core.bluejammer_control import ControlMap, Mode
    from src.ui.qt.device_tab import DeviceTab
    sent = []
    monkeypatch.setattr(
        DeviceTab, "_bj_http_request",
        staticmethod(lambda method, url, body: sent.append((method, url, body)) or 200),
    )
    tab = _tab()
    tab._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    tab._bj_build_controller()
    tab._bj_stop()
    _drain_bj(tab, qapp)
    assert sent and sent[0][0] == "POST" and sent[0][1].endswith("/mode")
    assert "stop sent" in tab._bj_status.text().lower()


# ── transport-unreachable fail-safe (regression: safety button must not crash the app) ───────────

def test_bj_http_request_translates_transport_error_to_control_unavailable(qapp, monkeypatch):
    """The HTTP boundary must translate a raw transport failure (URLError/OSError/timeout) into
    ControlUnavailable — the same contract HttpTransport.send uses for a non-2xx status — instead of
    letting the raw exception leak up into a Qt clicked-slot (which, with no sys.excepthook, aborts
    the whole app)."""
    import urllib.error
    import urllib.request
    from src.core.bluejammer_control import ControlUnavailable
    from src.ui.qt.device_tab import DeviceTab

    def _boom(*a, **k):
        raise urllib.error.URLError("Network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(ControlUnavailable):
        DeviceTab._bj_http_request("POST", "http://192.168.1.1/mode", "idle")


def test_stop_with_validated_map_but_unreachable_device_is_safe(qapp, monkeypatch):
    """Regression for the safety-critical STOP: with a validated map, if the device is unreachable
    (wrong network / timeout) urlopen raises URLError. STOP must NOT raise out of the Qt slot — it must
    surface the fail-safe 'cut power / web UI' guidance."""
    import urllib.error
    import urllib.request
    from src.core.bluejammer_control import ControlMap, Mode

    def _boom(*a, **k):
        raise urllib.error.URLError("Network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    tab = _tab()
    tab._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    tab._bj_build_controller()
    tab._bj_stop()  # must NOT raise (pre-fix: URLError escapes the slot)
    _drain_bj(tab, qapp)
    assert "unavailable" in tab._bj_status.text().lower()
    assert "web ui" in tab._bj_status.text().lower()


def test_arm_with_validated_map_but_unreachable_device_is_safe(qapp, monkeypatch):
    """Regression mirror of STOP for arming: attested + confirmed + validated map, but the device is
    unreachable (socket timeout). _bj_set_mode must NOT raise out of the Qt slot — it must surface the
    recoverable fail-safe status instead of aborting the app."""
    import socket
    import urllib.request
    from PyQt5.QtWidgets import QMessageBox
    from src.core.bluejammer_control import ControlMap, Mode

    def _timeout(*a, **k):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _timeout)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    tab = _tab()
    tab._bj_attest.setChecked(True)
    tab._bj_map = ControlMap(http_calls={Mode.WIFI: ("POST", "/mode", "wifi")}, validated=True)
    tab._bj_build_controller()
    tab._bj_set_mode(Mode.WIFI)  # must NOT raise (pre-fix: socket.timeout escapes both except clauses)
    _drain_bj(tab, qapp)
    assert "unavailable" in tab._bj_status.text().lower()


# ── GUI-thread offload (UI-audit Batch UI-3): STOP/arm must not block the event loop ──────────────

def test_bj_stop_runs_off_the_gui_thread(qapp, monkeypatch):
    """Regression: the controller HTTP call (blocking, up to 4s) must run on a worker thread, not the GUI
    thread — otherwise pressing the safety STOP button froze the whole app for up to 4s."""
    import threading

    from src.core.bluejammer_control import ControlMap, Mode
    from src.ui.qt.device_tab import DeviceTab

    gui_ident = threading.get_ident()
    seen: dict = {}

    def _fake_req(method, url, body):
        seen.setdefault("ident", threading.get_ident())
        return 200

    monkeypatch.setattr(DeviceTab, "_bj_http_request", staticmethod(_fake_req))
    tab = _tab()
    tab._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    tab._bj_build_controller()

    tab._bj_stop()
    assert tab._bj_workers, "STOP should have spawned a worker"
    assert "stop…" in tab._bj_status.text().lower()  # immediate pending status, UI stays live

    _drain_bj(tab, qapp)
    assert seen.get("ident") is not None
    assert seen["ident"] != gui_ident  # the blocking HTTP call ran off the GUI thread
    assert "stop sent" in tab._bj_status.text().lower()


def test_bj_shutdown_joins_workers(qapp, monkeypatch):
    """shutdown() (called from MainWindow.closeEvent) must join in-flight BJ workers so no QThread is
    destroyed mid-run on exit."""
    from src.core.bluejammer_control import ControlMap, Mode
    from src.ui.qt.device_tab import DeviceTab

    monkeypatch.setattr(DeviceTab, "_bj_http_request", staticmethod(lambda *a, **k: 200))
    tab = _tab()
    tab._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    tab._bj_build_controller()

    tab._bj_stop()
    tab.shutdown()  # must join without hanging or raising
    qapp.processEvents()
    assert all(not w.isRunning() for w in tab._bj_workers)
