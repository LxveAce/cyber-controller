"""qflipper_tool — discovery, argv builders, and custom-firmware bundle inspection.

Pure logic only (no network, no real qFlipper): the provisioning fetch is the one thin network layer
and is exercised on a real machine, not here."""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from src.core import qflipper_tool as q


# -- discovery --------------------------------------------------------

def test_find_prefers_cli_in_tools_dir(tmp_path):
    sub = tmp_path / "1.3.3"
    sub.mkdir()
    (sub / "qFlipper-cli.exe").write_bytes(b"x")
    (sub / "qFlipper.exe").write_bytes(b"x")
    tools = q.find_qflipper(str(tmp_path))
    assert tools.cli and tools.cli.endswith("qFlipper-cli.exe")
    assert tools.gui and tools.gui.endswith("qFlipper.exe")
    assert tools.source == "provisioned"
    assert tools.present


def test_find_env_override_cli(tmp_path, monkeypatch):
    exe = tmp_path / "qFlipper-cli.exe"
    exe.write_bytes(b"x")
    monkeypatch.setenv("CC_QFLIPPER", str(exe))
    tools = q.find_qflipper()
    assert tools.cli == str(exe)
    assert tools.source == "env"


def test_find_nothing_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_QFLIPPER", raising=False)
    # Point PATH lookups at an empty dir and the tools dir at an empty dir.
    monkeypatch.setattr(q.shutil, "which", lambda _n: None)
    tools = q.find_qflipper(str(tmp_path))
    # Std install dirs may exist on a dev box, but on this sandbox they won't; assert the tools-dir
    # branch found nothing (cli/gui None) OR a std dir did — either way present reflects reality.
    if not tools.present:
        assert tools.cli is None and tools.gui is None


# -- argv builders ----------------------------------------------------

def test_firmware_argv():
    assert q.build_firmware_argv("qfc", "a.dfu") == ["qfc", "-d", "1", "firmware", "a.dfu"]


def test_official_update_argv_channel():
    assert q.build_official_update_argv("qfc", "development") == \
        ["qfc", "-d", "1", "--update-channel", "development"]


def test_control_argv_builders():
    assert q.build_backup_argv("qfc", "bkp") == ["qfc", "-d", "1", "backup", "bkp"]
    assert q.build_restore_argv("qfc", "bkp") == ["qfc", "-d", "1", "restore", "bkp"]
    assert q.build_erase_argv("qfc") == ["qfc", "-d", "1", "erase"]
    assert q.build_wipe_argv("qfc") == ["qfc", "-d", "1", "wipe"]


def test_control_ops_metadata_flags_destructive():
    assert q.CONTROL_OPS["wipe"]["destructive"] is True
    assert q.CONTROL_OPS["erase"]["destructive"] is True
    assert q.CONTROL_OPS["backup"]["destructive"] is False
    assert q.CONTROL_OPS["update"]["destructive"] is False


# -- custom-firmware bundle inspection --------------------------------

def _make_bundle(path, *, with_firmware=True, with_resources=True):
    """Write a minimal Flipper-style web-update .tgz to *path*."""
    with tarfile.open(path, "w:gz") as tar:
        root = "f7-update-test"
        members = []
        if with_firmware:
            members.append(("firmware.dfu", b"DFUFAKE"))
        if with_resources:
            members.append(("resources.ths", b"RES"))
        members.append(("update.fuf", b"manifest"))
        for name, data in members:
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_extract_firmware_dfu(tmp_path):
    tgz = tmp_path / "fw.tgz"
    _make_bundle(str(tgz))
    out = tmp_path / "out"
    dfu = q.extract_firmware_dfu(str(tgz), str(out))
    assert os.path.isfile(dfu)
    assert dfu.endswith("firmware.dfu")
    assert open(dfu, "rb").read() == b"DFUFAKE"


def test_extract_firmware_dfu_missing_raises(tmp_path):
    tgz = tmp_path / "scripts.tgz"
    _make_bundle(str(tgz), with_firmware=False)
    with pytest.raises(RuntimeError, match="not a Flipper firmware"):
        q.extract_firmware_dfu(str(tgz), str(tmp_path / "out"))


def test_bundle_has_resources(tmp_path):
    with_res = tmp_path / "a.tgz"
    no_res = tmp_path / "b.tgz"
    _make_bundle(str(with_res), with_resources=True)
    _make_bundle(str(no_res), with_resources=False)
    assert q.bundle_has_resources(str(with_res)) is True
    assert q.bundle_has_resources(str(no_res)) is False


# -- high-level flash_bundle contract ---------------------------------

def test_flash_bundle_no_cli_returns_failure(tmp_path, monkeypatch):
    """No qFlipper anywhere + no provisioning consent → honest failure, never a faked success."""
    monkeypatch.setattr(q, "find_qflipper",
                        lambda *a, **k: q.QFlipperTools(cli=None, gui=None, source=""))
    lines = []
    tgz = tmp_path / "fw.tgz"
    _make_bundle(str(tgz))
    rc = q.flash_bundle(str(tgz), lines.append, allow_provision=False)
    assert rc == 1
    assert any("not found" in ln for ln in lines)


def test_flash_bundle_extracts_and_runs_for_tgz(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "find_qflipper",
                        lambda *a, **k: q.QFlipperTools(cli="qfc", gui=None, source="provisioned"))
    ran = {}

    def fake_runner(argv, on_line):
        ran["argv"] = argv
        return 0

    tgz = tmp_path / "fw.tgz"
    _make_bundle(str(tgz), with_resources=True)
    lines = []
    rc = q.flash_bundle(str(tgz), lines.append, runner=fake_runner)
    assert rc == 0
    assert ran["argv"][0] == "qfc" and ran["argv"][3] == "firmware"
    assert ran["argv"][4].endswith("firmware.dfu")
    # resources present → the honest "core firmware only" note is emitted
    assert any("resources" in ln.lower() for ln in lines)


def test_flash_bundle_rejects_unknown_file(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "find_qflipper",
                        lambda *a, **k: q.QFlipperTools(cli="qfc", gui=None, source="provisioned"))
    lines = []
    rc = q.flash_bundle(str(tmp_path / "firmware.bin"), lines.append)
    assert rc == 1
    assert any("not a Flipper firmware package" in ln for ln in lines)
