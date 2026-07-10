"""Drift-lock: the firmware-profile count claimed in README.md must equal the number of profile JSONs
actually shipped in src/config/profiles/. Prevents the badge/body from silently going stale again (it
had drifted to '21' while 26 profiles shipped)."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _profile_json_count() -> int:
    return len(list((_ROOT / "src" / "config" / "profiles").glob("*.json")))


def test_readme_firmware_count_matches_disk():
    n = _profile_json_count()
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    badge = re.search(r"firmware%20profiles-(\d+)-", readme)
    assert badge and int(badge.group(1)) == n, f"README badge != {n} shipped profiles"
    assert f"**{n} firmware profiles** across" in readme, f"README body count != {n}"
    assert f"{n} firmware profiles ship in" in readme, f"README 'ship in' count != {n}"


def test_readme_firmware_table_rows_match_disk():
    """The badge/prose counts are integers; they can pass while the actual enumerated
    Supported-Firmwares table silently drops a profile (each JSON must have exactly one row,
    'Custom / local .bin' included). Lock the table's data-row count to the profiles on disk."""
    n = _profile_json_count()
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\| Firmware \| Purpose \| Chips / boards \| Backend \|\n\|[-| ]+\|\n((?:\|.*\n)+)", readme)
    assert m, "Supported-Firmwares table not found (header/format changed?)"
    rows = [ln for ln in m.group(1).splitlines() if ln.strip().startswith("|")]
    assert len(rows) == n, f"firmware table has {len(rows)} rows but {n} profiles ship on disk"
