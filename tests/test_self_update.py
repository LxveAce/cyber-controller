"""Tests for the Phase-2 in-place self-updater (:mod:`src.core.self_update`).

Everything destructive (downloads, overwriting a binary, os.execv) is stubbed. These tests only
cover asset selection, checksum parsing/verification, the frozen guard, the swap-script text,
and the orchestration wiring. No network, no real binary is ever touched.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from src.core import self_update as su
from src.core import updater

# ── platform_key ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,machine,expected", [
    ("Windows", "AMD64", "windows-x64"),
    ("windows", "x86", "windows-x64"),
    ("Darwin", "arm64", "macos-arm64"),
    ("Darwin", "x86_64", "macos-arm64"),   # we only ship arm64 mac
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Linux", "arm64", "linux-arm64"),
])
def test_platform_key(system, machine, expected):
    assert su.platform_key(system, machine) == expected


def test_platform_key_unsupported():
    with pytest.raises(su.SelfUpdateError):
        su.platform_key("Plan9", "pdp11")


# ── select_asset ────────────────────────────────────────────────────────────────────────────────

def _assets():
    return [
        {"name": "cyber-controller-v1.5.1-windows-x64.exe", "browser_download_url": "u1"},
        {"name": "cyber-controller-1.5.1-setup.exe", "browser_download_url": "u2"},
        {"name": "cyber-controller-v1.5.1-linux-x64", "browser_download_url": "u3"},
        {"name": "cyber-controller-v1.5.1-linux-arm64", "browser_download_url": "u4"},
        {"name": "cyber-controller-v1.5.1-macos-arm64", "browser_download_url": "u5"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "u6"},
    ]


@pytest.mark.parametrize("key,url", [
    ("windows-x64", "u1"),
    ("linux-x64", "u3"),
    ("linux-arm64", "u4"),
    ("macos-arm64", "u5"),
])
def test_select_asset_picks_onefile(key, url):
    got = su.select_asset(_assets(), key)
    assert got is not None and got["browser_download_url"] == url


def test_select_asset_skips_setup_installer():
    # windows selection must be the standalone .exe, never the setup installer.
    got = su.select_asset(_assets(), "windows-x64")
    assert "setup" not in got["name"].lower()


def test_select_asset_none_when_absent():
    only_win = [{"name": "cyber-controller-v1.5.1-windows-x64.exe", "browser_download_url": "u1"}]
    assert su.select_asset(only_win, "linux-x64") is None


def test_select_asset_linux_rejects_exe_and_txt():
    assets = [{"name": "cyber-controller-v1.5.1-linux-x64.exe", "browser_download_url": "x"}]
    assert su.select_asset(assets, "linux-x64") is None


# ── parse_sha256sums ──────────────────────────────────────────────────────────────────────────────

def test_parse_sha256sums_plain_and_binary_marker():
    a = "0" * 64
    b = "a" * 64
    text = f"{a}  cyber-controller-v1.5.1-linux-x64\n{b} *cyber-controller-v1.5.1-windows-x64.exe\n"
    sums = su.parse_sha256sums(text)
    assert sums["cyber-controller-v1.5.1-linux-x64"] == a
    assert sums["cyber-controller-v1.5.1-windows-x64.exe"] == b


def test_parse_sha256sums_skips_junk():
    text = "# a comment\n\nnot-a-hash file\n" + ("f" * 64) + "  good\n"
    sums = su.parse_sha256sums(text)
    assert sums == {"good": "f" * 64}


# ── sha256_file ───────────────────────────────────────────────────────────────────────────────────

def test_sha256_file(tmp_path):
    p = tmp_path / "blob.bin"
    data = b"cyber-controller onefile bytes" * 1000
    p.write_bytes(data)
    assert su.sha256_file(str(p)) == hashlib.sha256(data).hexdigest()


# ── find_release ──────────────────────────────────────────────────────────────────────────────────

def test_find_release_tolerates_v_prefix():
    releases = [{"tag_name": "v1.5.0"}, {"tag_name": "v1.5.1"}]
    assert su.find_release(releases, "1.5.1")["tag_name"] == "v1.5.1"
    assert su.find_release(releases, "v1.5.1")["tag_name"] == "v1.5.1"
    assert su.find_release(releases, "v9.9.9") is None


# ── win_swap_script ───────────────────────────────────────────────────────────────────────────────

def test_win_swap_script_content():
    s = su.win_swap_script(4242, r"C:\app\new.exe", r"C:\app\cur.exe")
    assert 'PID eq 4242' in s and 'find "4242"' in s      # waits on our PID
    assert 'goto wait' in s                                # loops until we exit
    assert 'move /Y "C:\\app\\new.exe" "C:\\app\\cur.exe"' in s  # swaps new over old
    assert 'start "" "C:\\app\\cur.exe"' in s              # relaunches
    assert 'del "%~f0"' in s                               # cleans itself up


def test_win_swap_script_gates_move_failure_with_breadcrumb():
    # A failed swap must NOT silently relaunch the stale exe as if it updated: the script gates on
    # move's errorlevel and drops a breadcrumb the next launch reads.
    cur, new = r"C:\app\cur.exe", r"C:\app\new.exe"
    s = su.win_swap_script(4242, new, cur)
    marker = su.failed_update_marker(cur)
    assert "move /Y" in s
    assert "if errorlevel 1" in s                           # the swap result is checked
    tail = s.split("if errorlevel 1", 1)[1]
    assert f'>"{marker}"' in tail                           # failed move writes a breadcrumb...
    assert f'staged update left at "{new}"' in tail         # ...that records the orphaned .new
    assert f'del "{marker}"' in tail                        # a successful move clears a stale one
    assert f'start "" "{cur}"' in s                         # app still comes back
    assert 'del "%~f0"' in s


# ── _apply_windows: non-ASCII exe path (accented Windows username) ──────────────────────────────────

def test_apply_windows_writes_non_ascii_path_script(monkeypatch, tmp_path):
    """Regression: a non-ASCII exe path (e.g. an accented Windows username 'José') must NOT crash the
    swap-script write with UnicodeEncodeError. The old ascii-encoded write raised on the 'é' AFTER the
    new binary was already downloaded, verified, and staged. The script must be written in a code page
    that round-trips the path, so cmd.exe's move/start target the right file."""
    spawned = {}

    def fake_popen(argv, **kw):
        spawned["argv"] = argv
        return object()  # the real code ignores the return value

    monkeypatch.setattr(su.subprocess, "Popen", fake_popen)

    cur = r"C:\Users\José\AppData\Local\Programs\cyber-controller\cyber-controller.exe"
    new = cur + ".new"
    su._apply_windows(cur, new, pid=4242)  # must not raise

    script = spawned["argv"][-1]
    assert os.path.isfile(script)
    try:
        with open(script, "rb") as fh:
            text = fh.read().decode(su._oem_encoding())  # decode in the same code page it was written
        assert "José" in text                             # the accented path survived intact
        assert "4242" in text                             # PID still embedded
        assert '\r\n' in text                             # CRLF line endings preserved (binary write)
    finally:
        os.remove(script)


def test_apply_windows_encode_failure_is_fail_closed(monkeypatch):
    """If a path truly can't be encoded in the target code page, the failure surfaces as
    SelfUpdateError (the module's fail-closed contract), not a raw UnicodeEncodeError — so callers
    catching SelfUpdateError handle it cleanly."""
    monkeypatch.setattr(su, "_oem_encoding", lambda: "ascii")  # force the accented path to be unencodable

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn the swap helper when the script can't be written")

    monkeypatch.setattr(su.subprocess, "Popen", _no_spawn)

    with pytest.raises(su.SelfUpdateError):
        su._apply_windows(r"C:\Users\José\app.exe", r"C:\Users\José\app.exe.new", pid=1)


def test_failed_update_marker_roundtrip(tmp_path):
    cur = tmp_path / "cyber-controller.exe"
    cur.write_bytes(b"OLD")
    assert su.read_failed_update(str(cur)) is None          # clean launch: nothing to report

    # Simulate what the swap helper leaves behind on a failed move: a breadcrumb + orphaned .new.
    marker = su.failed_update_marker(str(cur))
    with open(marker, "w", encoding="ascii") as fh:
        fh.write("update did not apply - could not replace the running binary.")
    orphan = tmp_path / "cyber-controller-v9.9.9-windows-x64.exe.new"
    orphan.write_bytes(b"NEW")

    msg = su.read_failed_update(str(cur))                   # next launch surfaces the failure
    assert msg is not None and "did not apply" in msg

    su.clear_failed_update(str(cur))                        # acknowledging clears + sweeps leftovers
    assert su.read_failed_update(str(cur)) is None
    assert not os.path.exists(marker)
    assert not orphan.exists()


# ── frozen-build guard (destructive paths refuse on a source checkout) ────────────────────────────

def test_apply_refuses_when_not_frozen(monkeypatch):
    monkeypatch.setattr(su, "is_frozen", lambda: False)
    with pytest.raises(su.SelfUpdateError, match="non-frozen"):
        su.apply("/x/cur", "/x/new", "linux-x64")


def test_self_update_refuses_when_not_frozen(monkeypatch):
    monkeypatch.setattr(su, "is_frozen", lambda: False)
    r = updater.CheckResult(status="NEWER", latest_tag="v1.5.1")
    with pytest.raises(su.SelfUpdateError, match="non-frozen"):
        su.self_update(r, releases=[])


# ── fetch_sums requires a SHA256SUMS asset ────────────────────────────────────────────────────────

def test_fetch_sums_missing_is_fatal():
    with pytest.raises(su.SelfUpdateError, match="SHA256SUMS"):
        su.fetch_sums([{"name": "cyber-controller-v1.5.1-linux-x64", "browser_download_url": "u"}])


# ── orchestration: happy path + checksum-mismatch fail-closed (no restart, no network) ────────────

def _stage_env(monkeypatch, tmp_path, content: bytes):
    """Wire self_update so it runs fully offline against tmp_path, as a 'frozen linux-x64' build."""
    cur = tmp_path / "cyber-controller"
    cur.write_bytes(b"OLD BINARY")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(cur))
    monkeypatch.setattr(su, "platform_key", lambda *a, **k: "linux-x64")

    def fake_download(url, dest, timeout=su.DEFAULT_TIMEOUT, progress=None):
        with open(dest, "wb") as fh:
            fh.write(content)
        if progress:
            progress(len(content), len(content))
        return dest

    monkeypatch.setattr(su, "download_asset", fake_download)
    releases = [{
        "tag_name": "v1.5.1",
        "assets": [
            {"name": "cyber-controller-v1.5.1-linux-x64", "browser_download_url": "http://dl"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "http://sums"},
        ],
    }]
    return cur, releases


def test_self_update_happy_path_stages_verified_binary(monkeypatch, tmp_path):
    content = b"NEW BINARY v1.5.1 bytes"
    cur, releases = _stage_env(monkeypatch, tmp_path, content)
    good = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(
        su, "fetch_sums",
        lambda assets, timeout=su.DEFAULT_TIMEOUT: {"cyber-controller-v1.5.1-linux-x64": good})

    r = updater.CheckResult(status="NEWER", latest_tag="v1.5.1")
    staged = su.self_update(r, releases=releases, restart=False)

    assert staged.endswith("cyber-controller-v1.5.1-linux-x64.new")
    with open(staged, "rb") as fh:
        assert fh.read() == content
    # nothing left half-downloaded, and the old binary is untouched (swap only happens in apply()).
    assert not (tmp_path / "cyber-controller-v1.5.1-linux-x64.part").exists()
    assert cur.read_bytes() == b"OLD BINARY"


def test_self_update_checksum_mismatch_fails_closed(monkeypatch, tmp_path):
    content = b"TAMPERED BYTES"
    cur, releases = _stage_env(monkeypatch, tmp_path, content)
    monkeypatch.setattr(
        su, "fetch_sums",
        lambda assets, timeout=su.DEFAULT_TIMEOUT: {"cyber-controller-v1.5.1-linux-x64": "b" * 64})

    r = updater.CheckResult(status="NEWER", latest_tag="v1.5.1")
    with pytest.raises(su.SelfUpdateError, match="checksum mismatch"):
        su.self_update(r, releases=releases, restart=False)

    # fail-closed: the unverified download is deleted and nothing is staged.
    assert not (tmp_path / "cyber-controller-v1.5.1-linux-x64.part").exists()
    assert not (tmp_path / "cyber-controller-v1.5.1-linux-x64.new").exists()
