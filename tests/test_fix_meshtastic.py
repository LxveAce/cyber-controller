"""Honesty guards for the Meshtastic protocol fix.

SOURCE-VERIFIED: Meshtastic's serial link is protobuf-framed (StreamAPI), not a
plain-text CLI. CC's text commands are written but discarded by the firmware, so
they must NOT be advertised as working controls. This module locks in the honest
fix: no sendable commands, no target/broadcast actions, and a passive parser that
makes no structured Node/Position/Message claims (those arrive as protobuf the
text parser cannot decode).
"""

from __future__ import annotations

from src.models.target import TargetType
from src.protocols import get_protocol, meshtastic
from src.protocols.base import ParsedEvent
from src.protocols.meshtastic import MeshtasticProtocol


# ── get_commands(): no buttons that claim to control the device ──────

def test_get_commands_is_empty() -> None:
    """No text commands: the firmware discards them, so we ship none."""
    assert MeshtasticProtocol().get_commands() == []


def test_get_commands_claims_no_device_control() -> None:
    """Belt-and-suspenders: even if a future edit re-adds entries, none may
    claim to control the device over the (protobuf) serial link."""
    cmds = MeshtasticProtocol().get_commands()
    control_words = ("info", "nodes", "send", "reboot", "relay")
    for ci in cmds:
        name = ci.name.lower()
        assert not any(w in name for w in control_words), ci.name


def test_cached_commands_also_empty() -> None:
    assert get_protocol("meshtastic").cached_commands() == []


# ── TARGET_ACTIONS: no AP (or any) action ────────────────────────────

def test_target_actions_has_no_ap_action() -> None:
    assert not meshtastic.TARGET_ACTIONS.get(TargetType.AP)


def test_target_actions_is_empty() -> None:
    assert meshtastic.TARGET_ACTIONS == {}


# ── BROADCAST_CAPABILITIES: no phantom "Mesh Status" / relay verb ─────

def test_broadcast_capabilities_is_empty() -> None:
    """The old MESH_RELAY -> "nodes" broadcast wrote text the firmware ignores.
    Removed rather than shipped as a phantom button."""
    assert meshtastic.BROADCAST_CAPABILITIES == {}


# ── parse_line: honest passive scrape, no structured telemetry ───────

def test_parse_line_emits_generic_info_only() -> None:
    """A pipe-delimited 'Node:' line (a format the firmware does not emit) is
    surfaced verbatim as a generic info event — NOT decoded into structured
    node telemetry (no snr / battery / node_id fields)."""
    line = "Node: !a1b2c3d4 | Name: BaseCamp | SNR: 9.5 | Battery: 92%"
    event = MeshtasticProtocol().parse_line(line)
    assert isinstance(event, ParsedEvent)
    assert event.event_type == "info"
    assert event.raw == line
    # No structured telemetry claims.
    assert set(event.data.keys()) <= {"message"}
    for fabricated in ("snr", "battery", "node_id", "lat", "lon"):
        assert fabricated not in event.data


def test_parse_line_blank_is_noise() -> None:
    assert MeshtasticProtocol().parse_line("   ") is None
