"""Tests for ``src.security.encrypted_storage`` (AES-256-GCM SecureStorage).

Covered:
    * encrypt/decrypt round-trips an arbitrary dict;
    * a wrong passphrase raises ValueError (GCM auth fails closed);
    * flipping a single ciphertext byte raises ValueError (GCM tamper detection);
    * save() then load() round-trips through a tmp_path file.

``cryptography`` is a hard runtime dep and is present, but it is imported behind
``importorskip`` so the suite stays clean even on a stripped install.
"""

from __future__ import annotations

import pytest

# SecureStorage fails closed without cryptography; skip rather than error if absent.
pytest.importorskip("cryptography")
encrypted_storage = pytest.importorskip("src.security.encrypted_storage")

SecureStorage = encrypted_storage.SecureStorage

_SAMPLE = {
    "session": "abc123",
    "targets": [{"mac": "AA:BB:CC:DD:EE:FF", "rssi": -42}],
    "count": 7,
    "nested": {"flag": True, "ratio": 0.5},
}


def test_encrypt_decrypt_roundtrip() -> None:
    store = SecureStorage("correct horse battery staple")
    blob = store.encrypt(_SAMPLE)
    assert isinstance(blob, (bytes, bytearray))
    assert store.decrypt(blob) == _SAMPLE


def test_ciphertext_is_not_plaintext() -> None:
    store = SecureStorage("pw")
    blob = store.encrypt({"secret": "topsecretvalue"})
    assert b"topsecretvalue" not in blob


def test_wrong_passphrase_raises_value_error() -> None:
    blob = SecureStorage("right-pass").encrypt(_SAMPLE)
    with pytest.raises(ValueError):
        SecureStorage("wrong-pass").decrypt(blob)


def test_tampered_ciphertext_raises_value_error() -> None:
    store = SecureStorage("pw")
    blob = bytearray(store.encrypt(_SAMPLE))
    # Flip one bit in the last byte (inside the ciphertext+tag region) -> GCM rejects it.
    blob[-1] ^= 0x01
    with pytest.raises(ValueError):
        store.decrypt(bytes(blob))


def test_save_then_load_roundtrip(tmp_path) -> None:
    store = SecureStorage("file-pass")
    path = tmp_path / "vault.bin"
    store.save(_SAMPLE, path)
    assert path.exists()
    assert store.load(path) == _SAMPLE


def test_empty_passphrase_rejected() -> None:
    with pytest.raises(ValueError):
        SecureStorage("")


def test_save_is_atomic_a_failed_commit_keeps_the_prior_entry(tmp_path, monkeypatch) -> None:
    """A crash/power-loss mid-write must NOT destroy an existing entry. save() now writes a temp file +
    fsync + os.replace, so a failure at the atomic rename (simulated power loss) leaves the previous
    complete ciphertext intact and still decryptable. The old O_TRUNC-in-place write truncated the .enc
    to 0 first, so the same crash left a partial blob whose GCM tag failed to verify — the entry lost."""
    import os

    store = SecureStorage("file-pass")
    path = tmp_path / "vault.bin"
    store.save({"keep": "me"}, path)              # a good, complete entry on disk
    good_before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("simulated power loss at commit")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"keep": "OVERWRITE"}, path)   # commit fails mid-save

    assert path.read_bytes() == good_before        # prior ciphertext intact — not truncated
    assert store.load(path) == {"keep": "me"}      # and still decryptable
    assert not list(tmp_path.glob("vault.bin.*.tmp"))  # the temp file was cleaned up
