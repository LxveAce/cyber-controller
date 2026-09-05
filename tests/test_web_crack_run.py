"""B6 — POST /api/crack/run: the consent-gated native crack, streamed. The consent gate is the safety
boundary, so it gets the most tests; one end-to-end test cracks a real inline PMKID over a wordlist."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("flask")

from src.core import native_crack as nc
from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.capture import CaptureRecord
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _app(store):
    return create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), capture_store=store)


def _http(app):
    c = app.test_client()
    token = new_csrf_token()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = c.application.extensions["cc_web_credentials"].generation
        sess["csrf"] = token
    return c, token


def _pmkid_line(pw: str, essid: str = "netbyte") -> str:
    ap = bytes.fromhex("fc690c158264")
    sta = bytes.fromhex("f4747f87f9f4")
    pmkid = nc.compute_pmkid(nc.pmk(pw, essid), ap, sta)
    return f"WPA*01*{pmkid.hex()}*{ap.hex()}*{sta.hex()}*{essid.encode().hex()}***"


def _store_with_pmkid(pw: str):
    store = CaptureStore()
    store.add(CaptureRecord(bssid="fc:69:0c:15:82:64", ssid="netbyte", capture_type="pmkid",
                            device_source="COM4", hc22000_line=_pmkid_line(pw)))
    return store


# ── the consent gate (safety boundary) ───────────────────────────────────────
def test_run_refused_without_consent(tmp_path):
    store = _store_with_pmkid("password1")
    c, token = _http(_app(store)[0])
    key = store.all()[0].key
    wl = tmp_path / "w.txt"; wl.write_text("password1\n", encoding="utf-8")
    for body in ({"capture_key": key, "wordlist": str(wl)},               # consent absent
                 {"consent": False, "capture_key": key, "wordlist": str(wl)},
                 {"consent": "true", "capture_key": key, "wordlist": str(wl)}):  # string, not bool
        r = c.post("/api/crack/run", json=body, headers={"X-CSRF-Token": token})
        assert r.status_code == 403, body
        assert "consent" in r.get_json()["error"].lower()


def test_run_requires_csrf(tmp_path):
    store = _store_with_pmkid("password1")
    c, _t = _http(_app(store)[0])
    r = c.post("/api/crack/run", json={"consent": True, "capture_key": "x", "wordlist": "y"})
    assert r.status_code in (400, 403)


def test_run_rejects_unknown_capture_and_bad_wordlist(tmp_path):
    store = _store_with_pmkid("password1")
    c, token = _http(_app(store)[0])
    key = store.all()[0].key
    r1 = c.post("/api/crack/run", json={"consent": True, "capture_key": "nope", "wordlist": "x"},
                headers={"X-CSRF-Token": token})
    assert r1.status_code == 400
    r2 = c.post("/api/crack/run", json={"consent": True, "capture_key": key, "wordlist": str(tmp_path / "missing.txt")},
                headers={"X-CSRF-Token": token})
    assert r2.status_code == 400


def test_run_rejects_uncrackable_capture(tmp_path):
    store = CaptureStore()
    store.add(CaptureRecord(bssid="11:22:33:44:55:66", ssid="OnDevice", capture_type="eapol",
                            pcap_path="/sd/not-local.pcap"))   # no local file, no inline line
    c, token = _http(_app(store)[0])
    key = store.all()[0].key
    wl = tmp_path / "w.txt"; wl.write_text("x\n", encoding="utf-8")
    r = c.post("/api/crack/run", json={"consent": True, "capture_key": key, "wordlist": str(wl)},
               headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    assert "no local" in r.get_json()["error"]


def test_stop_endpoint():
    c, token = _http(_app(CaptureStore())[0])
    r = c.post("/api/crack/stop", json={}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    assert r.get_json()["stopping"] is True


# ── end-to-end: a real PMKID cracks over the wordlist, streamed to crack_done ──
def test_run_cracks_real_pmkid_end_to_end(tmp_path):
    pw = "password1"
    store = _store_with_pmkid(pw)
    app, socketio = _app(store)
    c, token = _http(app)
    key = store.all()[0].key
    wl = tmp_path / "w.txt"; wl.write_text("nope1234\n" + pw + "\n", encoding="utf-8")

    sio = socketio.test_client(app, flask_test_client=c, auth={"csrf": token})
    assert sio.is_connected()

    r = c.post("/api/crack/run", json={"consent": True, "capture_key": key, "wordlist": str(wl)},
               headers={"X-CSRF-Token": token})
    assert r.status_code == 202 and r.get_json()["started"] is True

    done = None
    deadline = time.time() + 15
    while time.time() < deadline and done is None:
        for ev in sio.get_received():
            if ev["name"] == "crack_done":
                done = ev["args"][0]
        if done is None:
            time.sleep(0.2)
    assert done is not None, "no crack_done event"
    assert done["cracked"] is True
    assert done["password"] == pw
    # the store record was flipped to cracked in place
    assert store.get(key).crack_status == "cracked"
    assert store.get(key).password == pw
    sio.disconnect()
