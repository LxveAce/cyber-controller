"""rayhunter NETWORK installer path in adb_backend — the current (non-ADB) method CC drives to put
rayhunter on the Orbic RC400L. Pure logic + mocks, no real device/network.

Covers: installer discovery, the install argv (password never echoed), status parsing, and the
subnet-collision guard's shape."""

from __future__ import annotations

import os

from src.core.backends import adb_backend as a


def test_find_installer(tmp_path):
    assert a.find_installer(str(tmp_path)) is None
    sub = tmp_path / "rayhunter-vX-windows-x86_64"
    sub.mkdir()
    exe = sub / "installer.exe"
    exe.write_bytes(b"x")
    found = a.find_installer(str(tmp_path))
    assert found and found.endswith("installer.exe")


def test_install_orbic_network_argv_and_password_masked(monkeypatch):
    monkeypatch.setattr(a, "find_installer", lambda *args, **kw: "C:/tools/installer.exe")
    captured = {}

    def fake_runner(argv, on_line):
        captured["argv"] = argv
        on_line("$ " + a._redacted_cmdline(argv))  # exercise the redaction the default runner uses
        return 0

    lines = []
    rc = a.install_orbic_network("s3cr3t-pw", lines.append, admin_ip="192.168.1.1", runner=fake_runner)
    assert rc == 0
    argv = captured["argv"]
    assert argv[1] == "orbic"
    assert "--admin-password" in argv and "s3cr3t-pw" in argv          # passed to the tool
    assert all("s3cr3t-pw" not in ln for ln in lines)                  # but NEVER echoed to the log


def test_install_orbic_network_no_installer_fails(monkeypatch):
    monkeypatch.setattr(a, "find_installer", lambda *args, **kw: None)
    lines = []
    rc = a.install_orbic_network("pw", lines.append, allow_provision=False)
    assert rc == 1
    assert any("not available" in ln for ln in lines)


def test_install_orbic_network_nonzero_exit_is_failure(monkeypatch):
    monkeypatch.setattr(a, "find_installer", lambda *args, **kw: "installer.exe")
    lines = []
    rc = a.install_orbic_network("pw", lines.append, runner=lambda argv, on_line: 2)
    assert rc == 2
    assert any("exited 2" in ln for ln in lines)


class _Resp:
    def __init__(self, ok, payload):
        self.ok = ok
        self._payload = payload

    def json(self):
        return self._payload


def test_orbic_status_running(monkeypatch):
    payload = {"runtime_metadata": {"rayhunter_version": "0.12.0"}, "battery_status": {"level": 100}}
    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: _Resp(True, payload))
    st = a.orbic_status("192.168.1.1")
    assert st["reachable"] and st["running"]
    assert st["version"] == "0.12.0"
    assert st["url"] == "http://192.168.1.1:8080"


def test_orbic_status_unreachable_is_not_an_error(monkeypatch):
    def boom(*args, **kw):
        raise OSError("no route")
    monkeypatch.setattr(a.requests, "get", boom)
    st = a.orbic_status("192.168.1.1")
    assert st["reachable"] is False and st["running"] is False
    assert st["version"] is None  # never fabricated


def test_installer_tools_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_TOOLS_DIR", str(tmp_path))
    assert a.installer_tools_dir() == os.path.join(str(tmp_path), "rayhunter")


def test_subnet_conflict_shape_non_windows(monkeypatch):
    # On non-Windows it returns the "unknown" shape rather than guessing.
    monkeypatch.setattr(a.os, "name", "posix")
    out = a.orbic_subnet_conflict()
    assert out == {"conflict": False, "orbic_iface": None, "other_ifaces": []}
