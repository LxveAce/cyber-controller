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


@pytest.mark.skipif(sys.platform != "win32", reason="win_acl ctypes SID resolution is Windows-only")
def test_win_acl_ctypes_resolves_sid():
    from src.security.win_acl import _token_user_sid_ctypes
    sid = _token_user_sid_ctypes()
    # With the argtypes/restype declarations the 64-bit call chain succeeds and returns a real SID string;
    # pre-fix it truncated pointers and returned None (making restrict_to_current_user a silent no-op).
    assert sid is not None and sid.startswith("S-1-"), f"expected a resolved SID, got {sid!r}"
