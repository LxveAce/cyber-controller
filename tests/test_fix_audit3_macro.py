"""Regression guards for the cc-deep-audit-3 macro_recorder cluster (2026-07-13, M1/M2/M3).

    M1 (HIGH, safety) the offensive-macro arm gate lived only in the Qt tab, so `--ui tk` (or any
      other caller) replayed attack templates with NO confirmation. The gate is now ENFORCED in the
      engine: play() refuses an offensive macro unless the caller passes armed=True.
    M2 (HIGH, verify-never-fake) MacroStep.expected_response was advertised but never checked —
      playback always reported success. play() now verifies it against a wired read_response
      (failing on mismatch) and, when no response channel is wired, reports the check as NOT
      verified instead of silently claiming success.
    M3 (MED, data-loss) save_macro sanitized the name to a filename with no collision/empty guard:
      two distinct names that sanitized alike silently clobbered, and an empty name produced a
      hidden ".json" the UI could never list. Now empty falls back to a stem and a distinct
      collision rolls over to name-N.json.

Pure engine logic — no hardware, no Qt/Tk. secure_store is forced OFF so the plaintext save runs.
"""
from __future__ import annotations

import pytest

from src.core.macro_recorder import Macro, MacroRecorder, MacroStep


def _recon(**kw) -> Macro:
    return Macro(name=kw.pop("name", "Recon Scan"), device_protocol=kw.pop("protocol", "marauder"),
                 steps=kw.pop("steps", [MacroStep(command="scanap")]), **kw)


def _offensive(**kw) -> Macro:
    # device_protocol ending in "-attack" makes is_offensive_macro True (see its heuristic).
    return Macro(name=kw.pop("name", "Deauth"), device_protocol="marauder-attack",
                 steps=kw.pop("steps", [MacroStep(command="deauth")]), **kw)


# ── M1: engine-level arm gate ─────────────────────────────────────────────────────────────────────

def test_offensive_macro_refused_without_arm(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    rec.play(_offensive(), send_command=sent.append,
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert sent == [], "an un-armed offensive macro must NOT transmit any command"
    assert done and done[0][0] is False
    assert "not armed" in done[0][1].lower()


def test_offensive_macro_plays_when_armed(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    done: list = []
    rec.play(_offensive(), send_command=sent.append, armed=True,
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert sent == ["deauth"], "an armed offensive macro plays"
    assert done and done[0][0] is True


def test_recon_macro_plays_without_arm(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    sent: list[str] = []
    rec.play(_recon(), send_command=sent.append, async_=False)
    assert sent == ["scanap"], "a non-offensive recon macro plays with no arm needed"


# ── M2: expected_response is really checked / honestly reported ───────────────────────────────────

def _macro_with_expected() -> Macro:
    return _recon(steps=[MacroStep(command="scan -t ap", expected_response="AP:.*")])


def test_expected_response_unverified_is_reported_not_silently_succeeded(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    done: list = []
    rec.play(_macro_with_expected(), send_command=lambda _c: None,
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert done and done[0][0] is True
    assert "not response-verified" in done[0][1].lower(), \
        "with no response channel, the check must be reported as NOT verified — not a bare success"


def test_expected_response_match_passes(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    done: list = []
    rec.play(_macro_with_expected(), send_command=lambda _c: None,
             read_response=lambda _t: "AP: 00:11:22:33:44:55",
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert done and done[0][0] is True
    assert done[0][1] == "Playback complete"  # verified -> clean success, no caveat


def test_expected_response_mismatch_fails(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    done: list = []
    rec.play(_macro_with_expected(), send_command=lambda _c: None,
             read_response=lambda _t: "ERROR: not scanning",
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert done and done[0][0] is False
    assert "did not match" in done[0][1]


# ── M3: save filename collision + empty name ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _plaintext_saves(monkeypatch):
    """Force the plaintext save path (secure container OFF) for the save tests."""
    import src.security.secure_store as ss
    monkeypatch.setattr(ss, "enabled", lambda: False)


def test_distinct_names_do_not_clobber(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    p1 = rec.save_macro(_recon(name="Wifi Scan", steps=[MacroStep(command="a")]))
    p2 = rec.save_macro(_recon(name="Wifi_Scan", steps=[MacroStep(command="b")]))  # sanitizes alike
    assert p1 != p2, "two distinct macros that sanitize to the same filename must not clobber"
    assert p1.exists() and p2.exists()
    assert p2.name == "wifi_scan-1.json"
    names = {m["name"] for m in rec.list_saved_macros()}
    assert {"Wifi Scan", "Wifi_Scan"} <= names


def test_resaving_same_macro_overwrites_in_place(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    m = _recon(name="Wifi Scan", steps=[MacroStep(command="a")])
    p1 = rec.save_macro(m)
    p2 = rec.save_macro(m)  # same macro name -> update in place, no proliferation
    assert p1 == p2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_empty_name_is_not_a_hidden_file(tmp_path):
    rec = MacroRecorder(macros_dir=tmp_path)
    p = rec.save_macro(_recon(name="   ", steps=[MacroStep(command="a")]))
    assert not p.name.startswith("."), "an empty name must not become a hidden .json"
    assert rec.list_saved_macros(), "the saved macro must be listable (recoverable)"
