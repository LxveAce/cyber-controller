"""BlueJammer remote-controller framework (src/core/bluejammer_control.py).

Pure logic (no Qt, no hardware): the fail-safe gate (refuse to send without a validated control map),
STOP-first / arm-gated safety model, and the UART (no-AP) + HTTP transports — all via mock send paths.
The real frames/endpoints are hardware-captured; these tests verify the SAFETY + dispatch logic.
"""

from __future__ import annotations

import pytest

from src.core.bluejammer_control import (
    BlueJammerController,
    ControlMap,
    ControlUnavailable,
    HttpTransport,
    Mode,
    UartTransport,
)


def _uart_capture():
    sent = []
    return UartTransport(sent.append, name="uart"), sent


def test_refuses_to_send_without_validated_map():
    t, sent = _uart_capture()
    ctrl = BlueJammerController(t, ControlMap())  # empty, not validated
    with pytest.raises(ControlUnavailable):
        ctrl.stop()
    assert sent == []  # fail-safe: nothing guessed onto the wire
    assert ctrl.available is False


def test_stop_sends_idle_frame_when_validated():
    t, sent = _uart_capture()
    cmap = ControlMap(uart_frames={Mode.IDLE: b"\x02IDLE\x03"}, validated=True)
    ctrl = BlueJammerController(t, cmap)
    assert ctrl.available is True
    ctrl.stop()
    assert sent == [b"\x02IDLE\x03"]


def test_arming_requires_confirmation():
    t, sent = _uart_capture()
    cmap = ControlMap(uart_frames={Mode.WIFI: b"\x02WIFI\x03"}, validated=True)
    ctrl = BlueJammerController(t, cmap)
    with pytest.raises(PermissionError):
        ctrl.set_mode(Mode.WIFI)              # no confirm -> blocked
    assert sent == []
    ctrl.set_mode(Mode.WIFI, confirm_unsafe=True)
    assert sent == [b"\x02WIFI\x03"]


def test_stop_is_never_gated():
    t, sent = _uart_capture()
    cmap = ControlMap(uart_frames={Mode.IDLE: b"idle"}, validated=True)
    ctrl = BlueJammerController(t, cmap)
    ctrl.stop()  # no confirm_unsafe needed for STOP
    assert sent == [b"idle"]


def test_missing_frame_for_mode_raises():
    t, sent = _uart_capture()
    cmap = ControlMap(uart_frames={Mode.IDLE: b"idle"}, validated=True)  # no WIFI frame
    ctrl = BlueJammerController(t, cmap)
    with pytest.raises(ControlUnavailable):
        ctrl.set_mode(Mode.WIFI, confirm_unsafe=True)
    assert sent == []


def test_uart_ack_failure_surfaces():
    cmap = ControlMap(uart_frames={Mode.IDLE: b"idle"}, validated=True)
    t = UartTransport(lambda b: None, ack_fn=lambda: False)  # device never acks
    ctrl = BlueJammerController(t, cmap)
    with pytest.raises(ControlUnavailable):
        ctrl.stop()


def test_http_transport_option():
    calls = []
    def req(method, url, body):
        calls.append((method, url, body))
        return 200
    cmap = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    ctrl = BlueJammerController(HttpTransport(req), cmap)
    ctrl.stop()
    assert calls == [("POST", "http://192.168.1.1/mode", "idle")]


def test_http_non_2xx_raises():
    cmap = ControlMap(http_calls={Mode.IDLE: ("POST", "/mode", "idle")}, validated=True)
    ctrl = BlueJammerController(HttpTransport(lambda m, u, b: 500), cmap)
    with pytest.raises(ControlUnavailable):
        ctrl.stop()


def test_mode_helpers():
    assert Mode.IDLE.is_stop and not Mode.IDLE.is_jamming
    assert Mode.WIFI.is_jamming and not Mode.WIFI.is_stop


def test_on_event_callback():
    events = []
    cmap = ControlMap(uart_frames={Mode.IDLE: b"idle"}, validated=True)
    t, _ = _uart_capture()
    ctrl = BlueJammerController(t, cmap, on_event=lambda *a: events.append(a))
    ctrl.stop()
    assert events and events[0][0] == "sent" and events[0][1] is Mode.IDLE
