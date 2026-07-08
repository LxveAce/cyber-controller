"""Tests for MultiWardriveSession — multi-board wardrive with a shared GPS + merged dedup (F1).

The model core: several boards share one GPS fix, their AP streams de-duplicate into one merged WiGLE CSV
(strongest RSSI wins), per-board counts attribute first-sightings, and ap_count is the unique-AP total.
Pure — no Qt, no serial.
"""
import io

from src.core import wardrive as wd

FIX = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"   # quality 1 -> has_fix
NOFIX = "$GPGGA,123519,,,,,0,00,,,M,,M,,*47"                                # quality 0 -> no fix


def _sess():
    return wd.MultiWardriveSession(io.StringIO())


def test_shared_gps_gates_all_boards():
    s = _sess()
    s.add_board("COM3")
    s.add_board("COM4")
    assert s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:A") is False   # no fix yet
    s.update_gps(FIX)                                        # ONE shared fix feeds every board
    assert s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:A") is True
    assert s.observe("COM4", "BSSID:AA:BB:CC:DD:EE:02 RSSI:-50 Ch:6 ESSID:B") is True
    assert s.ap_count == 2
    assert s.per_board == {"COM3": 1, "COM4": 1}


def test_same_ap_from_two_boards_is_deduped():
    s = _sess()
    s.update_gps(FIX)
    assert s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:FF RSSI:-60 Ch:1 ESSID:N") is True    # first-seen by COM3
    assert s.observe("COM4", "BSSID:AA:BB:CC:DD:EE:FF RSSI:-70 Ch:1 ESSID:N") is False   # weaker dup -> dropped
    assert s.ap_count == 1
    assert s.per_board["COM3"] == 1 and s.per_board.get("COM4", 0) == 0


def test_stronger_resighting_by_other_board_refreshes_not_recounts():
    s = _sess()
    s.update_gps(FIX)
    s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:FF RSSI:-70 Ch:1 ESSID:N")                   # COM3 first-seen (weak)
    assert s.observe("COM4", "BSSID:AA:BB:CC:DD:EE:FF RSSI:-30 Ch:1 ESSID:N") is True    # stronger -> row refresh
    assert s.ap_count == 1                                   # still one unique AP
    assert s.per_board == {"COM3": 1, "COM4": 0}             # credit stays with the first board
    assert list(s.seen.values()) == [-30]                   # strongest RSSI retained (case-normalized bssid key)


def test_merged_csv_has_header_and_both_rows():
    buf = io.StringIO()
    s = wd.MultiWardriveSession(buf)
    s.update_gps(FIX)
    s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:A")
    s.observe("COM4", "BSSID:AA:BB:CC:DD:EE:02 RSSI:-50 Ch:6 ESSID:B")
    text = buf.getvalue()
    assert wd.WIGLE_HEADER in text                           # one shared, standards-compliant WiGLE CSV
    assert "AA:BB:CC:DD:EE:01" in text and "AA:BB:CC:DD:EE:02" in text


def test_no_fix_logs_nothing():
    s = _sess()
    s.update_gps(NOFIX)
    assert s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:A") is False
    assert s.ap_count == 0


def test_zero_rssi_from_another_board_does_not_hijack_merged_row():
    # A board emitting an explicit `RSSI:0` (0 dBm = no-signal sentinel) for an already-merged BSSID yields
    # obs.rssi=0 and DOES reach the dedup comparison. A raw `0 <= -40` would let it overwrite the merged
    # strongest-RSSI row and shared location; the _signal_key sentinel (0 -> -9999) keeps the strong reading.
    buf = io.StringIO()
    s = wd.MultiWardriveSession(buf)
    s.update_gps(FIX)
    assert s.observe("COM3", "BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:6 ESSID:N") is True     # strong, first-seen
    assert list(s.seen.values()) == [-40]
    # Same BSSID on another board carrying an explicit RSSI:0 -> obs.rssi=0, must NOT overwrite.
    assert s.observe("COM4", "BSSID:AA:BB:CC:DD:EE:FF RSSI:0 Ch:6 ESSID:N") is False
    assert list(s.seen.values()) == [-40]                    # strongest RSSI / location retained
    assert s.per_board.get("COM4", 0) == 0                   # not credited, not double-counted
    rows = [ln for ln in buf.getvalue().splitlines()[2:] if ln]
    assert len(rows) == 1 and rows[0].split(",")[6] == "-40"  # only the strong row was written


def test_interleaved_multiline_streams_do_not_cross_contaminate():
    # Modern Marauder streams each AP across separate ESSID/BSSID/RSSI lines. Two boards scanning at once
    # interleave those fragments, so the reassembly state MUST be per-port — a shared accumulator would
    # stitch COM3's ESSID onto COM4's BSSID (or vice-versa) and mislabel the merged WiGLE rows.
    buf = io.StringIO()
    s = wd.MultiWardriveSession(buf)
    s.update_gps(FIX)
    seq = [
        ("COM3", "ESSID: NetA"), ("COM4", "ESSID: NetB"),
        ("COM3", "BSSID: aa:bb:cc:dd:ee:01"), ("COM4", "BSSID: aa:bb:cc:dd:ee:02"),
        ("COM3", "RSSI: -40"), ("COM4", "RSSI: -55"),
    ]
    for port, line in seq:
        s.observe(port, line)
    rows = {r.split(",")[0]: r.split(",") for r in buf.getvalue().splitlines()[2:] if r}
    assert rows["AA:BB:CC:DD:EE:01"][1] == "NetA" and rows["AA:BB:CC:DD:EE:01"][6] == "-40"
    assert rows["AA:BB:CC:DD:EE:02"][1] == "NetB" and rows["AA:BB:CC:DD:EE:02"][6] == "-55"
    assert s.ap_count == 2
    assert s.per_board == {"COM3": 1, "COM4": 1}
