"""Regression tests for the beat-259 (cc-deep-audit-9) confirmed findings.

Each test is DISCRIMINATING: it fails against the pre-fix code and passes against the fix.

- [0] esp32_div: an attacker BLE device Name embedding an "AP: SSID=..." substring must route to
  ble_found, not be laundered into ap_found (a phantom AP that desyncs the select-ordinal).
- [1]/[2] ghost_esp: the _RE_AP / _RE_BLE patterns must not catastrophically backtrack (ReDoS) on a
  crafted whitespace-run line — the parse runs on the serial reader thread.
- [3] halehound: a [GUARDIAN] rogue-AP line whose name embeds "[WIFI] SSID: ..." must stay rogue_ap
  (evil-twin alert), not be downgraded to a benign ap_found.
- [7] physical_key: the brute-force lockout remaining-time must be clamped to the cooldown, so a
  backward wall-clock step can't lock the owner out for years.
"""

from __future__ import annotations

import threading
import unittest.mock as mock

from src.protocols import get_protocol


def _completes_within(fn, timeout=3.0):
    """Run fn() in a daemon thread; return True iff it finished within *timeout* seconds.

    On the pre-fix regex a crafted line backtracks for tens of seconds to minutes, so the thread is
    still alive at the timeout (-> False); the fixed regex returns in milliseconds (-> True)."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive()


# ── [0] esp32_div: BLE name carrying an "AP:" substring must NOT be misrouted to ap_found ─────────

def test_esp32div_ble_name_with_ap_substring_not_misrouted():
    p = get_protocol("esp32-div")
    line = ("[BLE] Device: MAC=DE:AD:BE:EF:00:01 "
            "Name=AP: SSID=x BSSID=11:22:33:44:55:66 CH=1 RSSI=-1 RSSI=-60")
    ev = p.parse_line(line)
    assert ev is not None
    # Pre-fix: _RE_AP.search claimed the embedded "AP: SSID=x BSSID=.." first -> ap_found for the
    # BLE MAC. Fixed: a BLE line is detected up front and the AP/STA branches are skipped for it.
    assert ev.event_type == "ble_found"
    assert ev.data["mac"] == "DE:AD:BE:EF:00:01"


def test_esp32div_genuine_ap_still_ap_found():
    p = get_protocol("esp32-div")
    ev = p.parse_line("[WiFi] AP: SSID=HomeNet BSSID=aa:bb:cc:dd:ee:ff CH=6 RSSI=-40")
    assert ev is not None and ev.event_type == "ap_found"
    assert ev.data["bssid"] == "aa:bb:cc:dd:ee:ff"


# ── [1]/[2] ghost_esp: _RE_AP / _RE_BLE must not ReDoS on a crafted whitespace-run line ───────────

def test_ghostesp_ap_redos_line_parses_promptly():
    p = get_protocol("ghost-esp")
    payload = "SSID: " + " " * 6000 + "x"   # no '|' -> pre-fix _RE_AP catastrophically backtracked
    assert _completes_within(lambda: p.parse_line(payload))


def test_ghostesp_ble_redos_line_parses_promptly():
    p = get_protocol("ghost-esp")
    payload = "BLE Device: AA:BB:CC:DD:EE:FF Name: " + " " * 6000 + "x"   # no 'RSSI:'
    assert _completes_within(lambda: p.parse_line(payload))


def test_ghostesp_genuine_ap_and_ble_still_parse():
    p = get_protocol("ghost-esp")
    ap = p.parse_line("SSID: MyNet | BSSID: B4:BF:E9:11:19:AD | CH: 1 | RSSI: -23")
    assert ap is not None and ap.event_type == "ap_found"
    assert ap.data["ssid"] == "MyNet" and ap.data["bssid"] == "B4:BF:E9:11:19:AD"
    assert ap.data["channel"] == 1 and ap.data["rssi"] == -23
    ble = p.parse_line("BLE Device: AA:BB:CC:DD:EE:FF Name: MyPhone RSSI: -55")
    assert ble is not None and ble.event_type == "ble_found"
    assert ble.data["name"] == "MyPhone" and ble.data["mac"] == "AA:BB:CC:DD:EE:FF"


# ── [3] halehound: a Guardian rogue-AP line must not be downgraded to a benign ap_found ───────────

def test_halehound_guardian_rogue_ap_not_downgraded():
    p = get_protocol("halehound")
    line = ("[GUARDIAN] ROGUE AP: [WIFI] SSID: CorpWiFi "
            "| BSSID: DE:AD:BE:EF:00:01 | CH: 6 | RSSI: -30")
    ev = p.parse_line(line)
    assert ev is not None
    # Pre-fix: the embedded "[WIFI] SSID: ... | BSSID: .. | CH: .. | RSSI: .." satisfied _RE_WIFI_AP
    # (unanchored .search) first -> ap_found, suppressing the evil-twin alert. Fixed: ^ anchors the
    # marker so a field can't forge one; the line falls through to the Guardian branch.
    assert ev.event_type == "rogue_ap"
    assert ev.data["bssid"] == "DE:AD:BE:EF:00:01"


def test_halehound_genuine_wifi_and_guardian_parse():
    p = get_protocol("halehound")
    ap = p.parse_line("[WIFI] SSID: HomeNet | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -42")
    assert ap is not None and ap.event_type == "ap_found"
    assert ap.data["ssid"] == "HomeNet" and ap.data["bssid"] == "AA:BB:CC:DD:EE:FF"
    g = p.parse_line("[GUARDIAN] ROGUE AP: EvilTwin | BSSID: 11:22:33:44:55:66 | CH: 6 | RSSI: -30")
    assert g is not None and g.event_type == "rogue_ap"
    assert g.data["ssid"] == "EvilTwin"


# ── [7] physical_key: lockout remaining-time must be clamped against a backward clock step ────────

def test_lockout_remaining_clamped_against_backward_clock():
    from src.security import physical_key as pk

    # 5 failures == the lockout threshold; last_failure_ts in the FUTURE vs _now = a backward
    # wall-clock step (dead CMOS battery / RTC reset) after the lockout was stored.
    cfg = {"failed_attempts": 5, "last_failure_ts": 2_000_000_000.0}   # ~2033
    with mock.patch.object(pk, "_now", return_value=1_000_000_000.0):  # ~2001
        rem = pk._lockout_remaining(cfg)
    # Pre-fix: max(0, int(last + cooldown - now)) ~= 1e9 seconds (decades) -> owner bricked. Fixed:
    # clamped to the cooldown (30s at the threshold), never above _LOCKOUT_MAX_SECS.
    assert rem <= pk._LOCKOUT_MAX_SECS
    assert rem == pk._LOCKOUT_BASE_SECS
