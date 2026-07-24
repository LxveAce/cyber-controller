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
import zlib
from pathlib import Path

from src.core.resources import resource_path

log = logging.getLogger(__name__)

# Frozen-safe (resource_path resolves to the repo in dev and _MEIPASS in the PyInstaller build). Was a
# __file__-relative path, which — combined with the table not being --add-data'd — meant the OUI vendor table
# never shipped in the frozen .exe (vendor lookups silently returned "" in the installed app). C-8 class.
_TABLE_PATH = resource_path("src", "config", "oui_table.tsv.gz")
_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
_HEX_ONLY = re.compile(r"[0-9A-Fa-f]+")
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
    """Lazy-load the bundled gzipped ``prefix<TAB>org`` table into the module cache.

    Builds into a LOCAL dict and publishes it to the cache only after the read finishes, so a
    truncated/corrupt table never leaves a HALF-loaded table cached. Any unreadable table (missing,
    corrupt gzip, truncated, mis-encoded) degrades to empty — vendor enrichment is optional, never
    critical, so it must return "" rather than raise into serial ingestion or the targets page."""
    global _table
    if _table is not None:
        return _table
    tbl: dict[str, str] = {}
    try:
        with gzip.open(_TABLE_PATH, "rt", encoding="utf-8") as f:
            for line in f:
                pref, _sep, name = line.partition("\t")
                name = name.rstrip("\n")
                # Variable-length prefixes: 6 hex = 24-bit MA-L, 7 = 28-bit MA-M, 9 = 36-bit MA-S.
                if len(pref) in (6, 7, 9) and name:
                    tbl[pref.upper()] = name
    except FileNotFoundError:
        log.warning("OUI table missing at %s; vendor lookups will return ''", _TABLE_PATH)
        tbl = {}
    except (OSError, EOFError, UnicodeDecodeError, zlib.error) as exc:
        # Corrupt / truncated / mis-encoded table -> degrade to empty (same as missing), never raise
        # into a caller and never cache the partial rows read before the failure. zlib.error covers
        # in-body DEFLATE corruption (e.g. an invalid block): unlike gzip.BadGzipFile (bad header or
        # trailer CRC, which IS an OSError) it subclasses Exception directly, so without it a
        # mid-stream decode failure would escape, leave _table=None, and re-raise on every lookup.
        log.warning("OUI table %s unreadable (%s); vendor lookups return ''", _TABLE_PATH, exc)
        tbl = {}
    _table = tbl
    return _table


def _clean_mac(mac: str) -> str | None:
    """Full 12-hex uppercase MAC if it's a real unicast, globally-administered address, else None.

    Same guards as :func:`normalize_oui` — multicast (group bit) and locally-administered (randomized privacy)
    MACs carry no IEEE vendor — but returns the WHOLE MAC so a longest-prefix lookup can test the 36/28/24-bit
    prefixes, not just the 24-bit OUI."""
    if not mac:
        return None
    hex_only = _NON_HEX.sub("", mac)
    if len(hex_only) < 12:
        return None
    first_octet = int(hex_only[:2], 16)
    if first_octet & 0b01:   # multicast / group address
        return None
    if first_octet & 0b10:   # locally administered (randomized privacy MAC) — no IEEE vendor
        return None
    return hex_only[:12].upper()


def lookup_vendor(mac: str) -> str:
    """Resolve *mac* to its IEEE-registered vendor, or "" if unknown / not vendor-assigned.

    Longest-prefix match: a 36-bit (MA-S) or 28-bit (MA-M) sub-block wins over the 24-bit (MA-L) OUI, because
    IEEE sub-assigns a /24 administrator's space to many small orgs — so a 24-bit-only lookup returns the block
    administrator (often blank/withheld), not the device's actual vendor."""
    full = _clean_mac(mac)
    if full is None:
        return ""
    table = _load_table()
    for n in (9, 7, 6):   # MA-S (36-bit) -> MA-M (28-bit) -> MA-L (24-bit)
        org = table.get(full[:n])
        if org:
            return org
    return ""


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
    """Merge a Wireshark ``manuf`` file (``MAC[/bits]<TAB>Short[<TAB>Long]``) into the live table, keyed by the
    variable-length IEEE prefix — 24-bit (MA-L), 28-bit (MA-M) AND 36-bit (MA-S) blocks all merge, so a runtime
    refresh matches the bundled table's coverage. Prefers the full vendor name (3rd column). Returns the count."""
    table = _load_table()
    added = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            token = parts[0].split("/")
            nhex = (int(token[1]) // 4) if len(token) == 2 and token[1].strip().isdigit() else 6
            if nhex not in (6, 7, 9):  # IEEE block sizes are 24/28/36 bits
                continue
            pref = _NON_HEX.sub("", token[0])[:nhex].upper()
            full = parts[2].strip() if len(parts) > 2 and parts[2].strip() else ""
            name = full or (parts[1].strip() if len(parts) > 1 else "")
            if len(pref) == nhex and _HEX_ONLY.fullmatch(pref) and name:
                table[pref] = name
                added += 1
    return added
