"""Macro recorder ↔ secure container integration.

Recorded sessions are encrypted at rest when the container is enabled + unlocked, listed as
``secured``, round-trip through load, and securely delete — and fall back to plaintext JSON when the
container is off (so the feature is opt-in and never blocks the existing flow).
"""

from __future__ import annotations

import pytest

ss = pytest.importorskip("src.security.secure_store")
from src.security import access_gate  # noqa: E402
from src.core.macro_recorder import Macro, MacroRecorder, MacroStep  # noqa: E402


class _FakeVault:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


def test_macro_saved_into_container(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    vault = _FakeVault()
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: vault)
    monkeypatch.setattr(ss, "enabled", lambda: True)

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    m = Macro(name="Recon Sweep", steps=[MacroStep(command="scanap")])
    p = rec.save_macro(m)

    assert p.suffix == ".enc"
    assert ss.is_container_path(p)
    assert b"scanap" not in p.read_bytes()  # command not in cleartext on disk

    listed = rec.list_saved_macros()
    assert any(x["secured"] and x["name"] == "Recon Sweep" for x in listed)

    loaded = rec.load_macro(p)
    assert loaded.name == "Recon Sweep"
    assert loaded.steps[0].command == "scanap"

    assert rec.delete_macro(p) is True
    assert not p.exists()


def test_macro_tamper_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    vault = _FakeVault()
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: vault)
    monkeypatch.setattr(ss, "enabled", lambda: True)

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    p = rec.save_macro(Macro(name="T", steps=[MacroStep(command="x")]))
    b = bytearray(p.read_bytes())
    b[-1] ^= 0xFF
    p.write_bytes(bytes(b))
    with pytest.raises(Exception):
        rec.load_macro(p)


def test_macro_plaintext_when_container_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    monkeypatch.setattr(ss, "enabled", lambda: False)  # off → plaintext fallback
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: None)

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    p = rec.save_macro(Macro(name="Plain", steps=[MacroStep(command="hop")]))
    assert p.suffix == ".json"
    assert rec.load_macro(p).steps[0].command == "hop"
    # listing has no secured entries when the container is off
    assert all(not x.get("secured") for x in rec.list_saved_macros())
