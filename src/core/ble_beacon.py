"""BLE beacon manufacturer-data decoders — RX-ONLY (decode received advertisements, never transmit).

``parse_ibeacon`` decodes the Apple iBeacon manufacturer-specific data payload (a public de-facto
format) into its proximity UUID, major, minor, and measured-power fields. It is a pure function over
bytes — no Qt, no I/O, no device access — so it unit-tests headless against golden vectors, and it
never authors or advertises a beacon frame (decode only).

Eddystone (#28) will join this module; the SubGHz EV1527 (#37) is a separate radio.
"""
from __future__ import annotations

from dataclasses import dataclass

# The iBeacon manufacturer-specific data, as it sits in a BLE advertisement's AD type-0xFF payload:
#   company_id[2 LE] | type[1] | length[1] | uuid[16] | major[2 BE] | minor[2 BE] | power[1]
_APPLE_COMPANY_ID = b"\x4c\x00"  # 0x004C (Apple, Inc.), little-endian as it sits in the AD data
_IBEACON_TYPE = 0x02
_IBEACON_LEN = 0x15             # 21 = bytes that follow (uuid16 + major2 + minor2 + power1)
_IBEACON_MIN = 25               # 2 company + 1 type + 1 length + 21 payload


@dataclass(frozen=True)
class IBeacon:
    """A decoded Apple iBeacon advertisement (received, not transmitted)."""

    uuid: str        # canonical 8-4-4-4-12 proximity UUID (lowercase hex)
    major: int       # 0..65535
    minor: int       # 0..65535
    tx_power: int    # measured signal power at 1 m, signed dBm (calibration value from the beacon)


def _format_uuid(u: bytes) -> str:
    h = u.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def parse_ibeacon(mfg_data: bytes) -> "IBeacon | None":
    """Decode an Apple iBeacon from the FULL BLE manufacturer-specific data (company id included):
    ``4C 00 | 02 15 | uuid[16] | major[2 BE] | minor[2 BE] | tx_power[1 signed]``.

    Returns ``None`` for anything that is not a well-formed iBeacon — a wrong company id, a wrong
    beacon type/length, or a short buffer — so a non-iBeacon advertisement is never turned into a
    fabricated beacon. RX/decode only.
    """
    if not isinstance(mfg_data, (bytes, bytearray)) or len(mfg_data) < _IBEACON_MIN:
        return None
    b = bytes(mfg_data)
    if b[0:2] != _APPLE_COMPANY_ID:
        return None
    if b[2] != _IBEACON_TYPE or b[3] != _IBEACON_LEN:
        return None
    return IBeacon(
        uuid=_format_uuid(b[4:20]),
        major=int.from_bytes(b[20:22], "big"),
        minor=int.from_bytes(b[22:24], "big"),
        tx_power=int.from_bytes(b[24:25], "big", signed=True),
    )
