#!/usr/bin/env python3
"""Generate the web-flasher manifest for the flagship firmwares.

The website's browser flasher (esptool-js) needs, per firmware, the exact fetchable release-asset
``.bin`` URL(s) and the flash offset each one is written at. The desktop app already knows this — it
resolves the latest release + lays out the flash segments in :mod:`src.core.flash_core`. This script
REUSES that resolution (the single source of truth) so the web flasher writes exactly what the
desktop does; it does not re-derive URLs or offsets independently.

Output shape (JSON)::

    {
      "generated": "<how>",
      "note": "...",
      "firmwares": {
        "ghostesp": {
          "repo": "GhostESP-Revival/GhostESP",
          "tag": "v2.0",
          "image_model": "merged-single-bin",
          "variants": [
            {"name": "...", "label": "...", "chip": "esp32",
             "segments": [{"offset": "0x0", "url": "https://.../asset.bin"}]}
          ]
        },
        "marauder": { ... segments = bootloader/partitions/boot_app0 (FlashFiles) + app ... }
      },
      "skipped": [ {"firmware": "marauder", "variant": "...", "chip": "esp32c5",
                    "reason": "no web-flashable support-file mapping"} ]
    }

A MERGED firmware (ghost_esp, bruce) is one segment — the single ``.bin`` at its app offset (0x0).
MARAUDER ships the app ``.bin`` only; its bootloader/partitions/boot_app0 live in the repo's
``FlashFiles/`` tree, so a full web flash needs those raw URLs too — and only for the chips that have
a support-file mapping (esp32 / esp32s2 / esp32s3). Other marauder chips are reported under "skipped".

Because the profiles track the *latest* upstream release, this manifest goes stale as upstream
releases — regenerate it (CI on a schedule, or on demand) rather than trusting a committed copy.
Run:  ``py scripts/gen_web_flasher_manifest.py [--out path.json]``  (hits the GitHub API).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run standalone

from src.core import flash_core as fc  # noqa: E402  (after the sys.path bootstrap above)

#: The flagship firmwares Atlas asked for first.
FLAGSHIP_IDS = ("marauder", "ghostesp", "bruce")


def support_segments(profile: fc.FirmwareProfile, chip: str) -> Optional[List[Dict[str, str]]]:
    """The bootloader/partitions/boot_app0 flash segments (offset + raw URL) a multi-file firmware
    needs, read from the profile's OWN ``support_files`` config (the same config GenericProfile's
    downloader uses) and built with flash_core's URL helpers — so the manifest can't drift from what
    the desktop flashes. Returns None when the chip has no support mapping (can't full-flash it from
    the browser), or when the firmware declares no support config."""
    cfg = getattr(profile, "cfg", None)
    sf = cfg.get("support_files") if cfg else None
    if not sf:
        return None

    if sf["source"] == "repo_tree":
        branch = (sf.get("branches") or ["main"])[0]
        dirmap = sf.get("support_dir_by_chip")
        if dirmap is not None:
            d = dirmap.get(chip)
            if not d:
                return None                          # no dir for this chip -> not web-flashable
        else:
            d = ""
        segs = [
            {"offset": fc._bootloader_offset(chip),
             "url": fc._tree_url(sf, branch, sf["bootloader_path"].format(dir=d))},
            {"offset": sf["partitions_offset"],
             "url": fc._tree_url(sf, branch, sf["partitions_path"].format(dir=d))},
        ]
        if sf.get("include_boot_app0") and sf.get("boot_app0_path"):
            segs.append({"offset": sf["boot_app0_offset"],
                         "url": fc._tree_url(sf, branch, sf["boot_app0_path"].format(dir=d))})
        return segs

    if sf["source"] == "pinned":
        return [{"offset": meta["offset"], "url": fc._pinned_url(cfg, meta["source"], nm)}
                for nm, meta in sf["pinned_files"].items()]

    return None


def variant_entry(profile: fc.FirmwareProfile, asset: Dict) -> Optional[Dict]:
    """Build the manifest entry for one release asset (a flashable variant), or None when the
    variant can't be fully flashed from the browser (a multi-file chip with no support mapping)."""
    chip = asset.get("chip") or ""
    app_url = asset.get("url")
    app_offset = asset.get("offset") or profile.app_offset(chip)

    if profile.image_model == fc.IMAGE_MERGED:
        segments = [{"offset": app_offset, "url": app_url}]
    else:
        support = support_segments(profile, chip)
        if support is None:
            return None
        segments = support + [{"offset": app_offset, "url": app_url}]

    return {
        "name": asset.get("name"),
        "label": asset.get("label"),
        "chip": chip,
        "segments": segments,
    }


def manifest_for_assets(profile: fc.FirmwareProfile, tag: str, assets: List[Dict]) -> Dict:
    """PURE: build one firmware's manifest section from its resolved release assets. Separated from
    the network fetch so it is unit-testable with canned assets."""
    variants: List[Dict] = []
    skipped: List[Dict] = []
    for a in assets:
        entry = variant_entry(profile, a)
        if entry is None:
            skipped.append({"firmware": profile.id, "variant": a.get("name"),
                            "chip": a.get("chip"), "reason": "no web-flashable support-file mapping"})
            continue
        variants.append(entry)
    section = {
        "repo": profile.repo,
        "tag": tag,
        "image_model": profile.image_model,
        "variants": variants,
    }
    return {"section": section, "skipped": skipped}


def build_manifest(ids=FLAGSHIP_IDS) -> Dict:
    """Resolve each firmware's latest release (NETWORK) and assemble the full manifest."""
    firmwares: Dict[str, Dict] = {}
    skipped: List[Dict] = []
    for pid in ids:
        profile = fc.get_profile(pid)
        tag, assets = profile.latest_release()
        built = manifest_for_assets(profile, tag, assets)
        firmwares[pid] = built["section"]
        skipped.extend(built["skipped"])
    return {
        "generated": "scripts/gen_web_flasher_manifest.py (reuses src.core.flash_core resolution)",
        "note": ("Tracks the LATEST upstream release, so it goes stale on new releases — regenerate "
                 "rather than trusting a committed copy. Multi-file support segments (bootloader/"
                 "partitions/boot_app0) use each profile's first configured branch; the desktop "
                 "falls back through the rest if one 404s."),
        "firmwares": firmwares,
        "skipped": skipped,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the web-flasher manifest (flagship firmwares).")
    ap.add_argument("--out", default="", help="Write JSON here (default: stdout).")
    args = ap.parse_args(argv)
    manifest = build_manifest()
    text = json.dumps(manifest, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
