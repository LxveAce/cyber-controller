"""OUI → vendor lookup (IEEE MA-L registry).

A passive enrichment: given a device MAC, resolve its 24-bit OUI prefix to the registering
organization so scanned APs / stations / BLE devices carry a vendor label. Locally-administered,
multicast and randomized-privacy MACs resolve to "" — no IEEE vendor is assigned to them, so we
never fabricate one.

The bundled table (``src/config/oui_table.tsv.gz``) is generated from the public IEEE registry
(https://standards-oui.ieee.org/oui/oui.csv) by ``scripts/gen_oui_table.py`` and is lazy-loaded on
first lookup. ``load_ieee_csv(path)`` / ``load_manuf(path)`` merge a user-supplied full registry at
runtime (offline BYO / refresh), mirroring the wordlist/hash bring-your-own pattern.
"""
from __future__ import annotations

import csv
import gzip
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_TABLE_PATH = Path(__file__).resolve().parent.parent / "config" / "oui_table.tsv.gz"
_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
_NON_HEX = re.compile(r"[^0-9A-Fa-f]")

# Lazy cache: OUI prefix (6 uppercase hex) -> organization name. None until first load.
_table: dict[str, str] | None = None


def normalize_oui(mac: str) -> str | None:
    """Return *mac*'s 24-bit OUI prefix (6 uppercase hex chars), or None if it has no IEEE vendor.

    Strips the usual separators (``: - .`` and spaces) and requires a full 12-hex (48-bit) MAC.
    Returns None for a non-MAC and — importantly — for multicast (group bit) or locally-administered
    (randomized-privacy) MACs: those first octets are self-assigned, carry NO registered vendor, and
    resolving one would fabricate a manufacturer.

    The 12-hex floor is load-bearing, not cosmetic: an index-only firmware (e.g. the BW16 Vampire)
    mints a MAC-less synthetic key ``idx:{port}:{index}`` for APs it saw without a BSSID, and stray
    hex characters in such a key can total ≥ 6 (``idx:COM7:196`` → ``DC7196`` → "Intel Corporate").
    Demanding a whole MAC drops those so a MAC-less target never gets a phantom vendor.
    """
    if not mac:
        return None
    hex_only = _NON_HEX.sub("", mac)
    if len(hex_only) < 12:   # need a full 48-bit MAC — a partial/synthetic key has no real OUI
        return None
    first_octet = int(hex_only[:2], 16)
    if first_octet & 0b01:   # multicast / group address
        return None
    if first_octet & 0b10:   # locally administered (randomized privacy MAC) — no IEEE vendor
        return None
    return hex_only[:6].upper()


def _load_table() -> dict[str, str]:
    """Lazy-load the bundled gzipped ``prefix<TAB>org`` table into the module cache."""
    global _table
    if _table is None:
        _table = {}
        try:
            with gzip.open(_TABLE_PATH, "rt", encoding="utf-8") as f:
                for line in f:
                    pref, _sep, name = line.partition("\t")
                    name = name.rstrip("\n")
                    if len(pref) == 6 and name:
                        _table[pref.upper()] = name
        except FileNotFoundError:
            log.warning("OUI table missing at %s; vendor lookups will return ''", _TABLE_PATH)
    return _table


def lookup_vendor(mac: str) -> str:
    """Resolve *mac* to its IEEE-registered vendor, or "" if unknown / not vendor-assigned."""
    pref = normalize_oui(mac)
    if pref is None:
        return ""
    return _load_table().get(pref, "")


def load_ieee_csv(path: str | Path) -> int:
    """Merge a full IEEE OUI CSV (``Registry,Assignment,Organization Name,Address``) into the live
    table for complete/refreshed coverage. Returns the number of OUIs merged."""
    table = _load_table()
    added = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            pref, name = row[1].strip().upper(), row[2].strip()
            if _HEX6.fullmatch(pref) and name and name.lower() != "private":
                table[pref] = name
                added += 1
    return added


def load_manuf(path: str | Path) -> int:
    """Merge a Wireshark ``manuf`` file (``AA:BB:CC<TAB>Short[<TAB>Long]``) into the live table.
    Only 24-bit OUIs are used (longer MA-M/MA-S blocks are skipped). Returns the number merged."""
    table = _load_table()
    added = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            token = parts[0].split("/")
            if len(token) == 2 and token[1].strip() != "24":
                continue  # a sub-OUI (MA-M/MA-S) block — we key strictly on 24-bit prefixes
            pref = _NON_HEX.sub("", token[0])[:6].upper()
            name = parts[1].strip() if len(parts) > 1 else ""
            if _HEX6.fullmatch(pref) and name:
                table[pref] = name
                added += 1
    return added
