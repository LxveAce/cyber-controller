"""Unit tests for strict release/asset selection helpers (src/core/update_select.py). Pure — no network."""
from __future__ import annotations

import pytest

from src.core import update_select as us


def _asset(name, size=1024):
    return {"name": name, "size": size, "browser_download_url": f"https://example/{name}"}


def _release(tag, *, draft=False, prerelease=False, assets=None):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets or []}


# ── supported_platform_key ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,machine,key", [
    ("Windows", "AMD64", "windows-x64"),
    ("Windows", "x86_64", "windows-x64"),
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Linux", "arm64", "linux-arm64"),
    ("Darwin", "arm64", "macos-arm64"),
])
def test_supported_platform_key_maps_exact_aliases(system, machine, key):
    assert us.supported_platform_key(system, machine) == key


@pytest.mark.parametrize("system,machine", [
    ("Linux", "riscv64"),
    ("Linux", "armv7l"),
    ("Linux", "i686"),
    ("Darwin", "x86_64"),      # Intel macOS is unsupported, NOT mapped to arm64
    ("Windows", "arm64"),
    ("FreeBSD", "x86_64"),
])
def test_unsupported_platform_is_rejected_not_coerced(system, machine):
    with pytest.raises(us.UnsupportedPlatform):
        us.supported_platform_key(system, machine)


# ── expected_asset_name ──────────────────────────────────────────────────────────────────────────────

def test_expected_asset_names():
    assert us.expected_asset_name("v2.0.1", "windows-x64") == "cyber-controller-v2.0.1-windows-x64.exe"
    assert us.expected_asset_name("v2.0.1", "windows-x64", installer=True) == \
        "cyber-controller-v2.0.1-windows-x64-setup.exe"
    assert us.expected_asset_name("v2.0.1", "linux-x64") == "cyber-controller-v2.0.1-linux-x64"
    assert us.expected_asset_name("v2.0.1", "macos-arm64") == "cyber-controller-v2.0.1-macos-arm64"


def test_expected_asset_name_rejects_non_windows_installer():
    with pytest.raises(us.ReleaseSelectionError):
        us.expected_asset_name("v2.0.1", "linux-x64", installer=True)


# ── select_release_asset ─────────────────────────────────────────────────────────────────────────────

def test_select_asset_exact_match():
    assets = [_asset("cyber-controller-v2.0.1-linux-x64"), _asset("SHA256SUMS.txt")]
    got = us.select_release_asset(assets, "v2.0.1", "linux-x64")
    assert got["name"] == "cyber-controller-v2.0.1-linux-x64"


@pytest.mark.parametrize("bad_name", [
    "cyber-controller-v2.0.1-linux-x64.zip",       # archive, not the extensionless binary
    "debug-symbols-linux-x64.tar.gz",              # a substring match the old selector accepted
    "cyber-controller-v2.0.1-linux-arm64",         # wrong arch
    "cyber-controller-v2.0.0-linux-x64",           # wrong tag
])
def test_select_asset_rejects_non_exact_or_archive(bad_name):
    assets = [_asset(bad_name), _asset("SHA256SUMS.txt")]
    with pytest.raises(us.ReleaseSelectionError):
        us.select_release_asset(assets, "v2.0.1", "linux-x64")


def test_select_asset_rejects_ambiguous_duplicate_name():
    dup = "cyber-controller-v2.0.1-linux-x64"
    with pytest.raises(us.ReleaseSelectionError):
        us.select_release_asset([_asset(dup), _asset(dup)], "v2.0.1", "linux-x64")


def test_select_asset_windows_portable_vs_installer():
    assets = [_asset("cyber-controller-v2.0.1-windows-x64.exe"),
              _asset("cyber-controller-v2.0.1-windows-x64-setup.exe")]
    assert us.select_release_asset(assets, "v2.0.1", "windows-x64")["name"].endswith("windows-x64.exe")
    assert us.select_release_asset(assets, "v2.0.1", "windows-x64", installer=True)["name"].endswith("setup.exe")


# ── select_published_release ─────────────────────────────────────────────────────────────────────────

def test_select_published_release_exact_tag():
    rels = [_release("v2.0.0"), _release("v2.0.1")]
    assert us.select_published_release(rels, "v2.0.1")["tag_name"] == "v2.0.1"


def test_select_release_requires_exact_tag_not_digit_equivalence():
    # "2.0.1" parses equal to "v2.0.1" but is NOT the exact tag — apply must not pin a different string.
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([_release("v2.0.1")], "2.0.1")


def test_select_release_rejects_draft_and_prerelease():
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([_release("v3.0.0", draft=True)], "v3.0.0")
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([_release("v3.0.0", prerelease=True)], "v3.0.0")


def test_select_release_missing_tag():
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([_release("v2.0.0")], "v9.9.9")


# ── validate_asset_size ──────────────────────────────────────────────────────────────────────────────

def test_validate_asset_size_accepts_plausible_int():
    assert us.validate_asset_size({"size": 40 * 1024 * 1024}) == 40 * 1024 * 1024


@pytest.mark.parametrize("size", [None, True, False, 0, -1, 1.5, "1024", 2 * us.MAX_ASSET_BYTES])
def test_validate_asset_size_rejects_bad(size):
    with pytest.raises(us.ReleaseSelectionError):
        us.validate_asset_size({"size": size})


# ── parse_sums_strict + checksum_for ─────────────────────────────────────────────────────────────────

def test_parse_sums_strict_parses_and_dedups_identical():
    d = "a" * 64
    text = f"{d}  cyber-controller-v2.0.1-linux-x64\n{d} *cyber-controller-v2.0.1-linux-x64\n"
    sums = us.parse_sums_strict(text)
    assert sums["cyber-controller-v2.0.1-linux-x64"] == d


def test_parse_sums_strict_rejects_conflicting_digest():
    text = ("a" * 64) + "  bin\n" + ("b" * 64) + "  bin\n"
    with pytest.raises(us.ReleaseSelectionError):
        us.parse_sums_strict(text)


def test_checksum_for_found_and_missing():
    d = "c" * 64
    assert us.checksum_for({"bin": d}, "bin") == d
    with pytest.raises(us.ReleaseSelectionError):
        us.checksum_for({"bin": d}, "other")


# ── end-to-end selection over a synthetic release ────────────────────────────────────────────────────

def test_end_to_end_pins_exact_release_asset_size_and_checksum():
    name = "cyber-controller-v2.0.1-linux-x64"
    digest = "d" * 64
    rel = _release("v2.0.1", assets=[_asset(name, size=50_000_000), _asset("SHA256SUMS.txt")])
    picked = us.select_published_release([rel], "v2.0.1")
    asset = us.select_release_asset(picked["assets"], "v2.0.1", "linux-x64")
    assert us.validate_asset_size(asset) == 50_000_000
    sums = us.parse_sums_strict(f"{digest}  {name}\n")
    assert us.checksum_for(sums, asset["name"]) == digest


# ── L2A-1: checksum filename identity ────────────────────────────────────────────────────────────────

def test_sums_keeps_full_filename_with_spaces_and_binary_marker():
    d = "a" * 64
    n = "cyber-controller-v2.0.1-linux-x64"
    # text mode with a filename that contains a space, and binary mode ('*') for the real target
    sums = us.parse_sums_strict(f"{d}  unrelated {n}\n" + ("b" * 64) + f" *{n}\n")
    assert sums["unrelated " + n] == d          # the full filename is the identity, not the last token
    assert sums[n] == "b" * 64                   # binary-mode '*' marker is not part of the filename
    assert "unrelated" not in us.checksum_for(sums, n)


def test_sums_double_asterisk_filename_not_stripped():
    d = "c" * 64
    sums = us.parse_sums_strict(f"{d}  **weird-name\n")
    assert sums["**weird-name"] == d             # leading '*' in the filename is preserved
    assert "weird-name" not in sums              # the stripped form is NOT created


def test_sums_no_false_conflict_for_different_filenames():
    n = "cyber-controller-v2.0.1-linux-x64"
    sums = us.parse_sums_strict(("a" * 64) + f"  {n}\n" + ("b" * 64) + f"  other {n}\n")
    assert us.checksum_for(sums, n) == "a" * 64  # different filenames -> no false ambiguous-digest conflict


# ── L2A-2: exact string tag + explicit boolean publication flags ─────────────────────────────────────

def test_select_release_rejects_numeric_tag_coercion():
    rel = {"tag_name": 201, "draft": False, "prerelease": False}   # numeric tag_name, not a string
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([rel], "201")


@pytest.mark.parametrize("flags", [
    {"prerelease": False},                       # draft missing
    {"draft": None, "prerelease": False},
    {"draft": False},                            # prerelease missing
    {"draft": False, "prerelease": []},
    {"draft": 0, "prerelease": 0},
])
def test_select_release_requires_explicit_boolean_flags(flags):
    rel = {"tag_name": "v3.0.0", **flags}
    with pytest.raises(us.ReleaseSelectionError):
        us.select_published_release([rel], "v3.0.0")


def test_select_release_accepts_explicit_false_flags():
    rel = {"tag_name": "v3.0.0", "draft": False, "prerelease": False}
    assert us.select_published_release([rel], "v3.0.0")["tag_name"] == "v3.0.0"


# ── L2A-3: strict system + key ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("system", ["WindowsCE", "win-not-an-os", "winnt", "wince"])
def test_platform_key_rejects_unknown_system_prefix(system):
    with pytest.raises(us.UnsupportedPlatform):
        us.supported_platform_key(system, "amd64")


@pytest.mark.parametrize("key", ["linux-riscv64", "macos-x64", "windows-arm64", "linux-x86"])
def test_expected_asset_name_rejects_unsupported_key(key):
    with pytest.raises(us.ReleaseSelectionError):
        us.expected_asset_name("v2.0.1", key)


def test_select_asset_rejects_unsupported_key():
    assets = [_asset("cyber-controller-v2.0.1-linux-riscv64")]
    with pytest.raises(us.ReleaseSelectionError):
        us.select_release_asset(assets, "v2.0.1", "linux-riscv64")


# ── L2A-4: the size ceiling itself must be a positive plain int ──────────────────────────────────────

@pytest.mark.parametrize("maximum", [float("inf"), float("nan"), 0, -1, True, 1.5])
def test_validate_asset_size_rejects_nonfinite_maximum(maximum):
    with pytest.raises(us.ReleaseSelectionError):
        us.validate_asset_size({"size": 2 * us.MAX_ASSET_BYTES}, maximum=maximum)


# ── L2A-1b: non-LF control chars in a filename are not record boundaries ──────────────────────────────

@pytest.mark.parametrize("sep", ["\v", "\f", "\x85", " ", " "])
def test_sums_non_lf_control_in_filename_is_not_a_record_boundary(sep):
    n = "cyber-controller-v2.0.1-linux-x64"
    d = "a" * 64
    sums = us.parse_sums_strict(f"{d}  {n}{sep}backup\n")
    assert n not in sums                      # the literal char is part of the filename, not a record split
    assert sums[n + sep + "backup"] == d      # ...and the full record filename carries the digest


def test_sums_still_splits_on_lf_and_crlf():
    d, e = "a" * 64, "b" * 64
    sums = us.parse_sums_strict(f"{d}  one\r\n{e}  two\n")   # CRLF + LF both split, \r trimmed
    assert sums["one"] == d and sums["two"] == e


def test_sums_removes_only_the_crlf_terminator_cr():
    # L2A-1c: a checksum record is terminated by LF; a CRLF contributes exactly ONE CR immediately before it.
    # rstrip("\r") wrongly removed EVERY trailing CR — a filename's own CR, and a CR on the unterminated final
    # record — silently authorizing the canonical name. Only the single CRLF-terminator CR may be removed.
    H = "a" * 64
    N = "cyber-controller-v2.0.1-linux-x64"

    # Fail-on-old: none of these may authorize the canonical name N; the extra/remaining CR is filename data.
    for trailer, kept in [
        ("\r\r\n", N + "\r"),        # extra CR before CRLF -> one CR remains in the filename
        ("\r\r\r\n", N + "\r\r"),    # repeated CRs before CRLF
        ("\r", N + "\r"),            # unterminated final record: no CRLF terminator -> no CR removed
        ("\r\r", N + "\r\r"),        # unterminated final record, repeated CR
    ]:
        sums = us.parse_sums_strict(f"{H}  {N}{trailer}")
        assert N not in sums                 # canonical name NOT authorized (the pre-fix code authorized it)
        assert sums[kept] == H               # the digest stays bound to the real, CR-bearing filename

    # Controls: supported terminators and an internal CR resolve to the exact intended identity.
    assert us.parse_sums_strict(f"{H}  {N}\n")[N] == H          # bare LF
    assert us.parse_sums_strict(f"{H}  {N}\r\n")[N] == H        # one CRLF terminator -> its single CR removed
    assert us.parse_sums_strict(f"{H}  {N}")[N] == H            # ordinary unterminated final line, no CR
    internal = us.parse_sums_strict(f"{H}  foo\rbar\n")         # internal CR is filename data, preserved
    assert internal["foo\rbar"] == H and "foobar" not in internal


# ── L2A-4b: the plain-int contract rejects int subclasses ────────────────────────────────────────────

def test_validate_asset_size_rejects_int_subclass_size_and_maximum():
    import enum

    class Count(int):
        pass

    class E(enum.IntEnum):
        X = 999

    with pytest.raises(us.ReleaseSelectionError):
        us.validate_asset_size({"size": Count(1024)})                    # subclass size rejected
    with pytest.raises(us.ReleaseSelectionError):
        us.validate_asset_size({"size": 1024}, maximum=Count(2048))      # subclass maximum rejected
    with pytest.raises(us.ReleaseSelectionError):
        us.validate_asset_size({"size": 1024}, maximum=E.X)              # IntEnum maximum rejected
