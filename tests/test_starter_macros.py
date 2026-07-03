"""Bundled starter/template macros — seeding + offensive arm gate.

Covers Track B "ship with template macros" so the macro list is not empty on first run:

* the bundled ``cc_*.json`` builtins match the real ``Macro`` shape and round-trip;
* first-run seeding writes the builtins into the macros dir (nothing transmits — only files);
* seeding never overwrites an existing same-name user macro;
* a builtin the user deletes (recorded in the ``.seeded.json`` ledger) is NOT re-seeded;
* the offensive-detection heuristic flags the attack templates but not the safe recon macros;
* the Play path shows the arm/confirm dialog for an offensive macro and skips it for a safe one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core.macro_recorder import (
    _SEEDED_LEDGER,
    Macro,
    MacroRecorder,
    MacroStep,
    is_offensive_macro,
)

_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "src" / "core" / "default_macros"


# ── Bundled builtins are well-formed ──────────────────────────────────

def test_ships_fourteen_builtins():
    files = sorted(_BUILTIN_DIR.glob("cc_*.json"))
    assert len(files) == 14  # 12 safe-recon/util + 2 offensive templates


def test_builtins_match_real_macro_shape():
    for f in sorted(_BUILTIN_DIR.glob("cc_*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        expected = {"name", "description", "steps", "created_at", "device_protocol"}
        assert set(data) == expected, f.name
        macro = Macro.from_dict(data)  # must not raise (MacroStep has no unknown keys)
        assert macro.to_dict()["steps"] == data["steps"]


def test_exactly_two_offensive_templates():
    offensive = []
    for f in sorted(_BUILTIN_DIR.glob("cc_*.json")):
        macro = Macro.from_dict(json.loads(f.read_text(encoding="utf-8")))
        if is_offensive_macro(macro):
            offensive.append(macro.name)
    assert len(offensive) == 2
    assert all(n.startswith("[TEMPLATE") for n in offensive)


def test_excluded_firmwares_not_shipped():
    protos = set()
    for f in sorted(_BUILTIN_DIR.glob("cc_*.json")):
        protos.add(Macro.from_dict(json.loads(f.read_text(encoding="utf-8"))).device_protocol)
    assert "meshtastic" not in protos  # protobuf over serial — poor fit, excluded
    assert "esp32-div" not in protos   # button/menu UI — poor fit, excluded


# ── Seeding logic (pure, no Qt) ───────────────────────────────────────

def test_first_run_seeds_all_builtins(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    written = rec.seed_default_macros()
    assert len(written) == 14
    listed = {m["name"] for m in rec.list_saved_macros()}
    assert "Marauder — AP scan + list" in listed
    # ledger is present but never surfaces as a macro
    assert (rec.macros_dir / _SEEDED_LEDGER).is_file()
    assert not any(m["name"].startswith(".seeded") for m in rec.list_saved_macros())


def test_seeding_is_idempotent(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    assert len(rec.seed_default_macros()) == 14
    assert rec.seed_default_macros() == []  # second run writes nothing


def test_seeding_never_clobbers_user_macro(tmp_path):
    macros_dir = tmp_path / "macros"
    macros_dir.mkdir(parents=True)
    # A user's own file that collides with a builtin filename must be preserved verbatim.
    clash = macros_dir / "cc_marauder_ap_scan.json"
    user_macro = Macro(name="MY OWN SCAN", steps=[MacroStep(command="mine")])
    clash.write_text(json.dumps(user_macro.to_dict()), encoding="utf-8")

    rec = MacroRecorder(macros_dir=macros_dir)
    written = rec.seed_default_macros()

    assert clash not in written
    assert json.loads(clash.read_text(encoding="utf-8"))["name"] == "MY OWN SCAN"  # untouched


def test_deleted_builtin_is_not_reseeded(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    rec.seed_default_macros()

    # User deletes one builtin. The ledger still records it as seeded-once.
    victim = rec.macros_dir / "cc_ghostesp_ble_scan.json"
    assert victim.exists()
    victim.unlink()

    reseeded = rec.seed_default_macros()
    assert reseeded == []           # nothing re-created
    assert not victim.exists()      # the deleted builtin stays deleted


def test_seeding_writes_plaintext_files_only(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    rec.seed_default_macros()
    for f in rec.macros_dir.glob("cc_*.json"):
        json.loads(f.read_text(encoding="utf-8"))  # readable plaintext JSON, not encrypted


# ── Offensive detection heuristic ─────────────────────────────────────

def test_is_offensive_flags_attack_protocol():
    m = Macro(name="x", device_protocol="marauder-attack", steps=[MacroStep(command="scanap")])
    assert is_offensive_macro(m) is True


def test_is_offensive_flags_template_name():
    m = Macro(name="[TEMPLATE — REVIEW] whatever", steps=[MacroStep(command="scanap")])
    assert is_offensive_macro(m) is True


def test_is_offensive_flags_attack_verb_step():
    m = Macro(name="plain", steps=[MacroStep(command="attack -t deauth")])
    assert is_offensive_macro(m) is True


def test_safe_recon_macro_is_not_offensive():
    m = Macro(
        name="Marauder — AP scan + list",
        device_protocol="marauder",
        steps=[
            MacroStep(command="scanap"),
            MacroStep(command="stopscan"),
            MacroStep(command="list -a"),
        ],
    )
    assert is_offensive_macro(m) is False


# ── Play-time arm gate (offscreen Qt) ─────────────────────────────────

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeConn:
    is_connected = True

    def write(self, _cmd):  # pragma: no cover - never reached (playback is monkeypatched)
        pass


class _FakeDM:
    def scan_ports(self):
        return []

    def get_connection(self, _port):
        return _FakeConn()


def _counter(calls, key):
    """Return a callable that bumps ``calls[key]`` — a tiny spy for monkeypatched methods."""
    def _inc(*_a, **_k):
        calls[key] += 1
    return _inc


def _make_tab(tmp_path, macro):
    from src.ui.qt.macro_tab import MacroTab

    rec = MacroRecorder(macros_dir=tmp_path / "macros")
    tab = MacroTab(rec, _FakeDM())
    tab._port_combo.addItem("COM_FAKE -- fake", "COM_FAKE")
    tab._port_combo.setCurrentIndex(tab._port_combo.count() - 1)
    tab._current_macro = macro
    return tab, rec


def test_offensive_macro_triggers_confirm_gate(qapp, tmp_path, monkeypatch):
    macro = Macro(
        name="[TEMPLATE — REVIEW] Marauder — targeted deauth",
        device_protocol="marauder-attack",
        steps=[MacroStep(command="attack -t deauth")],
    )
    tab, rec = _make_tab(tmp_path, macro)

    calls = {"warned": 0, "played": 0}
    monkeypatch.setattr(rec, "play", _counter(calls, "played"))

    def fake_warning(*_a, **_k):
        calls["warned"] += 1
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))

    tab._on_play()
    assert calls["warned"] == 1   # the arm dialog was shown
    assert calls["played"] == 0   # declining (No) blocks playback

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    tab._on_play()
    assert calls["played"] == 1   # explicit Yes arms and plays


def test_safe_macro_plays_without_gate(qapp, tmp_path, monkeypatch):
    macro = Macro(
        name="Marauder — AP scan + list",
        device_protocol="marauder",
        steps=[MacroStep(command="scanap"), MacroStep(command="stopscan")],
    )
    tab, rec = _make_tab(tmp_path, macro)

    calls = {"warned": 0, "played": 0}
    monkeypatch.setattr(rec, "play", _counter(calls, "played"))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_counter(calls, "warned")))

    tab._on_play()
    assert calls["warned"] == 0   # no arm gate for safe recon
    assert calls["played"] == 1   # plays straight through
