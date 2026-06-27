#!/usr/bin/env python3
"""Refresh the bundled Software-OS catalog's pinned (offline-fallback) versions.

Resolves each catalog entry's LATEST version live from upstream (Kali SHA256SUMS, Arch releng JSON
feed, Tails installer feed) and writes the result back into ``src/config/os_catalog.json`` so the
shipped offline fallback never goes stale. Run by ``.github/workflows/update-os-catalog.yml`` on a
schedule (which opens a PR with the diff); also runnable locally.

An entry that cannot be resolved online (network/feed failure) is left untouched.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import os_catalog as oc  # noqa: E402

CATALOG = ROOT / "src" / "config" / "os_catalog.json"


def main() -> int:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    changed = False
    for img in data.get("images", []):
        entry = oc.OSImage.from_dict(img)
        resolved = oc.resolve(entry, print, online=True)
        if resolved.source != "online" or not resolved.version or resolved.version == "?":
            print(f"[update] {entry.id}: not cleanly resolved online — leaving pinned at "
                  f"{img.get('pinned', {}).get('version', '?')}")
            continue
        pinned = img.setdefault("pinned", {})
        new_vals = {"version": resolved.version, "image_url": resolved.image_url,
                    "sha256": resolved.sha256}
        if resolved.sig_url:
            new_vals["sig_url"] = resolved.sig_url
        if resolved.checksums_url:
            new_vals["checksums_url"] = resolved.checksums_url
        if resolved.checksums_sig_url:
            new_vals["checksums_sig_url"] = resolved.checksums_sig_url
        for k, v in new_vals.items():
            if pinned.get(k) != v:
                pinned[k] = v
                changed = True
        print(f"[update] {entry.id}: pinned -> {resolved.version}")

    if changed:
        data["updated"] = datetime.date.today().isoformat()
        CATALOG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("[update] catalog refreshed.")
    else:
        print("[update] catalog already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
