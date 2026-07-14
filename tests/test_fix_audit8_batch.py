"""Regression tests for the beat-258 (cc-deep-audit-8) confirmed findings.

Each test is DISCRIMINATING: it fails against the pre-fix code and passes against the fix.

- [0] marauder: an attacker-named BLE line whose Name carries the substring 'ESSID' must route to
  ble_found, not be laundered into the AP TargetPool as a phantom deauth-able 'ap_found'.
- [2] lxveos: an oversized caps= bitmask must be left as a raw string (no O(bits^2) _decode_caps
  work on the serial reader thread), while a normal mask still decodes to capability slugs.
- [5] audit_trail: the torn-tail repair must rewrite the durable chain atomically (temp -> fsync ->
  os.replace), never a truncating write_text, so a crash mid-repair can't lose committed entries.
- [6] health_monitor: get_device_health must persist a fresh copy back under the lock instead of
  mutating the shared stored dict in place, so a concurrent reader can't see a torn field mix.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.protocols import get_protocol

# ── [0] marauder: BLE name carrying 'ESSID' must NOT be misrouted to ap_found ─────────────────────

def test_ble_line_with_essid_in_name_is_not_misrouted_to_ap():
    p = get_protocol("marauder")
    ev = p.parse_line("BLE: 11:22:33:44:55:66 Name: myESSID RSSI: -40")
    assert ev is not None
    # Pre-fix: the broad 'ESSID' substring test in the scanall AP branch (evaluated before the BLE
    # branch) claimed this line and emitted ap_found for the BLE MAC. Fixed: the _RE_BLE guard now
    # excludes it, so it reaches the real BLE branch.
    assert ev.event_type == "ble_found"
    assert ev.data["mac"] == "11:22:33:44:55:66"


def test_genuine_scanall_ap_line_still_parses_as_ap():
    # No-regression guard: a real single-line scanall AP record must still route to ap_found (the
    # new _RE_BLE/_RE_CLIENT exclusions are specific enough not to catch a genuine AP line).
    p = get_protocol("marauder")
    ev = p.parse_line("-52 Ch: 6 aa:bb:cc:dd:ee:ff ESSID: MyNet 11 15")
    assert ev is not None
    assert ev.event_type == "ap_found"
    assert ev.data["bssid"] == "aa:bb:cc:dd:ee:ff"


# ── [2] lxveos: oversized caps= bitmask must not drive the O(bits^2) expansion ────────────────────

def test_oversized_caps_bitmask_is_left_undecoded():
    p = get_protocol("lxveos")
    # 0x1 followed by 20 zero-nibbles = 84 bits (2^80), well past the 64-bit forward-compat cap.
    ev = p.parse_line("LXVEOS/1 status caps=0x100000000000000000000 heap=1000")
    assert ev is not None and ev.event_type == "device_info"
    # Pre-fix: int(val,16) accepted it and _decode_caps ran ~80+ growing big-int shifts, adding
    # caps_tokens. Fixed: over-limit mask stays a raw string and is never decoded.
    assert "caps_tokens" not in ev.data
    assert isinstance(ev.data.get("caps"), str)


def test_normal_caps_bitmask_still_decodes():
    p = get_protocol("lxveos")
    ev = p.parse_line("LXVEOS/1 status caps=0x007 heap=1000")
    assert ev is not None and ev.event_type == "device_info"
    assert ev.data.get("caps") == 0x007
    assert ev.data.get("caps_tokens") == ["wifi", "ble", "bt_classic"]


# ── [5] audit_trail: torn-tail repair must be atomic (os.replace), not a truncating write_text ────

def test_torn_tail_repair_is_atomic(tmp_path, monkeypatch):
    from src.security import audit_trail as audit_mod
    from src.security.audit_trail import AuditTrail

    path = tmp_path / "audit.jsonl"
    trail = AuditTrail(persist_path=path)
    trail.record("connect", {"port": "COM9"})
    trail.record("flash", {"fw": "marauder"})

    # Simulate an unclean exit: a partial, unparseable trailing line appended after the good chain.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"timestamp":"2026-07-14T00:00:00+00:00","action":"flas')

    # Count os.replace calls made during the load-time repair, still performing the real replace.
    real_replace = audit_mod.os.replace
    calls = {"n": 0}

    def counting_replace(src, dst):
        calls["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(audit_mod.os, "replace", counting_replace)

    # Reloading detects the torn tail and repairs the durable file.
    reloaded = AuditTrail(persist_path=path)

    # Pre-fix: the repair used path.write_text (O_TRUNC, no atomic replace) = zero os.replace calls.
    # Fixed: _atomic_write_text does temp -> fsync -> os.replace exactly once.
    assert calls["n"] >= 1
    # And the two committed entries survive the repair (the torn tail is dropped).
    actions = [e.action for e in reloaded.entries]
    assert actions == ["connect", "flash"]
    # The rewritten file is well-formed JSONL (every surviving line parses).
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


# ── [6] health_monitor: get_device_health must not alias/mutate the shared stored dict ────────────

def test_get_device_health_does_not_alias_stored_dict():
    from src.core.health_monitor import HealthMonitor

    hm = HealthMonitor()
    hm.register_device("COMX", connection=SimpleNamespace(is_connected=True))
    store_ref = hm._device_health["COMX"]

    hm.get_device_health("COMX")

    # Pre-fix: `cached = self._device_health.get(port, {})` bound the stored ref and mutated it in
    # place outside the lock, so the stored object stays identical (aliased). Fixed: a fresh copy is
    # computed and swapped back under the lock, so the stored dict is a NEW object.
    assert hm._device_health["COMX"] is not store_ref
    # The freshly-computed status is what got persisted (a live connection reads 'connected').
    assert hm._device_health["COMX"]["status"] == "connected"
