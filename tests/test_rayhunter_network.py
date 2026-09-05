"""rayhunter NETWORK installer path in adb_backend — the current (non-ADB) method CC drives to put
rayhunter on the Orbic RC400L. Pure logic + mocks, no real device/network.

Covers: installer discovery, the install argv (password never echoed), status parsing, and the
subnet-collision guard's shape."""

from __future__ import annotations

import json
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


class _Raw:
    def __init__(self, data):
        self._data = data

    def read(self, n, decode_content=True):
        return self._data[:n]


class _Resp:
    """Mimics the streaming interface orbic_status now uses: status_code + raw.read + close."""
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.raw = _Raw(json.dumps(payload).encode("utf-8") if payload is not None else b"")

    def close(self):
        pass


def test_orbic_status_running(monkeypatch):
    payload = {"runtime_metadata": {"rayhunter_version": "0.12.0"}, "battery_status": {"level": 100}}
    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: _Resp(200, payload))
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


# -- D05/D06: admin_ip validation + no shell/URL injection -----------

def test_valid_ipv4_accepts_good_rejects_injection():
    for good in ("192.168.1.1", "10.0.0.255", "172.16.0.1", "127.0.0.1"):
        assert a._valid_ipv4(good), good
    for bad in ("192.168.1.1'; whoami", "192.168.1", "192.168.1.1.1", "256.0.0.1",
                "192.168.1.-1", "orbic.local", "", "192.168.1.x", "0x7f.0.0.1", "1.1.1.1 "):
        assert not a._valid_ipv4(bad), bad


def test_is_local_ipv4():
    for local in ("192.168.1.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "169.254.1.1", "127.0.0.1"):
        assert a._is_local_ipv4(local), local
    for public in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "172.15.0.1",
                   "192.0.2.1", "198.51.100.1", "203.0.113.1", "240.0.0.1"):  # TEST-NET/reserved: NOT local
        assert not a._is_local_ipv4(public), public


def test_valid_ipv4_rejects_leading_zero_ambiguity():
    # R01: 010.0.0.1 is octal-ambiguous (a C resolver reads it as 8.0.0.1) — must be rejected, not
    # accepted-as-10.x, so the local-device scoping can't be bypassed.
    assert not a._valid_ipv4("010.0.0.1")
    assert not a._valid_ipv4("192.168.001.1")
    assert not a._valid_ipv4("1.1.1.1 ")
    assert not a._valid_ipv4("::1")
    assert not a._is_local_ipv4("010.0.0.1")   # non-canonical -> IPv4Address rejects -> not local


def test_orbic_status_does_not_follow_redirects(monkeypatch):
    """R01: a validated local endpoint that answers with a redirect must NOT be followed, and a 3xx is
    not 'running'."""
    seen = {}

    class _Resp:
        status_code = 302

        def close(self):
            seen["closed"] = True

    def fake_get(url, **kw):
        seen["allow_redirects"] = kw.get("allow_redirects")
        return _Resp()

    monkeypatch.setattr(a.requests, "get", fake_get)
    st = a.orbic_status("192.168.1.1")
    assert seen["allow_redirects"] is False
    assert st["reachable"] is True and st["running"] is False


def test_find_installer_prefers_active_pointer_over_stale_root(tmp_path):
    """R03: a stale root-level installer.exe must not be selected over the promoted versioned install."""
    import os as _os
    d = str(tmp_path / "rh")
    _os.makedirs(_os.path.join(d, "v9", "rayhunter-v9"))
    with open(_os.path.join(d, "installer.exe"), "w") as f:
        f.write("STALE-ROOT")
    with open(_os.path.join(d, "v9", "rayhunter-v9", "installer.exe"), "w") as f:
        f.write("FRESH-VERSIONED")
    with open(_os.path.join(d, "ACTIVE"), "w") as f:
        f.write("v9")
    found = a.find_installer(d)
    assert found is not None
    assert open(found).read() == "FRESH-VERSIONED"


def test_find_installer_rejects_traversal_pointer(tmp_path):
    """#2: an ACTIVE pointer that escapes the tools dir must resolve nothing (never a foreign installer),
    and a present-but-invalid pointer must NOT fall back to arbitrary discovery."""
    import os as _os
    d = str(tmp_path / "rh")
    _os.makedirs(d)
    # a legacy root installer that a walk-fallback would wrongly pick
    with open(_os.path.join(d, "installer.exe"), "w") as f:
        f.write("STALE")
    with open(_os.path.join(d, "ACTIVE"), "w") as f:
        f.write("../outside")
    assert a.find_installer(d) is None            # invalid pointer -> nothing, not the stale root file


def test_find_installer_corrupt_pointer_returns_none(tmp_path):
    import os as _os
    d = str(tmp_path / "rh")
    _os.makedirs(d)
    with open(_os.path.join(d, "ACTIVE"), "wb") as f:
        f.write(b"\xff\xfe\x00")                   # invalid UTF-8 pointer
    assert a.find_installer(d) is None            # corruption handled, no crash, no arbitrary fallback


def test_subnet_conflict_never_shells_a_malformed_ip(monkeypatch):
    """A malformed admin_ip must short-circuit before any subprocess — the old code interpolated it
    straight into a PowerShell -Command."""
    monkeypatch.setattr(a.os, "name", "nt")

    def boom(*args, **kw):
        raise AssertionError("subprocess.run must not be reached for a malformed admin_ip")

    monkeypatch.setattr(a.subprocess, "run", boom)
    out = a.orbic_subnet_conflict("192.168.1.1'; Remove-Item C:\\ -Recurse -Force")
    assert out == {"conflict": False, "orbic_iface": None, "other_ifaces": []}


def test_subnet_conflict_ps_command_is_constant(monkeypatch):
    """Even for a VALID admin_ip, the value must never appear in the PowerShell command text — the
    /24 filtering happens in Python now."""
    monkeypatch.setattr(a.os, "name", "nt")
    captured = {}

    class _R:
        stdout = "10.9.8.7|Remote NDIS based Internet Sharing Device\n10.9.8.7|Realtek USB GbE\n"

    def fake_run(argv, **kw):
        captured["cmd"] = argv[-1]
        return _R()

    monkeypatch.setattr(a.subprocess, "run", fake_run)
    out = a.orbic_subnet_conflict("10.9.8.7")
    assert "10.9.8.7" not in captured["cmd"]          # no interpolation of the address
    assert out["conflict"] is True                    # still detects the collision (Python-side filter)
    assert "Remote NDIS" in (out["orbic_iface"] or "")


def test_orbic_status_refuses_non_local_or_malformed_without_request(monkeypatch):
    def boom(*args, **kw):
        raise AssertionError("requests.get must not run for a non-local / invalid admin_ip")

    monkeypatch.setattr(a.requests, "get", boom)
    for bad in ("8.8.8.8", "evil.example.com", "192.168.1.1'", "1.1.1.1"):
        st = a.orbic_status(bad)
        assert st["reachable"] is False
        assert st["url"] is None
        assert "error" in st


# -- D04b/D07: staged rayhunter install + required checksum ----------

def test_provision_installer_requires_a_checksum(tmp_path, monkeypatch):
    """No .sha256 sidecar and no CC_RAYHUNTER_SHA256 pin → refuse to extract (fail-closed)."""
    monkeypatch.delenv("CC_RAYHUNTER_SHA256", raising=False)
    monkeypatch.setattr(a, "_github_latest", lambda repo: ("v9", [
        {"name": "pkg-windows-x86_64.zip", "browser_download_url": "https://github.com/x/pkg-windows-x86_64.zip"}]))
    monkeypatch.setattr(a, "_pick_platform_asset", lambda assets: assets[0])
    z = tmp_path / "z.zip"
    z.write_text("zip")
    monkeypatch.setattr(a, "_download_to", lambda *args, **kw: str(z))
    import pytest
    with pytest.raises(RuntimeError, match="refusing to extract"):
        a.provision_installer(lambda s: None, directory=str(tmp_path / "rh"))


def test_provision_installer_failure_keeps_existing(tmp_path, monkeypatch):
    """A failed extraction must preserve an existing installer (D04b) — the old code wiped the dir first."""
    import os as _os
    import pytest
    d = str(tmp_path / "rh")
    _os.makedirs(_os.path.join(d, "v1"))
    old = _os.path.join(d, "v1", "installer.exe")
    with open(old, "w") as f:
        f.write("OLD-INSTALLER")
    monkeypatch.setattr(a, "_github_latest", lambda repo: ("v2", [
        {"name": "pkg.zip", "browser_download_url": "https://github.com/x/pkg.zip"},
        {"name": "pkg.zip.sha256", "browser_download_url": "https://github.com/x/pkg.zip.sha256"}]))
    monkeypatch.setattr(a, "_pick_platform_asset", lambda assets: assets[0])
    z = tmp_path / "z.zip"
    z.write_text("zip")
    monkeypatch.setattr(a, "_download_to", lambda *args, **kw: str(z))
    monkeypatch.setattr(a, "_http_get", lambda url: b"deadbeef sidecar")
    monkeypatch.setattr(a, "_sha256_file", lambda p: "deadbeef")  # matches the sidecar digest

    def bad_extract(zip_path, dest, on_line):
        raise RuntimeError("corrupt archive")

    monkeypatch.setattr(a, "_extract_zip", bad_extract)
    with pytest.raises(RuntimeError, match="corrupt"):
        a.provision_installer(lambda s: None, directory=d)
    assert open(old).read() == "OLD-INSTALLER"                       # existing install survived
    assert not any(n.startswith(".stage") for n in _os.listdir(d))   # staging cleaned up


def test_rayhunter_api_rejects_malformed_admin_ip(monkeypatch, tmp_path):
    """Both the status GET and the install POST must 400 a malformed admin_ip at the boundary."""
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    from src.ui.web.app import create_app
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.security.web_auth import new_csrf_token

    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = new_csrf_token()
        csrf = sess["csrf"]
    bad = "192.168.1.1'; whoami"
    assert client.get("/api/rayhunter?admin_ip=" + bad,
                      headers={"X-CSRF-Token": csrf}).status_code == 400
    assert client.post("/api/rayhunter/install",
                       json={"admin_password": "x", "admin_ip": bad},
                       headers={"X-CSRF-Token": csrf}).status_code == 400


# -- Legacy check_status redirect/size hardening (localhost ADB-era helper) -------------------------
# check_status streams the body with allow_redirects=False and a shared byte cap (_STATUS_MAX_BYTES),
# reads at most cap+1 bytes, and always closes the response. These are pure mocks — no real device or
# network. Return-field/callback compatibility (running/status_code/response/error) is preserved.

class _StatusResp:
    """A streamed requests.Response stand-in for check_status: status_code + headers + raw.read + close.

    Tracks total bytes handed out by raw.read (to prove the body is NOT fully read on overflow) and
    whether close() ran (to prove the connection is always released)."""

    def __init__(self, status_code, body=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.read_total = 0
        self.closed = False
        resp = self

        class _Raw:
            def read(self, n, decode_content=True):
                chunk = resp._body[:n]
                resp.read_total += len(chunk)
                return chunk

        self.raw = _Raw()

    def close(self):
        self.closed = True


def test_check_status_does_not_follow_redirects(monkeypatch):
    """A 3xx from the (localhost) endpoint must NOT be followed, is not 'running', and is still closed."""
    seen = {}
    resp = _StatusResp(302, body=b"redirected")

    def fake_get(url, **kw):
        seen["allow_redirects"] = kw.get("allow_redirects")
        seen["stream"] = kw.get("stream")
        return resp

    monkeypatch.setattr(a.requests, "get", fake_get)
    out = a.check_status(lambda _l: None)
    assert seen["allow_redirects"] is False and seen["stream"] is True
    assert out["running"] is False and out["status_code"] == 302
    assert resp.closed is True


def test_check_status_rejects_oversize_by_content_length_without_reading_body(monkeypatch):
    """An advertised oversized Content-Length is rejected up front — the body is never streamed in."""
    huge = a._STATUS_MAX_BYTES + 10_000
    resp = _StatusResp(200, body=b"x" * huge, headers={"Content-Length": str(huge)})
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: resp)
    out = a.check_status(lambda _l: None)
    assert out["response"] is None and out["error"] == "response too large"
    assert out["status_code"] == 200            # fields preserved
    assert resp.read_total == 0                  # body was NOT read
    assert resp.closed is True


def test_check_status_rejects_oversize_stream_bounded_read(monkeypatch):
    """With a missing/lying Content-Length, the real stream size is still capped: at most cap+1 bytes are
    read (never the full oversized body) and the body is rejected."""
    huge = a._STATUS_MAX_BYTES + 50_000
    resp = _StatusResp(200, body=b"y" * huge)   # no Content-Length header
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: resp)
    out = a.check_status(lambda _l: None)
    assert out["response"] is None and out["error"] == "response too large"
    assert resp.read_total == a._STATUS_MAX_BYTES + 1   # bounded read, not the whole body
    assert resp.read_total < huge
    assert resp.closed is True


def test_check_status_closes_response_on_error(monkeypatch):
    """If reading the stream raises after the response is opened, the connection is still closed."""
    resp = _StatusResp(200, body=b"{}")

    def boom(n, decode_content=True):
        raise OSError("stream broke")

    resp.raw.read = boom
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: resp)
    out = a.check_status(lambda _l: None)
    assert out["running"] is False and out["error"] == "stream broke"
    assert resp.closed is True


def test_check_status_parses_valid_json(monkeypatch):
    payload = {"recording": True, "version": "0.12.0"}
    resp = _StatusResp(200, body=json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: resp)
    out = a.check_status(lambda _l: None)
    assert out["running"] is True and out["status_code"] == 200
    assert out["response"] == payload and out["error"] is None
    assert resp.closed is True


def test_check_status_non_json_falls_back_to_capped_text(monkeypatch):
    resp = _StatusResp(200, body=b"plain not-json body")
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: resp)
    out = a.check_status(lambda _l: None)
    assert out["running"] is True
    assert out["response"] == "plain not-json body"   # text fallback, matching the [:500] slice policy
    assert resp.closed is True


def test_check_status_connection_error_is_not_running(monkeypatch):
    def boom(*args, **kw):
        raise a.requests.ConnectionError("refused")
    monkeypatch.setattr(a.requests, "get", boom)
    out = a.check_status(lambda _l: None)
    assert out["running"] is False and out["error"] == "connection refused"
