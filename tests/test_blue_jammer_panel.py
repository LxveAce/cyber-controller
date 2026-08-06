"""BlueJammerPanel (src/ui/qt/blue_jammer_panel.py) — the extracted BlueJammer-V2 control/STOP card.

The welded DeviceTab ``_bj_*`` UI, lifted into a standalone widget so OPERATE ▸ Console and the
Devices tab can share one copy. These prove the gates survive the extraction: arming needs the
RF-shielded attestation and is INDEPENDENT of any serial ``arm_state`` (reform critic HIGH —
BlueJammer has no ``supports_arm``, so coupling would dead-disable every Arm button); TX is inert
without a validated map; STOP is ungated and supersedes a pending Arm on the serializing worker;
and control events reach the injected ``event_sink`` (the panel owns no terminal). Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.blue_jammer_panel import BlueJammerPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _drain(panel, qapp):
    """Pump the event loop until the (blocking, up-to-4s) serializing worker is idle so the final
    ``_bj_status`` is observable — arm/STOP run their controller HTTP call off the GUI thread."""
    import time
    q = panel._bj_queue
    if q is not None:
        for _ in range(1000):
            qapp.processEvents()
            with q._cond:
                idle = not q._deque and not q._processing
            if idle:
                break
            time.sleep(0.002)
    qapp.processEvents()


def test_full_control_surface_present(qapp):
    p = BlueJammerPanel()
    assert p._bj_stop_btn is not None
    assert len(p._bj_arm_btns) == 4
    assert not p._bj_attest.isChecked()
    assert all(not b.isEnabled() for b in p._bj_arm_btns)  # arm disabled by default


def test_attestation_is_the_only_arm_gate(qapp):
    """Critic HIGH: arming enables SOLELY on the attestation — never coupled to a serial arm_state
    (BlueJammer has no supports_arm; coupling would leave every Arm button permanently dead)."""
    p = BlueJammerPanel()
    p._bj_attest.setChecked(True)      # nothing else touched: no arm_state, no connection, no map
    assert all(b.isEnabled() for b in p._bj_arm_btns)
    p._bj_attest.setChecked(False)
    assert all(not b.isEnabled() for b in p._bj_arm_btns)


def test_open_webui(qapp, monkeypatch):
    import webbrowser
    got = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: got.setdefault("url", url))
    BlueJammerPanel()._open_bj_webui()
    assert got.get("url") == "http://192.168.1.1"


def test_event_sink_receives_control_events(qapp):
    from src.core.bluejammer_control import Mode
    events: list[str] = []
    p = BlueJammerPanel(event_sink=events.append)
    p._bj_on_event("armed", Mode.WIFI, "web-ui")
    assert events == ["[BlueJammer armed: WiFi via web-ui]"]


def test_no_event_sink_is_safe(qapp):
    from src.core.bluejammer_control import Mode
    BlueJammerPanel()._bj_on_event("armed", Mode.WIFI, "web-ui")  # event_sink=None -> no-op


def test_stop_without_map_is_safe_and_guides(qapp):
    p = BlueJammerPanel()
    p._bj_stop()  # must not raise
    _drain(p, qapp)
    assert "unavailable" in p._bj_status.text().lower()
    assert "web ui" in p._bj_status.text().lower()


def test_arm_blocked_without_attestation(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    from src.core.bluejammer_control import Mode
    p = BlueJammerPanel()
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: pytest.fail("must not confirm un-attested"))
    p._bj_set_mode(Mode.WIFI)
    assert "confirmation" in p._bj_status.text().lower()


def test_arm_unavailable_without_validated_map(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    from src.core.bluejammer_control import Mode
    p = BlueJammerPanel()
    p._bj_attest.setChecked(True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    p._bj_set_mode(Mode.WIFI)  # attested + confirmed, but no validated map -> fail-safe
    _drain(p, qapp)
    assert "unavailable" in p._bj_status.text().lower()


def test_shipped_scaffolding_is_inert(qapp):
    p = BlueJammerPanel()
    p._bj_build_controller()
    assert p._bj_controller is not None
    assert p._bj_controller.available is False  # no validated Idle/arm frame -> nothing can be sent
    assert not p._bj_map.validated
    assert not p._bj_map.uart_frames and not p._bj_map.http_calls


def test_parse_map_roundtrip_and_failsafe_default(qapp, tmp_path):
    import json

    from src.core.bluejammer_control import Mode
    good = tmp_path / "map.json"
    good.write_text(json.dumps({
        "validated": True,
        "http_calls": {"Idle": ["POST", "/mode", "idle"], "WiFi": ["POST", "/mode", "wifi"]},
    }), encoding="utf-8")
    cmap = BlueJammerPanel._bj_parse_map_file(str(good))
    assert cmap.validated
    assert cmap.http_calls[Mode.IDLE] == ("POST", "/mode", "idle")
    assert cmap.has_http(Mode.WIFI)
    # Fail-safe: a map omitting "validated" must NOT be trusted (no silent guessed-frame send).
    nomark = tmp_path / "nomark.json"
    nomark.write_text(
        json.dumps({"http_calls": {"Idle": ["POST", "/mode", "idle"]}}), encoding="utf-8")
    assert BlueJammerPanel._bj_parse_map_file(str(nomark)).validated is False


def test_http_request_translates_transport_error(qapp, monkeypatch):
    """The HTTP boundary translates a raw transport failure into ControlUnavailable instead of
    letting it escape a Qt clicked-slot (with no sys.excepthook, that aborts the app)."""
    import urllib.error
    import urllib.request

    from src.core.bluejammer_control import ControlUnavailable

    def _boom(*a, **k):
        raise urllib.error.URLError("Network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(ControlUnavailable):
        BlueJammerPanel._bj_http_request("POST", "http://192.168.1.1/mode", "idle")


def test_stop_runs_off_the_gui_thread(qapp, monkeypatch):
    """The controller HTTP call (blocking, up to 4s) runs on a worker thread, not the GUI thread —
    otherwise the safety STOP button froze the whole app for up to 4s."""
    import threading

    from src.core.bluejammer_control import ControlMap, Mode
    gui_ident = threading.get_ident()
    seen: dict = {}

    def _fake_req(method, url, body):
        seen.setdefault("ident", threading.get_ident())
        return 200

    monkeypatch.setattr(BlueJammerPanel, "_bj_http_request", staticmethod(_fake_req))
    p = BlueJammerPanel()
    p._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    p._bj_build_controller()
    p._bj_stop()
    assert p._bj_queue is not None, "STOP should have started the serializing worker"
    assert "stop…" in p._bj_status.text().lower()  # immediate pending status, UI stays live
    _drain(p, qapp)
    assert seen.get("ident") is not None
    assert seen["ident"] != gui_ident              # the blocking call ran off the GUI thread
    assert "stop sent" in p._bj_status.text().lower()


def test_queue_stop_purges_pending_arms_but_keeps_stop(qapp):
    """On the serializing queue a STOP drops any queued not-yet-started Arm (superseding it) and is
    itself always kept. Enqueue before start() so nothing has run — inspect the FIFO directly."""
    from src.ui.qt.blue_jammer_panel import _BjCommandQueue

    q = _BjCommandQueue()
    q.enqueue(1, "arm", lambda: "a1")
    q.enqueue(2, "arm", lambda: "a2")
    q.enqueue(3, "stop", lambda: "stop")   # supersedes both pending arms
    assert [op[1] for op in q._deque] == ["stop"]
    assert q._deque[0][0] == 3             # the STOP op id, intact
    q.enqueue(4, "arm", lambda: "a4")      # a new Arm after the STOP is legitimately kept
    assert [op[1] for op in q._deque] == ["stop", "arm"]


def test_arm_then_stop_last_payload_is_idle(qapp, monkeypatch):
    """Arm WiFi then STOP: with a validated map the LAST payload on the wire is the Idle/STOP
    frame — the single serializing worker guarantees press-order == device-order (audit §F2)."""
    from PyQt5.QtWidgets import QMessageBox

    from src.core.bluejammer_control import ControlMap, Mode
    sent: list = []
    monkeypatch.setattr(BlueJammerPanel, "_bj_http_request",
                        staticmethod(lambda method, url, body: sent.append(body) or 200))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    p = BlueJammerPanel()
    p._bj_map = ControlMap(
        http_calls={Mode.IDLE: ("POST", "/mode", "idle"), Mode.WIFI: ("POST", "/mode", "wifi")},
        validated=True,
    )
    p._bj_build_controller()
    p._bj_attest.setChecked(True)
    p._bj_set_mode(Mode.WIFI)   # enqueue Arm
    p._bj_stop()                # enqueue STOP — supersedes the still-pending Arm, always kept
    _drain(p, qapp)
    assert sent, "something should have reached the device"
    assert sent[-1] == "idle", f"last payload must be the Idle/STOP frame, got {sent!r}"
    assert "stop sent" in p._bj_status.text().lower()


def test_arm_disabled_while_op_pending(qapp, monkeypatch):
    """Arm buttons disable while an op is queued/running (so a 2nd Arm can't race a STOP) and
    re-enable once the queue drains; STOP is never gated this way."""
    from PyQt5.QtWidgets import QMessageBox

    from src.core.bluejammer_control import ControlMap, Mode
    monkeypatch.setattr(BlueJammerPanel, "_bj_http_request", staticmethod(lambda *a, **k: 200))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    p = BlueJammerPanel()
    p._bj_map = ControlMap(http_calls={Mode.WIFI: ("POST", "/mode", "wifi")}, validated=True)
    p._bj_build_controller()
    p._bj_attest.setChecked(True)
    assert all(b.isEnabled() for b in p._bj_arm_btns)
    p._bj_set_mode(Mode.WIFI)   # enqueue emits busy_changed(True) synchronously on the GUI thread
    assert all(not b.isEnabled() for b in p._bj_arm_btns)  # disabled while the op is pending
    assert p._bj_stop_btn.isEnabled()                      # STOP stays dispatchable
    _drain(p, qapp)
    assert all(b.isEnabled() for b in p._bj_arm_btns)      # re-enabled once idle (still attested)


def test_shutdown_joins_workers(qapp, monkeypatch):
    """shutdown() (called from MainWindow.closeEvent via the host) joins in-flight workers so no
    QThread is destroyed mid-run on exit."""
    from src.core.bluejammer_control import ControlMap, Mode
    monkeypatch.setattr(BlueJammerPanel, "_bj_http_request", staticmethod(lambda *a, **k: 200))
    p = BlueJammerPanel()
    p._bj_map = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    p._bj_build_controller()
    p._bj_stop()
    p.shutdown()  # must join without hanging or raising
    qapp.processEvents()
    assert p._bj_queue is not None and not p._bj_queue.isRunning()
