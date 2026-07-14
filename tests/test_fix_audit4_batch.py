"""Regression guards for the cc-deep-audit-4 pass-4 batch (2026-07-13, ledger pass4 rows C1-C4).

Each defect was re-confirmed against the real code (final-gate, verify-never-fake), fixed
minimally, and is pinned here:

    C1 (MED, verify-never-fake) crack_pipeline.convert_capture — dropped hcxpcapngtool's exit
      code AND stderr: a NONZERO exit (corrupt/truncated/unreadable capture, a converter crash)
      fell through to the size==0 branch and was laundered into the honest negative "nothing to
      crack", burying a real failure. Now streams stderr as [hcx:err] and RAISES on rc != 0.
    C2 (LOW) native_crack.crack — Stop/progress were gated on `tried` (valid candidates), so a
      wordlist of mostly out-of-range lines (<8 or >63 octets) never advanced `tried`, never
      called should_stop(), and the Stop button was a no-op while the run looked hung. Now keyed
      on lines SCANNED so cancellation stays responsive regardless of candidate validity.
    C3 (LOW) cross_comm cooldown keys — the (rule, target) key was a f"{name}:{target}" STRING
      and remove_rule matched on startswith(f"{name}:"). Rule names are free-form (may contain
      colons) and target keys carry colons (BSSID), so the join was ambiguous and the prefix
      match wiped a SIBLING rule's live cooldown, letting it re-fire inside its own window. Now a
      (rule.name, target_key) TUPLE; remove_rule matches k[0] EXACTLY.
    C4 (LOW) web send_command socket handler — an empty command fell through to conn.write(""),
      which appends the line terminator and transmits a bare newline onto the attached attack
      hardware. The HTTP /api/command twin 400s on `not command`; the WS path now rejects it too.

Pure logic + fakes: no hardware, hcxpcapngtool, hashcat, or real serial port is touched.
"""
from __future__ import annotations

import pytest

# ── C1: convert_capture surfaces a converter failure, not a laundered "nothing to crack" ──────

def _converter_tools():
    import src.core.crack_pipeline as cp
    return cp, {cp.CONVERTER: cp.ToolStatus(cp.CONVERTER, path="/x/hcxpcapngtool")}


def test_convert_capture_raises_on_converter_failure(monkeypatch, tmp_path):
    cp, tools = _converter_tools()
    cap = tmp_path / "cap.pcapng"
    cap.write_bytes(b"\x0a\x0d\x0d\x0a")        # any bytes; validate_capture checks name+existence
    out = tmp_path / "out.hc22000"
    monkeypatch.setattr(
        cp, "_run_tool",
        lambda argv, timeout, on_proc=None: (1, "", "fatal: could not read pcapng magic"))
    lines: list[str] = []
    with pytest.raises(RuntimeError) as ei:
        cp.convert_capture(str(cap), str(out), lines.append, tools=tools)
    msg = str(ei.value)
    assert "exit 1" in msg                        # the real exit code is reported, not swallowed
    assert "could not read pcapng magic" in msg   # the tool's own stderr is the failure hint
    assert any(ln.startswith("[hcx:err]") for ln in lines), "converter stderr must be streamed"


def test_convert_capture_zero_exit_empty_output_stays_honest_negative(monkeypatch, tmp_path):
    # The fix must NOT over-raise: hcxpcapngtool exits 0 even when it extracts zero hashes from a
    # VALID pcap, so rc == 0 + an empty output file is the genuine honest negative (return 0).
    cp, tools = _converter_tools()
    cap = tmp_path / "cap.pcapng"
    cap.write_bytes(b"\x0a\x0d\x0d\x0a")
    out = tmp_path / "out.hc22000"
    out.write_text("", encoding="utf-8")          # UI pre-creates an empty temp path
    monkeypatch.setattr(cp, "_run_tool", lambda argv, timeout, on_proc=None: (0, "", ""))
    lines: list[str] = []
    n = cp.convert_capture(str(cap), str(out), lines.append, tools=tools)
    assert n == 0
    assert any("nothing to crack" in ln for ln in lines)


# ── C2: native crack honors Stop even when every wordlist line is out of the WPA octet range ──

def test_native_crack_stops_on_out_of_range_wordlist(tmp_path):
    import src.core.native_crack as nc

    hs = nc.Handshake(kind="pmkid", essid="e", ap_mac=b"\x00" * 6, sta_mac=b"\x11" * 6,
                      pmkid=b"\x22" * 16)
    wl = tmp_path / "w.txt"
    # 50 lines, all 2 bytes -> below the 8-octet WPA floor, so `tried` never advances; only
    # `scanned` does. Pre-fix, should_stop() was gated on `tried` and NEVER consulted (Stop=no-op).
    wl.write_text("\n".join("ab" for _ in range(50)) + "\n", encoding="utf-8")
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return True

    res = nc.crack([hs], str(wl), should_stop=stop, progress_every=10)
    assert res.detail == "stopped", "Stop must fire on lines SCANNED, not only on candidates TRIED"
    assert res.tried == 0, "no in-range candidate was tried; the honest count stays 0"
    assert calls["n"] >= 1, "should_stop() must actually be consulted"


# ── C3: tuple cooldown keys keep sibling rules distinct despite colons in names AND targets ────

def _router():
    from src.core.cross_comm import AutoRouter, EventBus
    return AutoRouter(EventBus(), lambda port, cmd: None)


def test_tuple_cooldown_keys_disambiguate_colon_targets():
    from src.core.cross_comm import RoutingRule

    r = _router()
    r.add_rule(RoutingRule(name="wifi", cooldown=60))
    r.add_rule(RoutingRule(name="wifi:ap", cooldown=60))  # names are free-form / may hold colons
    # Target keys carry colons (a BSSID), so the OLD f"{name}:{target}" join was ambiguous:
    # ("wifi", "ap:aa:bb") and ("wifi:ap", "aa:bb") both collapsed to the SAME "wifi:ap:aa:bb".
    # As TUPLES they stay distinct; remove_rule("wifi") must drop only the k[0] == "wifi" entry.
    r._cooldowns[("wifi", "ap:aa:bb")] = 100.0
    r._cooldowns[("wifi:ap", "aa:bb")] = 200.0
    assert r.remove_rule("wifi") is True
    assert ("wifi", "ap:aa:bb") not in r._cooldowns, "the removed rule's own stamp must be dropped"
    assert ("wifi:ap", "aa:bb") in r._cooldowns, "a sibling rule's live cooldown must NOT be wiped"


# ── C4: the send_command socket handler rejects an empty command (no bare newline onto HW) ────

def _capture_socket_handlers(monkeypatch) -> dict:
    from src.ui.web import app as webapp

    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


class _SpyConn:
    """A connected SerialConnection stand-in that records every write() so the test can assert an
    empty command NEVER reaches the wire."""

    is_connected = True

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


def _web_env(monkeypatch, tmp_path):
    # CC_GATE_CONFIG isolation is mandatory for any web-auth test: a failed login without it writes
    # a real lockout to ~/.cyber-controller/access_gate.json and 429s later web tests (beat 248).
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def test_send_command_rejects_empty_command(monkeypatch, tmp_path):
    pytest.importorskip("flask")
    from flask import session

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.models.device import Device
    from src.ui.web import app as webapp

    _web_env(monkeypatch, tmp_path)
    emitted: list[dict] = []
    captured = _capture_socket_handlers(monkeypatch)
    monkeypatch.setattr(webapp, "emit",
                        lambda _evt, payload=None, **k: emitted.append(payload or {}))

    dm = DeviceManager()
    dm.add_device(Device(port="COM3", name="Marauder", firmware="marauder", connected=True))
    spy = _SpyConn()
    monkeypatch.setattr(dm, "get_connection", lambda port: spy if port == "COM3" else None)

    app, _sio = webapp.create_app(dm, FlashEngine(), EventBus(), TargetPool())
    handler = captured["send_command"]

    with app.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        session["authenticated"] = True
        handler({"port": "COM3", "command": ""})
        assert spy.writes == [], "an empty command must NOT reach conn.write() (no bare newline)"
        assert any("[Empty command ignored]" in (p.get("line", "")) for p in emitted)

        # Positive control: a real command DOES reach the wire, so the guard isn't over-broad.
        handler({"port": "COM3", "command": "scan"})
        assert spy.writes == ["scan"], "a non-empty command must still be written"
