"""Marauder 'Deauth Client' must be offered once the client's AP has been scanned.

Marauder deauth is AP-scoped (`select -a {index}`), and its client_found event carried no index, so the
"Deauth Client" action was always dropped by the ActionResolver. The client is now routed through its own
AP's scan ordinal (attached only when that AP was seen this scan), so the action resolves and selects the
client's AP; an unseen AP still yields no index and the action is dropped (never fired on a wrong AP).

End-to-end: real parser -> TargetIngestor._event_to_target -> ActionResolver.resolve.
"""

from __future__ import annotations

import types

from src.core.action_resolver import ActionResolver
from src.core.target_ingest import TargetIngestor
from src.protocols import get_protocol


def _resolve(port: str, target):
    dev = types.SimpleNamespace(port=port, firmware="marauder", name="marauder")
    dm = types.SimpleNamespace(list_connected=lambda: [dev])
    return ActionResolver(dm).resolve(target)


def test_client_deauth_offered_after_ap_seen():
    proto = get_protocol("marauder")
    # AP first (gets scan index 0), then a client on that AP.
    proto.parse_line("AP: HomeNet BSSID: AA:BB:CC:DD:EE:FF Ch: 6 RSSI: -40")
    ev = proto.parse_line("Client: 11:22:33:44:55:66 AP: AA:BB:CC:DD:EE:FF")
    t = TargetIngestor._event_to_target(ev, "COM3")
    assert t.extra.get("index") == 0  # routed to the AP's scan ordinal
    actions = _resolve("COM3", t)["COM3"]
    deauth = next(a for a in actions if a.name == "Deauth Client")
    assert deauth.pre_commands == ["select -a 0"]


def test_client_deauth_dropped_when_ap_unseen():
    proto = get_protocol("marauder")
    # Client whose AP was never scanned -> no index -> action dropped, not fired on a guessed AP.
    ev = proto.parse_line("Client: 99:88:77:66:55:44 AP: 12:34:56:78:9A:BC")
    t = TargetIngestor._event_to_target(ev, "COM3")
    assert "index" not in t.extra
    names = [a.name for a in _resolve("COM3", t).get("COM3", [])]
    assert "Deauth Client" not in names
    assert "Track Client" in names  # the non-index action is still offered
