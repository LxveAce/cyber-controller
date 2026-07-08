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
