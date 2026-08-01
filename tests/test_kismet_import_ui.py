"""Kismet ``.kismet`` (SQLite) import-UI wiring — `flock_heatmap_tab` routes a binary SQLite log
to `kismet_db_to_points` (by PATH) instead of the text `wardrive_points` dispatcher.

The reader's own decode logic is proven in `test_kismet_db_reader.py` (Atlas's lane). THIS pins
the UI seam: the SQLite header sniff (`_is_sqlite_db`), and that `load_wardrive_log` branches a
`.kismet` to the reader + plots it while a text WiGLE CSV still rides the text path. Offscreen.

(The reader is `KISMET_READER_HW_VERIFIED = False` — real-log fidelity awaits Ace's real capture.
Synthetic fixtures prove the ROUTING, not that a field `.kismet` decodes correctly.)
"""
from __future__ import annotations

import json
import os
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.ui.qt.flock_heatmap_tab import _is_sqlite_db

_DDL = """
CREATE TABLE KISMET (kismet_version TEXT, db_version INT, db_module TEXT);
CREATE TABLE devices (
    first_time INT, last_time INT, devkey TEXT, phyname TEXT, devmac TEXT,
    strongest_signal INT, min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    avg_lat REAL, avg_lon REAL, bytes_data INT, type TEXT, device BLOB,
    UNIQUE(phyname, devmac) ON CONFLICT REPLACE
);
"""


def _make_kismet(path, rows, dtype="Wi-Fi AP"):
    """rows = [(devmac, avg_lat, avg_lon, ssid)]; writes a v8 (REAL-coords) synthetic .kismet DB
    with each device typed *dtype* (default Wi-Fi AP), matching the kismet_db_to_points schema."""
    con = sqlite3.connect(str(path))
    con.executescript(_DDL)
    con.execute("INSERT INTO KISMET VALUES ('synthetic', 8, 'kismetlog')")
    for mac, lat, lon, ssid in rows:
        dev = {"dot11.device": {"dot11.device.last_beaconed_ssid_record":
                                {"dot11.advertisedssid.ssid": ssid}}}
        con.execute(
            "INSERT INTO devices (first_time,last_time,devkey,phyname,devmac,strongest_signal,"
            "min_lat,min_lon,max_lat,max_lon,avg_lat,avg_lon,bytes_data,type,device) "
            "VALUES (0,0,?,'IEEE802.11',?,-40,?,?,?,?,?,?,0,?,?)",
            (mac.replace(":", ""), mac, lat, lon, lat, lon, lat, lon, dtype, json.dumps(dev)))
    con.commit()
    con.close()


# ── the pure SQLite header sniff (Qt-free) ──
def test_is_sqlite_db_detects_a_kismet(tmp_path):
    p = tmp_path / "scan.kismet"
    _make_kismet(p, [("AA:BB:CC:DD:EE:01", 47.6, -122.3, "HomeNet")])
    assert _is_sqlite_db(str(p)) is True                       # real SQLite header magic


def test_is_sqlite_db_rejects_text_and_missing(tmp_path):
    csv = tmp_path / "w.csv"
    csv.write_text("WigleWifi-1.6\nAA:BB:CC:DD:EE:01,AP,[WPA2],t,6,2437,-50,48.1,11.1,0,0,,,WIFI\n")
    assert _is_sqlite_db(str(csv)) is False                    # a text CSV isn't SQLite
    assert _is_sqlite_db(str(tmp_path / "nope.kismet")) is False   # missing -> False, never raises


# ── the import routing, through a real offscreen tab ──
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_load_wardrive_log_routes_a_kismet_to_the_reader(qapp, tmp_path):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    p = tmp_path / "drive.kismet"
    _make_kismet(p, [("AA:BB:CC:DD:EE:01", 47.6062, -122.3321, "HomeNet"),
                     ("AA:BB:CC:DD:EE:02", 40.10, -74.20, "OtherNet")])
    w = FlockHeatmapTab()
    try:
        n = w.load_wardrive_log(str(p))          # binary .kismet -> kismet_db_to_points, by path
        assert n == 2 and w.wardrive_count == 2
        assert w._wardrive_layer is not None and len(w._wardrive_layer._dots) == 2   # plotted
    finally:
        w.shutdown()
        w.deleteLater()


def test_load_wardrive_log_still_routes_text_csv(qapp, tmp_path):
    # regression: the SQLite branch must NOT divert a normal text WiGLE CSV off the text path
    from src.core import wardrive as wd
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    csv = tmp_path / "w.csv"
    csv.write_text(wd.WIGLE_HEADER + "\n"
                   + "AA:BB:CC:DD:EE:01,AP1,[WPA2][ESS],t,6,2437,-50,48.1,11.1,0.0,0,,,WIFI\n")
    w = FlockHeatmapTab()
    try:
        assert w.load_wardrive_log(str(csv)) == 1 and w.wardrive_count == 1
    finally:
        w.shutdown()
        w.deleteLater()


def test_load_wardrive_log_bad_sqlite_is_safe(qapp, tmp_path):
    # a SQLite file that ISN'T a Kismet log (no devices table) -> reader returns [] -> 0, no crash
    p = tmp_path / "other.sqlite"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE t (x INT)")
    con.commit()
    con.close()
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    try:
        assert w.load_wardrive_log(str(p)) == 0 and w.wardrive_count == 0
    finally:
        w.shutdown()
        w.deleteLater()


def test_kismet_accepts_wds_and_adhoc_aps(tmp_path):
    # Gap 2 (grounded vs kismet/phy_80211.cc): a WDS AP + an Ad-Hoc cell ARE real GPS APs -- the old
    # exact ``== "Wi-Fi AP"`` match dropped them. They must import now; a non-AP type still drops.
    from src.core.wardrive_import import kismet_db_to_points
    for i, dtype in enumerate(("Wi-Fi AP", "Wi-Fi WDS AP", "Wi-Fi Ad-Hoc")):
        p = tmp_path / f"t{i}.kismet"
        _make_kismet(p, [(f"AA:BB:CC:DD:EE:0{i}", 47.6, -122.3, "Net")], dtype=dtype)
        assert len(kismet_db_to_points(str(p))) == 1, dtype
    p = tmp_path / "client.kismet"
    _make_kismet(p, [("AA:BB:CC:DD:EE:09", 47.6, -122.3, "Net")], dtype="Wi-Fi Client")
    assert kismet_db_to_points(str(p)) == []   # a non-AP type is still dropped
