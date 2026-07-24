"""Bluetooth SIG number → name lookups for BLE enrichment (company, appearance, service UUID).

Passive enrichment that turns the raw numbers a BLE advertiser carries into human names, so a scan row reads
"Apple, Inc." / "Heart Rate Sensor" / "Tile" instead of ``76`` / ``833`` / ``FEED``. Three resolvers:
- :func:`lookup_company` — the 16-bit company identifier from manufacturer-specific data (``company=<decimal>``
  on the LxveOS LXVEOS/1 ble line); bundled gzip table (all 3998 SIG companies).
- :func:`lookup_appearance` — the GAP "Appearance" value (``appr=<decimal>``); small embedded category table.
- :func:`resolve_uuid` — a service/member 16-bit UUID (or a base-aligned 128-bit UUID); small embedded table.
An unknown/unparseable value always resolves to "" — we never fabricate a name.

Mirrors :mod:`src.core.oui`: the bundled gzipped table (``src/config/ble_company_ids.tsv.gz``, ``HEX4<TAB>name``)
is generated from the public Bluetooth SIG Assigned Numbers (via NordicSemiconductor/bluetooth-numbers-database,
MIT — see ``ble_company_ids.SOURCE.md``) and is lazy-loaded on first lookup. An unknown/unparseable id resolves
to "" — we never fabricate a vendor.
"""
from __future__ import annotations

import gzip
import logging
import zlib

from src.core.resources import resource_path

log = logging.getLogger(__name__)

# Frozen-safe (resource_path resolves to the repo in dev and _MEIPASS in the PyInstaller build); the table is
# --add-data'd to src/config in build.py so it actually ships (a __file__-relative path would miss it — C-8).
_TABLE_PATH = resource_path("src", "config", "ble_company_ids.tsv.gz")

# Lazy cache: 4-hex company id (uppercase) -> company name. None until first load.
_table: dict[str, str] | None = None


def normalize_company(value: object) -> str | None:
    """Return *value*'s Bluetooth company id as a 4-hex uppercase key (``"004C"``), or None.

    Accepts an int, a decimal string (``"76"`` — the LxveOS LXVEOS/1 convention, parsed there as ``int(val)``),
    or an explicit hex string (``"0x004C"`` / ``"004c"``). A bare all-digit string is DECIMAL (matching the
    firmware), never hex — misreading "76" as hex 0x76 would mislabel the vendor. Out-of-range (not 0..0xFFFF)
    or unparseable -> None (so no fabricated lookup)."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — never a company id
        return None
    if isinstance(value, int):
        n = value
    else:
        s = str(value).strip().lower()
        if not s:
            return None
        try:
            if s.startswith("0x"):
                n = int(s, 16)
            elif s.isdigit():
                n = int(s)            # decimal — the firmware's convention
            else:
                n = int(s, 16)        # e.g. "4c" / "004c" with hex letters
        except ValueError:
            return None
    if n < 0 or n > 0xFFFF:
        return None
    return "%04X" % n


def _load_table() -> dict[str, str]:
    """Lazy-load the bundled gzipped ``HEX4<TAB>name`` table into the module cache. Publishes only after a
    clean read (never a half-loaded table); any unreadable table degrades to empty (enrichment is optional,
    never critical) rather than raising into BLE ingestion or the analyzer view."""
    global _table
    if _table is not None:
        return _table
    tbl: dict[str, str] = {}
    try:
        with gzip.open(_TABLE_PATH, "rt", encoding="utf-8") as f:
            for line in f:
                key, _sep, name = line.partition("\t")
                name = name.rstrip("\n")
                if len(key) == 4 and name:
                    tbl[key.upper()] = name
    except FileNotFoundError:
        log.warning("BLE company table missing at %s; company lookups return ''", _TABLE_PATH)
        tbl = {}
    except (OSError, EOFError, UnicodeDecodeError, zlib.error) as exc:
        log.warning("BLE company table %s unreadable (%s); company lookups return ''", _TABLE_PATH, exc)
        tbl = {}
    _table = tbl
    return _table


def lookup_company(value: object) -> str:
    """Resolve a BLE company id (int / decimal string / hex string) to its vendor name, or "" if unknown."""
    key = normalize_company(value)
    if key is None:
        return ""
    return _load_table().get(key, "")


# ── BLE appearance (GAP "Appearance" 16-bit value) ───────────────────────────────────────────────
# LxveOS emits this as ``appr=<decimal>`` on its ble line (the raw GAP appearance the advertiser sets);
# CC never resolved it, so a heart-rate strap read "appr:833" instead of "Heart Rate Sensor". The value
# is category (bits 15..6) + subcategory (bits 5..0); we name the specific value when it's a well-known
# one, else fall back to the category. Small + stable → embedded (no bundled file, so no frozen-build
# --add-data step). Source: Bluetooth SIG Assigned Numbers §2.6 (public reference data).
_APPEARANCE_CATEGORY: dict[int, str] = {
    0: "Unknown", 1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display",
    6: "Remote Control", 7: "Eye-glasses", 8: "Tag", 9: "Keyring", 10: "Media Player",
    11: "Barcode Scanner", 12: "Thermometer", 13: "Heart Rate Sensor", 14: "Blood Pressure",
    15: "Human Interface Device", 16: "Glucose Meter", 17: "Running Walking Sensor", 18: "Cycling",
    19: "Control Device", 20: "Network Device", 21: "Sensor", 22: "Light Fixtures", 23: "Fan",
    24: "HVAC", 25: "Air Conditioning", 26: "Humidifier", 27: "Heating", 28: "Access Control",
    29: "Motorized Device", 30: "Power Device", 31: "Light Source", 32: "Window Covering",
    33: "Audio Sink", 34: "Audio Source", 35: "Motorized Vehicle", 36: "Domestic Appliance",
    37: "Wearable Audio Device", 38: "Aircraft", 39: "AV Equipment", 40: "Display Equipment",
    41: "Hearing aid", 42: "Gaming", 43: "Signage", 49: "Pulse Oximeter", 50: "Weight Scale",
    51: "Personal Mobility Device", 52: "Continuous Glucose Monitor", 53: "Insulin Pump",
    54: "Medication Delivery", 55: "Spirometer", 81: "Outdoor Sports Activity",
}
# A few well-known full (category+subcategory) values worth naming precisely.
_APPEARANCE_VALUE: dict[int, str] = {
    0x0181: "Sports Watch", 0x0341: "Heart Rate Belt", 0x03C1: "Keyboard", 0x03C2: "Mouse",
    0x03C3: "Joystick", 0x03C4: "Gamepad", 0x03C5: "Digitizer Tablet", 0x03C6: "Card Reader",
    0x03C7: "Digital Pen", 0x0C40: "Insole", 0x0C80: "Wristband",
}


def _to_u16(value: object) -> int | None:
    """Coerce an int / decimal string / hex string ('0x1234' or '1234') to a 0..0xFFFF int, else None.
    A bare all-digit string is DECIMAL (the LxveOS convention), matching normalize_company()."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    else:
        s = str(value).strip().lower()
        if not s:
            return None
        try:
            if s.startswith("0x"):
                n = int(s, 16)
            elif s.isdigit():
                n = int(s)
            else:
                n = int(s, 16)
        except ValueError:
            return None
    return n if 0 <= n <= 0xFFFF else None


def lookup_appearance(value: object) -> str:
    """Resolve a BLE GAP appearance (int / decimal string / hex string) to a human name, or "" if unknown.

    Names the exact value when it's a well-known one (e.g. 962 -> "Mouse", 833 -> "Heart Rate Belt"), else
    the category (832 -> "Heart Rate Sensor"; 64 -> "Phone"). Category 0 ("Unknown") resolves to "" so we
    never label a device that carries the no-info appearance."""
    n = _to_u16(value)
    if n is None:
        return ""
    exact = _APPEARANCE_VALUE.get(n)
    if exact:
        return exact
    category = n >> 6
    if category == 0:  # "Unknown" carries no information — don't label with it
        return ""
    return _APPEARANCE_CATEGORY.get(category, "")


# ── BLE service / member 16-bit UUIDs ────────────────────────────────────────────────────────────
# Common GATT service UUIDs + well-known member (vendor) service UUIDs, for labeling an advertised
# service-class UUID as a name ("180D" -> "Heart Rate", "FEED" -> "Tile"). The LxveOS structured ble
# EVENT does not carry service UUIDs today (the firmware names them itself in its CLI table), so this is
# a ready utility for any caller that has a raw UUID (e.g. the Flipper-by-service-UUID detector, or a
# future event field). Source: Bluetooth SIG Assigned Numbers (16-bit UUIDs + Member service UUIDs).
_SERVICE_UUID: dict[str, str] = {
    "1800": "Generic Access", "1801": "Generic Attribute", "1802": "Immediate Alert",
    "1803": "Link Loss", "1804": "Tx Power", "1805": "Current Time", "1806": "Reference Time Update",
    "1807": "Next DST Change", "1808": "Glucose", "1809": "Health Thermometer",
    "180A": "Device Information", "180D": "Heart Rate", "180E": "Phone Alert Status", "180F": "Battery",
    "1810": "Blood Pressure", "1811": "Alert Notification", "1812": "Human Interface Device",
    "1813": "Scan Parameters", "1814": "Running Speed and Cadence", "1815": "Automation IO",
    "1816": "Cycling Speed and Cadence", "1818": "Cycling Power", "1819": "Location and Navigation",
    "181A": "Environmental Sensing", "181B": "Body Composition", "181C": "User Data",
    "181D": "Weight Scale", "181E": "Bond Management", "181F": "Continuous Glucose Monitoring",
    "1820": "Internet Protocol Support", "1821": "Indoor Positioning", "1822": "Pulse Oximeter",
    "1823": "HTTP Proxy", "1824": "Transport Discovery", "1825": "Object Transfer",
    "1826": "Fitness Machine", "1827": "Mesh Provisioning", "1828": "Mesh Proxy",
    "183A": "Insulin Delivery",
    # Member (vendor-registered) service UUIDs commonly seen in adverts:
    "FEAA": "Google Eddystone", "FE2C": "Google Fast Pair", "FE9F": "Google",
    "FD6F": "Exposure Notification", "FEED": "Tile", "FD44": "Apple", "FE07": "Sonos",
    "FDF3": "Amazon Sidewalk", "FEBE": "Bose",
}
_BT_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


def normalize_uuid(value: object) -> str | None:
    """Return *value*'s resolvable 16-bit UUID as a 4-hex uppercase key ("180D"), or None.

    Accepts a 16-bit int / "0x180D" / "180d", or a full 128-bit UUID string on the Bluetooth base
    (``0000180d-0000-1000-8000-00805f9b34fb`` -> "180D"). A 128-bit UUID that is NOT on the base (a
    proprietary/vendor 128-bit UUID) has no 16-bit alias -> None (we never guess a name for it)."""
    if isinstance(value, str):
        s = value.strip().lower().strip("{}")
        if len(s) == 36 and s.endswith(_BT_BASE_SUFFIX) and s[:4] == "0000":
            s = s[4:8]  # the 16-bit alias lives in the 3rd-4th bytes of a base UUID
            try:
                return "%04X" % int(s, 16)
            except ValueError:
                return None
    n = _to_u16(value)
    return "%04X" % n if n is not None else None


def resolve_uuid(value: object) -> str:
    """Resolve a BLE service/member UUID (16-bit int/hex, or a base-aligned 128-bit UUID) to its name,
    or "" if unknown. Non-base 128-bit UUIDs and unknown 16-bit ones resolve to "" (never fabricated)."""
    key = normalize_uuid(value)
    if key is None:
        return ""
    return _SERVICE_UUID.get(key, "")
