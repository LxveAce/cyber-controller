"""Tests for the Meshtastic reference enums (src/protocols/meshtastic_ref.py).

The values called out below are exactly the ones an earlier hand-typed 8-entry map got WRONG — so these lock
the authoritative snapshot in place (verify-never-fake).
"""

from __future__ import annotations

from src.protocols import meshtastic_proto as mp
from src.protocols.meshtastic_ref import (
    HARDWARE_MODELS,
    PORTNUMS,
    hardware_model_name,
    portnum_name,
)


def test_hardware_model_names_authoritative():
    assert hardware_model_name(43) == "HELTEC_V3"
    assert hardware_model_name(110) == "HELTEC_V4"
    # the six values the from-memory map got wrong:
    assert hardware_model_name(71) == "TRACKER_T1000_E"   # was mislabeled RAK4631
    assert hardware_model_name(9) == "RAK4631"            # was mislabeled HELTEC_V2_1
    assert hardware_model_name(4) == "TBEAM"              # was mislabeled HELTEC_V1
    assert hardware_model_name(31) == "STATION_G2"        # was mislabeled HELTEC_WSL_V3
    assert hardware_model_name(39) == "DIY_V1"            # was mislabeled HELTEC_WIRELESS_TRACKER
    assert hardware_model_name(77) == "M5STACK_COREBASIC"  # was mislabeled T_DECK
    assert hardware_model_name(0) == "UNSET"
    assert hardware_model_name(255) == "PRIVATE_HW"


def test_hardware_model_unknown_and_none():
    assert hardware_model_name(9999) == "hw#9999"
    assert hardware_model_name(None) == ""


def test_portnum_names():
    assert portnum_name(1) == "TEXT_MESSAGE_APP"
    assert portnum_name(3) == "POSITION_APP"
    assert portnum_name(4) == "NODEINFO_APP"
    assert portnum_name(70) == "TRACEROUTE_APP"
    assert portnum_name(9999) == "portnum#9999"
    assert portnum_name(None) == ""


def test_proto_delegates_to_ref():
    assert mp.hw_model_name(43) == "HELTEC_V3"
    assert mp.hw_model_name(71) == "TRACKER_T1000_E"


def test_portnum_label_on_packet_result():
    data = mp.field_varint(1, 3) + mp.field_bytes(2, b"")   # a POSITION_APP data payload
    packet = mp.field_fixed32(1, 0x1) + mp.field_bytes(4, data)
    res = mp.decode_fromradio(mp.field_bytes(2, packet))
    assert res.kind == "packet"
    assert res.portnum == 3
    assert res.portnum_label == "POSITION_APP"


def test_enum_coverage():
    assert len(HARDWARE_MODELS) == 145
    assert len(PORTNUMS) == 40
