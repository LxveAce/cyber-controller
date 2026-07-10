"""Tests for ``scripts/sync_site.py`` — the SSOT token-rewrite engine (first SSOT consumer).

Pure/tempfile only: no network, no gh, no real site. Everything that could be *wrong* — key
resolution, scalar enforcement, the marker backreference, rewrite/idempotency, the --check drift
exit code — is exercised here. Mirrors test_site_data.py's file-spec import of a scripts/ module.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_mod():
    spec = importlib.util.spec_from_file_location("sync_site", _ROOT / "scripts" / "sync_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load_mod()

DATA = {
    "cc_version": "1.6.9",
    "profile_count": 34,
    "parser_count": 11,
    "flag": True,
    "parsers": ["ESP32 Marauder", "Ghost ESP"],
    "products": {"cyber_controller": "1.6.9"},
    "latest_release": {"tag": "v1.6.9"},
}


def _mark(key: str, inner: str) -> str:
    return f"<!--ssot:{key}-->{inner}<!--/ssot:{key}-->"


# ── resolve_key / format_value ───────────────────────────────────────

def test_resolve_key_nested_and_missing() -> None:
    assert ss.resolve_key(DATA, "cc_version") == "1.6.9"
    assert ss.resolve_key(DATA, "products.cyber_controller") == "1.6.9"
    assert ss.resolve_key(DATA, "latest_release.tag") == "v1.6.9"
    with pytest.raises(ss.SsotError):
        ss.resolve_key(DATA, "nope")
    with pytest.raises(ss.SsotError):
        ss.resolve_key(DATA, "products.nope")


def test_format_value_scalars_and_reject_containers() -> None:
    assert ss.format_value("profile_count", 34) == "34"
    assert ss.format_value("cc_version", "1.6.9") == "1.6.9"
    assert ss.format_value("flag", True) == "true"  # lowercased for HTML/JS/JSON
    with pytest.raises(ss.SsotError):
        ss.format_value("parsers", ["a", "b"])  # a list is a page bug, not a token


# ── rewrite_text ─────────────────────────────────────────────────────

def test_rewrite_updates_stale_token() -> None:
    text = f"CC {_mark('cc_version', '0.0.0')} ships."
    out, changes = ss.rewrite_text(text, DATA)
    assert out == f"CC {_mark('cc_version', '1.6.9')} ships."
    assert changes == [{"key": "cc_version", "old": "0.0.0", "new": "1.6.9"}]


def test_rewrite_is_idempotent_when_current() -> None:
    text = f"CC {_mark('cc_version', '1.6.9')} ships."
    out, changes = ss.rewrite_text(text, DATA)
    assert out == text
    assert changes == []


def test_rewrite_int_and_dotted_keys() -> None:
    text = _mark("profile_count", "0") + " " + _mark("products.cyber_controller", "x")
    out, changes = ss.rewrite_text(text, DATA)
    assert _mark("profile_count", "34") in out
    assert _mark("products.cyber_controller", "1.6.9") in out
    assert len(changes) == 2


def test_rewrite_backreference_requires_matching_close_key() -> None:
    # Mismatched open/close keys do NOT form a region -> left untouched (not wrongly rewritten).
    text = "<!--ssot:cc_version-->x<!--/ssot:profile_count-->"
    out, changes = ss.rewrite_text(text, DATA)
    assert out == text
    assert changes == []


def test_rewrite_unknown_key_raises() -> None:
    with pytest.raises(ss.SsotError):
        ss.rewrite_text(_mark("does_not_exist", "x"), DATA)


def test_rewrite_nonscalar_key_raises() -> None:
    with pytest.raises(ss.SsotError):
        ss.rewrite_text(_mark("parsers", "x"), DATA)


def test_rewrite_multiline_region() -> None:
    text = "<!--ssot:cc_version-->\n old \n<!--/ssot:cc_version-->"
    out, changes = ss.rewrite_text(text, DATA)
    assert out == _mark("cc_version", "1.6.9")
    assert len(changes) == 1


# ── check_text ───────────────────────────────────────────────────────

def test_check_text_reports_stale_and_clean() -> None:
    stale = ss.check_text(_mark("cc_version", "0.0.0"), DATA)
    assert stale == [{"key": "cc_version", "current": "0.0.0", "expected": "1.6.9"}]
    assert ss.check_text(_mark("cc_version", "1.6.9"), DATA) == []


# ── iter_targets ─────────────────────────────────────────────────────

def test_iter_targets_file_dir_and_missing(tmp_path) -> None:
    f = tmp_path / "index.html"
    f.write_text("x")
    (tmp_path / "keep.py").write_text("x")
    assert ss.iter_targets(str(f), (".html",)) == [str(f)]
    found = ss.iter_targets(str(tmp_path), (".html",))
    assert found == [str(f)]  # .py excluded by the ext filter
    with pytest.raises(ss.SsotError):
        ss.iter_targets(str(tmp_path / "nope"), (".html",))


# ── main / CLI ───────────────────────────────────────────────────────

def _write_ssot(tmp_path) -> str:
    import json
    p = tmp_path / "site-data.json"
    p.write_text(json.dumps(DATA))
    return str(p)


def test_main_rewrites_in_place(tmp_path) -> None:
    ssot = _write_ssot(tmp_path)
    page = tmp_path / "page.html"
    page.write_text(f"v{_mark('cc_version', '0.0.0')}")
    rc = ss.main([str(page), "--ssot", ssot])
    assert rc == 0
    assert _mark("cc_version", "1.6.9") in page.read_text()


def test_main_check_exit_codes(tmp_path) -> None:
    ssot = _write_ssot(tmp_path)
    page = tmp_path / "page.html"
    page.write_text(_mark("cc_version", "0.0.0"))
    assert ss.main([str(page), "--check", "--ssot", ssot]) == 1  # stale -> drift
    page.write_text(_mark("cc_version", "1.6.9"))
    assert ss.main([str(page), "--check", "--ssot", ssot]) == 0  # in sync
