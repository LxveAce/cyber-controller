"""Regression guards for the 2026-07-13 portfolio-audit CC batch (ledger rows #3/#4/#5/#7/#8).

Four CONFIRMED defects in cyber-controller, each fixed minimally and pinned here:

    #4/#8 flash_core.read_bundle_manifest — validated an offset was PRESENT but not that it
      PARSES, so a manifest with a malformed "offset_hex"/"offset" slipped through and only blew up
      later in _bundle_offset at flash time (an uncaught ValueError inside flash_suicide). The
      reader now rejects an unparseable offset up front (twin of the universal-flasher fc799d7 fix).

    #3 crack_pipeline.run_aircrack — discarded aircrack-ng's exit code and stderr, so a capture
      with NO valid handshake (aircrack tested nothing and bailed) was laundered into the fabricated
      "dictionary exhausted" negative. It now distinguishes a genuine exhaustion (aircrack printed a
      live "N/M keys tested" line) from a bail-out and surfaces the real problem.

    #7 crack_pipeline.run_hashcat — an exit-0 (hashcat CRACKED the hash) whose --show read-back
      came back empty fell through to "dictionary exhausted", directly contradicting hashcat's own
      success exit. It now reports the honest read-back discrepancy instead of a false negative.

    #5 wardrive_tab — the default WiGLE CSV was a single fixed ~/wardrive-wigle.csv opened in
      truncating "w" mode (Simple mode hides the path field), so a second drive silently erased the
      first drive's capture. The default is now timestamped and every open rolls over to a fresh
      sibling name rather than clobbering an existing file.

Pure logic + fakes: no hardware, GPU, esptool, or external crack tool is invoked.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core import crack_pipeline as cp
from src.core import flash_core as fc

# ── #4/#8: read_bundle_manifest rejects an offset that is present but does not parse ────────

def _write_manifest(tmp_path, files):
    (tmp_path / "img.bin").write_bytes(b"\x00" * 16)  # a present file; only the offset is on trial
    (tmp_path / "bundle.json").write_text(
        json.dumps({"chip": "esp32", "files": files}), encoding="utf-8")
    return str(tmp_path)


def test_manifest_rejects_unparseable_offset_hex(tmp_path):
    bundle = _write_manifest(tmp_path, [{"file": "img.bin", "offset_hex": "0xZZ"}])
    with pytest.raises(ValueError, match="unparseable offset"):
        fc.read_bundle_manifest(bundle)


def test_manifest_rejects_nonnumeric_offset(tmp_path):
    bundle = _write_manifest(tmp_path, [{"file": "img.bin", "offset": "not-a-number"}])
    with pytest.raises(ValueError, match="unparseable offset"):
        fc.read_bundle_manifest(bundle)


def test_manifest_accepts_valid_offsets_and_bundle_offset_never_raises(tmp_path):
    bundle = _write_manifest(tmp_path, [{"file": "img.bin", "offset_hex": "0x10000"}])
    manifest = fc.read_bundle_manifest(bundle)
    # The contract: after a successful read, _bundle_offset can never raise on a returned entry.
    assert fc._bundle_offset(manifest["files"][0]) == 0x10000


def test_manifest_accepts_decimal_offset(tmp_path):
    bundle = _write_manifest(tmp_path, [{"file": "img.bin", "offset": 65536}])
    manifest = fc.read_bundle_manifest(bundle)
    assert fc._bundle_offset(manifest["files"][0]) == 65536


# ── #3: run_aircrack tells a genuine exhaustion apart from a no-handshake bail-out ────────────────

def _capture_and_wordlist(tmp_path):
    cap = tmp_path / "c.pcap"
    cap.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 32)  # real file/ext; _run_tool is mocked

    wl = tmp_path / "w.txt"
    wl.write_text("password\nletmein\n", encoding="utf-8")
    return str(cap), str(wl)


def _aircrack_tools():
    return {cp.AIRCRACK: cp.ToolStatus(cp.AIRCRACK, path="aircrack-ng")}


def test_run_aircrack_no_handshake_is_not_reported_as_exhausted(monkeypatch, tmp_path):
    cap, wl = _capture_and_wordlist(tmp_path)
    # aircrack bailed: it never printed a "keys tested" line and exited non-zero with an error.
    monkeypatch.setattr(
        cp, "_run_tool",
        lambda a, t, on_proc=None: (1, "No networks found, exiting.\n", "no valid handshakes"))
    res = cp.run_aircrack(cap, wl, lambda _s: None, tools=_aircrack_tools())
    assert res.cracked is False
    assert "dictionary exhausted" not in res.detail  # the false claim must NOT appear
    assert "tested no keys" in res.detail            # the honest bail-out negative


def test_run_aircrack_real_exhaustion_still_reads_as_not_in_wordlist(monkeypatch, tmp_path):
    cap, wl = _capture_and_wordlist(tmp_path)
    # aircrack actually ran the wordlist (a "keys tested" progress line) and found nothing.
    out = "      [00:00:02] 2/2 keys tested (12.34 k/s)\n\n            KEY NOT FOUND\n"
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (1, out, ""))
    res = cp.run_aircrack(cap, wl, lambda _s: None, tools=_aircrack_tools())
    assert res.cracked is False
    assert "dictionary exhausted" in res.detail


def test_run_aircrack_key_found_still_wins(monkeypatch, tmp_path):
    cap, wl = _capture_and_wordlist(tmp_path)
    out = "      [00:00:01] 1/2 keys tested\n\nKEY FOUND! [ letmein ]\n"
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (0, out, ""))
    res = cp.run_aircrack(cap, wl, lambda _s: None, tools=_aircrack_tools())
    assert res.cracked is True and res.password == "letmein"


# ── #7: run_hashcat exit-0 with an empty --show read-back is a discrepancy, not "exhausted" ───────

def _hash_and_wordlist(tmp_path):
    hf = tmp_path / "h.hc22000"
    hf.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    wl = tmp_path / "w.txt"
    wl.write_text("password\n", encoding="utf-8")
    return str(hf), str(wl)


def test_run_hashcat_exit0_empty_readback_is_not_exhausted(monkeypatch, tmp_path):
    hf, wl = _hash_and_wordlist(tmp_path)
    tools = {cp.HASHCAT: cp.ToolStatus(cp.HASHCAT, path="hashcat")}
    # hashcat exited 0 (a crack), but --show returned nothing (potfile race / read failure).
    monkeypatch.setattr(cp, "_run_tool", lambda a, t, on_proc=None: (0, "Cracked", ""))
    monkeypatch.setattr(cp, "_read_show_results", lambda *a, **k: [])
    res = cp.run_hashcat(hf, wl, lambda _s: None, tools=tools)
    assert res.cracked is False
    assert "dictionary exhausted" not in res.detail   # never the false claim on a success exit
    assert "exit 0" in res.detail and "not trustworthy" in res.detail


def test_run_hashcat_exit0_with_readback_reports_the_key(monkeypatch, tmp_path):
    hf, wl = _hash_and_wordlist(tmp_path)
    tools = {cp.HASHCAT: cp.ToolStatus(cp.HASHCAT, path="hashcat")}
    monkeypatch.setattr(cp, "_run_tool", lambda a, t, on_proc=None: (0, "Cracked", ""))
    monkeypatch.setattr(
        cp, "_read_show_results",
        lambda *a, **k: [{"ssid": "Net", "bssid": "aa:bb:cc:dd:ee:ff", "password": "s3cret"}])
    res = cp.run_hashcat(hf, wl, lambda _s: None, tools=tools)
    assert res.cracked is True and res.password == "s3cret"


# ── #5: wardrive default path is unique and a run never truncates a prior drive's CSV ─────────────

wt = pytest.importorskip("src.ui.qt.wardrive_tab")


def test_nonclobber_returns_path_when_free(tmp_path):
    p = str(tmp_path / "drive.csv")
    assert wt._nonclobber_path(p) == p  # nothing there yet -> use it as-is


def test_nonclobber_rolls_over_existing_files(tmp_path):
    p = tmp_path / "drive.csv"
    p.write_text("first drive\n", encoding="utf-8")
    got = wt._nonclobber_path(str(p))
    assert got == str(tmp_path / "drive-1.csv")
    # the previous drive's bytes are untouched
    assert p.read_text(encoding="utf-8") == "first drive\n"


def test_nonclobber_finds_next_free_sibling(tmp_path):
    (tmp_path / "drive.csv").write_text("0", encoding="utf-8")
    (tmp_path / "drive-1.csv").write_text("1", encoding="utf-8")
    (tmp_path / "drive-2.csv").write_text("2", encoding="utf-8")
    assert wt._nonclobber_path(str(tmp_path / "drive.csv")) == str(tmp_path / "drive-3.csv")


def test_default_out_path_is_timestamped_csv():
    d = wt._default_out_path()
    base = os.path.basename(d)
    assert base.startswith("wardrive-wigle-") and base.endswith(".csv")
    assert base != "wardrive-wigle.csv"  # not the old fixed clobber-prone name
