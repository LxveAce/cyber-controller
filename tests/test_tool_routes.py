"""Route tests for the async 'Get tools' job endpoints (src/ui/web/app.py).

Exercises the real owner-bound registry + destination admission + queue-lifetime finalizer through a Flask
test client, using a SYNTHETIC aircrack pack (harmless bytes) and an isolated enable dir + gate config so no
real PUA binary, Defender, or the machine gate is ever touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

pytest.importorskip("flask")
pyzipper = pytest.importorskip("pyzipper")

from src.core import tool_bundle
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


def _write_ac_pack(directory, good=b"MZ aircrack"):
    directory.mkdir(parents=True, exist_ok=True)
    with pyzipper.AESZipFile(str(directory / "ac.pack"), "w",
                             compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(tool_bundle.PACK_PASSWORD)
        z.writestr("aircrack-ng.exe", good)
    manifest = {"name": "ac", "tool": "aircrack-ng", "version": "1.7", "platform": "windows",
                "primary_exe": "aircrack-ng.exe", "archive_sha1": "0" * 40,
                "files": [{"name": "aircrack-ng.exe", "sha256": hashlib.sha256(good).hexdigest()}],
                "file_count": 1}
    (directory / "ac.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))   # never touch the real machine gate
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    _write_ac_pack(tmp_path / "packs")
    monkeypatch.setattr(tool_bundle, "packs_dir", lambda: str(tmp_path / "packs"))
    monkeypatch.setattr(tool_bundle, "enable_dir", lambda: str(tmp_path / "tools"))
    monkeypatch.setattr("src.core.defender.is_windows", lambda: False)   # skip the exe launch probe


def _app():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    return app


def _authed(app, csrf=None):
    c = app.test_client()
    token = csrf or new_csrf_token()
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


def test_async_routes_require_auth():
    c = _app().test_client()   # no session
    assert c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}).status_code == 401
    assert c.get("/api/crack/job/" + "0" * 32).status_code == 401
    assert c.post("/api/crack/job/" + "0" * 32 + "/cancel", json={}).status_code == 401


def test_enable_async_happy_path_202_then_status_then_result():
    app = _app()
    c, tok = _authed(app)
    r = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 202
    job_id = r.get_json()["job_id"]
    assert len(job_id) == 32 and all(ch in "0123456789abcdef" for ch in job_id)   # 32 lowercase hex
    snap = _poll(c, job_id)
    assert snap["state"] == "succeeded" and snap["tool"] == "aircrack-ng"          # tool name, not pack name
    result = c.get("/api/crack/job/" + job_id + "/result")
    assert result.status_code == 200
    env = result.get_json()
    assert env["schema_version"] == 1 and env["tool"] == "aircrack-ng"
    assert env["source"] == "bundled" and env["verification_method"] == "sha256"
    assert env["state"] == "succeeded" and env["path"].endswith("aircrack-ng.exe")


def test_enable_async_unknown_pack_is_400():
    app = _app()
    c, tok = _authed(app)
    r = c.post("/api/crack/enable-bundled/async", json={"pack": "no-such"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400


def test_enable_async_requires_csrf():
    app = _app()
    c, _tok = _authed(app)
    r = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"})   # no X-CSRF-Token / _csrf
    assert r.status_code == 403


def test_install_tool_async_non_installable_is_400():
    # hashcat ships only as a .7z (not auto-installable here), so it's not in installable_tools() -> 400.
    # (test_tool_routes_install.py covers the wired route: 202, .7z-spec -> 422, runtime-work release, etc.)
    app = _app()
    c, tok = _authed(app)
    r = c.post("/api/crack/install-tool/async", json={"tool": "hashcat"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400


def test_enable_async_conflict_is_409_when_destination_reserved():
    app = _app()
    c, tok = _authed(app)
    dest = os.path.join(tool_bundle.enable_dir(), "aircrack-ng")
    held = tool_bundle.acquire_destination(dest)          # simulate an in-flight op holding the destination
    try:
        r = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
        assert r.status_code == 409
    finally:
        tool_bundle.release_destination(held)


def test_finalizer_releases_destination_so_a_second_job_can_run():
    app = _app()
    c, tok = _authed(app)
    first = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"},
                   headers={"X-CSRF-Token": tok}).get_json()["job_id"]
    _poll(c, first)
    # the queue-time admission was released by the finalizer at terminal, so the destination is free again
    r2 = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
    assert r2.status_code == 202
    assert _poll(c, r2.get_json()["job_id"])["state"] == "succeeded"


def test_jobs_are_owner_scoped_across_sessions():
    app = _app()
    c1, t1 = _authed(app, csrf="csrf-session-one")
    c2, t2 = _authed(app, csrf="csrf-session-two")            # same app+credential, DIFFERENT csrf -> owner
    job_id = c1.post("/api/crack/enable-bundled/async", json={"pack": "ac"},
                     headers={"X-CSRF-Token": t1}).get_json()["job_id"]
    _poll(c1, job_id)
    # a different session cannot see, fetch or cancel another owner's job
    assert c2.get("/api/crack/job/" + job_id).status_code == 404
    assert c2.get("/api/crack/job/" + job_id + "/result").status_code == 404
    assert c2.post("/api/crack/job/" + job_id + "/cancel", json={}, headers={"X-CSRF-Token": t2}).status_code == 404
    # the owner still can
    assert c1.get("/api/crack/job/" + job_id).status_code == 200


def test_same_cookie_polls_after_a_refresh():
    app = _app()
    c, tok = _authed(app)
    job_id = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"},
                    headers={"X-CSRF-Token": tok}).get_json()["job_id"]
    _poll(c, job_id)
    # a fresh client carrying the SAME session cookie keeps ownership (same csrf+generation -> same owner)
    c2 = app.test_client()
    with c.session_transaction() as sess:
        cookie = dict(sess)
    with c2.session_transaction() as sess:
        sess.update(cookie)
    assert c2.get("/api/crack/job/" + job_id).status_code == 200


def test_unknown_job_is_404():
    app = _app()
    c, _tok = _authed(app)
    assert c.get("/api/crack/job/" + "a" * 32).status_code == 404
    assert c.get("/api/crack/job/" + "a" * 32 + "/result").status_code == 404


def test_cancel_returns_cancel_requested_boolean():
    app = _app()
    c, tok = _authed(app)
    # a terminal job can't be cancelled -> cancel_requested false
    job_id = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"},
                    headers={"X-CSRF-Token": tok}).get_json()["job_id"]
    _poll(c, job_id)
    r = c.post("/api/crack/job/" + job_id + "/cancel", json={}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.get_json()["cancel_requested"] is False


# ── R1: a pre-registration start failure must release the queue-time admission (503, not a strand) ──

def test_pre_registration_failure_releases_queue_admission(monkeypatch):
    app = _app()
    c, tok = _authed(app)
    import src.core.tool_jobs as tj

    def boom(_dest):
        raise OSError("destination resolution failed before the job was registered")

    monkeypatch.setattr(tj, "canonical_dest", boom)   # fail inside registry.start, after the route acquired
    r = c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 503                        # finite start-failure, not a 500
    monkeypatch.undo()
    # the destination is NOT stranded — it can be acquired again
    dest = os.path.join(tool_bundle.enable_dir(), "aircrack-ng")
    lease = tool_bundle.acquire_destination(dest)      # raises DestinationBusy if the lease leaked
    assert tool_bundle.release_destination(lease) is True


def test_enable_async_control_after_admission_releases_destination():
    # R-DL5-P: a control exception from the bundled route's post-admission setup (here _audit) must release the
    # already-acquired destination — parity with the install route. Before the fix _audit ran outside cleanup
    # ownership, so the lease leaked and a fresh acquisition raised DestinationBusy.
    class _ControlAudit:
        def record(self, action, details):
            if action == "crack_enable_bundled_async":
                raise KeyboardInterrupt()

    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), audit=_ControlAudit())
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = app.extensions["cc_web_credentials"].generation
        sess["csrf"] = new_csrf_token()
    with c.session_transaction() as sess:
        tok = sess["csrf"]
    with pytest.raises(KeyboardInterrupt):
        c.post("/api/crack/enable-bundled/async", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
    dest = os.path.join(tool_bundle.enable_dir(), "aircrack-ng")
    lease = tool_bundle.acquire_destination(dest)      # succeeds only if the admission was released
    assert tool_bundle.release_destination(lease) is True


# ── R2: a sync route must report a queued async admission as 409, not 500/502 ──

def test_sync_enable_reports_queued_admission_as_409():
    app = _app()
    c, tok = _authed(app)
    dest = os.path.join(tool_bundle.enable_dir(), "aircrack-ng")
    held = tool_bundle.acquire_destination(dest)       # a queued async job holds the destination
    try:
        r = c.post("/api/crack/enable-bundled", json={"pack": "ac"}, headers={"X-CSRF-Token": tok})
        assert r.status_code == 409
    finally:
        tool_bundle.release_destination(held)


def test_sync_install_maps_destination_busy_to_409(monkeypatch):
    app = _app()
    c, tok = _authed(app)
    from src.core import tool_installer

    class _Spec:
        archive = "zip"

    def busy(*_a, **_k):
        raise tool_bundle.DestinationBusy("held by a queued job")

    monkeypatch.setattr(tool_installer, "installable_tools", lambda: ["aircrack-ng"])
    monkeypatch.setattr(tool_installer, "spec_for", lambda _t: _Spec())
    monkeypatch.setattr(tool_installer, "install_tool", busy)
    r = c.post("/api/crack/install-tool", json={"tool": "aircrack-ng"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 409
