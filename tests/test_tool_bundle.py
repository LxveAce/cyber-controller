"""Unit tests for the bundled crack-tool pack layer (src/core/tool_bundle.py).

The EXTRACTION mechanism is tested against a SYNTHETIC pack (a harmless dummy file), never the real
aircrack binaries — extracting a real PUA binary would trip Windows Defender and delete it mid-test.
Listing the shipped packs + reading their manifests IS exercised (no extraction, so it's Defender-safe).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from email.parser import Parser
from pathlib import Path

import pytest

from src.core import tool_bundle as tb

pyzipper = pytest.importorskip("pyzipper")


def _is_lean_sdist():
    root = Path(__file__).resolve().parent.parent
    # Git source takes precedence over leftover build metadata. A plain source archive has no
    # PKG-INFO either; missing packs in either source layout must remain a failure.
    if (root / ".git").exists():
        return False
    try:
        metadata = Parser().parsestr((root / "PKG-INFO").read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return False
    return metadata.get_all("Name", []) == ["cyber-controller"]


def test_shipped_aircrack_pack_is_listed_with_manifest():
    if _is_lean_sdist():
        archives = [p for p in Path(tb.packs_dir()).rglob("*") if p.suffix.lower() == ".pack"]
        assert not archives, "lean Python distributions must not contain executable tool packs"
        assert not tb.list_packs(), "lean Python distributions must not advertise bundled packs"
        return
    packs = {p.tool: p for p in tb.list_packs()}
    ac = packs.get("aircrack-ng")
    assert ac is not None, "the bundled aircrack-ng pack should be listed"
    assert ac.platform == "windows"
    assert ac.primary_exe == "aircrack-ng.exe"
    assert ac.manifest.get("archive_sha1") == "872ef4f731080626d7cee893ef42c8f630ce90cd"
    assert ac.manifest.get("file_count", 0) >= 30  # the full suite + LICENSE/AUTHORS
    # every manifest file entry carries a real sha256 (integrity is enforceable on extract)
    assert all(len(f.get("sha256", "")) == 64 for f in ac.manifest["files"])


def test_pack_for_tool_matches_platform(tmp_path, monkeypatch):
    directory, _ = _make_synthetic_pack(tmp_path)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(directory))
    assert tb.pack_for_tool("dummy", "windows") is not None
    assert tb.pack_for_tool("dummy", "linux") is None
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


def test_extract_pack_rejects_unlisted_member(tmp_path, monkeypatch):
    # A member present in the .pack but ABSENT from the manifest (e.g. a sideload DLL planted in a
    # tampered pack) must be rejected. The pack password is public, so the manifest SHA-256 is the
    # only integrity control — extraction fails closed on any unlisted file, never writing it into
    # (Defender-excluded) tools dir.
    payload = b"#!/bin/sh\necho harmless-stub\n"
    directory = tmp_path / "packs"
    directory.mkdir()
    with pyzipper.AESZipFile(str(directory / "dummy-tool.pack"), "w",
                             compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(tb.PACK_PASSWORD)
        z.writestr("dummy.bin", payload)
        z.writestr("evil.dll", b"MZ planted-payload")   # NOT listed in the manifest
    manifest = {
        "name": "dummy-tool", "tool": "dummy", "version": "1.0", "platform": "windows",
        "primary_exe": "dummy.bin", "archive_sha1": "0" * 40,
        "files": [{"name": "dummy.bin", "size": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}],
        "file_count": 1,
    }
    (directory / "dummy-tool.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(tb, "packs_dir", lambda: str(directory))
    pack = tb.pack_for_tool("dummy", "windows")
    with pytest.raises(RuntimeError):
        tb.extract_pack(pack, str(tmp_path / "out3"))


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


def _write_ac_pack(directory, good, *, planted=False):
    """A synthetic aircrack-ng pack whose primary exe is the FIRST member, optionally followed by an
    unlisted (tampered) member so the failure happens AFTER the good exe was already written."""
    directory.mkdir(parents=True, exist_ok=True)
    with pyzipper.AESZipFile(str(directory / "ac.pack"), "w",
                             compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(tb.PACK_PASSWORD)
        z.writestr("aircrack-ng.exe", good)         # listed + valid, written first
        if planted:
            z.writestr("evil.dll", b"MZ planted")    # not in the manifest -> mid-extract fail
    manifest = {"name": "ac", "tool": "aircrack-ng", "version": "1.7", "platform": "windows",
                "primary_exe": "aircrack-ng.exe", "archive_sha1": "0" * 40,
                "files": [{"name": "aircrack-ng.exe", "sha256": hashlib.sha256(good).hexdigest()}],
                "file_count": 1}
    (directory / "ac.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_extract_pack_leaves_no_partial_tree_on_failure(tmp_path, monkeypatch):
    # A mid-extract failure must remove the good members it already wrote — else, with the exe
    # extracted before the bad member, installed_tools()/detect_tools() would see it "present"
    # from a failed, half-verified enable.
    good = b"MZ aircrack stub"
    packs = tmp_path / "packs"
    _write_ac_pack(packs, good, planted=True)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    dest = tmp_path / "tools" / "aircrack-ng"
    with pytest.raises(RuntimeError):
        tb.extract_pack(pack, str(dest))
    # the already-written primary exe is gone: no partial tree survives the failure
    assert not (dest / "aircrack-ng.exe").exists()
    assert list(dest.glob("*")) == []
    # ...and discovery does NOT resolve the tool from the cleaned-up dir
    from src.core import tool_installer
    assert "aircrack-ng" not in tool_installer.installed_tools(str(tmp_path / "tools"))


def test_extract_pack_then_installed_tools_finds_it(tmp_path, monkeypatch):
    # The enable -> re-check loop: after a pack extracts into tools/<tool>/, the resolver must
    # discover the primary exe so /api/crack-tools reports it present without a restart.
    good = b"MZ aircrack stub"
    packs = tmp_path / "packs"
    _write_ac_pack(packs, good, planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    tb.extract_pack(pack, str(tools_dir / "aircrack-ng"))
    from src.core import tool_installer
    found = tool_installer.installed_tools(str(tools_dir))
    assert found.get("aircrack-ng", "").endswith("aircrack-ng.exe")


# ── transactional publish: prior install, torn writes, incomplete archive, concurrency, cancel

def _seed_prior_install(tools_dir, body=b"OLD-GOOD-INSTALL"):
    """Pre-create a complete prior install at tools/aircrack-ng/aircrack-ng.exe."""
    dest = tools_dir / "aircrack-ng"
    dest.mkdir(parents=True)
    (dest / "aircrack-ng.exe").write_bytes(body)
    return dest


def test_extract_pack_preserves_prior_install_on_rejected_pack(tmp_path, monkeypatch):
    # T1: a pack with a valid REPLACEMENT exe followed by an unlisted member must NOT destroy the
    # complete install — the rejection happens in staging, the live dir is never touched.
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=True)   # valid exe first, then unlisted evil.dll
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = _seed_prior_install(tools_dir)
    with pytest.raises(RuntimeError):
        tb.extract_pack(pack, str(dest))
    assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-GOOD-INSTALL"   # prior install intact
    from src.core import tool_installer
    assert tool_installer.installed_tools(str(tools_dir)).get("aircrack-ng", "").endswith("aircrack-ng.exe")


def test_extract_pack_torn_write_preserves_prior_and_hides_partial(tmp_path, monkeypatch):
    # T2: a write/close (os.replace of a member's .part) failure mid-extract must leave NO
    # partial and must not touch the prior install.
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = _seed_prior_install(tools_dir)

    real_replace, tripped = os.replace, {"done": False}

    def flaky_replace(a, b):
        if str(a).endswith(".part") and not tripped["done"]:
            tripped["done"] = True
            raise OSError("injected torn write on member commit")
        return real_replace(a, b)

    monkeypatch.setattr(tb.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        tb.extract_pack(pack, str(dest))
    monkeypatch.setattr(tb.os, "replace", real_replace)
    assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-GOOD-INSTALL"   # prior install intact
    # no orphaned staging dir survived, and the tool dir still holds only the prior good exe
    assert not any(p.name.startswith(".cc-stage-") for p in tools_dir.iterdir())
    assert list((tools_dir / "aircrack-ng").glob("*")) == [tools_dir / "aircrack-ng" / "aircrack-ng.exe"]


def test_extract_pack_rejects_incomplete_archive(tmp_path, monkeypatch):
    # An archive missing a manifest-listed member must fail closed (not publish a partial tool).
    good = b"MZ aircrack"
    directory = tmp_path / "packs"
    directory.mkdir()
    with pyzipper.AESZipFile(str(directory / "ac.pack"), "w",
                             compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(tb.PACK_PASSWORD)
        z.writestr("aircrack-ng.exe", good)          # only ONE of the two manifest members is present
    manifest = {"name": "ac", "tool": "aircrack-ng", "version": "1.7", "platform": "windows",
                "primary_exe": "aircrack-ng.exe", "archive_sha1": "0" * 40,
                "files": [{"name": "aircrack-ng.exe", "sha256": hashlib.sha256(good).hexdigest()},
                          {"name": "airmon-ng.exe", "sha256": hashlib.sha256(b"missing").hexdigest()}],
                "file_count": 2}
    (directory / "ac.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(tb, "packs_dir", lambda: str(directory))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    with pytest.raises(RuntimeError, match="incomplete"):
        tb.extract_pack(pack, str(tools_dir / "aircrack-ng"))
    from src.core import tool_installer
    assert "aircrack-ng" not in tool_installer.installed_tools(str(tools_dir))


def test_extract_pack_serializes_concurrent_jobs(tmp_path, monkeypatch):
    # Two concurrent publishes to the same dest must serialize (one writer per destination) and
    # single valid install with no leftover staging.
    good = b"MZ aircrack"
    packs = tmp_path / "packs"
    _write_ac_pack(packs, good, planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = str(tools_dir / "aircrack-ng")
    errors: list[Exception] = []

    def worker():
        try:
            tb.extract_pack(pack, dest)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert (tools_dir / "aircrack-ng" / "aircrack-ng.exe").read_bytes() == good
    assert not any(p.name.startswith(".cc-stage-") for p in tools_dir.iterdir())


def test_extract_pack_cancellation_before_publish_leaves_prior(tmp_path, monkeypatch):
    # Cooperative cancel before the atomic publish must abort with ToolCancelled and not touch the
    # install.
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = _seed_prior_install(tools_dir)
    with pytest.raises(tb.ToolCancelled):
        tb.extract_pack(pack, str(dest), should_cancel=lambda: True)
    assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-GOOD-INSTALL"
    assert not any(p.name.startswith(".cc-stage-") for p in tools_dir.iterdir())


def test_promotion_and_restore_failure_preserves_prior_install(tmp_path, monkeypatch):
    # T3: if BOTH the publish (pkg->dest) and the restore (backup->dest) fail, the prior install
    # survive at a recoverable location and BOTH errors must be reported — never deleted by cleanup.
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools = tmp_path / "tools"
    dest = tools / "aircrack-ng"
    dest.mkdir(parents=True)
    (dest / "aircrack-ng.exe").write_bytes(b"OLD-PRIMARY")
    (dest / "dep").mkdir()
    (dest / "dep" / "lib.dll").write_bytes(b"OLD-DEP")   # a nested dependency of the prior install

    real_replace = os.replace

    def fail_move_into_dest(a, b):
        # fail every rename whose DESTINATION is the live install dir (both pkg->dest and the
        # backup->dest); the dest->backup move-aside still succeeds.
        if os.path.realpath(b) == os.path.realpath(str(dest)):
            raise PermissionError(13, "Access is denied")
        return real_replace(a, b)

    monkeypatch.setattr(tb.os, "replace", fail_move_into_dest)
    with pytest.raises(RuntimeError, match="could not be restored") as ei:
        tb.extract_pack(pack, str(dest))
    monkeypatch.setattr(tb.os, "replace", real_replace)
    msg = str(ei.value)
    assert "promote:" in msg and "restore:" in msg      # both failures reported

    preserved = list(tools.glob(".cc-backup-*/old"))
    assert preserved, "the prior install must be preserved in a recovery location"
    old = preserved[0]
    assert (old / "aircrack-ng.exe").read_bytes() == b"OLD-PRIMARY"
    assert (old / "dep" / "lib.dll").read_bytes() == b"OLD-DEP"   # nested dependency bytes survived too
    assert not any(p.name.startswith(".cc-stage-") for p in tools.iterdir())   # staging cleaned


@pytest.mark.skipif(sys.platform != "win32", reason="destination case-folding is Windows-only")
def test_windows_case_alias_shares_one_destination_lock(tmp_path):
    # T4: on Windows tools/aircrack-ng and tools/AIRCRACK-NG are the SAME dir, so they must resolve
    # one canonical key + one in-process lock (real serialization), even before the dir exists.
    a = str(tmp_path / "tools" / "aircrack-ng")
    b = str(tmp_path / "tools" / "AIRCRACK-NG")
    assert tb.canonical_dest(a) == tb.canonical_dest(b)
    assert tb._lock_for_dest(a) is tb._lock_for_dest(b)


def test_posix_case_distinct_destinations_stay_distinct(tmp_path):
    # The mirror invariant: on POSIX (case-sensitive), those two names are DIFFERENT destinations.
    if sys.platform == "win32":
        import pytest as _p
        _p.skip("POSIX case-sensitivity check")
    a = str(tmp_path / "tools" / "aircrack-ng")
    b = str(tmp_path / "tools" / "AIRCRACK-NG")
    assert tb.canonical_dest(a) != tb.canonical_dest(b)


def _seed_prior_with_dep(tools):
    dest = tools / "aircrack-ng"
    dest.mkdir(parents=True)
    (dest / "aircrack-ng.exe").write_bytes(b"OLD-PRIMARY")
    (dest / "dep").mkdir()
    (dest / "dep" / "lib.dll").write_bytes(b"OLD-DEP")
    return dest


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interruption_at_publish_preserves_prior_install(tmp_path, monkeypatch, interrupt):
    # T5: an interruption (KeyboardInterrupt/SystemExit — NOT an OSError, so it bypasses the restore
    # branch) between the move-aside and the publish must NOT let cleanup delete the only prior
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools = tmp_path / "tools"
    dest = _seed_prior_with_dep(tools)

    real_replace = os.replace

    def interrupt_on_publish(a, b):
        # the pkg->dest publish comes right after the successful dest->backup move-aside
        if os.path.realpath(b) == os.path.realpath(str(dest)) and ".cc-stage-" in str(a):
            raise interrupt()
        return real_replace(a, b)

    monkeypatch.setattr(tb.os, "replace", interrupt_on_publish)
    with pytest.raises(interrupt):
        tb.extract_pack(pack, str(dest))
    monkeypatch.setattr(tb.os, "replace", real_replace)
    preserved = list(tools.glob(".cc-backup-*/old"))
    assert preserved, "the prior install must survive an interruption at the publish boundary"
    old = preserved[0]
    assert (old / "aircrack-ng.exe").read_bytes() == b"OLD-PRIMARY"
    assert (old / "dep" / "lib.dll").read_bytes() == b"OLD-DEP"      # nested dependency survived too
    assert not any(p.name.startswith(".cc-stage-") for p in tools.iterdir())


def test_successful_replacement_cleans_the_backup(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"NEW-PRIMARY", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools = tmp_path / "tools"
    dest = _seed_prior_with_dep(tools)
    tb.extract_pack(pack, str(dest))
    assert (dest / "aircrack-ng.exe").read_bytes() == b"NEW-PRIMARY"                 # replaced
    assert not any(p.name.startswith((".cc-backup-", ".cc-stage-")) for p in tools.iterdir())


def test_promote_fail_then_restore_recovers_and_cleans_backup(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"NEW-PRIMARY", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools = tmp_path / "tools"
    dest = _seed_prior_with_dep(tools)

    real_replace = os.replace

    def fail_publish_only(a, b):
        # fail the pkg->dest publish (source in staging) but let the backup->dest restore succeed
        if os.path.realpath(b) == os.path.realpath(str(dest)) and ".cc-stage-" in str(a):
            raise PermissionError(13, "Access is denied")
        return real_replace(a, b)

    monkeypatch.setattr(tb.os, "replace", fail_publish_only)
    with pytest.raises(PermissionError):
        tb.extract_pack(pack, str(dest))
    monkeypatch.setattr(tb.os, "replace", real_replace)
    assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-PRIMARY"                 # prior restored
    assert not any(p.name.startswith((".cc-backup-", ".cc-stage-")) for p in tools.iterdir())


# ── begin_commit publish boundary + typed EnableOutcome (async job seam) ──

def test_begin_commit_fires_once_at_the_publish_boundary(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = _seed_prior_install(tools_dir)
    calls = []

    def bc():
        calls.append(1)
        # staging is complete, but the prior install has NOT been moved aside yet at the boundary
        assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-GOOD-INSTALL"

    tb.extract_pack(pack, str(dest), begin_commit=bc)
    assert calls == [1]                                              # exactly once
    assert (dest / "aircrack-ng.exe").read_bytes() == b"MZ NEW aircrack"   # published after the boundary


def test_begin_commit_raise_aborts_before_publish_and_preserves_prior(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    tools_dir = tmp_path / "tools"
    dest = _seed_prior_install(tools_dir)

    def bc():
        raise tb.ToolCancelled("cancelled at the commit boundary")

    with pytest.raises(tb.ToolCancelled):
        tb.extract_pack(pack, str(dest), begin_commit=bc)
    assert (dest / "aircrack-ng.exe").read_bytes() == b"OLD-GOOD-INSTALL"       # prior intact
    assert not any(p.name.startswith((".cc-stage-", ".cc-backup-")) for p in tools_dir.iterdir())


def test_enable_bundled_result_success_and_legacy_tuple(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    monkeypatch.setattr(tb, "enable_dir", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr("src.core.defender.is_windows", lambda: False)   # skip the exe-runs launch probe
    pack = tb.pack_for_tool("aircrack-ng", "windows")
    out = tb.enable_bundled_result(pack)
    assert out.status == "succeeded"
    assert out.exe and out.exe.endswith("aircrack-ng.exe")
    assert out.verification_method == "sha256"                          # honest: members verify by SHA-256
    ok, msg = tb.enable_bundled(pack)                                    # legacy wrapper still works
    assert ok is True and "enabled" in msg


def test_enable_bundled_result_distinguishes_cancel_from_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "enable_dir", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr("src.core.defender.is_windows", lambda: False)
    good_packs = tmp_path / "packs"
    _write_ac_pack(good_packs, b"MZ NEW aircrack", planted=False)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(good_packs))
    pack = tb.pack_for_tool("aircrack-ng", "windows")

    # a pre-publish cancel is CANCELLED (and begin_commit is never reached — the cancel check precedes it)
    bc_calls = []
    cout = tb.enable_bundled_result(pack, should_cancel=lambda: True,
                                    begin_commit=lambda: bc_calls.append(1))
    assert cout.status == "cancelled" and bc_calls == []
    assert tb.enable_bundled(pack, should_cancel=lambda: True)[0] is False

    # a bad pack (unlisted member) is FAILED, distinct from cancelled
    bad_packs = tmp_path / "packs_bad"
    _write_ac_pack(bad_packs, b"MZ NEW aircrack", planted=True)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(bad_packs))
    badpack = tb.pack_for_tool("aircrack-ng", "windows")
    fout = tb.enable_bundled_result(badpack)
    assert fout.status == "failed"
    assert tb.enable_bundled(badpack)[0] is False


# ── B2: a post-publish progress-observer failure must not relabel a completed install ──

def _enable_ready(tmp_path, monkeypatch, planted=False):
    packs = tmp_path / "packs"
    _write_ac_pack(packs, b"MZ NEW aircrack", planted=planted)
    monkeypatch.setattr(tb, "packs_dir", lambda: str(packs))
    monkeypatch.setattr(tb, "enable_dir", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr("src.core.defender.is_windows", lambda: False)
    return tb.pack_for_tool("aircrack-ng", "windows")


def test_post_publish_observer_runtimeerror_stays_succeeded(tmp_path, monkeypatch):
    pack = _enable_ready(tmp_path, monkeypatch)

    def raise_after_publish(line):
        if "published to" in line:
            raise RuntimeError("observer blew up AFTER publish")

    out = tb.enable_bundled_result(pack, on_line=raise_after_publish)
    assert out.status == "succeeded"          # a post-publish observer failure can't undo the publish
    assert (tmp_path / "tools" / "aircrack-ng" / "aircrack-ng.exe").read_bytes() == b"MZ NEW aircrack"


def test_post_publish_observer_toolcancelled_stays_succeeded(tmp_path, monkeypatch):
    pack = _enable_ready(tmp_path, monkeypatch)

    def cancel_after_publish(line):
        if "published to" in line:
            raise tb.ToolCancelled("late cancel after publish")

    out = tb.enable_bundled_result(pack, on_line=cancel_after_publish)
    assert out.status == "succeeded"          # a broad observer 'cancel' after publish is NOT a cancellation


def test_post_publish_keyboardinterrupt_still_propagates(tmp_path, monkeypatch):
    pack = _enable_ready(tmp_path, monkeypatch)

    def ki_after_publish(line):
        if "published to" in line:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):     # control-interruption is never swallowed
        tb.enable_bundled_result(pack, on_line=ki_after_publish)
