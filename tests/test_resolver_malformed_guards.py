"""A malformed firmware profile must fail HONESTLY, not crash with an opaque AttributeError on
the Flash path. Regression for the two crashes the flash-matrix audit surfaced (2026-07-15):
  - sniffle: resolver_params.asset_match is a bare template string, not an object.
  - zstack_coordinator: api_url points at /releases (a JSON list) not /releases/latest.
Pre-guard both raised `AttributeError: '...' object has no attribute 'get'`; now they raise a
descriptive ValueError so the cause is obvious instead of a stack trace.
"""
import json

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


def test_resolve_github_rejects_non_dict_asset_match():
    # sniffle-class: asset_match is a bare "sniffle_<chip>.hex" string, not an object. The guard
    # fires before any network call, so no monkeypatch is needed.
    cfg = {"resolver_params": {"api_url": "https://x/releases/latest",
                               "asset_match": "sniffle_<chip>.hex"}}
    with pytest.raises(ValueError, match="asset_match"):
        flash_core._resolve_github(cfg)


def test_github_latest_rejects_list_response(monkeypatch):
    # zstack-class: /releases (plural) returns a JSON list, not a single release object.
    monkeypatch.setattr(flash_core, "_http_get",
                        lambda url: json.dumps([{"tag_name": "x"}]).encode())
    with pytest.raises(ValueError, match="releases"):
        flash_core._github_latest("https://x/releases")


def test_github_latest_still_resolves_a_single_object(monkeypatch):
    # Guard must not over-reject: a well-formed single release object still resolves.
    payload = {"tag_name": "v1", "assets": [{"name": "a.bin"}]}
    monkeypatch.setattr(flash_core, "_http_get", lambda url: json.dumps(payload).encode())
    tag, assets = flash_core._github_latest("https://x/releases/latest")
    assert tag == "v1"
    assert assets == [{"name": "a.bin"}]
