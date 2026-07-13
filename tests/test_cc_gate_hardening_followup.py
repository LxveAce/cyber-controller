"""Follow-up hardening from the 2026-07-06 CC-GATE double-check (ledger CC-GATE / W-1).

Two fixes, both fail-closed / defensive:
  * physical_key.load_config: a config that is valid JSON but NOT an object ([], null, 123, "x") is now
    treated as corrupt (fails closed) instead of raising AttributeError on cfg.setdefault — which would have
    crashed the --clear-gate corrupt-config recovery path Opus shipped.
  * win_acl._token_user_sid_ctypes: the ctypes fallback now declares Win32 argtypes/restype, so on 64-bit
    Windows it actually resolves the token SID (was truncating handles/pointers -> None -> restrict_to_
    current_user() silently no-op'd, leaving secrets on the inherited ACL in frozen builds). W-1.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from src.security import physical_key as pk


@pytest.mark.parametrize("body", ["[]", "null", "123", '"a string"', "true"])
def test_valid_json_but_not_object_is_corrupt(body, tmp_path, monkeypatch):
    cfgfile = tmp_path / "access_gate.json"
    cfgfile.write_text(body, encoding="utf-8")
    monkeypatch.setattr(pk, "_config_path", lambda: pathlib.Path(cfgfile))
    cfg = pk.load_config()  # must not raise
    assert cfg.get("__corrupt__") is True
    assert pk.config_is_corrupt() is True
    assert pk.is_configured() is False  # corrupt -> fails closed, never "unconfigured no-op"


def test_record_failed_attempt_does_not_launder_a_corrupt_config(tmp_path, monkeypatch):
    """SEC-C2 (fail-closed bypass): record_failed_attempt is reachable PRE-AUTH from the web remote on
    any wrong credential. When the gate config is present-but-unreadable (the __corrupt__ fail-closed
    sentinel), the failed-attempt counter write must NOT rewrite it — save_config strips __corrupt__, so
    a write would launder the corrupt file into a clean NO-FACTOR default and silently destroy the
    fail-closed state (a configured gate becomes a no-op, or a vault-present gate becomes a permanent
    data-loss lockout). The counter write must be a no-op on a corrupt config."""
    cfgfile = tmp_path / "access_gate.json"
    monkeypatch.setattr(pk, "_config_path", lambda: pathlib.Path(cfgfile))
    pk.set_admin_password("hunter2")                 # a real, configured gate
    assert pk.is_configured() is True
    cfgfile.write_text("{ this is not valid json", encoding="utf-8")  # becomes unreadable
    assert pk.config_is_corrupt() is True

    pk.record_failed_attempt(allow_wipe=False)       # pre-auth wrong-credential path (web remote)

    assert pk.config_is_corrupt() is True            # NOT laundered — still fails closed
    assert pk.is_configured() is False
    assert "__corrupt__" not in cfgfile.read_text(encoding="utf-8")  # sentinel never persisted


def test_owner_can_still_recover_a_corrupt_config_by_reconfiguring(tmp_path, monkeypatch):
    """The skip-if-corrupt guard is scoped to the COUNTER writers only. An authoritative reconfigure
    (set_admin_password) must still overwrite a corrupt config so the owner retains an in-app recovery
    path — otherwise a corrupt config could never be fixed without manual file deletion."""
    cfgfile = tmp_path / "access_gate.json"
    monkeypatch.setattr(pk, "_config_path", lambda: pathlib.Path(cfgfile))
    cfgfile.write_text("{ corrupt", encoding="utf-8")
    assert pk.config_is_corrupt() is True

    pk.set_admin_password("recovered")               # owner recovery — allowed to clobber a corrupt config

    assert pk.config_is_corrupt() is False
    assert pk.is_configured() is True
    assert pk.verify_admin_password("recovered") is True


@pytest.mark.skipif(sys.platform != "win32", reason="win_acl ctypes SID resolution is Windows-only")
def test_win_acl_ctypes_resolves_sid():
    from src.security.win_acl import _token_user_sid_ctypes
    sid = _token_user_sid_ctypes()
    # With the argtypes/restype declarations the 64-bit call chain succeeds and returns a real SID string;
    # pre-fix it truncated pointers and returned None (making restrict_to_current_user a silent no-op).
    assert sid is not None and sid.startswith("S-1-"), f"expected a resolved SID, got {sid!r}"
