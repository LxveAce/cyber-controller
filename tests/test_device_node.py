"""DeviceNode enrichment (cross-comm rework, S1): a connected device surfaces its firmware capabilities as a
node. Additive + read-only over the protocol capability map — the first building block of the comms rework
(see command-center cc-rework-PLAN.md). No existing behavior changes; this just gives each node a capability view.
"""
from __future__ import annotations

import pytest

from src.models.device import Device, Protocol
from src.protocols import capabilities_for, driver_type_for


def test_capabilities_delegate_to_protocol_map():
    d = Device(port="COM_X", firmware="marauder", protocol=Protocol.MARAUDER)
    assert d.capabilities == capabilities_for("marauder")
    assert isinstance(d.capabilities, frozenset)


def test_capabilities_surface_real_tokens():
    # Marauder is Wi-Fi + BLE + GPS capable — the node should report that, not an opaque blob.
    caps = Device(port="COM_X", firmware="marauder").capabilities
    assert {"wifi", "ble", "gps"} <= caps


def test_capabilities_prefer_firmware_over_protocol_enum():
    # The firmware identifier is the lookup key; the protocol enum is only the fallback.
    d = Device(port="COM_X", firmware="marauder", protocol=Protocol.UNKNOWN)
    assert d.capabilities == capabilities_for("marauder")


def test_capabilities_empty_for_unknown():
    assert Device(port="COM_Y").capabilities == frozenset()


def test_capabilities_readonly_frozenset():
    caps = Device(port="COM_X", firmware="bruce").capabilities
    assert isinstance(caps, frozenset)
    with pytest.raises(AttributeError):
        caps.add("hack")  # frozenset has no .add — callers can't corrupt the map


# ── driver_type (S1): a node says honestly whether it even has a text command channel ──────────────────

def test_driver_type_defaults_to_text_cli():
    # A normal line-shell firmware is text-cli; an unknown firmware falls back to text-cli too.
    assert Device(port="COM_X", firmware="marauder").driver_type == "text-cli"
    assert Device(port="COM_Y").driver_type == "text-cli"


def test_driver_type_stream_for_meshtastic():
    # Meshtastic is a protobuf StreamAPI, not a text CLI — the node must not look like a sendable shell.
    assert Device(port="COM_X", firmware="meshtastic").driver_type == "stream"


def test_driver_type_controlmap_for_bluejammer():
    # BlueJammer has no serial command channel (web-UI control) — control-map, not an empty CLI.
    assert Device(port="COM_X", firmware="bluejammer").driver_type == "controlmap"


def test_driver_type_delegates_to_helper():
    d = Device(port="COM_X", firmware="meshtastic", protocol=Protocol.MESHTASTIC)
    assert d.driver_type == driver_type_for("meshtastic")


def test_driver_type_prefers_firmware_over_protocol_enum():
    # The firmware identifier is the lookup key; the protocol enum is only the fallback.
    d = Device(port="COM_X", firmware="meshtastic", protocol=Protocol.UNKNOWN)
    assert d.driver_type == "stream"


def test_driver_type_helper_unknown_falls_back():
    assert driver_type_for("does-not-exist") == "text-cli"
