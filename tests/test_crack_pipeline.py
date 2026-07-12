"""Tests for ``src.core.crack_pipeline`` — the pure/argv/parse core of the WPA crack pipeline.

No hardware, no GPU, and none of the external tools (hcxpcapngtool/hashcat/aircrack-ng) need to be
installed: everything tested here shapes argv strings, parses tool output, or validates paths. The
subprocess orchestration is not exercised here (it needs the real tools) -- but its argv and its
result parsing, which are the parts that could be *wrong*, are pure and fully covered.
"""

from __future__ import annotations

import pytest

cp = pytest.importorskip("src.core.crack_pipeline")

from src.core.crack_pipeline import (  # noqa: E402
    HASHCAT_MODE_WPA,
    ToolStatus,
    available_backends,
    build_aircrack_argv,
    build_convert_argv,
    build_hashcat_argv,
    capability_text,
    consent_prompt_text,
    count_extractable,
    detect_tools,
    missing_tools_text,
    parse_aircrack_output,
    parse_hashcat_show,
    validate_capture,
    validate_wordlist,
)

# A realistic hashcat-22000 PMKID line for ESSID "TestNet" (546573744e6574), AP aabbccddeeff.
_HASHLINE = "WPA*01*2582a8281bf9d4308d6f5731d0e61c61*aabbccddeeff*112233445566*546573744e6574***"


# ── ToolStatus / detection ───────────────────────────────────────────

def test_toolstatus_present() -> None:
    assert not ToolStatus("hashcat").present
    assert ToolStatus("hashcat", path="/usr/bin/hashcat").present


def test_available_backends_matrix() -> None:
    def s(present: dict[str, bool]) -> dict[str, ToolStatus]:
        return {n: ToolStatus(n, path=("/x/" + n if present.get(n) else None)) for n in
                ("hcxpcapngtool", "hashcat", "aircrack-ng")}

    # "native" (CC's own cracker) is ALWAYS available and listed first; the external tools layer on top.
    # hashcat path needs BOTH converter and hashcat; aircrack needs only itself.
    assert available_backends(s({"hcxpcapngtool": True, "hashcat": True})) == ["native", "hashcat"]
    assert available_backends(s({"aircrack-ng": True})) == ["native", "aircrack"]
    assert available_backends(s({"hashcat": True})) == ["native"]  # converter missing -> no hashcat path
    both = available_backends(s({"hcxpcapngtool": True, "hashcat": True, "aircrack-ng": True}))
    assert both == ["native", "hashcat", "aircrack"]  # native, then hashcat, then aircrack
    assert available_backends(s({})) == ["native"]  # native always works out of the box


def test_detect_tools_all_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cp.shutil, "which", lambda _n: None)
    tools = detect_tools()
    assert set(tools) == {"hcxpcapngtool", "hashcat", "aircrack-ng"}
    assert all(not t.present for t in tools.values())


# ── input validation ─────────────────────────────────────────────────

def test_validate_capture(tmp_path) -> None:
    good = tmp_path / "cap.pcapng"
    good.write_bytes(b"\x00")
    assert validate_capture(str(good)) == str(good)
    with pytest.raises(ValueError):
        validate_capture(str(tmp_path / "missing.pcapng"))
    wrong = tmp_path / "notes.txt"
    wrong.write_text("x")
    with pytest.raises(ValueError):
        validate_capture(str(wrong))
    with pytest.raises(ValueError):
        validate_capture("")


def test_validate_wordlist(tmp_path) -> None:
    wl = tmp_path / "rockyou.txt"
    wl.write_text("password\n123456\n")
    assert validate_wordlist(str(wl)) == str(wl)
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    with pytest.raises(ValueError):
        validate_wordlist(str(empty))  # empty wordlist == tried nothing
    with pytest.raises(ValueError):
        validate_wordlist(str(tmp_path / "nope.txt"))


# ── argv construction ────────────────────────────────────────────────

def test_build_convert_argv() -> None:
    argv = build_convert_argv("in.pcapng", "out.hc22000")
    assert argv == ["hcxpcapngtool", "-o", "out.hc22000", "in.pcapng"]


def test_build_hashcat_argv_is_dictionary_only() -> None:
    argv = build_hashcat_argv("h.hc22000", "wl.txt")
    assert argv[:6] == ["hashcat", "-m", HASHCAT_MODE_WPA, "-a", "0", "h.hc22000"]
    assert argv[-1] == "wl.txt"
    # Load-bearing honesty invariant: mode is straight/dictionary (-a 0), NEVER mask/brute (-a 3).
    assert argv[argv.index("-a") + 1] == "0"
    assert "3" not in argv


def test_build_hashcat_argv_show() -> None:
    assert "--show" in build_hashcat_argv("h", "w", show=True)
    assert "--show" not in build_hashcat_argv("h", "w", show=False)


def test_build_aircrack_argv() -> None:
    assert build_aircrack_argv("cap.pcap", "wl.txt") == ["aircrack-ng", "-w", "wl.txt", "cap.pcap"]
    with_b = build_aircrack_argv("cap.pcap", "wl.txt", bssid="AA:BB:CC:DD:EE:FF")
    assert with_b == ["aircrack-ng", "-w", "wl.txt", "-b", "AA:BB:CC:DD:EE:FF", "cap.pcap"]


# ── output parsing ───────────────────────────────────────────────────

def test_count_extractable() -> None:
    text = f"{_HASHLINE}\n{_HASHLINE}\n# a comment\n\n"
    assert count_extractable(text) == 2
    assert count_extractable("") == 0
    assert count_extractable("no hashes here\n") == 0


def test_parse_hashcat_show_decodes_essid_and_password() -> None:
    creds = parse_hashcat_show(f"{_HASHLINE}:hunter2\n")
    assert len(creds) == 1
    assert creds[0]["ssid"] == "TestNet"
    assert creds[0]["bssid"] == "aabbccddeeff"
    assert creds[0]["password"] == "hunter2"


def test_parse_hashcat_show_password_with_colons() -> None:
    # The 22000 hashline has no ':' of its own, so a password containing ':' must survive intact.
    creds = parse_hashcat_show(f"{_HASHLINE}:pa:ss:word\n")
    assert creds[0]["password"] == "pa:ss:word"


def test_parse_hashcat_show_ignores_noise() -> None:
    assert parse_hashcat_show("Session..........: hashcat\nStatus...: Exhausted\n") == []
    assert parse_hashcat_show("") == []


def test_parse_aircrack_output() -> None:
    assert parse_aircrack_output("KEY FOUND! [ correcthorse ]") == "correcthorse"
    assert parse_aircrack_output("      KEY FOUND! [ p@ss w0rd ]  ") == "p@ss w0rd"
    assert parse_aircrack_output("Passphrase not in dictionary") is None
    assert parse_aircrack_output("") is None


# ── consent / honesty copy ───────────────────────────────────────────

def test_consent_prompt_names_target() -> None:
    assert "AUTHORIZED USE ONLY" in consent_prompt_text()
    assert "MyWiFi" in consent_prompt_text(ssid="MyWiFi")
    assert "dictionary attack" in consent_prompt_text().lower()


def test_capability_text_is_honest() -> None:
    t = capability_text().lower()
    assert "dictionary" in t
    assert "does not brute-force" in t
    assert "built-in" in t and "native" in t  # CC's own cracker works out of the box, no install


def test_missing_tools_text() -> None:
    absent = {n: ToolStatus(n) for n in ("hcxpcapngtool", "hashcat", "aircrack-ng")}
    msg = missing_tools_text("hashcat", absent)
    assert "hcxpcapngtool" in msg and "hashcat" in msg
    present = {"hcxpcapngtool": ToolStatus("hcxpcapngtool", path="/x"),
               "hashcat": ToolStatus("hashcat", path="/x"),
               "aircrack-ng": ToolStatus("aircrack-ng")}
    assert missing_tools_text("hashcat", present) == ""  # nothing missing -> empty


# ── honest-negative / verify-never-fake guards ───────────────────────

def test_run_hashcat_reports_tool_error_not_false_negative(tmp_path, monkeypatch) -> None:
    """A hashcat run that exits nonzero-and-not-exhausted (e.g. no OpenCL device, a malformed .hc22000)
    tested NOTHING. It must be reported as a tool error — not laundered into a clean 'key not in
    wordlist' negative that falsely blames the operator's wordlist."""
    wl = tmp_path / "w.txt"
    wl.write_text("aaaaaaaa\n", encoding="utf-8")
    hf = tmp_path / "hash.hc22000"
    hf.write_text("WPA*01*x\n", encoding="utf-8")
    tools = {cp.HASHCAT: ToolStatus(cp.HASHCAT, path="/x/hashcat")}

    class _R:
        returncode = 255
        stdout = ""
        stderr = "clHostMemAlloc(): CL_OUT_OF_HOST_MEMORY\nNo devices found/left"

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(cp, "_read_show_results", lambda *a, **k: [])   # potfile empty
    res = cp.run_hashcat(str(hf), str(wl), lambda *_a: None, tools=tools)
    assert not res.cracked
    assert "not in wordlist" not in res.detail          # NOT a false negative
    assert "exit 255" in res.detail and "No devices" in res.detail


def test_run_hashcat_exhausted_stays_an_honest_negative(tmp_path, monkeypatch) -> None:
    """exit 1 = dictionary exhausted is a legitimate negative and must still read 'not in wordlist'."""
    wl = tmp_path / "w.txt"
    wl.write_text("aaaaaaaa\n", encoding="utf-8")
    hf = tmp_path / "h.hc22000"
    hf.write_text("x\n", encoding="utf-8")
    tools = {cp.HASHCAT: ToolStatus(cp.HASHCAT, path="/x/hashcat")}

    class _R:
        returncode = 1
        stdout = "Status.......: Exhausted"
        stderr = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(cp, "_read_show_results", lambda *a, **k: [])
    res = cp.run_hashcat(str(hf), str(wl), lambda *_a: None, tools=tools)
    assert not res.cracked and "not in wordlist" in res.detail


def test_run_native_no_matching_bssid_is_honest_negative(tmp_path, monkeypatch) -> None:
    """run_native with a BSSID that matches no handshake must NOT silently fall back to cracking ALL
    handshakes (a different, non-targeted AP within the capture). It returns an honest negative and
    never reaches the cracker."""
    from src.core import native_crack as nc
    from src.core import wpa_capture as wc

    cap = tmp_path / "c.pcap"
    cap.write_bytes(b"\x00" * 64)                        # exists + valid ext (parse_capture is mocked)
    wl = tmp_path / "w.txt"
    wl.write_text("aaaaaaaa\n", encoding="utf-8")
    hs = nc.Handshake(kind="pmkid", essid="Real", ap_mac=bytes.fromhex("aabbccddeeff"),
                      sta_mac=bytes.fromhex("112233445566"), pmkid=b"\x00" * 16)
    monkeypatch.setattr(wc, "parse_capture", lambda _p: [hs])

    def _must_not_crack(*_a, **_k):
        raise AssertionError("run_native fell back to cracking despite a non-matching BSSID")

    monkeypatch.setattr(nc, "crack", _must_not_crack)
    res = cp.run_native(str(cap), str(wl), lambda *_a: None, bssid="00:00:00:00:00:00")
    assert not res.cracked
    assert res.hashes_extracted == 0
    assert "no handshake for BSSID" in res.detail
