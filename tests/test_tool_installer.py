"""Unit tests for the crack-tool installer/resolver. Pure logic + local file I/O; no network.
The install_tool() download path is intentionally not exercised here (thin best-effort layer, same
policy as crack_pipeline's subprocess orchestration and wordlist_manager's download)."""
from __future__ import annotations

import hashlib

from src.core import tool_installer as ti
from src.core.crack_pipeline import AIRCRACK, CONVERTER, HASHCAT


def test_platform_key_known():
    assert ti.platform_key() in ("windows", "linux", "macos", "unknown") or ti.platform_key()


def test_spec_for_aircrack_windows_only():
    spec = ti.spec_for(AIRCRACK, "windows")
    assert spec is not None
    assert spec.archive == "zip"
    assert spec.exe_name == "aircrack-ng.exe"
    assert spec.sha1, "aircrack-ng spec must carry the vendor SHA-1 anchor"
    # aircrack-ng ships no official Linux/macOS binary; hashcat/hcx aren't auto-fetchable at all.
    assert ti.spec_for(AIRCRACK, "linux") is None
    assert ti.spec_for(AIRCRACK, "macos") is None
    assert ti.spec_for(HASHCAT, "windows") is None
    assert ti.spec_for(CONVERTER, "windows") is None


def test_installable_tools_by_os():
    assert ti.installable_tools("windows") == [AIRCRACK]
    assert ti.installable_tools("linux") == []
    assert ti.installable_tools("macos") == []


def test_guidance_present_for_every_tool_and_os():
    for tool in (AIRCRACK, HASHCAT, CONVERTER):
        for os_key in ("windows", "linux", "macos"):
            assert ti.guidance_for(tool, os_key), f"missing guidance for {tool}/{os_key}"


def test_installed_tools_scans_subdirs_and_top(tmp_path):
    # aircrack installs into tools/aircrack-ng/aircrack-ng.exe; a dropped hashcat.exe sits at top.
    (tmp_path / "aircrack-ng").mkdir()
    (tmp_path / "aircrack-ng" / "aircrack-ng.exe").write_bytes(b"stub")
    (tmp_path / "hashcat.exe").write_bytes(b"stub")
    found = ti.installed_tools(str(tmp_path))
    assert found.get(AIRCRACK, "").endswith("aircrack-ng.exe")
    assert found.get(HASHCAT, "").endswith("hashcat.exe")
    assert CONVERTER not in found


def test_verify_archive_sha1_and_refusal(tmp_path):
    f = tmp_path / "a.zip"
    f.write_bytes(b"hello-archive")
    good_sha1 = hashlib.sha1(b"hello-archive").hexdigest()
    ok, _ = ti.verify_archive(str(f), ti.ToolInstallSpec(
        tool=AIRCRACK, os_key="windows", version="x", url="u", archive="zip",
        member_prefix="p/", exe_name="e.exe", license="GPL-2.0", sha1=good_sha1))
    assert ok
    bad, _ = ti.verify_archive(str(f), ti.ToolInstallSpec(
        tool=AIRCRACK, os_key="windows", version="x", url="u", archive="zip",
        member_prefix="p/", exe_name="e.exe", license="GPL-2.0", sha1="0" * 40))
    assert not bad
    # a spec with NO anchor must refuse (never install unverified)
    none_ok, _ = ti.verify_archive(str(f), ti.ToolInstallSpec(
        tool=AIRCRACK, os_key="windows", version="x", url="u", archive="zip",
        member_prefix="p/", exe_name="e.exe", license="GPL-2.0"))
    assert not none_ok


def test_tool_availability_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TOOLS_DIR", str(tmp_path))
    rows = {r.tool: r for r in ti.tool_availability("windows")}
    assert set(rows) == {AIRCRACK, HASHCAT, CONVERTER}
    for r in rows.values():
        assert r.guidance


def test_detect_tools_falls_back_to_tools_dir(tmp_path, monkeypatch):
    # With aircrack-ng absent from PATH but present in the CC tools dir, detect_tools must find it.
    from src.core import crack_pipeline as cp
    (tmp_path / "aircrack-ng").mkdir()
    exe = tmp_path / "aircrack-ng" / "aircrack-ng.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setenv("CC_TOOLS_DIR", str(tmp_path))
    monkeypatch.setattr(cp.shutil, "which", lambda _n: None)  # nothing on PATH
    tools = cp.detect_tools()
    assert tools[AIRCRACK].path == str(exe)
    assert tools[AIRCRACK].present
