"""The staged executable's header and architecture are validated before the swap, at two placements:
after the checksum and before the .part-to-.new rename (the module may remove only the download it
made in the same call), and as a read-only guard in apply() before dispatch (the caller owns the
staged path; nothing is deleted). Refusals are finite SelfUpdateError with the underlying error as
context; no struct.error or OSError escapes.

Doubles only: fake release metadata, checksum fetch and download; temp files stand in for the
running binary and the staged file; the swap functions are recorders. Nothing is executed.
"""
from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace

import pytest

from src.core import self_update as su
from src.core import update_exe_format as uf
from tests import exe_images as img

TAG = "v9.9.9"
HOSTS = {"linux-x64": ("Linux", "x86_64"), "linux-arm64": ("Linux", "aarch64"),
         "windows-x64": ("Windows", "AMD64"), "macos-arm64": ("Darwin", "arm64")}


def asset_name(key):
    return f"cyber-controller-{TAG}-{key}" + (".exe" if key == "windows-x64" else "")


@pytest.fixture
def frozen_onefile(monkeypatch, tmp_path):
    exe = tmp_path / "cyber-controller"
    exe.write_bytes(b"current build (double)\n")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onefile")
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    return exe


def stage(monkeypatch, key, content):
    """Run self_update() offline for *key* with *content* as the downloaded asset; returns
    the staged path or raises whatever self_update raises."""
    system, machine = HOSTS[key]
    monkeypatch.setattr(su.platform, "system", lambda: system)
    monkeypatch.setattr(su.platform, "machine", lambda: machine)
    name = asset_name(key)
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(su, "fetch_sums", lambda assets, timeout=0: {name: digest})

    def download(url, dest, timeout=0, progress=None):
        with open(dest, "wb") as fh:
            fh.write(content)

    monkeypatch.setattr(su, "download_asset", download)
    release = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [
        {"name": name, "browser_download_url": "https://github.com/x/y/d/" + name},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/x/y/s"}]}
    return su.self_update(SimpleNamespace(latest_tag=TAG), releases=[release], restart=False)


# ---- staging gate ----------------------------------------------------------------------------

@pytest.mark.parametrize("key", list(HOSTS))
def test_valid_image_for_each_key_is_staged(monkeypatch, frozen_onefile, key):
    content = img.image_for(key)
    staged = stage(monkeypatch, key, content)
    assert staged.endswith(asset_name(key) + ".new")
    with open(staged, "rb") as fh:
        assert fh.read() == content


def test_universal_macho_with_a_far_arm64_slice_is_staged(monkeypatch, frozen_onefile):
    content = img.macho_fat(first_offset=16384, stride=16384)
    assert stage(monkeypatch, "macos-arm64", content).endswith(".new")


@pytest.mark.parametrize("key,content,reason", [
    ("linux-x64", img.elf64(img.EM_AARCH64), "architecture mismatch"),
    ("linux-arm64", img.elf64(img.EM_X86_64), "architecture mismatch"),
    ("windows-x64", img.pe32plus(img.PE_I386), "architecture mismatch"),
    ("windows-x64", img.elf64(), "not a PE"),
    ("macos-arm64", img.macho_thin(img.CPU_X86_64), "not the exact arm64"),
    ("macos-arm64", img.macho_fat(cpus=(img.CPU_X86_64,)), "no arm64 slice"),
    ("linux-x64", b"definitely not an executable\n" * 8, "not an ELF"),
    ("linux-x64", b"\x7fELF" + b"\x00" * 20, "too short"),
], ids=["elf-wrong-arch", "elf-wrong-arch-2", "pe-i386", "elf-for-windows", "macho-x86_64",
        "fat-no-arm64", "garbage", "short-file"])
def test_rejected_download_removes_only_this_attempts_part_and_stages_nothing(
        monkeypatch, frozen_onefile, key, content, reason):
    other = frozen_onefile.parent / "someone-elses.part"
    other.write_bytes(b"not ours\n")
    with pytest.raises(su.SelfUpdateError) as info:
        stage(monkeypatch, key, content)
    message = str(info.value)
    assert f"is not a {key} executable" in message and reason in message
    assert isinstance(info.value.__cause__, uf.ExecutableFormatError), "context preserved"
    directory = frozen_onefile.parent
    assert not list(directory.glob("cyber-controller-*.part")), "this attempt's download removed"
    assert not list(directory.glob("*.new")), "nothing staged"
    assert other.exists() and other.read_bytes() == b"not ours\n", "foreign .part untouched"
    assert frozen_onefile.read_bytes() == b"current build (double)\n", "running binary untouched"


def test_checksum_still_runs_before_the_header_gate(monkeypatch, frozen_onefile):
    content = img.elf64(img.EM_AARCH64)     # would fail the gate; the checksum fails first
    monkeypatch.setattr(su, "validate_staged_executable",
                        lambda *a: pytest.fail("gate reached before the checksum"))
    system, machine = HOSTS["linux-x64"]
    monkeypatch.setattr(su.platform, "system", lambda: system)
    monkeypatch.setattr(su.platform, "machine", lambda: machine)
    name = asset_name("linux-x64")
    monkeypatch.setattr(su, "fetch_sums", lambda assets, timeout=0: {name: "0" * 64})

    def download(url, dest, timeout=0, progress=None):
        with open(dest, "wb") as fh:
            fh.write(content)

    monkeypatch.setattr(su, "download_asset", download)
    release = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [
        {"name": name, "browser_download_url": "u"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "s"}]}
    with pytest.raises(su.SelfUpdateError, match="checksum mismatch"):
        su.self_update(SimpleNamespace(latest_tag=TAG), releases=[release], restart=False)


# ---- apply() guard ---------------------------------------------------------------------------

@pytest.fixture
def swap_recorders(monkeypatch):
    calls = []
    monkeypatch.setattr(su, "_apply_unix", lambda cur, new, argv: calls.append(("unix", new)))
    monkeypatch.setattr(su, "_apply_windows",
                        lambda cur, new, pid: calls.append(("win", new)))
    return calls


@pytest.mark.parametrize("key", list(HOSTS))
def test_apply_dispatches_a_valid_caller_supplied_file(frozen_onefile, swap_recorders, tmp_path,
                                                        key):
    staged = tmp_path / (asset_name(key) + ".new")
    staged.write_bytes(img.image_for(key))
    su.apply(str(frozen_onefile), str(staged), key, pid=1234, argv=["cc"])
    assert swap_recorders == [("win" if key == "windows-x64" else "unix", str(staged))]


@pytest.mark.parametrize("key,content", [
    ("linux-x64", img.elf64(img.EM_AARCH64)),
    ("windows-x64", b"MZ" + b"\x00" * 200),
    ("macos-arm64", b"\x00" * 100),
    ("linux-arm64", b"\x7fELF"),
], ids=["elf-wrong-arch", "pe-no-header", "zeros", "four-bytes"])
def test_apply_refuses_a_bad_caller_supplied_file_and_deletes_nothing(
        frozen_onefile, swap_recorders, tmp_path, key, content):
    staged = tmp_path / "owner-supplied.bin"
    staged.write_bytes(content)
    with pytest.raises(su.SelfUpdateError) as info:
        su.apply(str(frozen_onefile), str(staged), key, pid=1234, argv=["cc"])
    assert isinstance(info.value.__cause__, uf.ExecutableFormatError)
    assert swap_recorders == [], "no swap dispatched"
    assert staged.exists() and staged.read_bytes() == content, "caller-owned file survives"


def test_apply_on_a_missing_path_is_a_finite_error_not_oserror(frozen_onefile, swap_recorders,
                                                                tmp_path):
    with pytest.raises(su.SelfUpdateError, match="could not inspect") as info:
        su.apply(str(frozen_onefile), str(tmp_path / "absent.new"), "linux-x64", argv=["cc"])
    assert isinstance(info.value.__cause__, OSError)
    assert swap_recorders == []


def test_apply_guard_runs_after_the_frozen_and_onefile_checks(monkeypatch, tmp_path,
                                                               swap_recorders):
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onedir")
    monkeypatch.setattr(su, "validate_staged_executable",
                        lambda *a: pytest.fail("guard reached before the onedir refusal"))
    with pytest.raises(su.SelfUpdateError, match="one-folder"):
        su.apply(str(tmp_path / "cur"), str(tmp_path / "staged"), "linux-x64", argv=["cc"])


# ---- the boundary itself ---------------------------------------------------------------------

def test_boundary_reads_only_the_planned_prefix_from_one_handle(monkeypatch, tmp_path):
    image = img.elf64(phoff=8192) + b"\x00" * 100000
    path = tmp_path / "big.new"
    path.write_bytes(image)
    seen = []
    real = uf.required_prefix_length

    def planner(read, key, length):
        need = real(read, key, length)
        seen.append((need, length))
        return need

    monkeypatch.setattr(uf, "required_prefix_length", planner)
    su.validate_staged_executable(str(path), "linux-x64")
    assert seen == [(8248, len(image))], "planned extent 8248 of a 108 KB file, fstat length used"


def test_boundary_converts_struct_error_defensively(monkeypatch, tmp_path):
    path = tmp_path / "x.new"
    path.write_bytes(img.elf64())

    def broken(read, key, length):
        raise struct.error("synthetic")

    monkeypatch.setattr(uf, "required_prefix_length", broken)
    with pytest.raises(su.SelfUpdateError, match="could not be parsed") as info:
        su.validate_staged_executable(str(path), "linux-x64")
    assert isinstance(info.value.__cause__, struct.error)


@pytest.mark.parametrize("seed", range(6))
def test_arbitrary_staged_bytes_always_yield_a_finite_error(tmp_path, seed):
    import random
    rnd = random.Random(seed)
    path = tmp_path / f"r{seed}.new"
    path.write_bytes(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 400))))
    for key in ("linux-x64", "windows-x64", "macos-arm64"):
        with pytest.raises(su.SelfUpdateError):
            su.validate_staged_executable(str(path), key)
