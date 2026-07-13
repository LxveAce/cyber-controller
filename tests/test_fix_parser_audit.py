"""Regression guards for the 2026-07-12 untrusted-parser audit (cc-parser-audit).

Four CONFIRMED defects across the wardrive + macro_recorder parsers, each triggered by realistic
device / hand-edited-file input:

    #1 wardrive.parse_nmea — a device-glitched NMEA sentence with an out-of-range degree value
       (|lat|>90 / |lon|>180) was accepted as a valid fix and stamped onto every logged row.
    #2 wardrive.summarize_wigle_csv — the WiGLE file is append-only (a fresh row per stronger
       re-sighting), so counting raw rows over-reported every headline stat; now de-duped by MAC.
    #3 macro_recorder — a non-numeric JSON delay_ms crashed the playback loop's `> 0` compare and
       killed the daemon thread WITHOUT firing the completion callback (UI wedged). Now the delay is
       coerced at load AND any playback error routes through complete(False, ...).
    #4 macro_recorder.list_saved_macros — one invalid-UTF-8 file in the macros dir crashed the whole
       listing (the except tuple omitted UnicodeDecodeError). Now it is skipped.

Pure logic / synchronous playback / fakes — no hardware.
"""

from __future__ import annotations

import json


# ── #1 wardrive: out-of-range NMEA coordinates rejected ──────────────────────────────────────────

def test_dm_to_dd_rejects_out_of_range():
    from src.core.wardrive import _dm_to_dd
    assert _dm_to_dd("9999.99", "N", 90.0) is None       # 100.66 deg latitude — impossible
    assert _dm_to_dd("18099.99", "E", 180.0) is None     # 181.66 deg longitude — impossible
    assert abs(_dm_to_dd("4807.038", "N", 90.0) - 48.1173) < 0.001   # valid lat survives
    assert abs(_dm_to_dd("01131.000", "E", 180.0) - 11.5167) < 0.001  # valid lon survives


def test_parse_nmea_out_of_range_is_not_a_fix():
    from src.core.wardrive import parse_nmea
    bad = parse_nmea("$GPGGA,120000.00,9999.99,N,18099.99,E,1,05,1.0,10.0,M,,,,*00")
    assert bad is not None
    assert bad.has_fix is False        # impossible coordinate -> no usable position
    good = parse_nmea("$GPGGA,120000.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert good.has_fix is True
    assert abs(good.lat - 48.1173) < 0.01 and abs(good.lon - 11.5167) < 0.01


# ── #2 wardrive: summarize de-dupes by MAC (append-only file has many rows per network) ──────────

def _wigle_row(mac: str, auth: str, ch: int, rssi: int, lat: str) -> str:
    cols = [""] * 14
    cols[0], cols[2], cols[4], cols[6], cols[7] = mac, auth, str(ch), str(rssi), lat
    return ",".join(cols)


def test_summarize_wigle_csv_dedupes_by_mac():
    from src.core.wardrive import summarize_wigle_csv
    text = "WigleWifi-1.4\n" + ",".join(["MAC"] + ["h"] * 13) + "\n" + "\n".join([
        _wigle_row("AA:BB:CC:DD:EE:01", "[WPA2-PSK]", 6, -80, "37.0"),   # same BSSID, weak
        _wigle_row("AA:BB:CC:DD:EE:01", "[WPA2-PSK]", 6, -70, "37.0"),   # ...stronger re-sighting
        _wigle_row("AA:BB:CC:DD:EE:01", "[WPA2-PSK]", 6, -50, "37.0"),   # ...strongest
        _wigle_row("AA:BB:CC:DD:EE:02", "[OPEN]", 36, -60, "37.0"),      # a distinct 5 GHz network
    ]) + "\n"
    s = summarize_wigle_csv(text)
    assert s["networks"] == 2                       # 2 unique BSSIDs, NOT 4 raw rows
    assert s["wpa"] == 1 and s["open"] == 1
    assert s["band_24ghz"] == 1 and s["band_5ghz"] == 1
    assert s["with_gps"] == 2
    assert s["rssi_strongest"] == -50               # strongest-per-network kept


# ── #3 macro_recorder: bad delay_ms coerced + playback never dies silently ───────────────────────

def test_from_dict_coerces_bad_delay_ms():
    from src.core.macro_recorder import Macro
    m = Macro.from_dict({"name": "m", "steps": [
        {"command": "a", "delay_ms": "100"},     # JSON string
        {"command": "b", "delay_ms": -5},        # negative
        {"command": "c", "delay_ms": 10 ** 12},  # absurd -> clamped
        {"command": "d"},                        # missing -> default 0
    ]})
    assert [s.delay_ms for s in m.steps] == [100, 0, 3_600_000, 0]
    assert all(isinstance(s.delay_ms, int) for s in m.steps)  # `delay_ms > 0` can never raise now


def test_play_notifies_complete_on_a_bad_step_instead_of_wedging(tmp_path):
    # A step whose delay_ms survived as a non-int (crafted directly, bypassing from_dict) must route the
    # resulting error through complete(False, ...) — not silently kill the playback thread (UI wedge).
    from src.core.macro_recorder import Macro, MacroRecorder, MacroStep
    rec = MacroRecorder(macros_dir=tmp_path)
    macro = Macro(name="m", steps=[MacroStep(command="a"), MacroStep(command="b", delay_ms="bad")])  # type: ignore[arg-type]
    sent: list[str] = []
    done: list = []
    rec.play(macro, send_command=sent.append,
             complete_callback=lambda ok, msg: done.append((ok, msg)), async_=False)
    assert done, "completion callback must fire even on a malformed step (no silent thread death)"
    assert done[0][0] is False
    assert sent == ["a"]  # step 0 sent before the bad step-1 delay raised


# ── #4 macro_recorder: list survives an invalid-UTF-8 file in the macros dir ─────────────────────

def test_list_saved_macros_survives_bad_utf8_file(tmp_path):
    from src.core.macro_recorder import MacroRecorder
    rec = MacroRecorder(macros_dir=tmp_path)
    (tmp_path / "good.json").write_text(json.dumps({"name": "good", "steps": []}), encoding="utf-8")
    (tmp_path / "bad.json").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    names = [m["name"] for m in rec.list_saved_macros()]
    assert "good" in names   # the bad file is skipped, not fatal to the whole listing
