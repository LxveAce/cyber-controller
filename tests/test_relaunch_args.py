"""Inert tests for the relaunch-args sidecar. No process launch, app restart, update, device,
network or package action: the module only does pure encoding and own-file I/O, and every failure
path is injected by monkeypatching os.open / os.fdopen / os.remove.
"""
from __future__ import annotations

import os

import pytest

from src.core import relaunch_args as ra

HOSTILE = [
    "--ui", "web", "--port", "8080",
    "a value with spaces", "", "unicöde–中文",
    'quotes"and\'apostrophe', "percent%50", "amp&ersand", "caret^x", "pipe|x",
    "semi;colon", "star*x", "back\\slash", "new\nline", "tab\tchar",
]


def _exe(tmp_path):
    return str(tmp_path / "cyber-controller.exe")


# ---- token format -------------------------------------------------------------------------------
def test_new_token_is_valid_and_fixed_length():
    t = ra.new_token()
    assert ra.is_valid_token(t) and len(t) == ra.TOKEN_HEX_LEN and t == t.lower()
    assert ra.new_token() != ra.new_token()  # per-attempt uniqueness


@pytest.mark.parametrize("bad", [
    None, 123, b"a" * 32, "", "AB" * 16, "z" * 32,
    "0" * 31, "0" * 33, "0" * 30 + "gg", " " + "0" * 31,
])
def test_is_valid_token_rejects_everything_but_exact_hex(bad):
    assert ra.is_valid_token(bad) is False


def test_sidecar_path_validates_token_before_deriving(tmp_path):
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.sidecar_path(_exe(tmp_path), "not-a-token")
    assert e.value.reason == "malformed-token"
    tok = ra.new_token()
    p = ra.sidecar_path(_exe(tmp_path), tok)
    assert p == os.path.realpath(_exe(tmp_path)) + ".cc-relaunch-" + tok + ".json"


# ---- encode / decode ----------------------------------------------------------------------------
def test_encode_decode_roundtrip_hostile_values():
    assert ra.decode_args(ra.encode_args(HOSTILE)) == HOSTILE


def test_encode_decode_lone_surrogate_roundtrips_without_raw_exception():
    # decode accepts an escaped lone surrogate; encode must not raise a raw UnicodeEncodeError on it
    # (ensure_ascii=True re-emits it as \uXXXX). Symmetric + lossless.
    surrogate = ["\ud800", "ok", "\udfff-tail"]
    assert ra.decode_args(ra.encode_args(surrogate)) == surrogate


def test_encode_empty_is_empty_list():
    assert ra.encode_args([]) == b"[]"
    assert ra.decode_args(b"[]") == []


def test_encode_rejects_non_string_element():
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.encode_args(["ok", 5])
    assert e.value.reason == "not-strings"


def test_encode_oversized():
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.encode_args(["x" * (ra.MAX_SIDECAR_BYTES + 10)])
    assert e.value.reason == "oversized"


@pytest.mark.parametrize("data,reason", [
    (b"x" * (ra.MAX_SIDECAR_BYTES + 1), "oversized"),
    (b"not json", "not-a-list"),
    (b"{}", "not-a-list"),
    (b'"a string"', "not-a-list"),
    (b'[1, 2]', "not-strings"),
    (b'["ok", 3]', "not-strings"),
    (b"\xff\xfe", "not-a-list"),          # invalid UTF-8
    (b"[" * 20000, "not-a-list"),         # deeply nested -> json.loads RecursionError (R1)
], ids=["oversized", "not-json", "dict", "string", "ints", "mixed", "bad-utf8", "nested"])
def test_decode_rejects(data, reason):
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.decode_args(data)
    assert e.value.reason == reason


@pytest.mark.parametrize("bad", ["abc", b"abc", bytearray(b"abc")])
def test_encode_rejects_bare_string(bad):
    # a bare str/bytes would silently split into one arg per character
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.encode_args(bad)
    assert e.value.reason == "not-strings"


def test_consume_nested_payload_deletes_and_reports(tmp_path):
    tok = ra.new_token()
    path = ra.sidecar_path(_exe(tmp_path), tok)
    with open(path, "wb") as fh:
        fh.write(b"[" * 20000)
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "not-a-list"
    assert not os.path.exists(path)  # file still removed on a RecursionError payload


def test_consume_fdopen_failure_closes_fd_and_leaves_file(tmp_path, monkeypatch):
    tok = ra.new_token()
    ra.write_sidecar(_exe(tmp_path), tok, ["a"])
    closed = {}
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.__setitem__("fd", fd), real_close(fd))[1])
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: (_ for _ in ()).throw(OSError("fdopen")))
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "io"
    assert "fd" in closed  # raw fd closed on the read side too (no leak)


def test_consume_ignores_mtime_no_freshness_expiry(tmp_path):
    tok = ra.new_token()
    path = ra.write_sidecar(_exe(tmp_path), tok, ["delayed"])
    old = os.stat(path).st_mtime - 7200  # 2 hours ago
    os.utime(path, (old, old))
    assert ra.consume_sidecar(_exe(tmp_path), tok) == ["delayed"]  # delayed relaunch restores


# ---- write / consume round trip -----------------------------------------------------------------
def test_write_then_consume_roundtrip_and_deletes(tmp_path):
    tok = ra.new_token()
    path = ra.write_sidecar(_exe(tmp_path), tok, HOSTILE)
    assert os.path.exists(path)
    assert ra.consume_sidecar(_exe(tmp_path), tok) == HOSTILE
    assert not os.path.exists(path)  # one-time consume deletes exactly this file


def test_write_empty_args_roundtrips_empty(tmp_path):
    tok = ra.new_token()
    ra.write_sidecar(_exe(tmp_path), tok, [])
    assert ra.consume_sidecar(_exe(tmp_path), tok) == []  # token issued -> sidecar present (P2)


def test_write_collision(tmp_path):
    tok = ra.new_token()
    ra.write_sidecar(_exe(tmp_path), tok, ["a"])
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.write_sidecar(_exe(tmp_path), tok, ["b"])
    assert e.value.reason == "collision"


def test_consume_missing(tmp_path):
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), ra.new_token())
    assert e.value.reason == "missing"


def test_repeated_consume_is_missing(tmp_path):
    tok = ra.new_token()
    ra.write_sidecar(_exe(tmp_path), tok, ["a"])
    ra.consume_sidecar(_exe(tmp_path), tok)
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "missing"


def test_consume_oversized_still_deletes(tmp_path):
    tok = ra.new_token()
    path = ra.sidecar_path(_exe(tmp_path), tok)
    with open(path, "wb") as fh:
        fh.write(b"x" * (ra.MAX_SIDECAR_BYTES + 100))
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "oversized"
    assert not os.path.exists(path)  # this attempt's own file is removed even on a bad payload


def test_consume_corrupt_still_deletes(tmp_path):
    tok = ra.new_token()
    path = ra.sidecar_path(_exe(tmp_path), tok)
    with open(path, "wb") as fh:
        fh.write(b"{not a list}")
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "not-a-list"
    assert not os.path.exists(path)


# ---- launch isolation: never consume/sweep a sibling ---------------------------------------------
def test_foreign_sidecar_survives_and_is_not_consumed(tmp_path):
    mine, foreign = ra.new_token(), ra.new_token()
    fpath = ra.write_sidecar(_exe(tmp_path), foreign, ["foreign"])
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), mine)          # my token -> my (absent) file only
    assert e.value.reason == "missing"
    assert os.path.exists(fpath)  # the foreign file is untouched (no sibling sweep, P3)


def test_no_sibling_sweep_on_success(tmp_path):
    a, b, c = ra.new_token(), ra.new_token(), ra.new_token()
    pa = ra.write_sidecar(_exe(tmp_path), a, ["a"])
    pb = ra.write_sidecar(_exe(tmp_path), b, ["b"])
    ra.write_sidecar(_exe(tmp_path), c, ["c"])
    ra.consume_sidecar(_exe(tmp_path), c)                    # consuming c leaves a and b alone
    assert os.path.exists(pa) and os.path.exists(pb)


# ---- injected filesystem failures ---------------------------------------------------------------
def test_write_open_io_failure(tmp_path, monkeypatch):
    real_open = os.open

    def boom(path, *a, **k):
        if ".cc-relaunch-" in str(path):
            raise OSError("open denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", boom)
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.write_sidecar(_exe(tmp_path), ra.new_token(), ["a"])
    assert e.value.reason == "io"


def test_write_fdopen_failure_closes_fd_and_removes(tmp_path, monkeypatch):
    closed = {}
    real_close = os.close

    def track_close(fd):
        closed["fd"] = fd
        return real_close(fd)

    def bad_fdopen(fd, *a, **k):
        raise OSError("fdopen failed")

    monkeypatch.setattr(os, "fdopen", bad_fdopen)
    monkeypatch.setattr(os, "close", track_close)
    tok = ra.new_token()
    path = ra.sidecar_path(_exe(tmp_path), tok)
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.write_sidecar(_exe(tmp_path), tok, ["a"])
    assert e.value.reason == "io"
    assert "fd" in closed                 # the raw fd was closed (no leak) before removal
    assert not os.path.exists(path)        # the partially created file was removed


def test_consume_delete_failure_is_io(tmp_path, monkeypatch):
    tok = ra.new_token()
    ra.write_sidecar(_exe(tmp_path), tok, ["a"])

    def bad_remove(path):
        raise OSError("remove denied")

    monkeypatch.setattr(os, "remove", bad_remove)
    with pytest.raises(ra.RelaunchArgsError) as e:
        ra.consume_sidecar(_exe(tmp_path), tok)
    assert e.value.reason == "io"


def test_module_does_no_env_or_launch():
    # Contract guard: the module never touches the environment or spawns anything.
    src = (os.path.join(os.path.dirname(ra.__file__), "relaunch_args.py"))
    text = open(src, encoding="utf-8").read()
    for forbidden in ("os.environ", "subprocess", "Popen", "os.exec", "os.system", "os.getenv"):
        assert forbidden not in text, forbidden
