# ble_company_ids.tsv.gz — source & attribution

Bundled lookup table mapping a 16-bit Bluetooth SIG **Company Identifier** → the registering company name,
used by `src/core/ble_numbers.py` to label BLE advertisers (e.g. `76` → "Apple, Inc.").

- **Format:** gzipped TSV, one row per company: `HEX4<TAB>name` (e.g. `004C\tApple, Inc.`), 4-hex uppercase key.
- **Entries:** 3998 (as generated 2026-07-23).
- **Upstream:** the public **Bluetooth SIG Assigned Numbers** (Company Identifiers), via
  [`NordicSemiconductor/bluetooth-numbers-database`](https://github.com/NordicSemiconductor/bluetooth-numbers-database)
  `v1/company_ids.json`.
- **License:** MIT (Copyright © 2019–2020 Nordic Semiconductor ASA). The underlying identifiers are published
  by the Bluetooth SIG as public assigned numbers. Compatible with redistribution + attribution here.
- **Retrieved:** 2026-07-23.

## Refresh

Re-download `v1/company_ids.json` from the upstream repo and regenerate:

```python
import json, gzip
j = json.load(open("company_ids.json", encoding="utf-8"))
with gzip.open("src/config/ble_company_ids.tsv.gz", "wt", encoding="utf-8") as f:
    for e in sorted(j, key=lambda e: e["code"]):
        name = (e.get("name") or "").strip()
        if name:
            f.write("%04X\t%s\n" % (e["code"] & 0xFFFF, name))
```

Policy (same as the OUI table): resolve only a known id; an unknown/unparseable id returns "" — never a
fabricated vendor.
