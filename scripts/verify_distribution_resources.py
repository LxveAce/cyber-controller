#!/usr/bin/env python3
"""Verify that a built Cyber Controller wheel contains its runtime resources.

This is an artifact check, not a source-tree check. It catches the failure mode
where Python modules build successfully but profiles, templates, static files,
maps, macros, or other data are absent from the wheel.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

# Representative load-bearing resources. Directory-wide counts below keep the
# check from passing with just one token file from a partially configured glob.
REQUIRED_SUFFIXES = (
    "src/config/ble_company_ids.tsv.gz",
    "src/config/maps/world_110m.geojson",
    "src/config/os_catalog.json",
    "src/config/oui_table.tsv.gz",
    "src/config/probes/cyd_probe.bin",
    "src/config/profiles/lxveos.json",
    "src/config/wordlists/10k-most-common.txt",
    "src/core/default_macros/cc_marauder_ap_scan.json",
    "src/ui/qt/theme/cyber_dark.qss",
    "src/ui/tui/styles.tcss",
    "src/ui/web/static/reform.css",
    "src/ui/web/static/reform.js",
    "src/ui/web/static/updates_card.js",
    "src/ui/web/static/updates_transport.js",
    "src/ui/web/static/vendor/socket.io.min.js",
    "src/ui/web/templates/reform.html",
    "share/cyber-controller/assets/cc-logo.png",
    "share/cyber-controller/assets/icons/device-view.svg",
    "share/cyber-controller/docs/HOWTO.md",
)

MINIMUM_PREFIX_COUNTS = {
    "src/config/profiles/": 52,
    "src/core/default_macros/": 13,
    "src/ui/web/templates/": 10,
}

# Python distributions deliberately do not carry encrypted executable packs.
# Keep this negative gate beside the positive resource inventory so a future
# broad package-data glob cannot silently reintroduce them.
FORBIDDEN_SUFFIXES = (".pack",)


def _has_suffix(members: set[str], suffix: str) -> bool:
    """Match package members directly and wheel ``.data/data`` members by suffix."""
    return any(name == suffix or name.endswith("/" + suffix) for name in members)


def missing_resources(wheel: str | Path) -> list[str]:
    """Return human-readable resource failures for *wheel* (empty means pass)."""
    path = Path(wheel)
    if not path.is_file():
        return [f"wheel not found: {path}"]
    try:
        with zipfile.ZipFile(path) as archive:
            members = {name.rstrip("/") for name in archive.namelist() if not name.endswith("/")}
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"could not read wheel {path}: {exc}"]

    missing = [f"missing resource: {suffix}" for suffix in REQUIRED_SUFFIXES
               if not _has_suffix(members, suffix)]
    for suffix in FORBIDDEN_SUFFIXES:
        forbidden = sorted(name for name in members if name.endswith(suffix))
        if forbidden:
            missing.append(f"forbidden distribution member ({suffix}): {forbidden[0]}")
    for prefix, minimum in MINIMUM_PREFIX_COUNTS.items():
        count = sum(name.startswith(prefix) for name in members)
        if count < minimum:
            missing.append(f"resource count {prefix}: found {count}, expected at least {minimum}")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="Path to a built .whl file")
    args = parser.parse_args(argv)
    failures = missing_resources(args.wheel)
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print(f"OK - required distribution resources present in {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
