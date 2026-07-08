"""A JSON-null (or non-dict) section in settings.json must fall back to the default section, not sail
through the deep-merge as None. Regression: _deep_merge passed a non-dict override straight through, so
load_settings returned {"serial": None} and every consumer that does settings["serial"].get(...) crashed
(e.g. the Tk Lite UI failed to even launch). load_settings promises a corrupt/partial file falls back to
defaults — that must hold for null SECTIONS, not just a non-object top level."""

from __future__ import annotations

import json

import pytest

from src.config import settings as S


@pytest.mark.parametrize("junk", [None, 5, "x", [1, 2]])
def test_deep_merge_coerces_null_section_to_default(junk):
    merged = S._deep_merge(S.DEFAULTS, {"serial": junk})
    assert isinstance(merged["serial"], dict)
    # falls back to the DEFAULT section rather than leaking the junk value through
    assert merged["serial"]["default_baud"] == S.DEFAULTS["serial"]["default_baud"]


def test_deep_merge_still_merges_a_real_section():
    merged = S._deep_merge(S.DEFAULTS, {"serial": {"default_baud": 9600}})
    assert merged["serial"]["default_baud"] == 9600
    assert merged["serial"]["timeout"] == S.DEFAULTS["serial"]["timeout"]  # missing key kept from default


def test_load_settings_survives_null_section(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"serial": None, "ui": None}), encoding="utf-8")
    monkeypatch.setattr(S, "SETTINGS_PATH", path)
    loaded = S.load_settings()
    # the null section is a usable dict again, so serial.get(...) can't AttributeError downstream
    assert isinstance(loaded["serial"], dict)
    assert loaded["serial"]["default_baud"] == S.DEFAULTS["serial"]["default_baud"]


def test_save_settings_fsyncs_temp_file_before_replace(tmp_path, monkeypatch):
    """save_settings must flush the temp file's data to stable storage BEFORE os.replace, else an
    abrupt power loss can leave settings.json pointing at unflushed/zero-filled blocks and load_settings
    silently resets every preference to defaults. Assert the durable fsync happens on the temp fd that
    is then renamed into place (the atomic os.replace alone guarantees only the metadata swap)."""
    import os

    path = tmp_path / "settings.json"
    monkeypatch.setattr(S, "SETTINGS_PATH", path)
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)

    fsynced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])

    replaced: dict[str, bool] = {"ok": False}
    real_replace = os.replace

    def tracking_replace(src, dst):
        # the fsync must have already happened by the time we commit the rename
        assert fsynced, "temp file was not fsync'd before os.replace"
        replaced["ok"] = True
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)

    S.save_settings({"serial": {"default_baud": 9600}})
    assert replaced["ok"] is True
    assert fsynced, "save_settings did not fsync the temp file"
    assert S.load_settings()["serial"]["default_baud"] == 9600
