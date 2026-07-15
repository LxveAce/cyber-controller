#!/usr/bin/env python3
"""Flash-asset reachability audit — matrix-QA method point 1 of the FLASH-DETECT-EVERYTHING
program (SSOT: command-center projects/cc-app/FLASH-AND-DETECT-EVERYTHING-2026-07-15.md).

The owner's mandate: for every firmware profile, flashing works on every board it lists —
OR the profile honestly states its real install method, with no dead Flash button. This tool
answers method-point-1 ("does the profile's release actually serve the expected asset TODAY,
for a token-less client?") so it does not rot silently the way HaleHound did.

It reuses CC's REAL resolvers (`flash_core._resolve_github` / `_resolve_pinned`) rather than
reimplementing them, so the audit tests exactly what a token-less CyberController client does.
Every github_release fetch is anonymous (`_github_latest` sends no token) — the same path the
frozen .exe takes.

Verdicts (per profile):
  OK           github_release/pinned resolved to >=1 flashable asset.
  SOURCE-ONLY  github_release resolved but upstream publishes NO matching asset -> the Flash
               button can never succeed (the HaleHound class). Actionable: build+mirror or relabel.
  BROKEN       pinned asset URL 404s, or a resolved release carries no matching asset.
  OS-IMAGE     resolver is absent (Kali/Pwnagotchi/RaspyJack/RayHunter) -> SD/OS image; correct
               behaviour is install-guidance, not an esptool flow. Not a defect.
  LOCAL        bundled/local asset (no network).
  ERROR        network / rate-limit / exception -> NOT a verdict; retry (never a false SOURCE-ONLY).

Non-esptool backends (qFlipper / SD / nRF-DFU / rtl8720 / HackRF / cc2538-bsl / adb) are labeled
with their real flash method so "not esptool" is never mistaken for "broken".

Usage:
  py scripts/audit_profile_assets.py            # full live audit (rate-budget aware)
  py scripts/audit_profile_assets.py --offline  # static classification only, no network
  py scripts/audit_profile_assets.py --only halehound,marauder
  py scripts/audit_profile_assets.py --json
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # project root, so `src.core...` resolves like CC's own imports

PROFILES_DIR = _ROOT / "src" / "config" / "profiles"

# backend -> the honest human-readable flash method (so non-esptool != broken)
FLASH_METHOD = {
    "esptool": "esptool (ESP serial)",
    "qflipper": "qFlipper / USB-DFU (Flipper Zero)",
    "sd": "SD-card image (copy, not serial-flash)",
    "nrf_dfu": "nrfutil / DFU (nRF dongle)",
    "rtl8720": "rtl8720 uploader (BW16)",
    "hackrf_spiflash": "HackRF SPI-flash (PortaPack Mayhem)",
    "cc2538_bsl": "cc2538-bsl (TI CC2538 / Sniffle)",
    "adb": "adb sideload (Android)",
}

# verdicts that are real, actionable defects (a Flash button that cannot work)
ACTIONABLE = {"SOURCE-ONLY", "BROKEN"}


def load_profiles(only: set[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if only is None or data.get("id") in only:
            out.append(data)
    return out


def classify(profile: dict) -> dict:
    """Offline classification — no network. Returns the static shape used by the report."""
    backend = profile.get("backend", "?")
    resolver = profile.get("resolver")
    return {
        "id": profile.get("id", "?"),
        "name": profile.get("name", "?"),
        "backend": backend,
        "resolver": resolver,
        "method": FLASH_METHOD.get(backend, backend),
        "esptool": backend == "esptool",
    }


def _head(url: str, timeout: float = 15.0) -> int:
    """HEAD a download URL (follows redirects to the CDN). Returns the HTTP status, 0 on error."""
    if not url:
        return 0
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def check_reachability(profile: dict) -> tuple[str, str]:
    """LIVE per-profile verdict, reusing CC's real resolvers. Returns (verdict, note)."""
    # import via importlib so an injected sys.modules fake (tests) is honored, and so --offline
    # never needs CC deps. `import a.b.c as fc` would bind by attribute access and load the real
    # module even when a fake is in sys.modules.
    fc = importlib.import_module("src.core.flash_core")

    resolver = profile.get("resolver")
    if resolver is None:
        return ("OS-IMAGE", "no resolver -> SD/OS image; install-guidance, not an ESP flash")
    if resolver == "local":
        return ("LOCAL", "bundled/local asset")
    if resolver == "github_release":
        try:
            tag, assets = fc._resolve_github(profile)
        except urllib.error.HTTPError as exc:
            return ("ERROR", f"HTTP {exc.code} (retry; not a source-only verdict)")
        except Exception as exc:  # noqa: BLE001 - audit must never crash on one bad profile
            return ("ERROR", f"{type(exc).__name__}: {exc}")
        if tag == "source-only" or not assets:
            return ("SOURCE-ONLY", "release serves no matching flashable asset (dead Flash button)")
        return ("OK", f"{len(assets)} asset(s) @ tag {tag}")
    if resolver == "pinned_release":
        try:
            tag, assets = fc._resolve_pinned(profile)
        except Exception as exc:  # noqa: BLE001
            return ("ERROR", f"{type(exc).__name__}: {exc}")
        if not assets:
            return ("BROKEN", "pinned release declares no assets")
        code = _head(assets[0].get("url", ""))
        if code == 200:
            return ("OK", f"{len(assets)} pinned asset(s); first HEAD 200 @ {tag}")
        return ("BROKEN", f"pinned asset HEAD {code} @ {tag}")
    return ("ERROR", f"unknown resolver {resolver!r}")


def _rate_budget() -> int:
    """Anonymous GitHub core rate remaining (the /rate_limit endpoint itself is not counted)."""
    try:
        with urllib.request.urlopen("https://api.github.com/rate_limit", timeout=10) as resp:
            return int(json.load(resp)["resources"]["core"]["remaining"])
    except Exception:
        return -1


def audit(only: set[str] | None, offline: bool) -> list[dict]:
    profiles = load_profiles(only)
    rows = [classify(p) for p in profiles]
    if offline:
        for row in rows:
            row["verdict"], row["note"] = "SKIPPED", "offline (static classification only)"
        return rows

    gh_needed = sum(1 for p in profiles if p.get("resolver") == "github_release")
    budget = _rate_budget()
    if 0 <= budget < gh_needed:
        print(
            f"# WARNING: anon GitHub budget {budget} < {gh_needed} github_release profiles — "
            f"the remainder will report ERROR (rate-limit); re-run next hour to finish.",
            file=sys.stderr,
        )
    for row, profile in zip(rows, profiles):
        row["verdict"], row["note"] = check_reachability(profile)
    return rows


def print_report(rows: list[dict]) -> None:
    by_verdict: dict[str, list[dict]] = {}
    for row in rows:
        by_verdict.setdefault(row["verdict"], []).append(row)
    print(f"# Profile flash-asset audit - {len(rows)} profiles\n")
    for row in rows:
        flag = "  <-- ACTIONABLE" if row["verdict"] in ACTIONABLE else ""
        print(f"{row['verdict']:<12} {row['id']:<22} {row['backend']:<14} {row['note']}{flag}")
    print("\n# Summary")
    for verdict in sorted(by_verdict):
        print(f"  {verdict:<12} {len(by_verdict[verdict])}")
    actionable = [r for r in rows if r["verdict"] in ACTIONABLE]
    if actionable:
        print(f"\n# ACTIONABLE ({len(actionable)}) — dead/broken Flash buttons to fix-or-relabel:")
        for row in actionable:
            print(f"  - {row['id']} ({row['backend']}): {row['note']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit firmware-profile flash-asset reachability.")
    ap.add_argument("--offline", action="store_true", help="static classification only, no network")
    ap.add_argument("--only", default="", help="comma-separated profile ids to check")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    rows = audit(only, args.offline)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
