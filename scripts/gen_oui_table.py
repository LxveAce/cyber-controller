#!/usr/bin/env python3
"""Generate the bundled MAC-prefix→vendor table from the public IEEE registry.

The registry is factual assignment data (as bundled by Wireshark, nmap, aircrack-ng, …); we redistribute the
prefix→organization mapping only, with attribution. Two input formats:

  * **Wireshark manuf** (RECOMMENDED — covers all three IEEE block sizes MA-L/M/S; the IEEE site blocks
    automated .csv access): ``MAC[/bits]<TAB>Short<TAB>Full``. Canonical:
    https://www.wireshark.org/download/automated/data/manuf
        python scripts/gen_oui_table.py --manuf path/to/manuf
  * **IEEE MA-L oui.csv** (24-bit only): ``Registry,Assignment,Organization Name,Address``.
        python scripts/gen_oui_table.py path/to/oui.csv

Both write ``src/config/oui_table.tsv.gz`` (``prefix<TAB>organization``, gzipped, sorted; prefix is 6/7/9 hex
for 24/28/36-bit blocks). The runtime loader is ``src/core/oui.py`` (longest-prefix match).
"""
from __future__ import annotations

import csv
import gzip
import re
import sys
from pathlib import Path

_OUT = Path(__file__).resolve().parent.parent / "src" / "config" / "oui_table.tsv.gz"
_NON_HEX = re.compile(r"[^0-9A-Fa-f]")


def _write(entries: dict[str, str]) -> int:
    body = "".join(f"{p}\t{entries[p]}\n" for p in sorted(entries))
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(_OUT, "wt", encoding="utf-8", newline="") as gz:
        gz.write(body)
    return len(entries)


def build_from_manuf(src: str) -> int:
    """Read a Wireshark ``manuf`` file (MA-L/M/S) and write the variable-length prefix→org table."""
    entries: dict[str, str] = {}
    with open(src, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            token = parts[0].split("/")
            nhex = (int(token[1]) // 4) if len(token) == 2 and token[1].strip().isdigit() else 6
            if nhex not in (6, 7, 9):  # IEEE block sizes: 24/28/36 bits
                continue
            pref = _NON_HEX.sub("", token[0])[:nhex].upper()
            name = parts[2].strip() if len(parts) > 2 and parts[2].strip() else parts[1].strip()
            if len(pref) == nhex and all(c in "0123456789ABCDEF" for c in pref) and name:
                entries[pref] = name
    return _write(entries)


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
    return _write(entries)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--manuf":
        n = build_from_manuf(sys.argv[2])
    elif len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        n = build(sys.argv[1])
    else:
        print(__doc__)
        raise SystemExit(2)
    print(f"wrote {_OUT} ({n} prefixes)")
