#!/usr/bin/env python3
"""Emit a frozen-app-resource manifest bound to an immutable Git commit.

Companion to ``scripts/verify_frozen_app_resources.py``. Run against the exact source checkout the
CI job built; the manifest's expected SHA256 values are the immutable commit's bytes, and the
checker then confirms the frozen onefile CArchive carries them.

Provenance model (FRC-1..3): the EXPECTED resource set is the pinned commit's inventory, not
whatever files happen to be on disk. The generator:
  * requires a full 40-hex commit that resolves to itself (no branch/HEAD/short ref);
  * resolves the commit's tree and, if --tree is given, requires it to match (an unrelated tree is
    rejected);
  * lists the commit's tracked paths (``git ls-tree``), filters them to build.py's --add-data
    application-resource directories/globs/files, and for EACH such expected path requires the
    working file to exist and its bytes to equal ``git show <commit>:<path>`` -- a deleted or
    modified resource is a failure, not a silent omission. Untracked extra files under those dirs
    are reported. Hashes come from the immutable commit bytes.

Coverage (FRC-4/FR-5): the declared scope is complete over build.py's --add-data directories;
``.pack`` payloads are opaque (inventoried/hashed only, never decrypted). The conditional Dead Man's
Switch submodule data (``deadmans-switch/host`` + ``firmware/partitions``) is, when the submodule is
checked out, taken from the submodule PIN's inventory: each expected file must exist and equal
``git show <pin>:<subrel>`` (per-file blobs, not just a HEAD match), and its row is appended to the
MAIN ``resources`` list -- so the checker verifies it against the archive (a report list is not
coverage). Extra working files under the selected conditional directories are rejected by the same
rule as the main scope (any file not in the pin's expected set), and the comparison runs regardless
of whether the commit records a submodule gitlink -- because build.py selects those directories by
``is_dir()`` independent of Git submodule state, so a present unpinned file there (materialized-but-
unpinned, unresolvable checkout, or no gitlink at all) cannot slip through. When not checked out the
gate is not-materialized, never silently ignored, and an absent/empty optional checkout has no
present files, so the optional checkout is never made mandatory. This walk reconciles to QA's
inventory v2 (``80964ae6``, 147 --add-data data resources; the two qt/theme .py are
--collect-submodules code, not data members).

Git failures capture stderr and surface as finite diagnostics; process-control exceptions are left
alone (no BaseException catch).

Usage:  gen_frozen_resource_manifest.py --source-root <git-checkout> --commit <40hex>
            [--tree <40hex>] --out <path>
Exit: 0 = manifest written; 2 = bad commit/tree, a missing/modified/extra resource, or a Git error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

DECLARED_DIRS = [
    "src/ui/web/templates", "src/ui/web/static", "src/config/maps", "src/config/profiles",
    "src/config/probes", "src/config/dms_partitions", "src/config/wordlists", "src/config/tools",
    "src/core/default_macros", "assets",
]
DECLARED_GLOBS = [("src/ui/qt/theme", r"\.qss$")]
DECLARED_FILES = [
    "src/config/os_catalog.json", "src/config/oui_table.tsv.gz",
    "src/config/ble_company_ids.tsv.gz", "src/ui/tui/styles.tcss", "docs/HOWTO.md",
]
CONDITIONAL_DIRS = ["deadmans-switch/host", "deadmans-switch/firmware/partitions"]
OPAQUE_SUFFIXES = (".pack",)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class GitError(Exception):
    """A git command failed; carries the captured stderr for a finite diagnostic."""


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', 'replace').strip()
        raise GitError(f"git {' '.join(args[:2])}: {detail}")
    return proc.stdout


def _is_declared(path: str) -> bool:
    if path in DECLARED_FILES:
        return True
    if any(path.startswith(d + "/") for d in DECLARED_DIRS):
        return True
    return any(path.startswith(d + "/") and re.search(pat, path) for d, pat in DECLARED_GLOBS)


def build(source_root: Path, commit: str, tree: str | None) -> dict:
    """Build the manifest bound to the immutable commit, or raise GitError/ValueError."""
    if not _HEX40.match(commit):
        raise ValueError(f"commit must be a full 40-hex SHA (no ref/branch/HEAD/short): {commit!r}")
    resolved = _git(source_root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:  # FRC-3: reject a mutable ref that resolves elsewhere
        raise ValueError(f"commit is a mutable ref, resolves elsewhere: {commit} -> {resolved}")
    resolved_tree = _git(source_root, "rev-parse", "--verify",
                         f"{commit}^{{tree}}").decode().strip()
    if tree is not None and tree != resolved_tree:  # FRC-2: an unrelated tree is rejected
        raise ValueError(f"--tree {tree} != commit tree {resolved_tree}")

    tracked = _git(source_root, "ls-tree", "-r", "--name-only", commit).decode().splitlines()
    expected = sorted(p for p in tracked if _is_declared(p))

    rows, missing, modified = [], [], []
    for rel in expected:  # FRC-1: the commit's inventory is authoritative
        git_bytes = _git(source_root, "show", f"{commit}:{rel}")  # GitError surfaces stderr
        work = source_root / rel
        if not work.is_file():
            missing.append(rel)
            continue
        if work.read_bytes() != git_bytes:
            modified.append(rel)
            continue
        opaque = Path(rel).suffix in OPAQUE_SUFFIXES
        rows.append({"path": rel, "sha256": hashlib.sha256(git_bytes).hexdigest(),
                     "size": len(git_bytes), "required": not opaque, "opaque": opaque,
                     "conditional": False})

    # Untracked extras: working files the commit does not track, using the SAME declared selection
    # rules for actual working paths as for expected paths -- whole DECLARED_DIRS AND the
    # DECLARED_GLOBS (an untracked direct theme/*.qss data member would otherwise evade this).
    expected_set = set(expected)
    extras = []
    for d in DECLARED_DIRS:
        base = source_root / d
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(source_root).as_posix()
                    if rel not in expected_set:
                        extras.append(rel)
    for d, pat in DECLARED_GLOBS:
        base = source_root / d
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(source_root).as_posix()
                    if re.search(pat, rel) and rel not in expected_set:
                        extras.append(rel)

    # FRC-4 + seam: conditional Dead Man's Switch submodule data. When checked out, derive the
    # EXPECTED set from the submodule PIN's inventory, require each working file to equal
    # ``git show <pin>:<subrel>`` (a HEAD match alone does not bind bytes), and append the rows
    # to the MAIN ``resources`` list -- so the checker actually verifies them against the archive (a
    # report list alone is not archive coverage). Collected rows are required, since the same
    # checkout that generated them is what build.py bundled.
    conditional = {"status": "not-materialized", "pinned_submodule": None, "file_count": 0}
    expected_dms_full = set()
    gitlink = None
    for line in _git(source_root, "ls-tree", commit, "deadmans-switch").decode().splitlines():
        if line.split("\t", 1)[-1] == "deadmans-switch" and " commit " in line:
            gitlink = line.split()[2]
    if gitlink:
        conditional["pinned_submodule"] = gitlink
        sub = source_root / "deadmans-switch"
        if sub.is_dir() and (sub / ".git").exists():
            sub_tracked = _git(sub, "ls-tree", "-r", "--name-only", gitlink).decode().splitlines()
            expected_dms = sorted(p for p in sub_tracked
                                  if p.startswith("host/") or p.startswith("firmware/partitions/"))
            for subrel in expected_dms:
                pinned = _git(sub, "show", f"{gitlink}:{subrel}")  # per-file pinned blob
                rel = "deadmans-switch/" + subrel
                expected_dms_full.add(rel)
                work = source_root / rel
                if not work.is_file():
                    missing.append(rel)
                    continue
                if work.read_bytes() != pinned:
                    modified.append(rel)
                    continue
                rows.append({"path": rel, "sha256": hashlib.sha256(pinned).hexdigest(),
                             "size": len(pinned), "required": True, "opaque": False,
                             "conditional": True})
                conditional["file_count"] += 1
            conditional["status"] = "collected"
    # Reject conditional EXTRAS by the SAME rule as the main scope, and -- crucially -- OUTSIDE the
    # gitlink branch: build.py selects deadmans-switch/host + firmware/partitions by ``is_dir()``
    # independent of whether Git records a submodule, so present files there are bundled regardless.
    # Any working file under a selected conditional directory not in the pin's qualified set
    # (``expected_dms_full``, populated only for a materialized checkout) is an extra. This covers a
    # materialized-but-unpinned file (e.g. host/extra-note.txt), a gitlink-present-but-unresolvable
    # checkout (.git gone -> pin set unavailable -> every present file unverifiable), AND a commit
    # with no gitlink at all but present unpinned conditional files (no qualified source set -> all
    # extra). An absent/empty selected directory has no present files, so optional data stays
    # not-materialized with no extras -- the optional checkout is never made mandatory.
    for d in CONDITIONAL_DIRS:
        base = source_root / d
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(source_root).as_posix()
                    if rel not in expected_dms_full:
                        extras.append(rel)

    if missing or modified:
        raise ValueError(json.dumps({"missing": missing, "modified": modified}))

    return {
        "source_identity": {"commit": commit, "tree": resolved_tree, "verified": True,
                            "verified_against": "git"},
        "coverage": ("expected set = pinned commit inventory (git ls-tree) filtered to build.py "
                     "--add-data resources; each verified present + byte-identical to git show "
                     "<commit>:<path>; reconciled to QA inventory v2 80964ae6 (147)"),
        "exclusions": [
            "encrypted tool .pack payloads are opaque (inventoried/hashed, not decrypted)",
            "generated runtime dependencies and platform inputs are out of scope",
            "src/ui/qt/theme non-.qss files are --collect-submodules CODE, not --add-data data",
        ],
        "resources": rows,
        "conditional_deadmans_switch": conditional,
        "untracked_extras": sorted(extras),
    }


def _fail(payload: dict) -> int:
    print(json.dumps(payload), file=sys.stderr)
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Emit a commit-bound frozen-app-resource manifest.")
    ap.add_argument("--source-root", type=Path, required=True, help="a git checkout of the source")
    ap.add_argument("--commit", required=True, help="full 40-hex commit SHA (no ref)")
    ap.add_argument("--tree")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        manifest = build(args.source_root, args.commit, args.tree)
    except GitError as exc:
        return _fail({"error": "git failure", "detail": str(exc)})
    except ValueError as exc:
        return _fail({"error": "source-binding failure", "detail": str(exc)})
    if manifest["untracked_extras"]:  # untracked extras under declared/conditional dirs: a finding
        return _fail({"error": "untracked extra files under declared or conditional resource dirs",
                      "extras": manifest["untracked_extras"]})
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "resources": len(manifest["resources"]),
                      "conditional": manifest["conditional_deadmans_switch"]["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
