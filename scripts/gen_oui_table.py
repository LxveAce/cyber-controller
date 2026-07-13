#!/usr/bin/env python3
"""Generate the bundled OUI→vendor table from the public IEEE MA-L registry.

Source (free, authoritative): https://standards-oui.ieee.org/oui/oui.csv
Columns: ``Registry,Assignment,Organization Name,Organization Address``. ``Assignment`` is the
24-bit OUI as 6 hex digits. The registry is factual assignment data (as bundled by Wireshark,
nmap, aircrack-ng, …); we redistribute the prefix→organization mapping only, with attribution.

Usage:
    python scripts/gen_oui_table.py path/to/oui.csv
    # writes src/config/oui_table.tsv.gz  (prefix<TAB>organization, gzipped, sorted)

Re-run whenever you refresh oui.csv from IEEE. The runtime loader is ``src/core/oui.py``; a user
can also load a fresh oui.csv at runtime via ``oui.load_ieee_csv(path)`` without regenerating.
"""
from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

_OUT = Path(__file__).resolve().parent.parent / "src" / "config" / "oui_table.tsv.gz"


def build(src_csv: str) -> int:
    """Read an IEEE oui.csv and write the compact gzipped prefix→org table. Returns the count."""
    entries: dict[str, str] = {}
    with open(src_csv, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            pref = row[1].strip().upper()
            name = row[2].strip()
            if len(pref) != 6 or not all(c in "0123456789ABCDEF" for c in pref):
                continue
            if not name or name.lower() == "private":  # unassigned/withheld blocks carry no vendor
                continue
            entries[pref] = name
    body = "".join(f"{p}\t{entries[p]}\n" for p in sorted(entries))
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(_OUT, "wt", encoding="utf-8", newline="") as gz:
        gz.write(body)
    return len(entries)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    n = build(sys.argv[1])
    print(f"wrote {_OUT} ({n} OUIs)")
