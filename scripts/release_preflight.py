#!/usr/bin/env python3
"""Preflight gate for the staged (dispatch-only, draft-only) release build.

Used by `.github/workflows/build-release.yml`. Reads the FULL release list JSON on stdin (from
`gh api --paginate repos/{repo}/releases` — the by-tag endpoint returns only PUBLISHED releases,
so drafts must be found by listing) and verifies, for the given tag:

  * the tag is well-formed (`vX.Y.Z` with an optional suffix — nothing shell-hostile ever
    reaches the build steps);
  * EXACTLY ONE release exists for the tag — zero means nothing to build into, and two (GitHub
    happily keeps multiple drafts with the same pending tag) means uploads could land on the
    wrong one;
  * that release is still a DRAFT — a published release is REJECTED, so a rerun after publishing
    can never touch the shipped assets;
  * with --require-assets: all five platform binaries are attached (the release's expected shape);
  * with --require-checksums: SHA256SUMS.txt is attached too (the full six-asset shape).

`--list-expected` prints the five expected binary names for the tag (one per line) and exits —
the workflow downloads exactly these names, never a glob, so a stray asset can't join the
checksum file and a missing one fails loudly.

Exit 0 only when every requested check passes. No network access; no secrets involved.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# vX.Y.Z with an optional dot/dash suffix (e.g. v2.0.1, v2.1.0-beta.2). Ends with \Z (not $, which
# also matches just before a trailing newline) and is only ever used with fullmatch(), so a value
# like "v2.0.1\n" is rejected — the tag is interpolated into asset names and shell env downstream.
TAG_RE = re.compile(r"v\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?\Z")


def expected_binaries(tag: str) -> list[str]:
    """The five platform binaries every release ships (the installer's `v` prefix comes from
    installer/cyber-controller.iss: OutputBaseFilename=cyber-controller-v{version}-...)."""
    return [
        f"cyber-controller-{tag}-windows-x64.exe",
        f"cyber-controller-{tag}-windows-x64-setup.exe",
        f"cyber-controller-{tag}-linux-x64",
        f"cyber-controller-{tag}-linux-arm64",
        f"cyber-controller-{tag}-macos-arm64",
    ]


def check(tag: str, releases_json: str, require_assets: bool = False,
          require_checksums: bool = False, out=sys.stderr) -> int:
    if not TAG_RE.fullmatch(tag):
        print(f"PREFLIGHT FAIL: tag {tag!r} is not a vX.Y.Z tag.", file=out)
        return 1
    try:
        releases = json.loads(releases_json)
    except json.JSONDecodeError as e:
        print(f"PREFLIGHT FAIL: release list is not valid JSON: {e}", file=out)
        return 1
    if not isinstance(releases, list):
        print("PREFLIGHT FAIL: release list JSON is not an array.", file=out)
        return 1
    # `gh api --paginate --slurp` wraps each page in an outer array; flatten to one release list.
    if releases and all(isinstance(page, list) for page in releases):
        releases = [r for page in releases for r in page]

    matches = [r for r in releases if isinstance(r, dict) and r.get("tag_name") == tag]
    if not matches:
        print(f"PREFLIGHT FAIL: no release exists for tag {tag} — create the DRAFT release first.",
              file=out)
        return 1
    if len(matches) > 1:
        print(f"PREFLIGHT FAIL: {len(matches)} releases carry tag {tag} — ambiguous target, "
              "uploads could land on the wrong one. Delete the extras first.", file=out)
        return 1

    rel = matches[0]
    if rel.get("draft") is not True:
        print(f"PREFLIGHT FAIL: the release for {tag} is PUBLISHED (draft={rel.get('draft')!r}). "
              "This workflow only builds into an existing DRAFT; refusing to touch published assets.",
              file=out)
        return 1

    required = []
    if require_assets:
        required += expected_binaries(tag)
    if require_checksums:
        required.append("SHA256SUMS.txt")
    if required:
        attached = {a.get("name") for a in rel.get("assets", []) if isinstance(a, dict)}
        missing = [n for n in required if n not in attached]
        if missing:
            print("PREFLIGHT FAIL: draft release is missing required asset(s): "
                  + ", ".join(missing), file=out)
            return 1

    print(f"preflight OK: {tag} -> exactly one DRAFT release"
          + (f", {len(required)} required assets attached" if required else ""), file=out)
    return 0


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("tag")
    p.add_argument("--require-assets", action="store_true",
                   help="also require all five platform binaries attached")
    p.add_argument("--require-checksums", action="store_true",
                   help="also require SHA256SUMS.txt attached")
    p.add_argument("--list-expected", action="store_true",
                   help="print the five expected binary names and exit (no stdin needed)")
    a = p.parse_args(argv)

    if a.list_expected:
        if not TAG_RE.fullmatch(a.tag):
            print(f"PREFLIGHT FAIL: tag {a.tag!r} is not a vX.Y.Z tag.", file=sys.stderr)
            return 1
        print("\n".join(expected_binaries(a.tag)))
        return 0

    text = stdin_text if stdin_text is not None else sys.stdin.read()
    return check(a.tag, text, require_assets=a.require_assets,
                 require_checksums=a.require_checksums)


if __name__ == "__main__":
    raise SystemExit(main())
