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
