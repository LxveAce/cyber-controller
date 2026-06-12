"""SHA-256 pinning for third-party firmware bundles (the BW16/RTL8720 Vampire bundle has no
upstream signature). verify_sha256 must pass on a match and reject a mismatch BEFORE flashing,
and the rtl8720 profile must pin every bundle file."""
from __future__ import annotations

import hashlib

import pytest

from src.core import flash_core


def test_verify_sha256_pass(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello-firmware")
    # Should not raise on a matching hash.
    flash_core.verify_sha256(str(p), hashlib.sha256(b"hello-firmware").hexdigest(), lambda s: None)


def test_verify_sha256_mismatch_raises(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello-firmware")
    with pytest.raises(ValueError):
        flash_core.verify_sha256(str(p), "0" * 64, lambda s: None)


def test_rtl8720_profile_pins_every_bundle_file():
    core = flash_core.get_profile("rtl8720")
    _tag, assets = core.latest_release()
    assert assets, "rtl8720 bundle should have assets"
    for a in assets:
        assert a.get("sha256") and len(a["sha256"]) == 64, f"{a['name']} not pinned"
