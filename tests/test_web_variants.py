"""Web /api/variants — the read-only backend for the flash-page Board/Variant picker (task #37).

The picker POSTs its chosen asset name straight back as /api/flash's ``variant`` field (added beat
171, b02c70a). This endpoint feeds the dropdown: given a profile name it returns the selectable
release variants the engine would resolve, WITHOUT touching hardware or a live GitHub fetch in the
test (``list_variants`` is monkeypatched — the real one is network-backed and non-fatal on failure).

Covers: a valid profile lists its variants (name/label/chip mapped), missing ``profile`` -> 400,
unknown profile -> 404, an offline/empty release -> 200 with an empty list (so the UI falls back to
"Default (auto-detect)" and the flash uses the per-chip default, i.e. unchanged pre-picker behavior),
and that the route is auth-gated.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web import app as web_app
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _a_real_profile_name() -> str:
    """A firmware profile that actually ships, so the endpoint clears its profiles.get() gate."""
    names = list(web_app._load_profiles().keys())
    assert names, "no shipped firmware profiles found"
    return names[0]


def _client(engine=None, authed=True):
    app, _sio = create_app(DeviceManager(), engine or FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    if authed:
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "tok"
    return client


def test_variants_lists_engine_resolved_builds(monkeypatch):
    # Stub the network-backed resolver so the test is hermetic (real list_variants hits GitHub).
    fake = [
        {"name": "Bruce-m5stack-core4mb.bin", "label": "M5Stack Core (4MB)", "chip": "esp32"},
        {"name": "Bruce-cyd-2432S028.bin", "label": "CYD 2432S028", "chip": "esp32", "url": "x"},
    ]
    monkeypatch.setattr(FlashEngine, "list_variants", lambda self, profile, chip=None: fake)

    resp = _client().get("/api/variants?profile=" + _a_real_profile_name())
    assert resp.status_code == 200
    variants = resp.get_json()["variants"]
    assert [v["name"] for v in variants] == [f["name"] for f in fake]
    # Only name/label/chip are surfaced (no internal url), exactly what the dropdown needs.
    assert variants[0] == {"name": "Bruce-m5stack-core4mb.bin", "label": "M5Stack Core (4MB)",
                           "chip": "esp32"}
    assert "url" not in variants[1]


def test_variants_missing_profile_is_400():
    resp = _client().get("/api/variants")
    assert resp.status_code == 400
    assert "profile is required" in resp.get_json()["error"]


def test_variants_unknown_profile_is_404():
    resp = _client().get("/api/variants?profile=no-such-firmware-xyz")
    assert resp.status_code == 404
    assert "Unknown profile" in resp.get_json()["error"]


def test_variants_offline_release_yields_empty_list(monkeypatch):
    # The real list_variants() returns [] when the release can't be fetched (offline). The endpoint
    # must pass that through as a clean empty list, NOT a 500 — the picker then shows only "Default".
    monkeypatch.setattr(FlashEngine, "list_variants", lambda self, profile, chip=None: [])
    resp = _client().get("/api/variants?profile=" + _a_real_profile_name())
    assert resp.status_code == 200
    assert resp.get_json()["variants"] == []


def test_variants_requires_auth():
    resp = _client(authed=False).get("/api/variants?profile=" + _a_real_profile_name())
    assert resp.status_code in (401, 403)
