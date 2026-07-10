#!/usr/bin/env python3
"""Generate ``site-data.json`` — the single source of truth every LxveLabs website reads for the
Cyber Controller version and counts, so no page hard-codes a number that can silently drift.

Why this exists: the sites used to hand-code the version ("v1.6.5"), the profile count ("33"),
and the parser count ("10"/"12") in each repo. Every release meant editing ~6 repos by hand, and
they fell behind. This generates those numbers from the real code, once, into ``site-data.json``;
every site consumes that file instead of hard-coding, and CI fails if a page drifts.

Three tiers, each honestly labelled in ``_meta.provenance``:
  DERIVED  (authoritative — read from code every run):
    cc_version    <- src/version.py ``__version__``
    profile_count <- number of src/config/profiles/*.json
    parser_count  <- real firmware parsers in src.protocols.PROTOCOL_DISPLAY_NAMES
                     (excludes the generic/raw passthrough fallbacks)
    parsers[]     <- their display names
  CURATED  (stable, one place — scripts/site_data_manual.json):
    backend_count, interface_count, tagline, products{...}
    (These are architectural figures, not cleanly code-derivable — e.g. "5 flash backends" counts
     the HW-validated backends and excludes the phase-3 scaffolds in flash_engine._backends.)
  ENRICHED (best-effort, via the `gh` CLI if available; null when offline/unauthenticated):
    latest_release {tag, url, published_at, assets[]}

Usage:
    python scripts/gen_site_data.py            # write site-data.json at the repo root
    python scripts/gen_site_data.py --check    # exit 1 if the committed file's DERIVED block is stale
    python scripts/gen_site_data.py --stdout    # print, don't write

The `--check` mode (also exercised by tests/test_site_data.py) is the drift guard: it regenerates the
DERIVED block and compares it to the committed site-data.json, ignoring the volatile _meta timestamp
and the ENRICHED release block. Wire `--check` into CI so a version/profile/parser change that forgets
to regenerate fails loudly — the same discipline as tests/test_profile_count.py, extended to the web.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "site-data.json")
MANUAL_PATH = os.path.join(REPO_ROOT, "scripts", "site_data_manual.json")

# Passthrough fallbacks in PROTOCOL_DISPLAY_NAMES that are NOT counted as real firmware parsers.
_FALLBACK_PARSERS = {"generic", "raw"}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def derive_cc_version() -> str:
    """Read ``__version__`` from src/version.py by text (no import — avoids pulling in GUI/serial deps)."""
    text = _read(os.path.join(REPO_ROOT, "src", "version.py"))
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    if not m:
        raise RuntimeError("could not find __version__ in src/version.py")
    return m.group(1)


def derive_profiles() -> list[str]:
    """Every firmware profile id = basename of src/config/profiles/*.json (the drift-locked SSOT)."""
    paths = glob.glob(os.path.join(REPO_ROOT, "src", "config", "profiles", "*.json"))
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in paths)


def derive_parsers() -> list[str]:
    """Real firmware parser display names from src.protocols.PROTOCOL_DISPLAY_NAMES via AST (no import),
    excluding the generic/raw passthrough fallbacks. Returns a de-duplicated, order-preserving list."""
    tree = ast.parse(_read(os.path.join(REPO_ROOT, "src", "protocols", "__init__.py")))
    display: dict[str, str] | None = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "PROTOCOL_DISPLAY_NAMES" for t in targets):
            display = ast.literal_eval(node.value)
            break
    if display is None:
        raise RuntimeError("could not find PROTOCOL_DISPLAY_NAMES in src/protocols/__init__.py")
    seen: dict[str, None] = {}
    for name, disp in display.items():
        if name in _FALLBACK_PARSERS:
            continue
        seen.setdefault(disp, None)
    return list(seen)


def derive_core() -> dict:
    """The DERIVED tier — deterministic, read straight from code. This is what the drift-check compares."""
    version = derive_cc_version()
    profiles = derive_profiles()
    parsers = derive_parsers()
    return {
        "cc_version": version,
        "profile_count": len(profiles),
        "parser_count": len(parsers),
        "parsers": parsers,
        "profiles": profiles,
    }


def load_manual() -> dict:
    if not os.path.exists(MANUAL_PATH):
        return {}
    return json.loads(_read(MANUAL_PATH))


def fetch_latest_release() -> dict | None:
    """Best-effort latest GitHub release via the `gh` CLI. Returns None if gh is missing, unauthenticated,
    or errors — never fails the generator (a release enrichment is optional; the DERIVED numbers are not)."""
    try:
        out = subprocess.run(
            ["gh", "release", "view", "--repo", "LxveAce/cyber-controller",
             "--json", "tagName,url,publishedAt,assets"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        rel = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    return {
        "tag": rel.get("tagName"),
        "url": rel.get("url"),
        "published_at": rel.get("publishedAt"),
        # Enriched for the site-sync consumer: each asset carries its download URL + size, so a
        # site can render real download links from the SSOT instead of hand-coding them.
        # (SHA256SUMS.txt is one of the assets; embedding its parsed contents is a future step.)
        "assets": [
            {"name": a.get("name"), "url": a.get("url"), "size": a.get("size")}
            for a in rel.get("assets", []) if a.get("name")
        ],
    }


def build(with_release: bool = True) -> dict:
    core = derive_core()
    manual = load_manual()
    products = dict(manual.get("products", {}))
    products["cyber_controller"] = core["cc_version"]  # derived value wins over any curated one

    data = {
        "_meta": {
            "note": "GENERATED by scripts/gen_site_data.py — do not hand-edit. Sites consume this; nothing hard-codes a version/count. Run the generator (or `--check` in CI) after any version/profile/parser change.",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "cyber-controller/scripts/gen_site_data.py",
            "provenance": {
                "derived_from_code": ["cc_version", "profile_count", "parser_count", "parsers", "profiles"],
                "curated_in_manifest": ["backend_count", "interface_count", "tagline", "products"],
                "enriched_best_effort": ["latest_release"],
            },
        },
        "cc_version": core["cc_version"],
        "profile_count": core["profile_count"],
        "parser_count": core["parser_count"],
        "parsers": core["parsers"],
        "profiles": core["profiles"],
        "backend_count": manual.get("backend_count"),
        "interface_count": manual.get("interface_count"),
        "tagline": manual.get("tagline"),
        "products": products,
        "latest_release": fetch_latest_release() if with_release else None,
    }
    return data


def _derived_subset(data: dict) -> dict:
    """The stable slice the drift-check compares — excludes the volatile _meta + enriched release."""
    return {k: data.get(k) for k in ("cc_version", "profile_count", "parser_count", "parsers", "profiles")}


def check() -> int:
    """Regenerate the DERIVED block and compare it to the committed site-data.json. Exit 1 on drift."""
    fresh = _derived_subset(build(with_release=False))
    if not os.path.exists(OUT_PATH):
        print("DRIFT: site-data.json does not exist — run: python scripts/gen_site_data.py", file=sys.stderr)
        return 1
    committed = _derived_subset(json.loads(_read(OUT_PATH)))
    if fresh != committed:
        print("DRIFT: site-data.json is stale vs the live code. Regenerate: python scripts/gen_site_data.py",
              file=sys.stderr)
        print(f"  code : {json.dumps(fresh, sort_keys=True)}", file=sys.stderr)
        print(f"  file : {json.dumps(committed, sort_keys=True)}", file=sys.stderr)
        return 1
    print("OK: site-data.json DERIVED block matches the live code.")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check()
    data = build()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if "--stdout" in argv:
        sys.stdout.write(text)
        return 0
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {OUT_PATH} - cc {data['cc_version']}, {data['profile_count']} profiles, "
          f"{data['parser_count']} parsers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
