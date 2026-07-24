"""Bluetooth SIG company-identifier → vendor name lookup (BLE manufacturer resolver).

A passive enrichment: given the 16-bit company identifier a BLE advertiser puts in its manufacturer-specific
data (``mfg_data[0..1]``, little-endian), resolve it to the registering company — so a BLE row shows
"Apple, Inc." instead of a raw ``76``. LxveOS emits this as ``company=<decimal>`` on its LXVEOS/1 ble line
(the firmware only names ~6 companies on-device, "never mislabelled"; this resolves all of them CC-side).

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
