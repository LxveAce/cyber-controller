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


def test_rekey_slot_honors_old_secret_via_unlock_with(v):
    """CHANGING an existing slot's secret must succeed when the OLD secret is supplied in
    unlock_with[name]. Previously set_factor did `avail[name]=secret`, clobbering the supplied OLD
    secret with the NEW one, so _dek_from could never unwrap and the re-key raised NeedExistingFactor
    (the root cause behind the gate/vault password-change desync)."""
    v.set_factor("password", b"old")
    v.open_vault({"password": b"old"}).set("k", "v")
    # old -> new, unlocking with the CURRENT (old) secret of the SAME slot
    v.set_factor("password", b"new", unlock_with={"password": b"old"})
    v_new = v.open_vault({"password": b"new"})
    assert v_new is not None and v_new.get("k") == "v"   # same DEK/data, re-wrapped under the new secret
    assert v.open_vault({"password": b"old"}) is None      # old secret no longer opens it


def test_reset_same_secret_still_self_unlocks(v):
    """Guard: re-setting a slot to the SAME secret with no unlock_with must still self-unlock (the
    setdefault only defers to unlock_with when it actually carries that factor)."""
    v.set_factor("password", b"pw")
    v.open_vault({"password": b"pw"}).set("k", "v")
    v.set_factor("password", b"pw")                        # no unlock_with; new==old self-unlocks
    assert v.open_vault({"password": b"pw"}).get("k") == "v"


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


def test_vault_set_concurrent_writes_do_not_lose_updates(v, monkeypatch):
    """Two subsystems writing DIFFERENT keys concurrently must not clobber each other's update.

    Vault.set is a load→mutate→save read-modify-write and the handle is a process-global singleton
    shared by node_provision (node_keys) and secure_store (secure_container_key) under separate,
    non-shared locks. Without a shared per-vault lock inside set(), both threads read the same on-disk
    state and whichever saves second drops the other's key — a lost update (orphaned ciphertext /
    dropped node). Here alpha and beta MUST both survive."""
    import threading
    import time

    v.set_factor("password", b"pw")
    vault = v.open_vault({"password": b"pw"})

    # Widen the load→save window so an unsynchronized set() reliably interleaves; the fix serializes
    # this (the second set() can't even load until the first releases), so both keys survive.
    orig_save = v.Vault.save

    def slow_save(self, data):
        time.sleep(0.3)
        return orig_save(self, data)

    monkeypatch.setattr(v.Vault, "save", slow_save)

    start = threading.Barrier(2)

    def writer(key, value):
        start.wait()
        vault.set(key, value)

    ta = threading.Thread(target=writer, args=("alpha", 1))
    tb = threading.Thread(target=writer, args=("beta", 2))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    final = v.open_vault({"password": b"pw"}).load()
    assert final.get("alpha") == 1
    assert final.get("beta") == 2


def test_vault_load_truncated_raises_clean_valueerror(tmp_path, monkeypatch):
    """A truncated vault.enc (shorter than nonce+tag) must fail closed with ONE clean ValueError,
    not a raw crash — and never returns plaintext."""
    import os

    import src.security.vault as vaultmod
    bad = tmp_path / "vault.enc"
    bad.write_bytes(b"\x00" * 8)  # < 12 (nonce) + 16 (tag)
    monkeypatch.setattr(vaultmod, "_data_path", lambda: bad)
    vd = vaultmod.Vault(os.urandom(32))
    with pytest.raises(ValueError, match="corrupt or tampered"):
        vd.load()


def test_vault_load_tampered_raises_clean_valueerror(tmp_path, monkeypatch):
    """A valid-length but bogus ciphertext (wrong key / bit-flip) must raise the same clean ValueError
    (from the caught InvalidTag), never propagate a raw InvalidTag."""
    import os

    import src.security.vault as vaultmod
    bad = tmp_path / "vault.enc"
    bad.write_bytes(os.urandom(12 + 32))  # nonce + bogus ct/tag → InvalidTag on decrypt
    monkeypatch.setattr(vaultmod, "_data_path", lambda: bad)
    vd = vaultmod.Vault(os.urandom(32))
    with pytest.raises(ValueError, match="corrupt or tampered"):
        vd.load()


def test_factors_on_truncated_header_fails_closed(v):
    """A truncated/0-byte header (crash during _save_hdr's O_TRUNC-then-write window) leaves
    is_provisioned() True — factors() must NOT raise a raw JSONDecodeError into the no-auth
    --gate-status command, but fail closed to an empty factor list."""
    v.set_factor("password", b"hunter2")
    v._hdr_path().write_text("", encoding="utf-8")   # simulate a truncated write
    assert v.is_provisioned() is True
    assert v.factors() == []   # was: unhandled json.JSONDecodeError


def test_factors_on_non_dict_header_fails_closed(v):
    v.set_factor("password", b"hunter2")
    v._hdr_path().write_text("null", encoding="utf-8")   # valid JSON, wrong type
    assert v.factors() == []


def test_set_factor_refuses_reprovision_on_unreadable_header(v):
    """SEC (data-loss): a present-but-unreadable header (corrupt/truncated, or a transient AV/indexer
    file lock) must NOT be treated as 'no vault'. _load_hdr() returns {} for both states, and the old
    `if not hdr:` re-provisioned a fresh random DEK and _save_hdr()-clobbered the header, orphaning
    vault.enc — every wrapped node key + the container key became permanently undecryptable. set_factor
    must now fail closed (NeedExistingFactor) and leave the encrypted data untouched, so the original
    factor still opens the original data once the header is restored."""
    v.set_factor("password", b"hunter2")
    v.open_vault({"password": b"hunter2"}).set("secret", "keep-me")
    good_hdr = v._hdr_path().read_bytes()
    enc_before = v._data_path().read_bytes()

    v._hdr_path().write_text("{ this is not valid json", encoding="utf-8")  # _load_hdr() -> {}
    with pytest.raises(v.NeedExistingFactor):
        v.set_factor("password", b"new-password")        # must NOT silently re-provision

    assert v._data_path().read_bytes() == enc_before      # vault.enc untouched — no destruction
    v._hdr_path().write_bytes(good_hdr)                    # restore header
    assert v.open_vault({"password": b"hunter2"}).get("secret") == "keep-me"   # original data intact


def test_set_factor_refuses_reprovision_when_only_data_present(v):
    """An orphaned vault.enc with no header (data already sealed under a lost DEK) must also fail closed
    rather than re-provision a fresh vault that silently dangles the old ciphertext."""
    v.set_factor("password", b"pw")
    v.open_vault({"password": b"pw"}).set("x", 1)
    v._hdr_path().unlink()                                 # header gone, vault.enc remains
    assert v.exists() is True
    with pytest.raises(v.NeedExistingFactor):
        v.set_factor("password", b"pw")


def test_set_factor_still_provisions_on_true_clean_slate(v):
    """Guard against over-blocking: with NOTHING on disk, set_factor still provisions a fresh vault."""
    assert v.exists() is False
    v.set_factor("password", b"first")                    # must not raise
    assert v.open_vault({"password": b"first"}) is not None


def test_vault_save_is_atomic_a_failed_commit_keeps_old_data(v, monkeypatch):
    """A crash at the vault.enc write must NOT destroy the existing store. Vault.save now writes a
    temp file + fsync + os.replace, so a failure at the atomic rename (simulated power loss) leaves the
    previous complete ciphertext intact — every node key + the container key survive. The old O_TRUNC
    in-place rewrite truncated vault.enc to 0 first, so the same crash lost EVERYTHING (raised
    'corrupt or tampered' on next open)."""
    import os

    v.set_factor("password", b"pw")
    v.open_vault({"password": b"pw"}).set("a", 1)

    def boom(*_a, **_k):
        raise OSError("simulated power loss at commit")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        v.open_vault({"password": b"pw"}).set("b", 2)   # commit fails mid-save

    # open_vault only READS, so the still-patched os.replace does not matter here.
    reopened = v.open_vault({"password": b"pw"})
    assert reopened is not None                          # vault.enc not truncated/corrupted
    data = reopened.load()
    assert data.get("a") == 1                            # prior data preserved
    assert "b" not in data                               # the failed write did not partially land


def test_save_hdr_is_atomic_a_failed_commit_keeps_the_vault_openable(v, monkeypatch):
    """A crash while rewriting vault.hdr.json (e.g. a password re-key) must NOT brick the vault. The
    header is the only wrapped copy of the DEK; the atomic temp+replace leaves the previous header
    intact on a failed commit, so the old factor still opens the vault. The old in-place O_TRUNC would
    leave a partial header and make the DEK unrecoverable by every factor."""
    import os

    v.set_factor("password", b"old")
    v.open_vault({"password": b"old"}).set("k", "v")

    def boom(*_a, **_k):
        raise OSError("simulated power loss at commit")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        v.set_factor("password", b"new", unlock_with={"password": b"old"})   # re-key crashes

    # factors()/open_vault only READ, so the still-patched os.replace does not matter here.
    assert v.factors() == ["password"]                   # header not corrupted
    assert v.open_vault({"password": b"old"}).get("k") == "v"   # still opens with the old secret


def test_set_factor_on_header_missing_salt_raises_need_existing(v):
    """A valid-JSON header missing 'salt' must surface as NeedExistingFactor out of set_factor,
    not a raw KeyError that set_password_cli doesn't catch."""
    import json as _json
    v.set_factor("password", b"hunter2")
    hdr = _json.loads(v._hdr_path().read_text(encoding="utf-8"))
    hdr.pop("salt", None)
    v._hdr_path().write_text(_json.dumps(hdr), encoding="utf-8")
    with pytest.raises(v.NeedExistingFactor):
        v.set_factor("password", b"newpass", unlock_with={"password": b"hunter2"})
