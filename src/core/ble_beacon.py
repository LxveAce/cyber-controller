"""BLE beacon advertisement decoders — RX-ONLY (decode received advertisements, never transmit).

* ``parse_ibeacon`` decodes the Apple iBeacon manufacturer-specific data (proximity UUID / major /
  minor / measured power).
* ``parse_eddystone`` decodes Google Eddystone service data (0xFEAA) — the UID, URL, and TLM frame
  types.

All are pure functions over bytes — no Qt, no I/O, no device access — so they unit-test headless
against golden vectors, and they never author or advertise a frame (decode only). The SubGHz EV1527
(#37) is a separate radio, decoded elsewhere.
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


# ── Eddystone (Google) — BLE service data for UUID 0xFEAA; frame type is the first byte ──────────
_EDDYSTONE_UID = 0x00
_EDDYSTONE_URL = 0x10
_EDDYSTONE_TLM = 0x20

# URL scheme prefixes (Eddystone-URL spec, byte after tx_power).
_URL_SCHEMES = ("http://www.", "https://www.", "http://", "https://")
# URL expansion codes 0x00–0x0d; bytes 0x0e–0x20 and 0x7f–0xff are reserved (rejected).
_URL_EXPANSIONS = (
    ".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
    ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov",
)


def parse_eddystone(service_data: bytes) -> "dict | None":
    """Decode a Google Eddystone advertisement from its 0xFEAA service-data payload. Dispatches on
    the frame-type byte: UID (0x00), URL (0x10), TLM (0x20). Returns ``None`` for an unknown frame,
    a reserved URL byte, or a short buffer — a non-Eddystone advert is never fabricated. RX only.
    """
    if not isinstance(service_data, (bytes, bytearray)) or len(service_data) < 1:
        return None
    b = bytes(service_data)
    frame = b[0]
    if frame == _EDDYSTONE_UID:
        return _parse_eddystone_uid(b)
    if frame == _EDDYSTONE_URL:
        return _parse_eddystone_url(b)
    if frame == _EDDYSTONE_TLM:
        return _parse_eddystone_tlm(b)
    return None


def _parse_eddystone_uid(b: bytes) -> "dict | None":
    # 0x00 | tx_power[1 signed] | namespace[10] | instance[6] (+ 2 RFU, optional)
    if len(b) < 18:
        return None
    return {
        "frame": "uid",
        "tx_power": int.from_bytes(b[1:2], "big", signed=True),
        "namespace": b[2:12].hex(),
        "instance": b[12:18].hex(),
    }


def _parse_eddystone_url(b: bytes) -> "dict | None":
    # 0x10 | tx_power[1 signed] | scheme[1] | encoded_url[...]
    if len(b) < 3 or b[2] >= len(_URL_SCHEMES):
        return None
    url = _URL_SCHEMES[b[2]]
    for byte in b[3:]:
        if byte < len(_URL_EXPANSIONS):
            url += _URL_EXPANSIONS[byte]
        elif 0x20 <= byte <= 0x7e:  # printable ASCII, used verbatim
            url += chr(byte)
        else:
            return None  # reserved byte — not a valid encoded URL
    return {
        "frame": "url",
        "tx_power": int.from_bytes(b[1:2], "big", signed=True),
        "url": url,
    }


def _parse_eddystone_tlm(b: bytes) -> "dict | None":
    # 0x20 | version[1] | vbatt[2 BE mV] | temp[2 BE 8.8] | adv_cnt[4 BE] | sec_cnt[4 BE, 0.1s]
    if len(b) < 14:
        return None
    return {
        "frame": "tlm",
        "version": b[1],
        "vbatt_mv": int.from_bytes(b[2:4], "big"),
        "temperature_c": int.from_bytes(b[4:6], "big", signed=True) / 256.0,
        "adv_count": int.from_bytes(b[6:10], "big"),
        "uptime_s": int.from_bytes(b[10:14], "big") / 10.0,
    }
