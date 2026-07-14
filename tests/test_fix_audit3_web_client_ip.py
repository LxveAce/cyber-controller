"""Regression guard for cc-deep-audit-3 W2 (2026-07-13): web client-IP behind a reverse proxy.

The web remote keyed its login/command RateLimiter AND its audit-trail identity on
`request.remote_addr`. Behind a reverse proxy that's the PROXY's IP, so every real client collapsed
into one rate-limit bucket and one audit identity (one client's failures rate-limit everyone; the
forensic trail can't tell clients apart). The fix resolves the real client from X-Forwarded-For —
but ONLY when the direct peer is a configured trusted proxy, because blindly trusting XFF would let
any client spoof its IP to get a fresh bucket / forge audit identity (worse than the original bug).

`_resolve_client_ip` is a pure function, tested directly for the spoofing scenarios; one integration
test proves it's actually wired into the login limiter (two XFF clients behind a trusted proxy get
INDEPENDENT rate-limit buckets).
"""
from __future__ import annotations

import base64

import pytest

app_mod = pytest.importorskip("src.ui.web.app")
from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402

_resolve = app_mod._resolve_client_ip


# ── pure resolver: proxy-aware but spoof-resistant ────────────────────────────────────────────────

def test_no_trusted_proxies_returns_remote_addr_ignoring_xff():
    """Default posture: with no trusted proxy, XFF is NEVER honored (it's client-forgeable)."""
    assert _resolve("203.0.113.7", "1.2.3.4", frozenset()) == "203.0.113.7"


def test_untrusted_peer_ignores_xff():
    """A direct (non-proxy) client cannot spoof its IP by sending its own XFF."""
    assert _resolve("203.0.113.7", "9.9.9.9", frozenset({"127.0.0.1"})) == "203.0.113.7"


def test_trusted_proxy_recovers_real_client():
    assert _resolve("127.0.0.1", "203.0.113.7", frozenset({"127.0.0.1"})) == "203.0.113.7"


def test_trusted_proxy_takes_rightmost_nontrusted_hop_defeating_client_spoof():
    """Client forges a left XFF entry; the proxy appends the REAL peer on the right. We walk from
    the right and return the real client — the forged left entry is never reached."""
    xff = "5.5.5.5, 203.0.113.7"  # "5.5.5.5" = client-forged; "203.0.113.7" = appended by the proxy
    assert _resolve("127.0.0.1", xff, frozenset({"127.0.0.1"})) == "203.0.113.7"


def test_chain_of_trusted_proxies_skips_all_trusted_hops():
    xff = "203.0.113.7, 10.0.0.2"  # client, then inner trusted proxy 10.0.0.2
    trusted = frozenset({"127.0.0.1", "10.0.0.2"})
    assert _resolve("127.0.0.1", xff, trusted) == "203.0.113.7"


def test_trusted_proxy_empty_xff_falls_back_to_proxy():
    assert _resolve("127.0.0.1", "", frozenset({"127.0.0.1"})) == "127.0.0.1"


def test_trusted_proxy_all_hops_trusted_falls_back_to_proxy():
    assert _resolve("127.0.0.1", "10.0.0.2", frozenset({"127.0.0.1", "10.0.0.2"})) == "127.0.0.1"


def test_none_remote_addr_is_unknown():
    assert _resolve(None, "", frozenset()) == "unknown"


# ── integration: the limiter is keyed on the RESOLVED client, not the shared proxy IP ─────────────

def _basic(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _client_trusting_localhost(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    # ISOLATE the persistent gate config to a throwaway file — this test drives FAILED logins, and
    # the web path calls physical_key.record_failed_attempt(), which would otherwise write a real
    # lockout into ~/.cyber-controller/access_gate.json (the machine's actual access gate) and lock
    # the owner out + poison every later web-login test. Never let a test touch the real gate.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    # Take the persistent (global) lockout out of the decision so ONLY the per-IP RateLimiter runs —
    # otherwise the shared brute-force counter trips first and masks the per-client separation.
    monkeypatch.setattr(app_mod.physical_key, "lockout_status",
                        lambda: {"locked": False, "remaining_secs": 0, "failed_attempts": 0})
    dm, fe = DeviceManager(), FlashEngine()
    bus = EventBus()
    pool = TargetPool(bus)
    # The test client's peer is 127.0.0.1 — trust it as the proxy so XFF selects the real client.
    app, _sio = app_mod.create_app(dm, fe, bus, pool, trusted_proxies=["127.0.0.1"])
    app.config.update(TESTING=True)
    return app.test_client()


def test_ratelimit_is_per_real_client_behind_trusted_proxy(monkeypatch, tmp_path):
    client = _client_trusting_localhost(monkeypatch, tmp_path)
    hdr_a = {**_basic("admin", "wrong"), "X-Forwarded-For": "198.51.100.10"}
    hdr_b = {**_basic("admin", "wrong"), "X-Forwarded-For": "198.51.100.99"}

    # Client A burns its whole login budget (8/60s): the 9th is rate-limited (429).
    for _ in range(8):
        assert client.get("/", headers=hdr_a).status_code == 401
    assert client.get("/", headers=hdr_a).status_code == 429

    # Client B — same proxy, DIFFERENT real IP — still has its own budget (would be 429 too if the
    # limiter had collapsed both onto 127.0.0.1). It gets the normal 401, not a 429.
    assert client.get("/", headers=hdr_b).status_code == 401
