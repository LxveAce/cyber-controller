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
