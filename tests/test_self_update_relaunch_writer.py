"""Inert tests for the Windows self-update relaunch-args WRITER (_apply_windows). No real process is
launched, no update applied, no device/network: subprocess.Popen is patched to a recorder and the
swap script is written to a temp dir. The sidecar contents are checked by consuming them with the
real relaunch_args module.
"""
from __future__ import annotations

import os

import pytest

from src.core import relaunch_args
from src.core import self_update as su


class _Rec:
    """A subprocess.Popen stand-in that records its call and never spawns anything."""

    def __init__(self):
        self.cmd = None
        self.env = None

    def __call__(self, cmd, **kw):
        self.cmd = cmd
        self.env = kw.get("env")
        return object()


def _exe(tmp_path):
    return str(tmp_path / "cyber-controller.exe")


def test_writer_passes_valid_token_in_child_env_and_writes_argv(tmp_path, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(su.subprocess, "Popen", rec)
    cur, new = _exe(tmp_path), _exe(tmp_path) + ".new"
    su._apply_windows(cur, new, 4321, [cur, "--ui", "web", "a b", ""])
    # the token is in the CHILD's env, a valid token, and os.environ was NOT mutated
    token = rec.env[relaunch_args.TOKEN_ENV]
    assert relaunch_args.is_valid_token(token)
    assert relaunch_args.TOKEN_ENV not in os.environ
    assert rec.env is not os.environ
    # the sidecar holds argv[1:] exactly (drops the program name), consumable by the real module
    assert relaunch_args.consume_sidecar(cur, token) == ["--ui", "web", "a b", ""]


def test_writer_empty_argv_writes_empty_list(tmp_path, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(su.subprocess, "Popen", rec)
    cur, new = _exe(tmp_path), _exe(tmp_path) + ".new"
    su._apply_windows(cur, new, 1, [cur])  # launched with no arguments
    token = rec.env[relaunch_args.TOKEN_ENV]
    assert relaunch_args.consume_sidecar(cur, token) == []  # token issued -> sidecar present (P2)


def test_writer_sidecar_failure_aborts_before_launch(tmp_path, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(su.subprocess, "Popen", rec)
    scripts = []
    real_mkstemp = su.tempfile.mkstemp

    def rec_mkstemp(**kw):
        fd, path = real_mkstemp(**kw)
        scripts.append(path)
        return fd, path

    monkeypatch.setattr(su.tempfile, "mkstemp", rec_mkstemp)

    def boom(*a, **k):
        raise relaunch_args.RelaunchArgsError("io")

    monkeypatch.setattr(relaunch_args, "write_sidecar", boom)
    new = _exe(tmp_path) + ".new"
    with open(new, "wb") as fh:
        fh.write(b"staged")  # the verified staged update
    with pytest.raises(su.SelfUpdateError) as e:
        su._apply_windows(_exe(tmp_path), new, 1, [_exe(tmp_path), "--ui", "web"])
    assert "update staged but not applied" in str(e.value)
    assert rec.cmd is None                          # the helper was NOT spawned
    assert os.path.exists(new)                      # the staged update is retained
    assert scripts and not os.path.exists(scripts[0])  # the swap script was cleaned on abort
    assert relaunch_args.TOKEN_ENV not in os.environ


def test_writer_spawn_failure_removes_sidecar_and_script(tmp_path, monkeypatch):
    written = {}
    real_write = relaunch_args.write_sidecar

    def track_write(exe, token, args):
        p = real_write(exe, token, args)
        written["path"] = p
        return p

    monkeypatch.setattr(relaunch_args, "write_sidecar", track_write)

    def bad_popen(*a, **k):
        raise OSError("no cmd")

    monkeypatch.setattr(su.subprocess, "Popen", bad_popen)
    with pytest.raises(su.SelfUpdateError):
        su._apply_windows(_exe(tmp_path), _exe(tmp_path) + ".new", 1, [_exe(tmp_path), "--x"])
    assert not os.path.exists(written["path"])  # this attempt's sidecar removed on spawn failure


def test_writer_does_not_mutate_global_environment(tmp_path, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(su.subprocess, "Popen", rec)
    before = dict(os.environ)
    su._apply_windows(_exe(tmp_path), _exe(tmp_path) + ".new", 1, [_exe(tmp_path), "--x"])
    assert dict(os.environ) == before  # global env untouched; token only in the child copy


def test_apply_forwards_argv_to_windows_writer(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onefile")
    monkeypatch.setattr(su, "validate_staged_executable", lambda *a, **k: None)
    monkeypatch.setattr(su, "_apply_windows",
                        lambda cur, new, pid, argv: seen.update(argv=list(argv)))
    su.apply(_exe(tmp_path), _exe(tmp_path) + ".new", "windows-x86_64", pid=7,
             argv=[_exe(tmp_path), "--ui", "web"])
    assert seen["argv"] == [_exe(tmp_path), "--ui", "web"]
    # argv=None falls back to sys.argv
    seen.clear()
    monkeypatch.setattr(su.sys, "argv", ["prog", "--fallback"])
    su.apply(_exe(tmp_path), _exe(tmp_path) + ".new", "windows-x86_64", pid=7)
    assert seen["argv"] == ["prog", "--fallback"]
