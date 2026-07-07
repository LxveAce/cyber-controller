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


def test_macro_refuses_plaintext_when_enabled_but_locked(tmp_path, monkeypatch):
    """SEC-B1: container ENABLED but the gate LOCKED must NOT silently write a plaintext macro — it
    must fail closed so the recorded session isn't leaked to disk in the clear."""
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    monkeypatch.setattr(ss, "enabled", lambda: True)
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: None)  # locked

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    with pytest.raises(RuntimeError):
        rec.save_macro(Macro(name="Secret Sweep", steps=[MacroStep(command="scanap")]))
    md = tmp_path / "macros"
    assert not md.exists() or not any(md.glob("*.json"))  # nothing leaked in cleartext


def test_list_skips_malformed_plaintext_macros(tmp_path, monkeypatch):
    """One corrupt/wrong-shape *.json in the macros dir must NOT crash the whole listing — the
    bad file is skipped and the good ones still list. Regression for the too-narrow except that
    only caught (json.JSONDecodeError, OSError) while the body raised AttributeError/TypeError."""
    monkeypatch.setattr(ss, "_DIR", tmp_path / "secure")
    monkeypatch.setattr(ss, "enabled", lambda: False)  # plaintext fallback, no secure listing
    monkeypatch.setattr(access_gate, "get_current_vault", lambda: None)

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    good = rec.save_macro(Macro(name="Good", steps=[MacroStep(command="scanap")]))
    assert good.suffix == ".json"

    md = tmp_path / "macros"
    # valid JSON but a bare array -> data.get(...) would raise AttributeError
    (md / "bad_array.json").write_text("[]", encoding="utf-8")
    # object with a non-list steps -> len(5) would raise TypeError
    (md / "bad_steps.json").write_text('{"name":"x","steps":5}', encoding="utf-8")
    # not even JSON -> JSONDecodeError (already handled, kept to prove the loop keeps going)
    (md / "not_json.json").write_text("{not json", encoding="utf-8")

    listed = rec.list_saved_macros()  # must not raise (pre-fix this raised AttributeError/TypeError)

    by_name = {x["name"]: x for x in listed}
    # the good macro still lists correctly despite the bad neighbours
    assert "Good" in by_name
    assert by_name["Good"]["step_count"] == 1
    # a non-object file (bare array) can't be represented as a macro -> skipped entirely
    assert not any(x["path"].endswith("bad_array.json") for x in listed)
    # a dict with a non-list steps is salvaged, never crashes: step_count coerced to 0
    assert all(isinstance(x["step_count"], int) for x in listed)
    salvaged = [x for x in listed if x["path"].endswith("bad_steps.json")]
    assert all(x["step_count"] == 0 for x in salvaged)


def test_enabled_fails_closed_on_settings_error(monkeypatch):
    """SEC-B1: an unexpected settings-read error must not be swallowed into False (which would
    silently permit a plaintext downgrade); enabled() re-raises so callers fail closed."""
    import src.config.settings as settings_mod

    def boom():
        raise RuntimeError("settings backend exploded")

    monkeypatch.setattr(settings_mod, "load_settings", boom)
    with pytest.raises(Exception):
        ss.enabled()
