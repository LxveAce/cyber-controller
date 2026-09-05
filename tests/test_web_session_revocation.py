"""End-to-end web session revocation on password change / clear / restart.

Exercises REAL Flask HTTP clients and REAL Socket.IO test clients against a live ``create_app`` server:
changing or clearing the web password rotates the credential generation, which revokes every OTHER
cookie (clean 401, no lockout increment) and actively disconnects every OTHER open socket (stopping a
passive serial fan-out and any host PTY), while the acting operator stays signed in. A restart (a fresh
process => a fresh generation) likewise revokes prior cookies. A failed persist revokes nothing.

All hardware / host-shell child processes are mocked; nothing touches the real ~/.cyber-controller."""

from __future__ import annotations

import base64
import threading

import pytest

pytest.importorskip("flask")

from flask import session  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.security import physical_key as pk  # noqa: E402
from src.security import web_auth  # noqa: E402
from src.ui.web import app as webapp  # noqa: E402

PASS = "test-pass-123"
NEWPASS = "new-pass-456"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    # Isolate the shared physical-key lockout AND every web_auth path to tmp — a change/clear in these
    # tests must never write the real password file or secret key.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", PASS)
    monkeypatch.delenv("CC_WEB_HOST_SHELL", raising=False)
    monkeypatch.delenv("CC_WEB_ALLOW_LAN", raising=False)
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path, raising=True)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json", raising=True)
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key", raising=True)


def _basic(user: str, pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _make(host_shell: bool = False):
    return webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                             host_shell_loopback=host_shell)


def _login(app, user: str = "admin", pw: str = PASS):
    """Log a fresh HTTP client in via Basic auth; return (client, csrf, cred_gen)."""
    c = app.test_client()
    r = c.get("/api/health", headers=_basic(user, pw))
    assert r.status_code == 200, r.status_code
    with c.session_transaction() as s:
        return c, s["csrf"], s["cred_gen"]


def _change(c, csrf: str, new_pw: str = NEWPASS, username: str = "admin"):
    return c.post("/api/web-password",
                  json={"new_password": new_pw, "username": username},
                  headers={"X-CSRF-Token": csrf})


def _clear(c, csrf: str):
    return c.post("/api/web-password", json={"reset": True}, headers={"X-CSRF-Token": csrf})


def _live_gen(app) -> str:
    return app.extensions["cc_web_credentials"].generation


# ── HTTP cookie revocation ────────────────────────────────────────────────────────────────────────

def test_change_revokes_other_session_401_and_no_lockout_bump():
    app, _sio = _make()
    actor, actor_csrf, gen0 = _login(app)
    victim, _v_csrf, victim_gen = _login(app)
    assert victim_gen == gen0                      # both at the same generation before any change
    assert victim.get("/api/health").status_code == 200

    before = pk.lockout_status()["failed_attempts"]
    assert _change(actor, actor_csrf).status_code == 200

    # The victim's cookie (old generation) is now rejected with a clean 401 …
    assert victim.get("/api/health").status_code == 401
    # … and a cookie-only (no Authorization header) retry must NOT drive the shared lockout counter.
    assert pk.lockout_status()["failed_attempts"] == before


def test_acting_operator_keeps_working_session_after_change():
    app, _sio = _make()
    actor, actor_csrf, _gen = _login(app)
    assert _change(actor, actor_csrf).status_code == 200
    # The change response re-stamped the acting cookie with the new generation.
    assert actor.get("/api/health").status_code == 200
    with actor.session_transaction() as s:
        assert s["cred_gen"] == _live_gen(app)


def test_reauth_after_revocation_issues_fresh_generation():
    app, _sio = _make()
    actor, actor_csrf, _gen = _login(app)
    victim, _c, _g = _login(app)
    assert _change(actor, actor_csrf).status_code == 200
    assert victim.get("/api/health").status_code == 401           # revoked
    # Re-login with the NEW password succeeds and stamps the current generation.
    assert victim.get("/api/health", headers=_basic("admin", NEWPASS)).status_code == 200
    with victim.session_transaction() as s:
        assert s["cred_gen"] == _live_gen(app)


def test_clear_revokes_others_but_live_password_still_verifies():
    # A SAVED password on disk, so the clear actually removes a file (the note that documents the
    # "current password stays in effect until restart" semantic is the removed==True branch).
    web_auth.save_web_password("admin", PASS)
    app, _sio = _make()
    actor, actor_csrf, _gen = _login(app)
    victim, _c, _g = _login(app)
    r = _clear(actor, actor_csrf)
    assert r.status_code == 200 and r.get_json()["reset"] is True
    # Other session revoked …
    assert victim.get("/api/health").status_code == 401
    # … acting session stays …
    assert actor.get("/api/health").status_code == 200
    # … and the RUNNING password still verifies until restart (clear preserves it; reverts on next start).
    assert victim.get("/api/health", headers=_basic("admin", PASS)).status_code == 200
    assert "stays in effect until you restart" in r.get_json()["note"]


def test_desktop_bootstrap_session_is_stamped_and_revoked_on_change():
    app, _sio = webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                                  desktop_token="boot-tok")
    desk = app.test_client()
    assert desk.get("/desktop-auth?token=boot-tok").status_code == 302
    with desk.session_transaction() as s:
        assert s["authenticated"] is True and s["cred_gen"] == _live_gen(app)
    assert desk.get("/api/health").status_code == 200
    # A password change (by a separately-authed operator) revokes the bootstrap session too.
    actor, actor_csrf, _gen = _login(app)
    assert _change(actor, actor_csrf).status_code == 200
    assert desk.get("/api/health").status_code == 401


def test_later_change_wins_earlier_actor_goes_stale():
    app, _sio = _make()
    a1, csrf1, _g1 = _login(app)
    a2, csrf2, _g2 = _login(app)
    assert _change(a1, csrf1, new_pw="first-pass-1").status_code == 200
    assert a1.get("/api/health").status_code == 200          # a1 is the current actor now
    # a2 re-authenticates with a1's new password, then makes a genuinely-LATER change.
    assert a2.get("/api/health", headers=_basic("admin", "first-pass-1")).status_code == 200
    with a2.session_transaction() as s:
        csrf2 = s["csrf"]
    assert _change(a2, csrf2, new_pw="second-pass-2").status_code == 200
    # The later change supersedes the generation: the earlier actor (a1) is now stale.
    assert a1.get("/api/health").status_code == 401
    assert a2.get("/api/health").status_code == 200


def test_failed_persist_revokes_nothing(monkeypatch):
    app, _sio = _make()
    actor, actor_csrf, _gen = _login(app)
    victim, _c, _g = _login(app)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(web_auth, "_write_record", boom)
    r = _change(actor, actor_csrf)
    assert r.status_code == 500
    # A change that did not commit revokes no session — both operators keep working.
    assert actor.get("/api/health").status_code == 200
    assert victim.get("/api/health").status_code == 200


def test_restart_new_generation_revokes_prior_cookie():
    """A restart is a fresh process with a fresh (non-persisted) generation, so a cookie from the prior
    process is rejected. Modelled by rotating the live generation out from under an existing cookie —
    exactly what a new process does — via the in-process credential object."""
    app, _sio = _make()
    victim, _c, gen0 = _login(app)
    assert victim.get("/api/health").status_code == 200
    # Simulate the fresh-process generation (a restart mints a new one; not persisted).
    new_gen = app.extensions["cc_web_credentials"].rotate_generation()
    assert new_gen != gen0
    assert victim.get("/api/health").status_code == 401
    # Re-login re-stamps the current generation.
    assert victim.get("/api/health", headers=_basic("admin", PASS)).status_code == 200


# ── per-event socket enforcement (direct handler invocation) ────────────────────────────────────────

def _capture_socket_handlers(monkeypatch) -> dict:
    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


def test_stale_generation_socket_events_are_noops(monkeypatch):
    emitted: list[dict] = []
    monkeypatch.setattr(webapp, "emit", lambda _e, payload=None, **k: emitted.append(payload or {}))
    captured = _capture_socket_handlers(monkeypatch)
    app, _sio = _make()
    subscribe = captured["subscribe_serial"]
    send = captured["send_command"]

    with app.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        # An authenticated session but with a WRONG (rotated-away) generation is not authed: every
        # per-event handler returns before doing anything (no [Subscribed]/[Unknown port] feedback).
        session["authenticated"] = True
        session["cred_gen"] = "some-old-generation-token"
        subscribe({"port": "COM3"})
        send({"port": "COM3", "command": "scan"})
    assert emitted == [], "a stale-generation socket event must be a silent no-op"


# ── live Socket.IO clients: disconnect on rotation, refuse stale connect ────────────────────────────

def test_open_socket_disconnected_on_change_and_stale_connect_refused(monkeypatch):
    app, sio = _make()
    actor, actor_csrf, _gen = _login(app)
    victim, victim_csrf, _vg = _login(app)

    disconnected: list = []
    orig = sio.server.disconnect

    def spy(sid, namespace=None, **k):
        disconnected.append(sid)
        return orig(sid, namespace=namespace, **k)

    monkeypatch.setattr(sio.server, "disconnect", spy)

    vsock = sio.test_client(app, flask_test_client=victim, auth={"csrf": victim_csrf})
    assert vsock.is_connected()

    assert _change(actor, actor_csrf).status_code == 200
    # The rotation actively disconnected the victim's open socket (stops passive fan-out / host PTY).
    assert disconnected, "a rotation must disconnect the tracked stale socket"
    assert not vsock.is_connected()

    # A fresh connect with the victim's now-stale cookie is refused at the connect handler.
    stale = sio.test_client(app, flask_test_client=victim, auth={"csrf": victim_csrf})
    assert not stale.is_connected()


def test_current_generation_socket_connect_accepted():
    app, sio = _make()
    actor, actor_csrf, _gen = _login(app)
    sock = sio.test_client(app, flask_test_client=actor, auth={"csrf": actor_csrf})
    assert sock.is_connected()
    sock.disconnect()


# ── unified disconnect handler: sid de-registration + host-PTY teardown ─────────────────────────────

class _FakeShell:
    instances: list = []

    def __init__(self, emit_cb):
        self.killed = False
        _FakeShell.instances.append(self)

    def start(self):
        pass

    def kill(self):
        self.killed = True


def test_unified_disconnect_tears_down_host_shell_when_enabled(monkeypatch):
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    _FakeShell.instances = []
    monkeypatch.setattr(webapp.host_shell, "HostShellSession", _FakeShell)
    app, sio = _make(host_shell=True)
    actor, actor_csrf, _gen = _login(app)
    sock = sio.test_client(app, flask_test_client=actor, auth={"csrf": actor_csrf})
    assert sock.is_connected()
    sock.emit("host_shell_open", {})
    sock.get_received()  # pump
    assert _FakeShell.instances, "host shell should have been opened"
    sock.disconnect()
    # The single unified disconnect handler tore the PTY down (previously only the host-shell-gated
    # handler did this; it must still fire, and also for the sid-tracking path).
    assert _FakeShell.instances[-1].killed is True


def test_disconnect_deregisters_sid_no_double_disconnect(monkeypatch):
    app, sio = _make()
    actor, actor_csrf, _gen = _login(app)
    sock = sio.test_client(app, flask_test_client=actor, auth={"csrf": actor_csrf})
    assert sock.is_connected()
    sock.disconnect()

    disconnected: list = []
    orig = sio.server.disconnect

    def spy(sid, namespace=None, **k):
        disconnected.append(sid)
        return orig(sid, namespace=namespace, **k)

    monkeypatch.setattr(sio.server, "disconnect", spy)
    # The sid was de-registered on disconnect, so a later rotation has nothing to disconnect for it.
    assert _change(actor, actor_csrf).status_code == 200
    assert disconnected == [], "a disconnected socket must not be swept again on rotation"


# ── deterministic ordering regressions (finding #1): sweep against the LIVE generation, not the caller's ──

def test_delayed_sweep_uses_live_generation_not_caller_captured(monkeypatch):
    """Deterministic interleave with events (no sleeps): operator A publishes generation A and PAUSES before
    its revoke sweep; operator B then publishes generation B and connects a B-generation socket; A resumes
    and sweeps. The sweep must read the CURRENT live generation (B) so B's socket SURVIVES — not A's stale
    captured generation, which would wrongly disconnect the newer session's socket. Fails on the pre-fix
    caller-captured-generation code; passes once the sweep reads creds.generation."""
    app, sio = _make()
    a_published = threading.Event()
    b_finished = threading.Event()

    real_apply = web_auth.apply_web_password

    def apply_with_pause(creds, new_pw, username):
        gen = real_apply(creds, new_pw, username)     # publishes + releases the writer lock
        if new_pw == "first-pass-A":
            a_published.set()                          # A committed gen A (live == A); handler not yet swept
            assert b_finished.wait(timeout=10)         # hold A BEFORE its sweep until B is fully done
        return gen

    monkeypatch.setattr(web_auth, "apply_web_password", apply_with_pause)

    actor_a, csrf_a, _ = _login(app)
    a_result: dict = {}

    def do_a_change():
        a_result["status"] = _change(actor_a, csrf_a, new_pw="first-pass-A").status_code

    ta = threading.Thread(target=do_a_change)
    ta.start()
    assert a_published.wait(timeout=10)                # A is paused after publishing, before its sweep

    # B re-authenticates with A's new password, makes a genuinely-LATER change (gen B), connects a B socket.
    actor_b = app.test_client()
    assert actor_b.get("/api/health", headers=_basic("admin", "first-pass-A")).status_code == 200
    with actor_b.session_transaction() as s:
        csrf_b = s["csrf"]
    assert _change(actor_b, csrf_b, new_pw="second-pass-B").status_code == 200
    bsock = sio.test_client(app, flask_test_client=actor_b, auth={"csrf": csrf_b})
    assert bsock.is_connected()

    b_finished.set()                                   # release A: its sweep now runs with live gen == B
    ta.join(timeout=10)
    assert a_result.get("status") == 200

    # The newer session's socket survived A's delayed sweep (the crux of finding #1).
    assert bsock.is_connected(), "a delayed earlier sweep must not disconnect a newer generation's socket"


def test_old_cookie_rejected_during_change_before_sweep_runs(monkeypatch):
    """Verify-during-change: the generation moves at PUBLISH time inside apply_web_password, so the instant a
    change commits — even before its socket sweep runs — an old-generation cookie is already rejected by the
    per-request auth check. Deterministic via an event that holds the actor between publish and its sweep."""
    app, _sio = _make()
    published = threading.Event()
    release = threading.Event()

    real_apply = web_auth.apply_web_password

    def apply_then_pause(creds, new_pw, username):
        gen = real_apply(creds, new_pw, username)
        published.set()
        assert release.wait(timeout=10)
        return gen

    monkeypatch.setattr(web_auth, "apply_web_password", apply_then_pause)

    actor, actor_csrf, _ = _login(app)
    victim, _c, _g = _login(app)
    t = threading.Thread(target=lambda: _change(actor, actor_csrf))
    t.start()
    try:
        assert published.wait(timeout=10)
        # Published (live generation moved) but the sweep has NOT run yet: the victim cookie is already stale.
        assert victim.get("/api/health").status_code == 401
    finally:
        release.set()
        t.join(timeout=10)


def test_connect_with_cookie_rotated_away_is_refused(monkeypatch):
    """Connect-vs-rotation: a socket whose cookie generation was rotated away before the handshake registers
    must be refused — the connect handler re-checks the LIVE generation under the sids lock, never the value
    the cookie was minted with."""
    app, sio = _make()
    victim, victim_csrf, _ = _login(app)
    # A rotation (another operator's change) advances the live generation before this cookie's socket connects.
    app.extensions["cc_web_credentials"].rotate_generation()
    stale = sio.test_client(app, flask_test_client=victim, auth={"csrf": victim_csrf})
    assert not stale.is_connected(), "a connect carrying a rotated-away generation must be refused"
