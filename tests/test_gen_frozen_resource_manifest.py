"""Inert tests for gen_frozen_resource_manifest.py.

Loads the helper from the real ``scripts/`` sibling or a colocated copy, without ambient PYTHONPATH
(FR-1). Each test builds a throwaway LOCAL git repo (no network, no build). Proves the commit-bound
provenance model: valid; deleted resource (FRC-1); modified resource; unrelated --tree (FRC-2);
mutable/short commit ref (FRC-3); untracked extra; and the conditional Dead Man's Switch submodule
path (FRC-4).
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


def _load(name):
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "scripts" / f"{name}.py", here / f"{name}.py"):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location(name, cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(f"{name}.py not found under scripts/ or beside the test")


G = _load("gen_frozen_resource_manifest")

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull}

SEED = {
    "src/ui/web/templates/reform.html": b"<html>reform</html>\n",
    "src/ui/web/static/reform.js": b"console.log(1)\n",
    "src/ui/web/static/map_outline.js": b"// outline\n",
    "src/config/maps/world_110m.geojson": b'{"type":"FeatureCollection"}\n',
    "src/config/profiles/lxveos.json": b'{"name":"lxveos"}\n',
    "src/core/default_macros/cc_marauder_ap_scan.json": b"{}\n",
    "src/config/tools/aircrack.pack": b"\x00OPAQUE\x01",
    "src/ui/qt/theme/cyber_dark.qss": b".x{}\n",
    "src/ui/qt/theme/__init__.py": b"# code, not data\n",
    "src/config/os_catalog.json": b"{}\n",
    "src/config/oui_table.tsv.gz": b"\x1f\x8b\x00OUI\n",
    "src/config/ble_company_ids.tsv.gz": b"\x1f\x8b\x00BLE\n",
    "src/ui/tui/styles.tcss": b".x{}\n",
    "docs/HOWTO.md": b"# how\n",
}


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=_ENV,
                          check=check)


def _seed(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)


def _repo(tmp_path, files=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _seed(repo, SEED if files is None else files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return repo, commit


def _run(repo, commit, tmp_path, tree=None):
    out = tmp_path / "m.json"
    argv = ["--source-root", str(repo), "--commit", commit, "--out", str(out)]
    if tree is not None:
        argv += ["--tree", tree]
    rc = G.main(argv)
    return rc, (json.loads(out.read_text()) if out.exists() else None)


def test_valid_commit_bound_manifest(tmp_path):
    repo, commit = _repo(tmp_path)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()
    rc, man = _run(repo, commit, tmp_path, tree=tree)
    assert rc == 0, man
    assert man["source_identity"] == {"commit": commit, "tree": tree, "verified": True,
                                      "verified_against": "git"}
    by = {r["path"]: r for r in man["resources"]}
    assert "src/ui/web/static/map_outline.js" in by  # FR-5 example present
    assert "src/ui/qt/theme/__init__.py" not in by   # code, not an --add-data data member
    assert by["src/config/tools/aircrack.pack"]["opaque"] is True
    assert man["conditional_deadmans_switch"]["status"] == "not-materialized"
    assert man["untracked_extras"] == []


def test_deleted_declared_resource_fails(tmp_path):
    # FRC-1: the commit tracks map_outline.js; deleting it from the tree must FAIL, not omit.
    repo, commit = _repo(tmp_path)
    (repo / "src/ui/web/static/map_outline.js").unlink()
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_modified_declared_resource_fails(tmp_path):
    repo, commit = _repo(tmp_path)
    (repo / "docs/HOWTO.md").write_bytes(b"# how CHANGED\n")
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_unrelated_tree_rejected(tmp_path):
    # FRC-2: a tree unrelated to the commit must be rejected.
    repo, commit = _repo(tmp_path)
    rc, _ = _run(repo, commit, tmp_path, tree="0" * 40)
    assert rc == 2


def test_mutable_or_short_commit_rejected(tmp_path):
    # FRC-3: HEAD / a branch name / a short SHA are not immutable commit identifiers.
    repo, commit = _repo(tmp_path)
    for ref in ("HEAD", "master", commit[:12]):
        out = tmp_path / f"m_{ref[:6]}.json"
        rc = G.main(["--source-root", str(repo), "--commit", ref, "--out", str(out)])
        assert rc == 2, ref
        assert not out.exists()


def test_untracked_extra_under_declared_dir_fails(tmp_path):
    repo, commit = _repo(tmp_path)
    (repo / "src/ui/web/static/sneaky.js").write_bytes(b"// not committed\n")
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_untracked_theme_qss_is_extra(tmp_path):
    # V3/QSS: a direct glob group -- an untracked theme/*.qss data member must not evade the extra
    # comparison (the extras check must apply DECLARED_GLOBS, not only whole DECLARED_DIRS).
    repo, commit = _repo(tmp_path)
    (repo / "src/ui/qt/theme/extra_theme.qss").write_bytes(b".y{}\n")  # *.qss, uncommitted
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_git_error_is_finite_diagnostic(tmp_path):
    # A non-repo source-root makes git fail; it must be a finite diagnostic, not a traceback.
    plain = tmp_path / "plain"
    plain.mkdir()
    out = tmp_path / "m.json"
    rc = G.main(["--source-root", str(plain), "--commit", "a" * 40, "--out", str(out)])
    assert rc == 2


def _repo_with_dms(tmp_path):
    sub = tmp_path / "dms"
    sub.mkdir()
    _git(sub, "init", "-q")
    _seed(sub, {"host/provision.py": b"# host\n", "firmware/partitions/guardian.csv": b"a,b\n"})
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "dms")
    repo, _ = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "deadmans-switch")
    _git(repo, "commit", "-q", "-m", "add dms")
    commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return repo, commit


def test_conditional_dms_rows_land_in_resources(tmp_path):
    # FRC-4/seam: collected DMS files must be in the MAIN resources list (checker-consumed), bound
    # per-file to the pin, and required (they were bundled from the present checkout).
    repo, commit = _repo_with_dms(tmp_path)
    rc, man = _run(repo, commit, tmp_path)
    assert rc == 0, man
    assert man["conditional_deadmans_switch"]["status"] == "collected"
    assert man["conditional_deadmans_switch"]["file_count"] == 2
    by = {r["path"]: r for r in man["resources"]}   # the checker iterates resources
    for rel in ("deadmans-switch/host/provision.py",
                "deadmans-switch/firmware/partitions/guardian.csv"):
        assert rel in by and by[rel]["conditional"] is True and by[rel]["required"] is True


def test_modified_dms_file_fails(tmp_path):
    # A DMS working file that does not match the pinned blob must fail (HEAD match is not enough).
    repo, commit = _repo_with_dms(tmp_path)
    (repo / "deadmans-switch/host/provision.py").write_bytes(b"# host TAMPERED\n")
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_checker_consumes_the_conditional_rows(tmp_path):
    # End-to-end: the generated manifest's DMS rows are actually verified by the checker against an
    # archive -- present+matching -> clean; a missing DMS member -> the checker reports it missing.
    from PyInstaller.archive.writers import CArchiveWriter
    V = _load("verify_frozen_app_resources")
    repo, commit = _repo_with_dms(tmp_path)
    rc, man = _run(repo, commit, tmp_path)
    assert rc == 0

    def _archive(rows, drop=()):
        entries = []
        for i, r in enumerate(rows):
            if r["path"] in drop:
                continue
            src = tmp_path / f"a_{i}.bin"
            src.write_bytes((repo / r["path"]).read_bytes())
            entries.append((r["path"], str(src), False, "x"))
        arc = tmp_path / "app.pkg"
        CArchiveWriter(str(arc), entries, "libpython3.12.so.1.0")
        return arc

    mpath = tmp_path / "man.json"
    mpath.write_text(json.dumps(man), encoding="utf-8")
    out = tmp_path / "rep.json"
    ok = _archive(man["resources"])
    assert V.main(["--archive", str(ok), "--manifest", str(mpath), "--out", str(out)]) == 0
    # drop a DMS member -> the checker (which consumed the row) reports it missing
    bad = _archive(man["resources"], drop=("deadmans-switch/host/provision.py",))
    r2 = tmp_path / "r2.json"
    rc2 = V.main(["--archive", str(bad), "--manifest", str(mpath), "--out", str(r2)])
    assert rc2 == 1
    rep = json.loads(r2.read_text())
    assert any("deadmans-switch/host/provision.py" in e for e in rep["errors"])


def test_materialized_conditional_extra_is_rejected(tmp_path):
    # FINAL-1 (root witness): a materialized checkout carrying an extra ordinary file that is NOT in
    # the pin (deadmans-switch/host/extra-note.txt) must be rejected as an extra, not silently
    # collected with source_identity.verified: true and untracked_extras: [].
    repo, commit = _repo_with_dms(tmp_path)
    (repo / "deadmans-switch/host/extra-note.txt").write_bytes(b"ordinary note, not pinned\n")
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_materialized_conditional_clean_has_no_extras(tmp_path):
    # Control: a clean materialized checkout (only pinned files present) yields zero conditional
    # extras and status collected.
    repo, commit = _repo_with_dms(tmp_path)
    rc, man = _run(repo, commit, tmp_path)
    assert rc == 0, man
    assert man["untracked_extras"] == []
    assert man["conditional_deadmans_switch"]["status"] == "collected"


def test_conditional_extra_csv_under_partitions_is_rejected(tmp_path):
    # The same rule applies to the firmware/partitions selected dir, with an ordinary CSV fixture.
    repo, commit = _repo_with_dms(tmp_path)
    (repo / "deadmans-switch/firmware/partitions/rogue.csv").write_bytes(b"x,y\n1,2\n")
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_present_but_unresolvable_conditional_files_are_extras(tmp_path):
    # HO-1: gitlink present but the checkout is not resolvable (.git removed) while host/firmware
    # files remain on disk -> the pin set is unavailable, so every present file is unverifiable and
    # is flagged as an extra (exit 2); status stays not-materialized.
    import shutil
    repo, commit = _repo_with_dms(tmp_path)
    dotgit = repo / "deadmans-switch" / ".git"
    shutil.rmtree(dotgit) if dotgit.is_dir() else dotgit.unlink()
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_absent_optional_checkout_stays_not_materialized(tmp_path):
    # The optional checkout is never made mandatory: gitlink present in the commit but the working
    # deadmans-switch dir absent -> not-materialized, zero conditional rows, no extras, exit 0.
    import shutil
    repo, commit = _repo_with_dms(tmp_path)
    shutil.rmtree(repo / "deadmans-switch")
    rc, man = _run(repo, commit, tmp_path)
    assert rc == 0, man
    assert man["conditional_deadmans_switch"]["status"] == "not-materialized"
    assert man["untracked_extras"] == []
    assert not any(r.get("conditional") for r in man["resources"])


def test_no_gitlink_present_conditional_data_is_rejected(tmp_path):
    # 2100 (root witness): a commit with NO submodule gitlink but present ordinary files under the
    # selected conditional directories must be refused. build.py selects deadmans-switch/host +
    # firmware/partitions by is_dir() regardless of gitlink, so unpinned present files there would
    # be bundled without provenance. The comparison must run outside the gitlink-presence branch.
    files = dict(SEED)
    files["deadmans-switch/host/note.txt"] = b"ordinary note, no submodule\n"
    files["deadmans-switch/firmware/partitions/table.csv"] = b"a,b\n1,2\n"
    repo, commit = _repo(tmp_path, files)
    rc, _ = _run(repo, commit, tmp_path)
    assert rc == 2


def test_no_gitlink_absent_conditional_data_is_clean(tmp_path):
    # Control (absent-data positive): no gitlink and no files under the selected conditional dirs ->
    # not-materialized, zero conditional rows, no extras, exit 0 (the ordinary commit case). The
    # gitlink-independent walk must not fabricate extras when those directories are absent.
    repo, commit = _repo(tmp_path)  # SEED has no deadmans-switch files
    rc, man = _run(repo, commit, tmp_path)
    assert rc == 0, man
    assert man["conditional_deadmans_switch"]["status"] == "not-materialized"
    assert man["untracked_extras"] == []
    assert not any(r.get("conditional") for r in man["resources"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
