"""After a successful bounded download, every remaining updater step fails finitely and cleans up
only the files this attempt owns: the digest read and the .part-to-.new rename remove this attempt's
.part on failure, the Windows swap helper owns its temporary script until the helper runs, and the
Unix replace leaves the verified staged file in place when it cannot install it.

Doubles only: the download is a fake that writes bytes, the header gate is a recorder, the swap
helper is never spawned and nothing is executed or installed. Temporary files stand in for the
running binary, the staged copy and foreign neighbours.
"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from src.core import self_update as su

TAG = "v9.9.9"
NAME = f"cyber-controller-{TAG}-linux-x64"
GOOD = b"verified bytes of the downloaded asset"


@pytest.fixture
def staging(monkeypatch, tmp_path):
    """A frozen linux-x64 onefile whose download and header gate are doubles; returns paths."""
    cur = tmp_path / "cyber-controller"
    cur.write_bytes(b"current build (double)")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onefile")
    monkeypatch.setattr(su, "current_exe", lambda: str(cur))
    monkeypatch.setattr(su, "platform_key", lambda *a, **k: "linux-x64")
    monkeypatch.setattr(su, "validate_staged_executable", lambda path, key: None)
    monkeypatch.setattr(su, "fetch_sums",
                        lambda assets, timeout=0: {NAME: hashlib.sha256(GOOD).hexdigest()})

    def download(url, dest, timeout=0, progress=None, expected_size=None):
        with open(dest, "wb") as fh:
            fh.write(GOOD)
        return dest

    monkeypatch.setattr(su, "download_asset", download)
    foreign_new = tmp_path / "other-app.new"
    foreign_new.write_bytes(b"foreign staged file")
    foreign_part = tmp_path / "other-app.part"
    foreign_part.write_bytes(b"foreign partial")
    return SimpleNamespace(dir=tmp_path, cur=cur, part=tmp_path / (NAME + ".part"),
                           staged=tmp_path / (NAME + ".new"), foreign_new=foreign_new,
                           foreign_part=foreign_part)


def run():
    release = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [
        {"name": NAME, "browser_download_url": "https://example.invalid/" + NAME,
         "size": len(GOOD)},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/s"}]}
    return su.self_update(SimpleNamespace(latest_tag=TAG), releases=[release], restart=False)


def neighbours_intact(env):
    assert env.foreign_new.read_bytes() == b"foreign staged file"
    assert env.foreign_part.read_bytes() == b"foreign partial"
    assert env.cur.read_bytes() == b"current build (double)"


# ---- staging: .part to .new -------------------------------------------------------------------

def test_rename_failure_is_finite_and_removes_only_this_attempts_part(monkeypatch, staging):
    def locked(src, dst):
        raise PermissionError(13, "injected lock")

    monkeypatch.setattr(su.os, "replace", locked)
    with pytest.raises(su.SelfUpdateError, match="could not stage") as info:
        run()
    assert isinstance(info.value.__cause__, PermissionError)
    assert not staging.part.exists() and not staging.staged.exists()
    neighbours_intact(staging)


def test_directory_at_the_staged_name_is_a_finite_refusal(staging):
    staging.staged.mkdir()
    (staging.staged / "keep").write_bytes(b"inside")
    with pytest.raises(su.SelfUpdateError, match="could not stage") as info:
        run()
    assert isinstance(info.value.__cause__, OSError)
    assert not staging.part.exists()
    assert (staging.staged / "keep").read_bytes() == b"inside", "the directory is not ours to touch"
    neighbours_intact(staging)


def test_digest_read_failure_is_finite_and_removes_only_this_attempts_part(monkeypatch, staging):
    def unreadable(path, _chunk=1 << 20):
        raise OSError(5, "injected read failure")

    monkeypatch.setattr(su, "sha256_file", unreadable)
    with pytest.raises(su.SelfUpdateError, match="could not read") as info:
        run()
    assert isinstance(info.value.__cause__, OSError)
    assert not staging.part.exists() and not staging.staged.exists()
    neighbours_intact(staging)


def test_existing_contents_at_the_staging_destination_are_replaced_by_the_fresh_download(
        staging):
    # The staging destination is deterministic per asset; staging replaces whatever it holds
    # with the freshly verified download (the existing recovery behaviour after a failed swap).
    # This proves the replacement only; it says nothing about the prior contents' provenance.
    staging.staged.write_bytes(b"whatever the destination held before")
    assert run() == str(staging.staged)
    assert staging.staged.read_bytes() == GOOD
    assert not staging.part.exists()
    neighbours_intact(staging)


# ---- Windows swap helper: the temp script is owned until the helper runs ----------------------

@pytest.fixture
def win_paths(tmp_path):
    cur = tmp_path / "cyber-controller.exe"
    cur.write_bytes(b"cur")
    new = tmp_path / "cyber-controller-v9.9.9-windows-x64.exe.new"
    new.write_bytes(b"new")
    return cur, new


def _record_mkstemp(monkeypatch):
    fds, scripts = [], []
    real = su.tempfile.mkstemp

    def rec(*a, **k):
        fd, path = real(*a, **k)
        fds.append(fd)
        scripts.append(path)
        return fd, path

    monkeypatch.setattr(su.tempfile, "mkstemp", rec)
    return fds, scripts


def test_windows_helper_file_object_failure_closes_descriptor_and_removes_script(monkeypatch,
                                                                                  win_paths):
    cur, new = win_paths
    fds, scripts = _record_mkstemp(monkeypatch)
    closed = []
    real_close = os.close
    monkeypatch.setattr(su.os, "close", lambda fd: (closed.append(fd), real_close(fd)))

    def broken(fd, *a, **k):
        raise OSError(24, "injected file-object failure")

    monkeypatch.setattr(su.os, "fdopen", broken)
    monkeypatch.setattr(su.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("helper must not spawn without a script"))
    with pytest.raises(su.SelfUpdateError, match="could not open") as info:
        su._apply_windows(str(cur), str(new), pid=4242)
    assert isinstance(info.value.__cause__, OSError)
    assert closed == fds and len(fds) == 1, "the raw descriptor was closed before removal"
    assert not os.path.exists(scripts[0]), "only this attempt's temp script removed"
    assert new.read_bytes() == b"new" and cur.read_bytes() == b"cur"


def test_windows_helper_spawn_failure_removes_script_and_keeps_the_staged_update(monkeypatch,
                                                                                 win_paths):
    cur, new = win_paths
    fds, scripts = _record_mkstemp(monkeypatch)

    def no_cmd(*a, **k):
        raise FileNotFoundError(2, "injected spawn failure")

    monkeypatch.setattr(su.subprocess, "Popen", no_cmd)
    with pytest.raises(su.SelfUpdateError, match="could not launch") as info:
        su._apply_windows(str(cur), str(new), pid=4242)
    assert isinstance(info.value.__cause__, FileNotFoundError)
    assert not os.path.exists(scripts[0])
    assert new.read_bytes() == b"new" and cur.read_bytes() == b"cur"


def test_windows_helper_script_creation_failure_is_finite(monkeypatch, win_paths):
    cur, new = win_paths

    def no_temp(*a, **k):
        raise OSError(28, "injected: no space for the script")

    monkeypatch.setattr(su.tempfile, "mkstemp", no_temp)
    monkeypatch.setattr(su.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(su.SelfUpdateError, match="could not create") as info:
        su._apply_windows(str(cur), str(new), pid=4242)
    assert isinstance(info.value.__cause__, OSError)
    assert new.read_bytes() == b"new"


def test_windows_helper_success_leaves_the_script_for_the_helper_and_spawns_it(monkeypatch,
                                                                              win_paths):
    cur, new = win_paths
    fds, scripts = _record_mkstemp(monkeypatch)
    spawned = []
    monkeypatch.setattr(su.subprocess, "Popen", lambda argv, **k: spawned.append(argv) or object())
    su._apply_windows(str(cur), str(new), pid=4242)
    assert spawned and spawned[0][-1] == scripts[0]
    assert os.path.exists(scripts[0]), "the helper deletes its own script after it runs"
    with open(scripts[0], "rb") as fh:
        assert b"4242" in fh.read()
    os.remove(scripts[0])


# ---- Unix apply: finite errors, staged file preserved when it cannot be installed ---------------

def test_unix_replace_failure_is_finite_and_leaves_staged_and_current_intact(monkeypatch,
                                                                             tmp_path):
    cur = tmp_path / "cyber-controller"
    cur.write_bytes(b"cur")
    new = tmp_path / (NAME + ".new")
    new.write_bytes(b"new")

    def not_writable(src, dst):
        raise PermissionError(13, "injected: binary directory not writable")

    monkeypatch.setattr(su.os, "replace", not_writable)
    monkeypatch.setattr(su.os, "execv", lambda *a: pytest.fail("execv must not run"))
    with pytest.raises(su.SelfUpdateError, match="could not replace") as info:
        su._apply_unix(str(cur), str(new), ["cc"])
    assert isinstance(info.value.__cause__, PermissionError)
    assert str(new) in str(info.value), "names where the verified update was left"
    assert new.read_bytes() == b"new" and cur.read_bytes() == b"cur"


def test_unix_relaunch_failure_after_install_is_finite_and_says_next_launch(monkeypatch,
                                                                             tmp_path):
    cur = tmp_path / "cyber-controller"
    cur.write_bytes(b"cur")
    new = tmp_path / (NAME + ".new")
    new.write_bytes(b"new")
    monkeypatch.setattr(su.os, "chmod", lambda *a, **k: None)

    def no_exec(*a):
        raise OSError(8, "injected exec failure")

    monkeypatch.setattr(su.os, "execv", no_exec)
    with pytest.raises(su.SelfUpdateError, match="next launch") as info:
        su._apply_unix(str(cur), str(new), ["cc"])
    assert isinstance(info.value.__cause__, OSError)
    assert cur.read_bytes() == b"new" and not new.exists(), "the replace had already happened"
