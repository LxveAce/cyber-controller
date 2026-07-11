"""Unit tests for the bundled crack-tool pack layer (src/core/tool_bundle.py).

The EXTRACTION mechanism is tested against a SYNTHETIC pack (a harmless dummy file), never the real
aircrack binaries — extracting a real PUA binary would trip Windows Defender and delete it mid-test.
Listing the shipped packs + reading their manifests IS exercised (no extraction, so it's Defender-safe).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.core import tool_bundle as tb

pyzipper = pytest.importorskip("pyzipper")


def test_shipped_aircrack_pack_is_listed_with_manifest():
    packs = {p.tool: p for p in tb.list_packs()}
    ac = packs.get("aircrack-ng")
    assert ac is not None, "the bundled aircrack-ng pack should be listed"
    assert ac.platform == "windows"
    assert ac.primary_exe == "aircrack-ng.exe"
    assert ac.manifest.get("archive_sha1") == "872ef4f731080626d7cee893ef42c8f630ce90cd"
    assert ac.manifest.get("file_count", 0) >= 30  # the full suite + LICENSE/AUTHORS
    # every manifest file entry carries a real sha256 (integrity is enforceable on extract)
    assert all(len(f.get("sha256", "")) == 64 for f in ac.manifest["files"])


def test_pack_for_tool_matches_platform():
    assert tb.pack_for_tool("aircrack-ng", "windows") is not None
    assert tb.pack_for_tool("aircrack-ng", "linux") is None
    assert tb.pack_for_tool("nonexistent-tool", "windows") is None


def _make_synthetic_pack(tmp_path):
    """Build a tiny AES pack + manifest (harmless payload) so extraction can be tested without the
    real PUA binaries."""
    payload = b"#!/bin/sh\necho harmless-stub\n"
    directory = tmp_path / "packs"
    directory.mkdir()
    pack_path = directory / "dummy-tool.pack"
    with pyzipper.AESZipFile(str(pack_path), "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as z:
        z.setpassword(tb.PACK_PASSWORD)
        z.writestr("dummy.bin", payload)
    manifest = {
        "name": "dummy-tool", "tool": "dummy", "version": "1.0", "platform": "windows",
        "primary_exe": "dummy.bin", "archive_sha1": "0" * 40,
        "files": [{"name": "dummy.bin", "size": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}],
        "file_count": 1,
    }
    (directory / "dummy-tool.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory, payload


def test_extract_pack_roundtrips_and_verifies(tmp_path, monkeypatch):
    directory, payload = _make_synthetic_pack(tmp_path)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(directory))
    pack = tb.pack_for_tool("dummy", "windows")
    assert pack is not None
    dest = tmp_path / "out"
    exe = tb.extract_pack(pack, str(dest))
    assert exe.endswith("dummy.bin")
    assert (dest / "dummy.bin").read_bytes() == payload


def test_extract_pack_rejects_tampered_manifest(tmp_path, monkeypatch):
    directory, _ = _make_synthetic_pack(tmp_path)
    # Corrupt the expected hash -> extraction must fail closed rather than install unverified bytes.
    mpath = directory / "dummy-tool.manifest.json"
    m = json.loads(mpath.read_text())
    m["files"][0]["sha256"] = "f" * 64
    mpath.write_text(json.dumps(m), encoding="utf-8")
    monkeypatch.setattr(tb, "packs_dir", lambda: str(directory))
    pack = tb.pack_for_tool("dummy", "windows")
    with pytest.raises(RuntimeError):
        tb.extract_pack(pack, str(tmp_path / "out2"))
