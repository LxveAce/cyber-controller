"""Tests for the physical-key access gate (src/security/physical_key.py)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def pk(tmp_path, monkeypatch):
    """Fresh physical_key module pointed at an isolated config file."""
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    import src.security.physical_key as _pk
    importlib.reload(_pk)
    return _pk


def test_unconfigured_is_noop(pk):
    assert pk.is_configured() is False
    granted, reason = pk.check_access(password=None, drives=[])
    assert granted is True
    assert "no gate" in reason


def test_admin_password_factor(pk):
    pk.set_admin_password("hunter2")
    assert pk.is_configured() is True
    assert pk.has_admin_password() is True
    assert pk.verify_admin_password("hunter2") is True
    assert pk.verify_admin_password("wrong") is False
    # default policy is "both"; only password configured -> password alone suffices
    assert pk.get_policy() == "both"
    assert pk.check_access(password="hunter2", drives=[])[0] is True
    assert pk.check_access(password="wrong", drives=[])[0] is False
    assert pk.check_access(password=None, drives=[])[0] is False


def test_create_and_detect_physical_key(pk, tmp_path):
    usb = tmp_path / "USB"
    usb.mkdir()
    kid = pk.create_physical_key(usb)
    assert kid.startswith("CCK-")
    assert (usb / pk.KEY_FILENAME).exists()
    # the app config stores only a verifier, never the secret
    cfg_text = Path(pk._config_path()).read_text(encoding="utf-8")
    secret_hex = __import__("json").loads((usb / pk.KEY_FILENAME).read_text())["secret"]
    assert secret_hex not in cfg_text
    # present when the right drive is supplied
    assert pk.key_present(drives=[usb]) is True
    # absent when no drives / wrong drive
    assert pk.key_present(drives=[tmp_path / "EMPTY"]) is False
    assert pk.key_present(drives=[]) is False


def test_tampered_key_rejected(pk, tmp_path):
    usb = tmp_path / "USB"; usb.mkdir()
    pk.create_physical_key(usb)
    kf = usb / pk.KEY_FILENAME
    import json
    data = json.loads(kf.read_text())
    data["secret"] = "00" * 32  # wrong secret
    kf.write_text(json.dumps(data))
    assert pk.key_present(drives=[usb]) is False


def test_policy_both_requires_password_and_key(pk, tmp_path):
    usb = tmp_path / "USB"; usb.mkdir()
    pk.set_admin_password("pw")
    pk.create_physical_key(usb)
    pk.set_policy("both")
    assert pk.check_access(password="pw", drives=[usb])[0] is True
    assert pk.check_access(password="pw", drives=[])[0] is False           # key missing
    assert pk.check_access(password="bad", drives=[usb])[0] is False        # pw wrong
    assert pk.check_access(password=None, drives=[usb])[0] is False         # no pw supplied


def test_policy_either(pk, tmp_path):
    usb = tmp_path / "USB"; usb.mkdir()
    pk.set_admin_password("pw")
    pk.create_physical_key(usb)
    pk.set_policy("either")
    assert pk.check_access(password="pw", drives=[])[0] is True             # password alone
    assert pk.check_access(password=None, drives=[usb])[0] is True          # key alone
    assert pk.check_access(password="bad", drives=[tmp_path / "x"])[0] is False


def test_policy_key_only(pk, tmp_path):
    usb = tmp_path / "USB"; usb.mkdir()
    pk.create_physical_key(usb)
    pk.set_policy("key")
    assert pk.check_access(password=None, drives=[usb])[0] is True
    assert pk.check_access(password=None, drives=[])[0] is False


def test_remove_and_clear(pk, tmp_path):
    usb = tmp_path / "USB"; usb.mkdir()
    pk.set_admin_password("pw")
    pk.create_physical_key(usb)
    pk.remove_physical_key()
    assert pk.has_physical_key() is False
    pk.clear_admin_password()
    assert pk.has_admin_password() is False
    assert pk.is_configured() is False


def test_set_policy_validates(pk):
    with pytest.raises(ValueError):
        pk.set_policy("nope")


def test_set_policy_rejects_unsatisfiable(pk, tmp_path):
    """An exclusive policy whose factor is missing must be rejected — otherwise the gate can never be
    satisfied and, since every mutation runs enforce() first, the owner self-locks out (destructive
    recovery only)."""
    with pytest.raises(ValueError):
        pk.set_policy("key")        # no key configured
    with pytest.raises(ValueError):
        pk.set_policy("password")   # no admin password configured
    # allowed once the required factor exists
    pk.set_admin_password("pw")
    pk.set_policy("password")
    usb = tmp_path / "USB"; usb.mkdir()
    pk.create_physical_key(usb)
    pk.set_policy("key")
    # 'both'/'either' stay allowed even with a single factor (evaluate only requires what exists)
    pk.clear_admin_password()
    pk.set_policy("either")
    pk.set_policy("both")
