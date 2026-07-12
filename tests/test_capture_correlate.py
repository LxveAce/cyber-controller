"""Capture correlation (punch-list #2, slice 5): the deauth -> handshake capture-confirm window.

Pure-core, no Qt. Verifies the CaptureCorrelator arms a window when a chain-event-bearing action
fires against a BSSID, confirms (publishes capture.confirmed) when a matching capture lands in the
window, prunes/ignores late or mismatched captures, respects failed-send status + missing
chain_events, and emits capture.timeout on an explicit sweep. Also checks the hub wires one in.
Time is injected so the window logic is deterministic (no sleeps).
"""
from __future__ import annotations

from src.core.capture_correlate import CaptureCorrelator
from src.core.cross_comm import EventBus


class _Clock:
    """An injectable monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _recorder(bus, topic):
    seen: list = []
    bus.subscribe(topic, lambda _t, payload: seen.append(payload))
    return seen


def _fire_deauth(bus, bssid="AA:BB:CC:DD:EE:FF", port="COM7", status="success"):
    bus.publish("action.executed", {
        "action": "Deauth AP", "port": port, "target_mac": bssid,
        "status": status, "chain_events": ["deauth_detected"]})


def _capture(bus, bssid="AA:BB:CC:DD:EE:FF", port="COM7", ctype="eapol"):
    bus.publish("capture.added", {"bssid": bssid, "device_source": port, "capture_type": ctype})


def test_deauth_then_capture_within_window_confirms():
    bus, clock = EventBus(), _Clock()
    CaptureCorrelator(bus, clock=clock, window_s=20.0)
    confirmed = _recorder(bus, "capture.confirmed")
    _fire_deauth(bus)
    clock.t += 8.0                                   # handshake arrives 8s later, inside the window
    _capture(bus)
    assert len(confirmed) == 1
    assert confirmed[0]["bssid"] == "AA:BB:CC:DD:EE:FF"
    assert confirmed[0]["action"] == "Deauth AP"
    assert confirmed[0]["elapsed_s"] == 8.0


def test_capture_after_window_does_not_confirm():
    bus, clock = EventBus(), _Clock()
    CaptureCorrelator(bus, clock=clock, window_s=20.0)
    confirmed = _recorder(bus, "capture.confirmed")
    _fire_deauth(bus)
    clock.t += 25.0                                  # too late — the window already lapsed
    _capture(bus)
    assert confirmed == []


def test_no_chain_events_does_not_arm():
    bus, clock = EventBus(), _Clock()
    corr = CaptureCorrelator(bus, clock=clock)
    bus.publish("action.executed", {"action": "Scan", "port": "COM7",
                                    "target_mac": "AA:BB:CC:DD:EE:FF", "status": "success"})
    assert corr.pending_count == 0
    confirmed = _recorder(bus, "capture.confirmed")
    _capture(bus)
    assert confirmed == []


def test_failed_send_does_not_arm():
    bus, clock = EventBus(), _Clock()
    corr = CaptureCorrelator(bus, clock=clock)
    _fire_deauth(bus, status="failed")
    assert corr.pending_count == 0


def test_bssid_mismatch_does_not_confirm():
    bus, clock = EventBus(), _Clock()
    CaptureCorrelator(bus, clock=clock)
    confirmed = _recorder(bus, "capture.confirmed")
    _fire_deauth(bus, bssid="AA:BB:CC:DD:EE:FF")
    _capture(bus, bssid="11:22:33:44:55:66")         # a different AP's handshake
    assert confirmed == []


def test_capture_matches_by_bssid_when_port_blank():
    bus, clock = EventBus(), _Clock()
    CaptureCorrelator(bus, clock=clock)
    confirmed = _recorder(bus, "capture.confirmed")
    _fire_deauth(bus, port="COM7")
    _capture(bus, port="")                            # capture arrived with no device_source
    assert len(confirmed) == 1


def test_sweep_publishes_timeout_for_expired_window():
    bus, clock = EventBus(), _Clock()
    corr = CaptureCorrelator(bus, clock=clock, window_s=20.0)
    timeouts = _recorder(bus, "capture.timeout")
    _fire_deauth(bus)
    assert corr.pending_count == 1
    clock.t += 21.0
    swept = corr.sweep()
    assert swept == ["AA:BB:CC:DD:EE:FF"]
    assert corr.pending_count == 0
    assert len(timeouts) == 1 and timeouts[0]["window_s"] == 20.0


def test_sweep_leaves_live_window_intact():
    bus, clock = EventBus(), _Clock()
    corr = CaptureCorrelator(bus, clock=clock, window_s=20.0)
    _fire_deauth(bus)
    clock.t += 5.0
    assert corr.sweep() == []                         # still inside the window
    assert corr.pending_count == 1


def test_hub_wires_correlator_and_capture_confirms_end_to_end():
    from src.core.cross_comm_hub import CrossCommHub
    from src.core.device_manager import DeviceManager
    from src.models.capture import CaptureRecord

    hub = CrossCommHub(DeviceManager())
    assert isinstance(hub.correlator, CaptureCorrelator)
    confirmed = _recorder(hub.bus, "capture.confirmed")
    _fire_deauth(hub.bus, bssid="AA:BB:CC:DD:EE:FF", port="COM7")
    # A real capture registered through the shared store publishes capture.added on the hub bus.
    hub.captures.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol",
                                   device_source="COM7"))
    assert len(confirmed) == 1 and confirmed[0]["bssid"] == "AA:BB:CC:DD:EE:FF"
