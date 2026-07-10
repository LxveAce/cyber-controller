#!/usr/bin/env python3
"""Site-sync — rewrite SSOT-marked tokens in a static site from ``site-data.json`` (commit-time).

The first *consumer* of the ``site-data.json`` SSOT that ``gen_site_data.py`` produces. The SSOT
carried authoritative numbers (cc_version, profile/parser counts, release info) but nothing read it,
so pages still hand-coded the version/counts and drifted. This closes that loop.

**How it works:** a page marks each value it wants kept in sync with an HTML-comment region:

    Cyber Controller <!--ssot:cc_version-->1.6.9<!--/ssot:cc_version--> supports
    <!--ssot:profile_count-->34<!--/ssot:profile_count--> firmwares.

``sync_site.py`` rewrites the text *between* the markers with the current SSOT value, in place, at
**commit time** — the shipped HTML stays static, so there is **no client-side fetch** (nothing for a
strict CSP to block, nothing to slow first paint). Markers are plain comments: they survive
minification, work in HTML/JS/Markdown alike, are visible to a human, and rewriting is idempotent.

A marker key is a **dotted path** into the SSOT (``cc_version``, ``products.cyber_controller``,
``latest_release.tag``) and must resolve to a **scalar** — you cannot splat a list/dict into a page
(that raises, loudly).

Usage:
    python scripts/sync_site.py path/to/site           # rewrite every managed token in place
    python scripts/sync_site.py index.html             # a single file
    python scripts/sync_site.py site --check           # exit 1 if any token is stale (CI guard)
    python scripts/sync_site.py site --ssot other.json # use a specific SSOT file
    python scripts/sync_site.py site --ext .html,.md    # restrict which files a dir scan touches

``--check`` is the per-site drift guard (wire it into each site repo's CI). The *auto-update*
workflow (fetch the SSOT on a CC release, run this without --check, commit if changed) needs a
cross-repo write token and is documented in command-center (owner-gated); this engine is that
workflow's reusable, unit-tested core.
"""
from __future__ import annotations

import json
import os
import re
import sys

# <!--ssot:KEY-->inner<!--/ssot:KEY-->  — the \1 backreference forces the close key to match the
# open, so a typo'd/mismatched pair simply doesn't match (never rewritten with a wrong value).
# DOTALL so a region may span lines.
_MARKER = re.compile(r"<!--ssot:([\w.]+)-->(.*?)<!--/ssot:\1-->", re.DOTALL)

# Default file types scanned when a directory is given.
_DEFAULT_EXTS = (".html", ".htm", ".md", ".js", ".css", ".txt", ".xml", ".json", ".svg")


class SsotError(Exception):
    """A token references a missing key, or a key that isn't a scalar — fail loud, never guess."""


def resolve_key(data: dict, dotted: str):
    """Walk a dotted path into *data* and return the value. Raises :class:`SsotError` if any segment
    is missing. Scalar-ness is enforced at render time by :func:`format_value`, not here."""
    cur = data
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            raise SsotError(f"SSOT key not found: {dotted!r} (failed at segment {seg!r})")
        cur = cur[seg]
    return cur


def format_value(dotted: str, value) -> str:
    """Render an SSOT value for inline insertion. Scalars only — a list/dict token is a page bug and
    raises. ``bool`` renders lowercase to match JSON/JS/HTML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise SsotError(
        f"SSOT key {dotted!r} is a {type(value).__name__}, not a scalar — a token must map to a "
        "single value (use a leaf key like 'latest_release.tag', not a list/object)."
    )


def rewrite_text(text: str, data: dict) -> "tuple[str, list[dict]]":
    """Rewrite every managed token in *text* with its current SSOT value. Returns ``(new_text,
    changes)`` where *changes* lists ``{key, old, new}`` for tokens whose inner content actually
    changed. Pure — no I/O. Unknown/non-scalar keys raise :class:`SsotError`."""
    changes: list[dict] = []

    def _sub(m: "re.Match[str]") -> str:
        key, old_inner = m.group(1), m.group(2)
        new_inner = format_value(key, resolve_key(data, key))
        if new_inner != old_inner:
            changes.append({"key": key, "old": old_inner, "new": new_inner})
        return f"<!--ssot:{key}-->{new_inner}<!--/ssot:{key}-->"

    return _MARKER.sub(_sub, text), changes


def check_text(text: str, data: dict) -> "list[dict]":
    """Return the stale tokens in *text* (``{key, current, expected}``) without modifying it. Empty
    list == in sync. Same resolution/scalar rules as :func:`rewrite_text`."""
    stale: list[dict] = []
    for m in _MARKER.finditer(text):
        key, cur = m.group(1), m.group(2)
        expected = format_value(key, resolve_key(data, key))
        if cur != expected:
            stale.append({"key": key, "current": cur, "expected": expected})
    return stale


def iter_targets(path: str, exts: "tuple[str, ...]") -> "list[str]":
    """A file → [that file]; a directory → files under it with a matching extension (sorted)."""
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        out: list[str] = []
        for root, _dirs, files in os.walk(path):
            if ".git" in root.split(os.sep):
                continue
            for f in files:
                if os.path.splitext(f)[1].lower() in exts:
                    out.append(os.path.join(root, f))
        return sorted(out)
    raise SsotError(f"path not found: {path!r}")


def _load_ssot(ssot_path: str) -> dict:
    if not os.path.isfile(ssot_path):
        raise SsotError(f"SSOT file not found: {ssot_path!r} (generate it with gen_site_data.py)")
    with open(ssot_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _arg_value(argv: "list[str]", flag: str, default: str) -> str:
    if flag in argv and argv.index(flag) + 1 < len(argv):
        return argv[argv.index(flag) + 1]
    return default


def main(argv: "list[str]") -> int:
    positional = [a for a in argv if not a.startswith("--")]
    # drop flag-values (the token after --ssot/--ext) from the positional list
    for flag in ("--ssot", "--ext"):
        if flag in argv:
            val = _arg_value(argv, flag, "")
            if val in positional:
                positional.remove(val)
    if not positional:
        print("usage: sync_site.py PATH [--check] [--ssot site-data.json] [--ext .html,.md]",
              file=sys.stderr)
        return 2

    target = positional[0]
    check = "--check" in argv
    ssot_path = _arg_value(argv, "--ssot", os.path.join(os.getcwd(), "site-data.json"))
    exts = tuple(e if e.startswith(".") else "." + e
                 for e in _arg_value(argv, "--ext", ",".join(_DEFAULT_EXTS)).split(","))

    try:
        data = _load_ssot(ssot_path)
        files = iter_targets(target, exts)
    except SsotError as exc:
        print(f"[sync_site] {exc}", file=sys.stderr)
        return 2

    total_changed = 0
    total_stale = 0
    for fp in files:
        with open(fp, "r", encoding="utf-8") as fh:
            text = fh.read()
        if not _MARKER.search(text):
            continue
        try:
            if check:
                stale = check_text(text, data)
                for s in stale:
                    print(f"DRIFT {fp}: {s['key']} is {s['current']!r}, "
                          f"SSOT says {s['expected']!r}", file=sys.stderr)
                total_stale += len(stale)
            else:
                new_text, changes = rewrite_text(text, data)
                if changes:
                    with open(fp, "w", encoding="utf-8", newline="") as fh:
                        fh.write(new_text)
                    for c in changes:
                        print(f"synced {fp}: {c['key']} {c['old']!r} -> {c['new']!r}")
                    total_changed += len(changes)
        except SsotError as exc:
            print(f"[sync_site] {fp}: {exc}", file=sys.stderr)
            return 2

    if check:
        if total_stale:
            print(f"[sync_site] DRIFT: {total_stale} stale token(s). Run sync_site.py to fix.",
                  file=sys.stderr)
            return 1
        print("OK: all managed tokens match the SSOT.")
        return 0
    print(f"[sync_site] synced {total_changed} token(s) across {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
