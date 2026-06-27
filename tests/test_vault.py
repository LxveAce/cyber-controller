"""Tests for the gate-keyed encrypted vault (src/security/vault.py)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    import src.security.vault as _v
    importlib.reload(_v)
    return _v


def test_first_factor_create_open_roundtrip(v):
    assert v.exists() is False
    v.set_factor("password", b"hunter2")
    assert v.is_provisioned() is True
    vault = v.open_vault({"password": b"hunter2"})
    assert vault is not None
    vault.set("secret_note", "launch codes: 1234")
    assert v.open_vault({"password": b"hunter2"}).get("secret_note") == "launch codes: 1234"


def test_wrong_factor_cannot_open(v):
    v.set_factor("password", b"hunter2")
    assert v.open_vault({"password": b"WRONG"}) is None
    assert v.open_vault({"key": b"whatever"}) is None
    assert v.open_vault({}) is None


def test_data_is_ciphertext_at_rest(v, tmp_path):
    v.set_factor("password", b"pw")
    vault = v.open_vault({"password": b"pw"})
    marker = "TOP-SECRET-PLAINTEXT-MARKER"
    vault.set("data", marker)
    raw = (tmp_path / "vault.enc").read_bytes()
    assert marker.encode() not in raw           # never stored in the clear
    # and the header carries no plaintext secret / DEK
    hdr = (tmp_path / "vault.hdr.json").read_text()
    assert "pw" not in hdr.replace('"p": 1', '')  # password not in header


def test_two_factors_either_unlocks(v):
    v.set_factor("password", b"pw")
    v.set_factor("key", b"key-secret-bytes", unlock_with={"password": b"pw"})
    vault = v.open_vault({"password": b"pw"})
    vault.set("x", 42)
    # key alone unlocks the SAME data (same DEK, two keyslots)
    assert v.open_vault({"key": b"key-secret-bytes"}).get("x") == 42
    assert v.open_vault({"password": b"pw"}).get("x") == 42


def test_add_factor_without_existing_raises(v):
    v.set_factor("password", b"pw")
    with pytest.raises(v.NeedExistingFactor):
        v.set_factor("key", b"new-key")          # no existing factor supplied to unwrap the DEK


def test_remove_factor_keeps_data(v):
    v.set_factor("password", b"pw")
    v.set_factor("key", b"ks", unlock_with={"password": b"pw"})
    v.open_vault({"password": b"pw"}).set("k", "v")
    v.remove_factor("password")
    assert v.open_vault({"password": b"pw"}) is None      # password slot gone
    assert v.open_vault({"key": b"ks"}).get("k") == "v"   # key still opens, data intact


def test_cannot_remove_last_factor(v):
    v.set_factor("password", b"pw")
    v.remove_factor("password")                  # refused (would orphan the data)
    assert v.open_vault({"password": b"pw"}) is not None
