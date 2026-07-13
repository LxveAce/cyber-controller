"""Regression guards for the 2026-07-12 crack-pipeline adversarial audit (cc-crack-audit).

Seven CONFIRMED defects in the WPA/PMKID crack subsystem, each triggered by realistic capture /
wordlist / subprocess-output input and each fixed minimally:

    A crack_pipeline.run_hashcat  — a timeout was laundered into a false "dictionary exhausted"
      negative (claimed the whole wordlist was tried). Now returns the honest timeout negative.
    B wpa_capture._iter_records   — a pcapng Enhanced Packet Block sliced packet data from the wrong
      offset (skipped 4 of the 5 leading u32 fields), so every frame in a real pcapng was misaligned
      by 4 bytes and no handshake was ever extracted.
    C convert_capture + crack_lab — a no-handshake capture yielded a 0-byte .hc22000 that was still
      fed to hashcat and reported as a TOOL FAILURE instead of the honest "no handshake" negative.
    D crack_pipeline.parse_aircrack_output — the KEY-FOUND regex truncated a passphrase containing
      ']' and stripped genuine edge whitespace, reporting a WRONG key.
    E convert_capture              — the hcxpcapngtool convert phase was not cancellable (Stop was a
      no-op until it finished); it now accepts on_proc and runs through the killable _run_tool.
    F _CrackWorker.run            — a key recovered in the same instant as Stop was discarded and
      reported as "stopped"; a successful crack now survives a late Stop.
    G CrackLabTab._on_done        — the write-back recorded the CURRENT combo wordlist, not the one
      the run actually used; it now records the run's wordlist.

Pure logic / offscreen Qt / fakes — no hardware, GPU, or external crack tool is invoked.
"""

from __future__ import annotations

import os
import struct
import subprocess

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core import crack_pipeline as cp


# ── A: run_hashcat timeout is an honest negative, not "dictionary exhausted" ──────────────────────

def test_run_hashcat_timeout_is_not_reported_as_exhausted(monkeypatch, tmp_path):
    hash_file = tmp_path / "h.hc22000"
    hash_file.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    wl = tmp_path / "w.txt"
    wl.write_text("password\n", encoding="utf-8")
    tools = {cp.HASHCAT: cp.ToolStatus(cp.HASHCAT, path="hashcat")}

    def _timeout(argv, timeout, on_proc=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(cp, "_run_tool", _timeout)
    monkeypatch.setattr(cp, "_read_show_results", lambda *a, **k: [])  # nothing cracked before the kill

    res = cp.run_hashcat(str(hash_file), str(wl), lambda _s: None, tools=tools, timeout=1)
    assert res.cracked is False
    assert res.detail == "timed out before exhausting the wordlist"
    assert "exhausted" not in res.detail or "before exhausting" in res.detail  # never the false claim


def test_run_hashcat_real_exhaustion_still_reads_as_not_in_wordlist(monkeypatch, tmp_path):
    # Guard the other side: a genuine exit-1 (exhausted) must STILL say "dictionary exhausted".
    hash_file = tmp_path / "h.hc22000"
    hash_file.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    wl = tmp_path / "w.txt"
    wl.write_text("password\n", encoding="utf-8")
    tools = {cp.HASHCAT: cp.ToolStatus(cp.HASHCAT, path="hashcat")}
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (1, "Status: Exhausted", ""))
    monkeypatch.setattr(cp, "_read_show_results", lambda *a, **k: [])
    res = cp.run_hashcat(str(hash_file), str(wl), lambda _s: None, tools=tools)
    assert res.cracked is False and "dictionary exhausted" in res.detail


# ── B: pcapng Enhanced Packet Block packet data is sliced at the correct offset ───────────────────

def _pcapng_with_epb(frame: bytes, linktype: int = 105) -> bytes:
    """A minimal spec-valid little-endian pcapng: SHB + IDB + one Enhanced Packet Block wrapping *frame*."""
    # Section Header Block (28 bytes)
    shb = struct.pack("<IIIHHqI", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1, 28)
    # Interface Description Block (20 bytes): linktype + reserved + snaplen
    idb = struct.pack("<IIHHII", 0x00000001, 20, linktype, 0, 65535, 20)
    # Enhanced Packet Block: iface_id, ts_high, ts_low, caplen, origlen, data (4-byte padded)
    caplen = len(frame)
    pad = (-caplen) % 4
    data = frame + b"\x00" * pad
    epb_len = 32 + len(data)  # 4 type + 4 len + 5*4 fields + data + 4 len
    epb = struct.pack("<IIIIIII", 0x00000006, epb_len, 0, 0, 0, caplen, caplen) + data + struct.pack("<I", epb_len)
    return shb + idb + epb


def test_pcapng_epb_frame_is_extracted_at_correct_offset():
    from src.core.wpa_capture import _iter_records

    frame = b"\xAA\xBB\xCC\xDD\x11\x22\x33\x44"  # 8-byte marker (linktype 105 => yielded verbatim)
    records = list(_iter_records(_pcapng_with_epb(frame)))
    assert records, "the EPB frame must be yielded (pre-fix: mis-sliced but still yielded garbage)"
    lt, got = records[-1]
    assert lt == 105
    # Pre-fix the slice started 4 bytes early, prefixing the frame with the origlen u32 (0x08000000 LE)
    # and truncating the tail. The correct frame is the marker verbatim, byte-for-byte.
    assert got == frame
    assert not got.startswith(b"\x08\x00\x00\x00")  # the mis-slice signature must be absent


# ── C: convert_capture reports an honest "no handshake" instead of feeding hashcat an empty file ──

def _valid_capture(tmp_path):
    cap = tmp_path / "c.pcap"
    cap.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20)  # any bytes; convert is mocked, only ext+exist matter
    return cap


def test_convert_capture_empty_output_is_honest_zero(monkeypatch, tmp_path):
    cap = _valid_capture(tmp_path)
    out = tmp_path / "out.hc22000"
    out.write_text("", encoding="utf-8")  # UI pre-creates the temp file -> present but empty
    tools = {cp.CONVERTER: cp.ToolStatus(cp.CONVERTER, path="hcxpcapngtool")}
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (0, "", ""))
    lines: list[str] = []
    n = cp.convert_capture(str(cap), str(out), lines.append, tools=tools)
    assert n == 0
    assert any("no PMKID or handshake" in ln for ln in lines)


def test_convert_capture_nonempty_but_zero_hashes_is_honest_zero(monkeypatch, tmp_path):
    cap = _valid_capture(tmp_path)
    out = tmp_path / "out.hc22000"
    out.write_text("garbage line with no WPA star prefix\n", encoding="utf-8")  # present, non-empty, 0 hashes
    tools = {cp.CONVERTER: cp.ToolStatus(cp.CONVERTER, path="hcxpcapngtool")}
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (0, "", ""))
    lines: list[str] = []
    n = cp.convert_capture(str(cap), str(out), lines.append, tools=tools)
    assert n == 0
    assert any("no PMKID or handshake" in ln for ln in lines)


def test_convert_capture_counts_real_hashes(monkeypatch, tmp_path):
    cap = _valid_capture(tmp_path)
    out = tmp_path / "out.hc22000"
    out.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    tools = {cp.CONVERTER: cp.ToolStatus(cp.CONVERTER, path="hcxpcapngtool")}
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (0, "", ""))
    n = cp.convert_capture(str(cap), str(out), lambda _s: None, tools=tools)
    assert n == 1


# ── D: aircrack key regex preserves ']' and edge whitespace ───────────────────────────────────────

def test_parse_aircrack_key_with_internal_bracket():
    assert cp.parse_aircrack_output("KEY FOUND! [ pa]s w0rd ]") == "pa]s w0rd"


def test_parse_aircrack_key_with_trailing_space():
    assert cp.parse_aircrack_output("KEY FOUND! [ mypass12  ]") == "mypass12 "


def test_parse_aircrack_key_existing_cases_unchanged():
    assert cp.parse_aircrack_output("KEY FOUND! [ correcthorse ]") == "correcthorse"
    assert cp.parse_aircrack_output("      KEY FOUND! [ p@ss w0rd ]  ") == "p@ss w0rd"
    assert cp.parse_aircrack_output("Passphrase not in dictionary") is None
    assert cp.parse_aircrack_output("") is None


# ── E: convert_capture hands its child to on_proc (cancellable) ──────────────────────────────────

def test_convert_capture_passes_child_to_on_proc(monkeypatch, tmp_path):
    cap = _valid_capture(tmp_path)
    out = tmp_path / "out.hc22000"
    out.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    tools = {cp.CONVERTER: cp.ToolStatus(cp.CONVERTER, path="hcxpcapngtool")}
    seen = {}

    def _capture_on_proc(argv, timeout, on_proc=None):
        seen["on_proc"] = on_proc
        return (0, "", "")

    monkeypatch.setattr(cp, "_run_tool", _capture_on_proc)
    sentinel = lambda _proc: None
    cp.convert_capture(str(cap), str(out), lambda _s: None, tools=tools, on_proc=sentinel)
    assert seen.get("on_proc") is sentinel  # the killable child is wired through to Stop


# ── F + G: Qt worker / write-back (offscreen) ────────────────────────────────────────────────────

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402
import src.ui.qt.crack_lab_tab as clt  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_worker_keeps_recovered_key_on_late_stop(qapp, monkeypatch):
    # F: a key that verified just as Stop fired must survive — never discarded as "stopped".
    monkeypatch.setattr(cp, "detect_tools", lambda: {})
    w = clt._CrackWorker("cap.pcap", "words.txt", "native")

    def _fake_native(capture, wordlist, on_line, bssid="", should_stop=None):
        w._stop = True  # user clicks Stop the instant the passphrase verifies
        return cp.CrackResult(cracked=True, password="s3cret", ssid="Net", detail="key recovered")

    monkeypatch.setattr(cp, "run_native", _fake_native)
    got: list = []
    w.done.connect(got.append)
    w.run()
    assert got and got[0].cracked is True and got[0].password == "s3cret"


def test_worker_reports_stopped_when_nothing_cracked(qapp, monkeypatch):
    # F (other side): a Stop with no recovered key still honestly reports "stopped".
    monkeypatch.setattr(cp, "detect_tools", lambda: {})
    w = clt._CrackWorker("cap.pcap", "words.txt", "native")

    def _fake_native(capture, wordlist, on_line, bssid="", should_stop=None):
        w._stop = True
        return cp.CrackResult(cracked=False, detail="key not in wordlist (dictionary exhausted)")

    monkeypatch.setattr(cp, "run_native", _fake_native)
    got: list = []
    w.done.connect(got.append)
    w.run()
    assert got and got[0].cracked is False and got[0].detail == "stopped"


def test_worker_short_circuits_hashcat_when_no_handshake(qapp, monkeypatch):
    # C: a 0-hash conversion must NOT spawn hashcat; it reports the honest "no handshake" negative.
    called = {"hashcat": False}
    monkeypatch.setattr(cp, "detect_tools", lambda: {})
    monkeypatch.setattr(cp, "convert_capture", lambda *a, **k: 0)

    def _run_hashcat(*a, **k):
        called["hashcat"] = True
        return cp.CrackResult(cracked=True, password="X")

    monkeypatch.setattr(cp, "run_hashcat", _run_hashcat)
    w = clt._CrackWorker("capture.pcap", "words.txt", "hashcat")
    got: list = []
    w.done.connect(got.append)
    w.run()
    assert called["hashcat"] is False
    assert got and got[0].cracked is False and "no PMKID or handshake" in got[0].detail


def test_worker_runs_hashcat_when_hashes_extracted(qapp, monkeypatch):
    called = {"hashcat": False}
    monkeypatch.setattr(cp, "detect_tools", lambda: {})
    monkeypatch.setattr(cp, "convert_capture", lambda *a, **k: 3)

    def _run_hashcat(*a, **k):
        called["hashcat"] = True
        return cp.CrackResult(cracked=False, detail="key not in wordlist (dictionary exhausted)")

    monkeypatch.setattr(cp, "run_hashcat", _run_hashcat)
    w = clt._CrackWorker("capture.pcap", "words.txt", "hashcat")
    got: list = []
    w.done.connect(got.append)
    w.run()
    assert called["hashcat"] is True


def test_on_done_records_the_run_wordlist_not_the_combo(qapp):
    # G: write-back must record the wordlist the run actually used, not whatever the combo shows now.
    tab = clt.CrackLabTab()

    class _Caps:
        def __init__(self):
            self.calls: list = []

        def mark_cracked(self, key, password, detail, wordlist):
            self.calls.append((key, password, detail, wordlist))

    caps = _Caps()
    tab._captures = caps
    tab._active_capture_key = "cap-key-1"
    tab._worker = clt._CrackWorker("cap.pcap", "ACTUAL_LIST.txt", "native")
    # simulate the operator changing the wordlist selection mid-run
    tab._wordlist_combo.clear()
    tab._wordlist_combo.addItem("changed", "CHANGED_LIST.txt")
    tab._on_done(cp.CrackResult(cracked=True, password="pw", ssid="Net", detail="key recovered"))
    assert caps.calls, "a cracked result on a loaded capture must write back"
    assert caps.calls[0][3] == "ACTUAL_LIST.txt"  # the run's list, NOT the combo's current selection
