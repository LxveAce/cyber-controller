"""Golden-vector tests for the iBeacon decoder (src/core/ble_beacon.py) — RX/decode only.

The vector is built independently from known fields (the classic Apple AirLocate proximity UUID
E2C56DB5-DFFB-48D2-B060-D0F5A71096E0, major=1, minor=2, measured power=-59 dBm), and we assert the
decoder recovers exactly those. Plus rejections: wrong company / type / length / truncated / empty.
"""
from __future__ import annotations

from src.core.ble_beacon import IBeacon, parse_ibeacon

# Known iBeacon fields (the well-documented Apple AirLocate sample UUID).
_UUID = "e2c56db5-dffb-48d2-b060-d0f5a71096e0"
_MAJOR = 1
_MINOR = 2
_TX_POWER = -59  # a typical measured-power calibration byte (0xC5 as a signed int8)


def _ibeacon_bytes(company=b"\x4c\x00", btype=0x02, length=0x15,
                   uuid=_UUID, major=_MAJOR, minor=_MINOR, power=_TX_POWER) -> bytes:
    """Assemble the full manufacturer-specific data from fields — the golden vector, by hand."""
    return (company
            + bytes([btype, length])
            + bytes.fromhex(uuid.replace("-", ""))
            + major.to_bytes(2, "big")
            + minor.to_bytes(2, "big")
            + power.to_bytes(1, "big", signed=True))


def test_parse_ibeacon_golden_vector():
    got = parse_ibeacon(_ibeacon_bytes())
    assert got == IBeacon(uuid=_UUID, major=_MAJOR, minor=_MINOR, tx_power=_TX_POWER)
    # the payload is exactly 25 bytes (2 company + 1 type + 1 length + 16 uuid + 2 + 2 + 1)
    assert len(_ibeacon_bytes()) == 25


def test_parse_ibeacon_reads_big_endian_major_minor_and_signed_power():
    b = _ibeacon_bytes(major=0x1234, minor=0xABCD, power=-100)
    got = parse_ibeacon(b)
    assert got is not None
    assert got.major == 0x1234 and got.minor == 0xABCD  # big-endian, not byte-swapped
    assert got.tx_power == -100                          # signed int8, not 156


def test_rejects_wrong_company_id():
    assert parse_ibeacon(_ibeacon_bytes(company=b"\x4d\x00")) is None   # not Apple 0x004C


def test_rejects_wrong_type_or_length():
    assert parse_ibeacon(_ibeacon_bytes(btype=0x03)) is None            # not the iBeacon type 0x02
    assert parse_ibeacon(_ibeacon_bytes(length=0x14)) is None           # not the length 0x15


def test_rejects_truncated_and_empty_and_non_bytes():
    assert parse_ibeacon(_ibeacon_bytes()[:20]) is None                 # short buffer
    assert parse_ibeacon(b"\x4c\x00\x02\x15") is None                   # header only, no payload
    assert parse_ibeacon(b"") is None
    assert parse_ibeacon(None) is None                                  # type: ignore[arg-type]
    assert parse_ibeacon("4c0002...") is None                          # type: ignore[arg-type]


def test_accepts_bytearray_and_ignores_trailing_bytes():
    b = bytearray(_ibeacon_bytes()) + b"\xff\xee"   # some scanners append extra AD bytes
    got = parse_ibeacon(b)
    assert got is not None and got.uuid == _UUID and got.major == _MAJOR
