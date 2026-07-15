"""Offline unit tests for scripts/audit_profile_assets.py — no network, no heavy CC imports.

The live resolvers are faked via sys.modules so we test the audit's VERDICT LOGIC in isolation:
the mapping from a resolver result to OK / SOURCE-ONLY / BROKEN / ERROR / OS-IMAGE / LOCAL, and
especially the guard that a rate-limit HTTPError becomes ERROR (retry), never a false SOURCE-ONLY.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import urllib.error
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit_profile_assets.py"
_spec = importlib.util.spec_from_file_location("audit_profile_assets", _SCRIPT)
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)


def _fake_flash_core(monkeypatch, *, github=None, pinned=None):
    """Inject a fake core.flash_core so check_reachability's lazy import picks it up."""
    fake = types.SimpleNamespace()
    fake._resolve_github = github or (lambda cfg: ("v1", [{"name": "a.bin", "url": "u"}]))
    fake._resolve_pinned = pinned or (lambda cfg: ("pinned", [{"name": "a.bin", "url": "u"}]))
    monkeypatch.setitem(sys.modules, "src.core.flash_core", fake)


def test_classify_is_offline_and_flags_esptool():
    row = audit_mod.classify({"id": "x", "name": "X", "backend": "esptool",
                              "resolver": "github_release"})
    assert row["esptool"] is True
    assert row["method"].startswith("esptool")
    alt = audit_mod.classify({"id": "f", "name": "F", "backend": "qflipper",
                              "resolver": "github_release"})
    assert alt["esptool"] is False
    assert "qFlipper" in alt["method"]  # non-esptool labeled with its real method


def test_github_with_assets_is_ok(monkeypatch):
    _fake_flash_core(monkeypatch, github=lambda cfg: ("v3.7", [{"name": "fw.bin", "url": "u"}]))
    verdict, _ = audit_mod.check_reachability({"resolver": "github_release"})
    assert verdict == "OK"


def test_github_source_only_is_flagged(monkeypatch):
    # HaleHound class: resolver returns ("source-only", []) when upstream has no matching asset.
    _fake_flash_core(monkeypatch, github=lambda cfg: ("source-only", []))
    verdict, note = audit_mod.check_reachability({"resolver": "github_release"})
    assert verdict == "SOURCE-ONLY"
    assert "dead Flash button" in note


def test_github_empty_assets_is_flagged(monkeypatch):
    _fake_flash_core(monkeypatch, github=lambda cfg: ("v9", []))
    verdict, _ = audit_mod.check_reachability({"resolver": "github_release"})
    assert verdict == "SOURCE-ONLY"


def test_rate_limit_is_error_not_source_only(monkeypatch):
    # The critical guard: a 403 rate-limit must NOT masquerade as a real SOURCE-ONLY finding.
    def boom(cfg):
        raise urllib.error.HTTPError("url", 403, "rate limited", {}, None)
    _fake_flash_core(monkeypatch, github=boom)
    verdict, note = audit_mod.check_reachability({"resolver": "github_release"})
    assert verdict == "ERROR"
    assert "403" in note


def test_pinned_ok_when_head_200(monkeypatch):
    _fake_flash_core(monkeypatch, pinned=lambda cfg: ("t", [{"name": "a.bin", "url": "u"}]))
    monkeypatch.setattr(audit_mod, "_head", lambda url, timeout=15.0: 200)
    verdict, _ = audit_mod.check_reachability({"resolver": "pinned_release"})
    assert verdict == "OK"


def test_pinned_broken_when_head_404(monkeypatch):
    _fake_flash_core(monkeypatch, pinned=lambda cfg: ("t", [{"name": "a.bin", "url": "u"}]))
    monkeypatch.setattr(audit_mod, "_head", lambda url, timeout=15.0: 404)
    verdict, note = audit_mod.check_reachability({"resolver": "pinned_release"})
    assert verdict == "BROKEN"
    assert "404" in note


@pytest.mark.parametrize("resolver,expected", [(None, "OS-IMAGE"), ("local", "LOCAL")])
def test_non_network_resolvers(resolver, expected):
    verdict, _ = audit_mod.check_reachability({"resolver": resolver})
    assert verdict == expected


def test_all_real_profiles_classify_offline():
    # Every shipped profile must classify without error (offline path, no network).
    rows = audit_mod.audit(only=None, offline=True)
    assert len(rows) >= 40
    assert all(r["verdict"] == "SKIPPED" for r in rows)


# --------------------------------------------------------------------------- schema mismatch

def test_schema_mismatch_flags_per_board_assets_array():
    # whad_butterfly class: a top-level per-board `assets` array under github_release.
    reason = audit_mod.schema_mismatch(
        {"resolver": "github_release", "resolver_params": {"api_url": "u", "assets": [{}]}})
    assert reason and "assets" in reason


def test_schema_mismatch_flags_string_asset_match():
    # sniffle class: asset_match as a bare template string.
    reason = audit_mod.schema_mismatch(
        {"resolver": "github_release",
         "resolver_params": {"api_url": "u/releases/latest", "asset_match": "sniffle_<chip>.hex"}})
    assert reason and "asset_match" in reason


def test_schema_mismatch_flags_releases_list_url():
    # zstack class: api_url points at /releases (a list).
    reason = audit_mod.schema_mismatch(
        {"resolver": "github_release", "resolver_params": {"api_url": "https://x/repos/y/releases"}})
    assert reason and "/releases" in reason


def test_schema_mismatch_none_for_well_formed():
    # A normal github_release profile (dict asset_match, /releases/latest) is NOT a mismatch.
    ok = {"resolver": "github_release",
          "resolver_params": {"api_url": "u/releases/latest",
                              "asset_match": {"include_suffixes": [".bin"]}}}
    assert audit_mod.schema_mismatch(ok) is None
    assert audit_mod.schema_mismatch({"resolver": "pinned_release"}) is None


def test_check_reachability_returns_schema_mismatch_without_network():
    # A mismatched profile is caught statically -> no resolver call, no network.
    verdict, _ = audit_mod.check_reachability(
        {"resolver": "github_release", "resolver_params": {"api_url": "u", "assets": [{}]}})
    assert verdict == "SCHEMA-MISMATCH"
