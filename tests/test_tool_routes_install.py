"""Route tests for the async tool-DOWNLOAD endpoint (POST /api/crack/install-tool/async).

The download is mocked (urlopen serves a synthetic in-memory zip, launch probe stubbed, isolated tools dir),
`spec_for`/`installable_tools` are overridden to a synthetic spec, and the machine gate is isolated — so no
real network, binary execution, or gate change occurs. Also verifies the queue-time runtime-work reservation
is released by the terminal finalizer (R-DL1), alongside the destination admission.
"""
from __future__ import annotations

import hashlib
import io
import time
import zipfile

import pytest

pytest.importorskip("flask")
pytest.importorskip("pyzipper")

from src.core import tool_installer
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


class _CM:
    def __init__(self, raw):
        self._raw = raw

    def __enter__(self):
        return self._raw

    def __exit__(self, *_a):
        self._raw.close()
        return False


def _zip_bytes(body=b"MZ stub"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pfx/aircrack-ng.exe", body)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _app():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    return app


def _authed(app):
    c = app.test_client()
    token = new_csrf_token()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = app.extensions["cc_web_credentials"].generation
        sess["csrf"] = token
    return c, token


def _poll(c, job_id, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = c.get("/api/crack/job/" + job_id)
        if r.status_code == 200 and not r.get_json()["active"]:
            return r.get_json()
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def _wire_install(monkeypatch, tmp_path, zip_bytes, *, archive="zip"):
    spec = tool_installer.ToolInstallSpec(
        tool="aircrack-ng", os_key="windows", version="1.7", url="http://example/ac.zip", archive=archive,
        member_prefix="pfx/", exe_name="aircrack-ng.exe", license="GPL",
        sha256=hashlib.sha256(zip_bytes).hexdigest(), size_bytes=len(zip_bytes))
    monkeypatch.setattr(tool_installer, "installable_tools", lambda: ["aircrack-ng"])
    monkeypatch.setattr(tool_installer, "spec_for", lambda t: spec if t == "aircrack-ng" else None)
    monkeypatch.setattr(tool_installer, "default_tools_dir", lambda: str(tmp_path / "itools"))
    monkeypatch.setattr(tool_installer.urllib.request, "urlopen",
                        lambda req, timeout=None: _CM(io.BytesIO(zip_bytes)))
    monkeypatch.setattr(tool_installer, "_launches", lambda _p: True)
    return spec


def test_install_async_happy_path_downloads_and_publishes(tmp_path, monkeypatch):
    app = _app()
    c, tok = _authed(app)
    z = _zip_bytes()
    _wire_install(monkeypatch, tmp_path, z)
    r = c.post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 202
    job_id = r.get_json()["job_id"]
    snap = _poll(c, job_id)
    assert snap["state"] == "succeeded" and snap["tool"] == "aircrack-ng"
    env = c.get("/api/crack/job/" + job_id + "/result").get_json()
    assert env["schema_version"] == 1 and env["tool"] == "aircrack-ng"
    assert env["source"] == "download" and env["verification_method"] == "sha256"
    assert env["state"] == "succeeded" and env["path"].endswith("aircrack-ng.exe")


def test_install_async_releases_runtime_work_and_admission_on_finish(monkeypatch, tmp_path):
    from src.ui.web import server_lifecycle
    trackers = []
    real = server_lifecycle.WorkTracker

    def capture():
        t = real()
        trackers.append(t)
        return t

    monkeypatch.setattr(server_lifecycle, "WorkTracker", capture)   # patched before create_app's lazy import
    app = _app()
    c, tok = _authed(app)
    z = _zip_bytes()
    _wire_install(monkeypatch, tmp_path, z)
    job_id = c.post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"},
                    headers={"X-CSRF-Token": tok}).get_json()["job_id"]
    _poll(c, job_id)
    time.sleep(0.05)
    assert trackers, "a WorkTracker was created for the app"
    assert all(t._active == 0 for t in trackers)   # the terminal finalizer released the runtime reservation
    # ...and the destination admission was released too (a second job for the same tool can start)
    r2 = c.post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"}, headers={"X-CSRF-Token": tok})
    assert r2.status_code == 202


def test_install_async_unknown_tool_is_400(monkeypatch, tmp_path):
    app = _app()
    c, tok = _authed(app)
    _wire_install(monkeypatch, tmp_path, _zip_bytes())
    r = c.post("/api/crack/install-tool/async", json={"tool": "no-such"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400


def test_install_async_non_zip_is_422(monkeypatch, tmp_path):
    app = _app()
    c, tok = _authed(app)
    _wire_install(monkeypatch, tmp_path, _zip_bytes(), archive="7z")
    r = c.post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 422


def test_install_async_requires_auth(monkeypatch, tmp_path):
    app = _app()
    _wire_install(monkeypatch, tmp_path, _zip_bytes())
    r = app.test_client().post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"})
    assert r.status_code == 401


def test_install_async_control_after_admission_releases_destination(monkeypatch, tmp_path):
    # R-DL5: a control exception from _audit AFTER the destination is acquired must still release it.
    from src.core import tool_bundle

    class _ControlAudit:
        def record(self, action, details):
            if action == "crack_install_tool_async":
                raise KeyboardInterrupt()

    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), audit=_ControlAudit())
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = app.extensions["cc_web_credentials"].generation
        sess["csrf"] = new_csrf_token()
    tok = None
    with c.session_transaction() as sess:
        tok = sess["csrf"]
    _wire_install(monkeypatch, tmp_path, _zip_bytes())
    dest = tool_installer.default_tools_dir() + "/aircrack-ng"
    with pytest.raises(KeyboardInterrupt):
        c.post("/api/crack/install-tool/async", json={"tool": "aircrack-ng"}, headers={"X-CSRF-Token": tok})
    # the admission was released despite the control exception, so it can be acquired again
    lease = tool_bundle.acquire_destination(dest)
    assert tool_bundle.release_destination(lease) is True
