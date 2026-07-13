"""Tests for the wardriving core (src/core/wardrive.py) — pure parsing + WiGLE CSV logic."""

from __future__ import annotations

import io

from src.core import wardrive as wd


def test_dm_to_dd():
    assert round(wd._dm_to_dd("4807.038", "N"), 4) == 48.1173
    assert round(wd._dm_to_dd("01131.000", "E"), 4) == 11.5167
    assert wd._dm_to_dd("4807.038", "S") < 0
    assert wd._dm_to_dd("", "N") is None


def test_parse_nmea_gga_fix():
    f = wd.parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert f is not None and f.has_fix
    assert round(f.lat, 3) == 48.117 and round(f.lon, 3) == 11.517 and f.alt == 545.4
    assert f.sats == 8 and f.hdop == 0.9          # GGA quality fields (7=sats, 8=HDOP) are captured


def test_parse_nmea_rmc_has_no_quality_fields():
    # RMC carries no satellite/HDOP fields -> they stay at the unknown defaults, position still parses
    f = wd.parse_nmea("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert f is not None and f.has_fix
    assert f.sats == 0 and f.hdop == 0.0


def test_parse_nmea_gga_garbled_quality_keeps_the_fix():
    # a garbled sats/HDOP must leave the quality unknown (0), never discard an otherwise-valid position
    f = wd.parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,XX,YY,545.4,M,46.9,M,,*47")
    assert f is not None and f.has_fix
    assert round(f.lat, 3) == 48.117 and f.sats == 0 and f.hdop == 0.0


def test_parse_nmea_gga_garbled_altitude_keeps_the_fix():
    # altitude is ancillary (the map uses lat/lon) — a garbled altitude must not discard a valid position
    f = wd.parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,GARBAGE,M,46.9,M,,*47")
    assert f is not None and f.has_fix
    assert round(f.lat, 3) == 48.117 and f.sats == 8 and f.hdop == 0.9 and f.alt == 0.0


def test_parse_nmea_gga_nofix():
    f = wd.parse_nmea("$GPGGA,123519,,,,,0,00,,,M,,M,,*47")
    assert f is not None and not f.has_fix


def test_parse_nmea_rmc():
    ok = wd.parse_nmea("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert ok is not None and ok.has_fix
    void = wd.parse_nmea("$GPRMC,123519,V,,,,,,,230394,,*00")
    assert void is not None and not void.has_fix
    assert wd.parse_nmea("not nmea") is None


def test_channel_to_frequency():
    assert wd.channel_to_frequency(1) == 2412
    assert wd.channel_to_frequency(6) == 2437
    assert wd.channel_to_frequency(14) == 2484
    assert wd.channel_to_frequency(36) == 5180
    assert wd.channel_to_frequency(0) == 0


def test_parse_marauder_ap():
    a = wd.parse_marauder_ap("RSSI:-50 Ch:6 BSSID:AA:BB:CC:DD:EE:FF ESSID:MyNet")
    assert a and a.bssid == "aa:bb:cc:dd:ee:ff" and a.rssi == -50 and a.channel == 6 and a.ssid == "MyNet"
    b = wd.parse_marauder_ap("0) BSSID: 11:22:33:44:55:66 | RSSI: -67 | Ch: 11 | WPA2 | ESSID: HomeNet")
    assert b and b.rssi == -67 and b.channel == 11 and b.ssid == "HomeNet" and "WPA2" in b.auth
    assert wd.parse_marauder_ap("no mac here") is None


def test_parse_marauder_ap_ssid_not_overcaptured_without_pipe():
    # Space-separated single-line record with NO '|' delimiter (the legacy Marauder ordering). The SSID
    # capture must stop at the next key token (BSSID/Ch/RSSI), not run to end-of-line and swallow the
    # trailing fields — otherwise that garbage string is written into the WiGLE SSID column and uploaded.
    a = wd.parse_marauder_ap("SSID: MyNet BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -52")
    assert a is not None
    assert a.ssid == "MyNet"                     # NOT "MyNet BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -52"
    assert a.bssid == "aa:bb:cc:dd:ee:ff" and a.channel == 6 and a.rssi == -52
    # The pipe-delimited variant is unchanged (stops at the first '|').
    b = wd.parse_marauder_ap("SSID: MyNet | BSSID: aa:bb:cc:dd:ee:ff | Ch: 6 | RSSI: -52")
    assert b is not None and b.ssid == "MyNet"


def test_to_wigle_row():
    obs = wd.ApObservation(bssid="aa:bb:cc:dd:ee:ff", ssid="Net", channel=6, rssi=-40, auth="[WPA2][ESS]")
    fix = wd.GpsFix(lat=48.1173, lon=11.5167, alt=545.4, has_fix=True)
    row = wd.to_wigle_row(obs, fix, "2026-06-27 12:00:00")
    cols = row.split(",")
    assert cols[0] == "AA:BB:CC:DD:EE:FF" and cols[1] == "Net"
    assert cols[4] == "6" and cols[5] == "2437" and cols[6] == "-40"
    assert cols[7] == "48.117300" and cols[13] == "WIFI"


def test_session_gating_and_dedup():
    buf = io.StringIO()
    s = wd.WardriveSession(buf)
    s.start()
    text = buf.getvalue()
    assert text.splitlines()[0].startswith("WigleWifi-1.6")
    assert text.splitlines()[1] == wd.WIGLE_HEADER

    # no fix yet -> no row
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:1 ESSID:Net") is False
    assert s.ap_count == 0

    s.update_gps("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert s.has_fix
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:1 ESSID:Net", now="2026-06-27 00:00:00") is True
    assert s.ap_count == 1
    # weaker RSSI for same BSSID -> not rewritten
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-60 Ch:1 ESSID:Net", now="2026-06-27 00:00:01") is False
    assert s.ap_count == 1
    # stronger RSSI -> rewritten, count unchanged
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-30 Ch:1 ESSID:Net", now="2026-06-27 00:00:02") is True
    assert s.ap_count == 1
    # new BSSID -> new row
    assert s.observe("BSSID:11:22:33:44:55:66 RSSI:-50 Ch:6 ESSID:Net2", now="2026-06-27 00:00:03") is True
    assert s.ap_count == 2

    data_rows = [ln for ln in buf.getvalue().splitlines()[2:] if ln]
    assert all(len(r.split(",")) == 14 for r in data_rows)


def test_csv_field_neutralizes_formula_injection():
    # An attacker-chosen SSID that begins with a spreadsheet formula trigger (= + - @ / tab / CR) must be
    # de-fanged with a leading single quote so opening the WiGLE CSV in Excel/LibreOffice can't evaluate it
    # (DDE/command execution). None of these payloads contain an RFC-4180 delimiter, so the old
    # quote-only path left them bare.
    assert wd._csv_field("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert wd._csv_field("+SUM(1+1)") == "'+SUM(1+1)"
    assert wd._csv_field("-2+3") == "'-2+3"
    assert wd._csv_field("@SUM(A1)") == "'@SUM(A1)"
    assert wd._csv_field("\t=1+1") == "'\t=1+1"
    # Leading CR is both de-fanged AND still delimiter-quoted (it contains \r).
    assert wd._csv_field("\r=1+1") == '"\'\r=1+1"'
    # Benign SSIDs are untouched (no spurious quoting).
    assert wd._csv_field("MyNet") == "MyNet"
    assert wd._csv_field("[WPA2][ESS]") == "[WPA2][ESS]"


def test_wigle_row_defangs_malicious_ssid():
    # End-to-end: a beacon whose SSID is a formula payload flows parse -> to_wigle_row and the SSID column
    # is written de-fanged, so the exported row cannot execute a formula when opened in a spreadsheet.
    # (Payload avoids '|', which parse_marauder_ap treats as an SSID terminator.)
    obs = wd.parse_marauder_ap("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:6 ESSID:=2+5+cmdexec")
    assert obs is not None and obs.ssid == "=2+5+cmdexec"
    fix = wd.GpsFix(lat=48.1173, lon=11.5167, alt=545.4, has_fix=True)
    row = wd.to_wigle_row(obs, fix, "2026-06-27 12:00:00")
    ssid_col = row.split(",")[1]
    assert not ssid_col.startswith("=")          # not a live formula
    assert ssid_col == "'=2+5+cmdexec"           # rendered as literal text instead


def test_zero_rssi_does_not_overwrite_strong_reading():
    # An explicit `RSSI:0` line (0 dBm = the parser's no-signal sentinel) for an already-mapped BSSID
    # produces obs.rssi=0 and DOES reach the dedup comparison. A raw `0 <= -40` would let that no-signal
    # sighting win and hijack the strongest-RSSI row/location; the _signal_key sentinel (0 -> -9999) blocks it.
    buf = io.StringIO()
    s = wd.WardriveSession(buf)
    s.start()
    # Strong reading at fix A -> logged.
    s.update_gps("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:6 ESSID:Net", now="2026-06-27 00:00:00") is True
    assert s.seen["aa:bb:cc:dd:ee:ff"] == -40
    # Move the rig to fix B, then a same-BSSID line carrying an explicit RSSI:0 (obs.rssi=0).
    s.update_gps("$GPGGA,123520,4810.000,N,01135.000,E,1,08,0.9,545.4,M,46.9,M,,*4E")
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:0 Ch:6 ESSID:Net", now="2026-06-27 00:00:01") is False
    assert s.seen["aa:bb:cc:dd:ee:ff"] == -40                 # strong reading (and its location) preserved
    rows = [ln for ln in buf.getvalue().splitlines()[2:] if ln]
    assert len(rows) == 1                                     # no second, no-signal row appended
    assert rows[0].split(",")[6] == "-40" and rows[0].split(",")[7] == "48.117300"


def test_multiline_marauder_scanall_is_reassembled():
    # Modern Marauder (v1.12.3+) streams each AP as SEPARATE serial lines. The session must stitch the
    # ESSID/BSSID/Ch/RSSI fragments into ONE WiGLE row — the pre-fix behaviour logged only the BSSID line,
    # producing a row with a blank SSID and 0 channel/frequency/RSSI (the whole feature lost).
    buf = io.StringIO()
    s = wd.WardriveSession(buf)
    s.start()
    s.update_gps("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    wrote = [s.observe(ln, now="2026-06-27 00:00:00")
             for ln in ["ESSID: MyNet", "BSSID: aa:bb:cc:dd:ee:ff", "Ch: 6", "RSSI: -52"]]
    # No fragment writes a row until BSSID + RSSI have both been seen; then exactly one row is emitted.
    assert wrote == [False, False, False, True]
    rows = [ln for ln in buf.getvalue().splitlines()[2:] if ln]
    assert len(rows) == 1                                     # NOT a stray all-zero BSSID-only row
    cols = rows[0].split(",")
    assert cols[0] == "AA:BB:CC:DD:EE:FF"
    assert cols[1] == "MyNet"                                 # SSID recovered (was "")
    assert cols[4] == "6" and cols[5] == "2437"              # channel + frequency recovered (were 0/0)
    assert cols[6] == "-52"                                   # RSSI recovered (was 0)
    assert s.ap_count == 1


def test_multiline_scanall_dedup_keeps_strongest_rssi():
    # Secondary effect of the multi-line drop: every AP used to land with rssi=0 (the missing sentinel),
    # so the strongest-RSSI location dedup never fired. With real RSSI now parsed from the stream, a
    # stronger re-sighting of the same BSSID refreshes the mapped row instead of being pinned to the first.
    buf = io.StringIO()
    s = wd.WardriveSession(buf)
    s.start()
    s.update_gps("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    for ln in ["ESSID: Cam", "BSSID: aa:bb:cc:dd:ee:ab", "RSSI: -70"]:
        s.observe(ln, now="2026-06-27 00:00:00")
    assert s.seen["aa:bb:cc:dd:ee:ab"] == -70
    # Weaker re-sighting -> not refreshed.
    for ln in ["ESSID: Cam", "BSSID: aa:bb:cc:dd:ee:ab", "RSSI: -80"]:
        s.observe(ln, now="2026-06-27 00:00:01")
    assert s.seen["aa:bb:cc:dd:ee:ab"] == -70
    # Stronger re-sighting -> refreshed to the closer reading.
    for ln in ["ESSID: Cam", "BSSID: aa:bb:cc:dd:ee:ab", "RSSI: -30"]:
        s.observe(ln, now="2026-06-27 00:00:02")
    assert s.seen["aa:bb:cc:dd:ee:ab"] == -30
    assert s.ap_count == 1                                    # still one unique AP across all sightings


# ── wardrive run summary ─────────────────────────────────────────────

_SUMMARY_CSV = (
    "WigleWifi-1.6,appRelease=1.0,model=CyberController,release=1.0,device=cyber-controller,"
    "display=,board=,brand=LxveAce,star=Sol,body=3,subBody=0\n"
    + wd.WIGLE_HEADER + "\n"
    "AA:BB:CC:DD:EE:01,Open1,[ESS],2026-06-27 00:00:00,6,2437,-40,48.1,11.5,0.0,0,,,WIFI\n"
    "AA:BB:CC:DD:EE:02,Secure2,[WPA2-PSK-CCMP][ESS],2026-06-27 00:00:01,6,2437,-55,48.1,11.5,0.0,0,,,WIFI\n"
    "AA:BB:CC:DD:EE:03,Weak3,[WEP][ESS],2026-06-27 00:00:02,11,2462,-70,,,0.0,0,,,WIFI\n"
    "AA:BB:CC:DD:EE:04,Fast4,[WPA2][ESS],2026-06-27 00:00:03,36,5180,-60,48.2,11.6,0.0,0,,,WIFI\n"
)


def test_summarize_wigle_csv():
    s = wd.summarize_wigle_csv(_SUMMARY_CSV)
    assert s["networks"] == 4
    assert s["open"] == 1 and s["wpa"] == 2 and s["wep"] == 1
    assert s["band_24ghz"] == 3 and s["band_5ghz"] == 1        # ch 6,6,11 vs ch 36
    assert s["with_gps"] == 3                                  # the WEP row had no lat/lon
    assert s["rssi_strongest"] == -40 and s["rssi_weakest"] == -70
    assert s["top_channels"][0] == (6, 2)                      # channel 6 is busiest


def test_summarize_wigle_csv_counts_gps_from_any_sighting_not_just_rssi_winner():
    # An imported CSV can hold several rows per BSSID where the RSSI winner lacks a GPS fix
    # but a weaker sighting has one. with_gps must credit the network, not read GPS off the winner.
    text = (
        wd.WIGLE_HEADER + "\n"
        "AA:BB:CC:DD:EE:10,Roam,[WPA2][ESS],2026-06-27 00:00:00,6,2437,-70,48.1,11.5,0.0,0,,,WIFI\n"
        "AA:BB:CC:DD:EE:10,Roam,[WPA2][ESS],2026-06-27 00:00:01,6,2437,-40,,,0.0,0,,,WIFI\n"
    )
    s = wd.summarize_wigle_csv(text)
    assert s["networks"] == 1          # dedup still collapses the BSSID to one network
    assert s["rssi_strongest"] == -40  # the no-GPS row is still the RSSI winner
    assert s["with_gps"] == 1          # ...but the GPS fix from the weaker sighting is not lost


def test_summarize_wigle_csv_header_only_and_empty():
    s = wd.summarize_wigle_csv("WigleWifi-1.6,x\n" + wd.WIGLE_HEADER + "\n")
    assert s["networks"] == 0 and s["top_channels"] == [] and s["rssi_strongest"] is None
    assert wd.summarize_wigle_csv("")["networks"] == 0        # totally empty must not crash


def test_summarize_wigle_csv_skips_garbled_rows():
    text = (
        wd.WIGLE_HEADER + "\n"
        "not,enough,cols\n"                                            # too few columns -> skipped
        "NOTAMAC,x,[ESS],t,6,2437,-40,48.1,11.5,0.0,0,,,WIFI\n"        # first field isn't a MAC -> skipped
        "AA:BB:CC:DD:EE:09,Ok,[ESS],t,6,2437,BADRSSI,48.1,11.5,0.0,0,,,WIFI\n"  # real row, unparseable RSSI
    )
    s = wd.summarize_wigle_csv(text)
    assert s["networks"] == 1                # only the real-MAC row counts
    assert s["rssi_strongest"] is None       # its RSSI didn't parse -> no range, but the row still counts
    assert s["with_gps"] == 1


def test_wardrive_summary_cli(tmp_path, capsys):
    p = tmp_path / "w.csv"
    p.write_text(_SUMMARY_CSV, encoding="utf-8")
    assert wd.wardrive_summary_cli(str(p)) == 0
    out = capsys.readouterr().out
    assert "networks: 4" in out and "open 1" in out and "ch6 x2" in out

    assert wd.wardrive_summary_cli(str(tmp_path / "nope.csv")) == 1
    assert "no such file" in capsys.readouterr().out


# ── real-hardware Marauder scanall format (regression: found on COM16, 2026-07-08) ──
# These are VERBATIM lines captured from a physical Marauder v1.12.3 `scanall`. The RSSI is a bare leading
# signed int with NO "RSSI:" label; before the _RSSI_LEAD_RE fix the accumulator saw no RSSI and emitted 0
# APs, so a real Wi-Fi scan produced an EMPTY wardrive CSV.
_REAL_SCANALL_LINES = [
    "-71 Ch: 2 0e:73:be:f8:72:74 ESSID: SpectrumSetup-7272 11 15",
    "-85 Ch: 2 a0:8a:06:a7:d1:51 ESSID: SpectrumSetup-D154 31 14",
    "-74 Ch: 2 b4:bf:e9:11:19:ad ESSID: ESP_1119AD 21 04",
    "-54 Ch: 6 00:04:ea:7b:f7:ee ESSID: DIRECT-50-HP Smart Tank 5100 11 05",
]


def test_ap_accumulator_parses_real_marauder_scanall_lines():
    acc = wd._ApAccumulator()
    aps = [o for o in (acc.feed(ln) for ln in _REAL_SCANALL_LINES) if o]
    assert len(aps) == 4                                    # every real AP line emits (was 0 before the fix)
    assert aps[0].bssid == "0e:73:be:f8:72:74"
    assert aps[0].rssi == -71 and aps[0].channel == 2
    assert aps[0].ssid == "SpectrumSetup-7272"              # trailing "11 15" metadata columns stripped
    # an SSID containing spaces AND an interior number is preserved; only the trailing metadata pair goes
    assert aps[3].bssid == "00:04:ea:7b:f7:ee" and aps[3].rssi == -54
    assert aps[3].ssid == "DIRECT-50-HP Smart Tank 5100"


def test_ap_accumulator_ignores_interleaved_station_lines():
    # Real Marauder scanall interleaves station/multicast rows ("N: ap: X -> sta: Y") between AP records.
    # Those carry two MACs but no ESSID/RSSI/Ch; they must NOT emit a phantom AP (e.g. a multicast MAC) or
    # corrupt the next real record. VERBATIM lines from the COM16 capture.
    seq = [
        "-71 Ch: 2 0e:73:be:f8:72:74 ESSID: SpectrumSetup-7272 11 15",
        "1: ap: 0e:73:be:f8:72:74 -> sta: 01:00:5e:00:00:07",
        " -71 Ch: 2 f0:7b:65:1a:36:76 ESSID: SpectrumSetup-3670 11 15",
        "2: ap: 0e:73:be:f8:72:74 -> sta: 01:00:5e:7f:ff:fa",
        "3: sta: 9c:f1:d4:48:6e:08 -> ap: 5c:fa:25:d6:21:d4",
    ]
    acc = wd._ApAccumulator()
    aps = [o for o in (acc.feed(ln) for ln in seq) if o]
    assert len(aps) == 2                                   # only the two real AP records emit
    assert [a.bssid for a in aps] == ["0e:73:be:f8:72:74", "f0:7b:65:1a:36:76"]
    assert all(not a.bssid.startswith("01:00:5e") for a in aps)   # never a multicast/station MAC as an AP
    assert aps[0].ssid == "SpectrumSetup-7272" and aps[1].rssi == -71


def test_extract_ap_fields_leading_rssi_and_labelled_still_work():
    f = wd._extract_ap_fields("-64 Ch: 6 00:04:ea:7b:f7:ee ESSID: MyNet")
    assert f["rssi"] == -64 and f["channel"] == 6 and f["bssid"] == "00:04:ea:7b:f7:ee"
    # the labelled legacy form is unaffected (RSSI_RE matches first, the leading fallback isn't consulted)
    f2 = wd._extract_ap_fields("SSID: Net BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -40")
    assert f2["rssi"] == -40
    # a channel number must NOT be mistaken for a leading RSSI (no bare signed int before 'Ch')
    f3 = wd._extract_ap_fields("BSSID: aa:bb:cc:dd:ee:ff Ch: 11")
    assert "rssi" not in f3
