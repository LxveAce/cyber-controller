"""Inert tests for verify_frozen_app_resources.py using small OWNED CArchive fixtures.

Loads the helper from the real ``scripts/`` sibling (public layout) or a colocated copy, without any
ambient PYTHONPATH or private same-folder import assumption (FR-1). No real build, no application
import, no member execution. Proves valid; missing; wrong-byte; ambiguous (case AND the exact
raw-duplicate the reader dict hides, FR-2); a damaged compressed member (FR-3a); an invalid
manifest path (FR-3b); and the required-without-sha input error.
"""
import hashlib
import importlib.util
import json
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


V = _load("verify_frozen_app_resources")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_archive(tmp_path, members, name="app.pkg", compress=False):
    """members: dict {dest_name: bytes}. Returns the archive path."""
    from PyInstaller.archive.writers import CArchiveWriter

    entries = []
    for i, (dest, data) in enumerate(members.items()):
        src = tmp_path / f"src_{i}.bin"
        src.write_bytes(data)
        entries.append((dest, str(src), compress, "x"))  # 'x' = DATA typecode
    arc = tmp_path / name
    CArchiveWriter(str(arc), entries, "libpython3.12.so.1.0")
    return arc


def _make_exact_duplicate_archive(tmp_path):
    """Two list entries with the SAME stored name -> two raw TOC records, one reader-dict entry."""
    from PyInstaller.archive.writers import CArchiveWriter

    a = tmp_path / "a.bin"
    a.write_bytes(b"AAAA")
    b = tmp_path / "b.bin"
    b.write_bytes(b"BBBB")
    arc = tmp_path / "dup.pkg"
    CArchiveWriter(str(arc), [("docs/note.txt", str(a), False, "x"),
                              ("docs/note.txt", str(b), False, "x")], "libpython3.12.so.1.0")
    return arc


REFORM = b"<html><body>reform</body></html>\n"
WORLD = b'{"type":"FeatureCollection","features":[]}\n'
JS = b"console.log('reform');\n"
PACK = b"\x00\x01OPAQUE-ENCRYPTED\x02\x03"


def _manifest(**over):
    resources = [
        {"path": "src/ui/web/templates/reform.html", "sha256": _sha(REFORM),
         "required": True, "opaque": False},
        {"path": "src/config/maps/world_110m.geojson", "sha256": _sha(WORLD),
         "required": True, "opaque": False},
        {"path": "src/ui/web/static/reform.js", "sha256": _sha(JS),
         "required": True, "opaque": False},
        {"path": "src/config/tools/aircrack.pack", "required": False, "opaque": True},
    ]
    m = {"source_identity": {"commit": "c" * 40}, "resources": resources}
    m.update(over)
    return m


def _run(tmp_path, archive, manifest):
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "report.json"
    rc = V.main(["--archive", str(archive), "--manifest", str(mpath), "--out", str(out)])
    report = json.loads(out.read_text()) if out.exists() else None
    return rc, report


def _complete(tmp_path):
    return _make_archive(tmp_path, {
        "src/ui/web/templates/reform.html": REFORM,
        "src/config/maps/world_110m.geojson": WORLD,
        "src/ui/web/static/reform.js": JS,
        "src/config/tools/aircrack.pack": PACK,
    })


def test_valid_bundle_passes(tmp_path):
    rc, report = _run(tmp_path, _complete(tmp_path), _manifest())
    assert rc == 0, report
    assert report["clean"] is True and report["errors"] == []
    by = {c["path"]: c for c in report["checked"]}
    assert by["src/ui/web/templates/reform.html"]["matches"] is True
    pack = by["src/config/tools/aircrack.pack"]
    assert pack["present"] and pack["opaque"] and pack["matches"] is None
    assert "not verified Git provenance" in report["source_identity_note"]


def test_missing_required_resource_fails(tmp_path):
    archive = _make_archive(tmp_path, {
        "src/ui/web/templates/reform.html": REFORM,
        "src/ui/web/static/reform.js": JS,
        "src/config/tools/aircrack.pack": PACK,
    })  # world map absent
    rc, report = _run(tmp_path, archive, _manifest())
    assert rc == 1
    assert any("missing required resource: src/config/maps/world_110m.geojson" in e
               for e in report["errors"])


def test_wrong_bytes_fails(tmp_path):
    archive = _make_archive(tmp_path, {
        "src/ui/web/templates/reform.html": REFORM + b"TAMPERED",
        "src/config/maps/world_110m.geojson": WORLD,
        "src/ui/web/static/reform.js": JS,
        "src/config/tools/aircrack.pack": PACK,
    })
    rc, report = _run(tmp_path, archive, _manifest())
    assert rc == 1
    assert any("wrong bytes for resource: src/ui/web/templates/reform.html" in e
               for e in report["errors"])


def test_case_insensitive_ambiguous_name_fails(tmp_path):
    archive = _make_archive(tmp_path, {
        "src/ui/web/static/reform.js": JS,
        "src/ui/web/static/Reform.js": JS,
    })
    rc, report = _run(tmp_path, archive, _manifest())
    assert rc == 1
    assert any("ambiguous archive" in e for e in report["errors"])


def test_exact_duplicate_raw_name_fails(tmp_path):
    # FR-2: two identical stored names collapse in reader.toc but must still be rejected.
    rc, report = _run(tmp_path, _make_exact_duplicate_archive(tmp_path),
                      _manifest(resources=[{"path": "docs/note.txt", "sha256": _sha(b"BBBB"),
                                            "required": True, "opaque": False}]))
    assert rc == 1
    assert any("ambiguous archive" in e for e in report["errors"])


def test_damaged_compressed_member_is_a_finding(tmp_path):
    # FR-3a: a corrupted compressed member makes extract() raise; it must be a finding, not a crash.
    archive = _make_archive(tmp_path, {"src/ui/web/static/reform.js": JS * 40}, compress=True)
    raw = bytearray(archive.read_bytes())
    raw[8] ^= 0xFF  # corrupt an early byte of the first member's compressed data
    archive.write_bytes(bytes(raw))
    m = _manifest(resources=[{"path": "src/ui/web/static/reform.js", "sha256": _sha(JS * 40),
                              "required": True, "opaque": False}])
    rc, report = _run(tmp_path, archive, m)
    assert rc == 1
    assert report is not None  # the promised report was written, not a traceback
    assert any("read/decompress error" in e or "wrong bytes" in e for e in report["errors"])


def test_invalid_manifest_path_is_input_error(tmp_path):
    # FR-3b: a doubled-separator path raises inside canonicalization; must be a finite input error.
    m = _manifest(resources=[{"path": "docs//note.txt", "sha256": _sha(b"x"),
                              "required": True, "opaque": False}])
    rc, report = _run(tmp_path, _complete(tmp_path), m)
    assert rc == 2
    assert report is not None and "input_error" in report


def test_malformed_archive_fails(tmp_path):
    bad = tmp_path / "not-an-archive.bin"
    bad.write_bytes(b"this is not a CArchive" * 100)
    rc, report = _run(tmp_path, bad, _manifest())
    assert rc == 1
    assert any("unreadable or malformed archive" in e for e in report["errors"])


def test_required_non_opaque_without_expected_sha_is_input_error(tmp_path):
    m = _manifest()
    del m["resources"][0]["sha256"]
    rc, _ = _run(tmp_path, _complete(tmp_path), m)
    assert rc == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
