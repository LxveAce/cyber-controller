"""DeviceNode enrichment (cross-comm rework, S1): a connected device surfaces its firmware capabilities as a
node. Additive + read-only over the protocol capability map — the first building block of the comms rework
(see command-center cc-rework-PLAN.md). No existing behavior changes; this just gives each node a capability view.
"""
from __future__ import annotations

import pytest

from src.models.device import Device, Protocol
from src.protocols import capabilities_for


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
