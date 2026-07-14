"""Beat 270 - web non-dict JSON body -> unhandled AttributeError / 500 (cc-deep-audit-11 [1] MED).

The four older state-changing handlers parsed the body with `request.get_json(force=True,
silent=True) or {}` and then called `data.get(...)`. `or {}` only rewrites FALSY results ({}, [], 0,
'', None) to a dict; a TRUTHY non-dict JSON body -- a non-empty array `[1,2]`, a non-zero number, a
non-empty string, or `true` -- passes through unchanged, so `data.get("port", "")` raises
`AttributeError: 'list' object has no attribute 'get'`, outside any try/except -> an unhandled HTTP
500 instead of a clean 4xx. The codebase already had `_json_body()` (isinstance-coerces to {}) but
applied it only to the /api/nodes/* routes; api_flash/api_connect/api_disconnect/api_command were
never converted, and `requires_csrf` repeated the bug when it read the token from the body.

Fix: route every request-body read through `_json_body()`.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_nondict_body_with_valid_csrf_is_not_500 (parametrized over the 4 routes): a truthy non-dict
    body with a valid CSRF header reaches the handler; HEAD 500s on `.get(...)`, the fix coerces it.
  - test_nondict_body_without_csrf_header_is_403_not_500: a non-dict body + no CSRF header hits the
    requires_csrf body branch; HEAD 500s on `[1].get("_csrf")`, the fix returns a clean 403.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    # Isolate the access gate to a temp file so a test never locks/rate-limits the real gate.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return client


@pytest.mark.parametrize("path", ["/api/command", "/api/flash", "/api/connect", "/api/disconnect"])
def test_nondict_body_with_valid_csrf_is_not_500(path):
    """Handler path: a truthy non-dict JSON body must be coerced to {}, not crash into a 500."""
    resp = _client().post(path, json=[1, 2], headers={"X-CSRF-Token": "tok"})
    assert resp.status_code != 500, f"{path}: a non-dict body must be coerced, not 500 on .get()"


def test_nondict_body_without_csrf_header_is_403_not_500():
    """CSRF path: a non-dict body with no CSRF header must be a clean 403, not a 500."""
    resp = _client().post("/api/command", json=[1])   # no X-CSRF-Token header, bare list body
    assert resp.status_code == 403, "requires_csrf must reject a non-dict body as 403, not 500"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
