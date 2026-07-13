"""Ingestor routing-boundary lock — the TargetIngestor must turn ONLY genuine discovery events into
pool Targets, and ONLY real crackable-material events into CaptureRecords. Every other event a
firmware emits (offensive-activity, status, telemetry, terminal chatter) must route to neither.

This is a safety invariant, not cosmetic: an offensive-activity line (deauth_sent, beacon_flood,
evil_portal, karma_event, mousejack) accidentally becoming a pool Target would make it an ACTIONABLE
node the AutoRouter / Operate menus could fire on. The list below is every event_type emitted across
the 15 protocols (grep event_type= src/protocols) minus the consumed set, so a future refactor that
starts routing one of these can't slip through silently. Motivated by the device_info audit (beat
226 — the one genuinely-dropped event that SHOULD route); this confirms the rest correctly do not.
"""
from __future__ import annotations

import pytest

from src.core.target_ingest import TargetIngestor
from src.protocols.base import ParsedEvent

# Emitted event_types that are intentionally NOT a pool target and NOT a capture. Offensive/activity
# events get a BSSID in their data on purpose: the guarantee is that the event_type gate — not a
# missing identifier — keeps them out, so a deauth_sent carrying a BSSID stays inert.
_OFFENSIVE_OR_ACTIVITY = [
    "deauth_sent", "deauth_detected", "beacon_flood", "beacon_spam", "evil_portal",
    "karma_event", "mousejack", "probe_request", "probe_activity", "iot_found",
    "capture",  # GhostESP evil-portal CREDENTIAL grab — deliberately excluded from the capture log
]
_STATUS_TELEMETRY_CHATTER = [
    "status", "info", "command", "warning", "stopped", "error", "scan_complete",
    "channel_changed", "gps_fix", "sd_event", "packet", "spectrum", "nrf_data",
    "version", "save", "ir_found",
]
_ALL_NON_ROUTED = _OFFENSIVE_OR_ACTIVITY + _STATUS_TELEMETRY_CHATTER


def _ingestor() -> TargetIngestor:
    return TargetIngestor(pool=None, captures=None)


@pytest.mark.parametrize("event_type", _ALL_NON_ROUTED)
def test_non_discovery_event_is_never_a_target_or_capture(event_type):
    ing = _ingestor()
    # BSSID + common identifier fields present, to prove the event_type gate (not a missing id) is
    # what keeps it out — an offensive event carrying a MAC must still not become an actionable one.
    ev = ParsedEvent(
        event_type=event_type,
        data={"bssid": "DE:AD:BE:EF:00:11", "mac": "DE:AD:BE:EF:00:11", "ssid": "victim",
              "rssi": -40, "channel": 6},
        raw=f"<{event_type}>",
    )
    assert TargetIngestor._event_to_target(ev, "COM_X") is None, f"{event_type} pooled"
    assert ing._event_to_capture(ev, "COM_X") is None, f"{event_type} captured"


def test_offensive_events_specifically_never_pool_as_actionable_targets():
    # Focused restatement of the safety property for the events that would be dangerous as targets.
    for et in _OFFENSIVE_OR_ACTIVITY:
        ev = ParsedEvent(event_type=et, data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="")
        assert TargetIngestor._event_to_target(ev, "COM_A") is None


def test_positive_control_real_discovery_and_capture_still_route():
    # Guards the negative tests from rotting into vacuous truth: the consumed events DO still route.
    ing = _ingestor()
    ap = ParsedEvent(
        event_type="ap_found",
        data={"bssid": "AA:BB:CC:00:11:22", "ssid": "net", "rssi": -50, "channel": 1}, raw="")
    t = TargetIngestor._event_to_target(ap, "COM_A")
    assert t is not None and t.mac == "AA:BB:CC:00:11:22"

    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:00:11:22"}, raw="")
    assert ing._event_to_capture(hs, "COM_A") is not None
