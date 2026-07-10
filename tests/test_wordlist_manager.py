"""Unit tests for the wordlist manager. Pure functions + file I/O against a temp dir; no network.
The download() path (urllib) is intentionally not exercised here -- it is the thin best-effort layer,
same policy as crack_pipeline's subprocess orchestration."""
from __future__ import annotations

import hashlib

import pytest

from src.core import wordlist_manager as wm

# -- catalog integrity ------------------------------------------------

def test_catalog_nonempty_and_ids_unique():
    cat = wm.catalog()
    assert cat, "catalog must not be empty"
    ids = [w.id for w in cat]
    assert len(ids) == len(set(ids)), "catalog ids must be unique"


def test_catalog_wpa_entries_come_first():
    cats = [w.category for w in wm.catalog()]
    # every 'wpa' entry precedes every 'general' entry (WPA cracker -> WPA lists surfaced first)
    last_wpa = max((i for i, c in enumerate(cats) if c == "wpa"), default=-1)
    first_general = min((i for i, c in enumerate(cats) if c == "general"), default=len(cats))
    assert last_wpa < first_general


def test_spec_by_id_roundtrip_and_miss():
    assert wm.spec_by_id("rockyou").name == "rockyou.txt"
    assert wm.spec_by_id("does-not-exist") is None


def test_seclists_urls_are_commit_pinned_not_master():
    for w in wm.catalog():
        if "SecLists" in w.url:
            assert "/master/" not in w.url, "SecLists must be pinned to a commit, not master"
            assert wm._SECLISTS_COMMIT in w.url


def test_pinned_entries_have_a_real_looking_sha():
    for w in wm.catalog():
        if wm.is_pinned(w):
            assert len(w.sha256) == 64 and all(c in "0123456789abcdef" for c in w.sha256.lower())


# -- formatting + paths -----------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (0, "0 B"), (573, "573 B"), (1023, "1023 B"),
    (1024, "1.0 KiB"), (73017, "71.3 KiB"), (139921497, "133.4 MiB"),
])
def test_format_size(n, expected):
    assert wm.format_size(n) == expected


def test_default_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_WORDLIST_DIR", str(tmp_path))
    assert wm.default_wordlist_dir() == str(tmp_path)
    monkeypatch.delenv("CC_WORDLIST_DIR", raising=False)
    assert wm.default_wordlist_dir().endswith("wordlists")


def test_filename_for_drops_gz():
    plain = wm.WordlistSpec(id="p", name="p", description="", url="http://x/a.txt", size_bytes=1)
    gz = wm.WordlistSpec(id="g", name="g", description="", url="http://x/a.txt.gz",
                         size_bytes=1, compressed="gz")
    assert wm.filename_for(plain) == "a.txt"
    assert wm.filename_for(gz) == "a.txt"


def test_target_path_uses_dir():
    spec = wm.spec_by_id("wpa-top62")
    assert wm.target_path(spec, "/tmp/wl").replace("\\", "/") == "/tmp/wl/probable-v2-wpa-top62.txt"


# -- integrity checks against real files ------------------------------

def _write(path, data: bytes):
    path.write_bytes(data)
    return str(path)


def test_sha256_file_matches_hashlib(tmp_path):
    p = _write(tmp_path / "w.txt", b"password\n123456\n")
    assert wm.sha256_file(p) == hashlib.sha256(b"password\n123456\n").hexdigest()


def test_verify_file_pinned_match_and_mismatch(tmp_path):
    data = b"correct horse battery staple\n"
    real = hashlib.sha256(data).hexdigest()
    spec = wm.WordlistSpec(id="t", name="t", description="", url="http://x/w.txt",
                           size_bytes=len(data), sha256=real)
    good = _write(tmp_path / "w.txt", data)
    ok, msg = wm.verify_file(good, spec)
    assert ok and "pinned" in msg

    bad_spec = wm.WordlistSpec(id="t", name="t", description="", url="http://x/w.txt",
                               size_bytes=len(data), sha256="0" * 64)
    ok2, msg2 = wm.verify_file(good, bad_spec)
    assert not ok2 and "mismatch" in msg2


def test_verify_file_size_only_flags_unpinned(tmp_path):
    data = b"x" * 100
    spec = wm.WordlistSpec(id="s", name="s", description="", url="http://x/w.txt",
                           size_bytes=100, sha256="")
    good = _write(tmp_path / "w.txt", data)
    ok, msg = wm.verify_file(good, spec)
    assert ok and "NOT pre-pinned" in msg

    wrong = wm.WordlistSpec(id="s", name="s", description="", url="http://x/w.txt",
                            size_bytes=999, sha256="")
    ok2, msg2 = wm.verify_file(good, wrong)
    assert not ok2 and "size mismatch" in msg2


def test_verify_file_missing(tmp_path):
    spec = wm.spec_by_id("rockyou")
    ok, msg = wm.verify_file(str(tmp_path / "nope.txt"), spec)
    assert not ok and "not installed" in msg


# -- install scan / BYO -----------------------------------------------

def test_scan_installed_lists_txt_only(tmp_path):
    (tmp_path / "a.txt").write_text("aaa")
    (tmp_path / "b.txt").write_text("bbbb")
    (tmp_path / "notes.md").write_text("ignore me")
    found = wm.scan_installed(str(tmp_path))
    names = [f["name"] for f in found]
    assert names == ["a.txt", "b.txt"]
    assert found[0]["size"] == 3 and found[0]["size_human"] == "3 B"


def test_scan_installed_missing_dir_is_empty():
    assert wm.scan_installed("/no/such/dir/anywhere") == []


def test_is_installed(tmp_path):
    spec = wm.spec_by_id("wpa-top62")
    assert not wm.is_installed(spec, str(tmp_path))
    (tmp_path / wm.filename_for(spec)).write_text("password\n")
    assert wm.is_installed(spec, str(tmp_path))


def test_register_byo_valid_and_invalid(tmp_path):
    good = tmp_path / "mine.txt"
    good.write_text("hunter2\n")
    assert wm.register_byo(str(good)) == str(good)
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    with pytest.raises(ValueError):
        wm.register_byo(str(empty))
    with pytest.raises(ValueError):
        wm.register_byo(str(tmp_path / "missing.txt"))


# -- UI copy ----------------------------------------------------------

def test_install_choices_text_offers_both_paths():
    t = wm.install_choices_text().lower()
    assert "prepackaged" in t and "your own" in t and "does not bundle" in t


def test_catalog_text_lists_every_entry():
    t = wm.catalog_text()
    for w in wm.catalog():
        assert w.name in t
