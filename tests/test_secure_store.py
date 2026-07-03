"""Secure container (src/security/secure_store.py) — encrypt app saves at rest, sealed when locked.

Isolated: _DIR is redirected to a tmp dir and the gate vault is faked, so nothing real is touched.
"""

from __future__ import annotations

import pytest

ss = pytest.importorskip("src.security.secure_store")
from src.security import access_gate  # noqa: E402


class _FakeVault:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


@pytest.fixture
def container(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    vault = _FakeVault()  # one persistent instance so the container key is stable across save/load
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: vault)
    monkeypatch.setattr(ss, "enabled", lambda: True)


def test_save_load_roundtrip(container):
    ss.save_text("logs", "session1", "TOP-SECRET-LOG-LINE")
    assert ss.load_text("logs", "session1") == "TOP-SECRET-LOG-LINE"


def test_no_plaintext_on_disk(container):
    marker = "UNIQUE-PLAINTEXT-MARKER-12345"
    p = ss.save_text("logs", "s", f"prefix {marker} suffix")
    raw = p.read_bytes()
    assert marker.encode() not in raw, "plaintext leaked into the container file"
    assert p.suffix == ".enc"


def test_sealed_when_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: None)  # locked / no gate
    monkeypatch.setattr(ss, "enabled", lambda: True)
    assert ss.available() is False
    assert ss.load_text("logs", "s") is None
    with pytest.raises(RuntimeError):
        ss.save_text("logs", "s", "data")


def test_tamper_fails_closed(container):
    p = ss.save_text("logs", "s", "data")
    b = bytearray(p.read_bytes())
    b[-1] ^= 0xFF  # corrupt the GCM tag
    p.write_bytes(bytes(b))
    with pytest.raises(Exception):
        ss.load_text("logs", "s")


def test_disabled_is_not_available(container, monkeypatch):
    monkeypatch.setattr(ss, "enabled", lambda: False)
    assert ss.available() is False


def test_available_and_reads_do_not_mint_a_key(container):
    """SEC-B2: a status check / read must not create+persist a key. Only the first save mints one."""
    vault = access_gate.get_current_vault()
    assert vault.get(ss._KEY_ENTRY) is None            # fresh vault, no key yet

    assert ss.available() is True                       # enabled + vault unlocked
    assert vault.get(ss._KEY_ENTRY) is None             # ...and the status check did NOT mint
    assert ss.load_text("logs", "absent") is None       # a pure read
    assert ss.list_names("logs") == []                  # another pure read
    assert vault.get(ss._KEY_ENTRY) is None             # still no key — reads are non-mutating

    ss.save_text("logs", "s", "data")                   # first save mints exactly one
    assert vault.get(ss._KEY_ENTRY) is not None


def test_concurrent_first_saves_converge_on_one_key(container):
    """SEC-B2: concurrent first-use must mint a SINGLE key, not a different one per caller (which
    would orphan the ciphertext written under the loser's key)."""
    import threading

    vault = access_gate.get_current_vault()
    n = 20
    keys = []
    guard = threading.Lock()
    start = threading.Barrier(n)

    def worker():
        start.wait()  # all fire at once to force the mint race
        k = ss._container_key(create=True)
        with guard:
            keys.append(k)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert keys and keys[0] is not None
    assert len(set(keys)) == 1                           # every caller got the same, single key
    assert vault.get(ss._KEY_ENTRY) == keys[0]
