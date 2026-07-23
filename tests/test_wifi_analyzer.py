"""Tests for the pure Wi-Fi-analyzer model (src/core/wifi_analyzer.py).

Drives the model with the ap_found / client_found / handshake / pmkid event shapes the real parsers
emit — Marauder/HaleHound use channel + bssid + ap_mac;
ESP32-DIV adds encryption + bssid; LxveOS uses
ch/auth for APs, ap for the station association,
and reports a handshake by essid (no bssid) — plus an
injected clock, so every behaviour (normalization,
aggregation, captures, freshness, sorting, bounds)
is deterministic and needs no Qt or serial.
"""
from __future__ import annotations

from src.core.wifi_analyzer import (
    AccessPoint,
    WifiAnalyzerModel,
    channel_of,
    client_assoc_bssid,
    client_mac,
    encryption_of,
    is_open,
    normalize_bssid,
    rssi_bars,
    rssi_quality,
)


# ── field extractors (firmware-agnostic across the real parser shapes) ──
def test_normalize_bssid_lowercases_and_rejects_missing():
    assert normalize_bssid({"bssid": "AA:BB:CC:DD:EE:FF"}) == "aa:bb:cc:dd:ee:ff"
    assert normalize_bssid({"ssid": "x"}) is None          # BW16 index-only AP: no bssid
    assert normalize_bssid({"bssid": "   "}) is None       # whitespace-only -> empty key
    assert normalize_bssid("not a dict") is None


def test_client_assoc_and_mac_across_field_names():
    # Marauder: client_mac + ap_mac
    assert (client_assoc_bssid({"client_mac": "a", "ap_mac": "DE:AD:BE:EF:00:01"})
            == "de:ad:be:ef:00:01")
    assert client_mac({"client_mac": "11:22:33:44:55:66"}) == "11:22:33:44:55:66"
    # ESP32-DIV / serial fork: mac + bssid
    assert client_assoc_bssid({"mac": "x", "bssid": "de:ad:be:ef:00:02"}) == "de:ad:be:ef:00:02"
    # LxveOS: mac + ap
    assert client_assoc_bssid({"mac": "x", "ap": "de:ad:be:ef:00:03"}) == "de:ad:be:ef:00:03"
    assert client_mac({"mac": "aa:bb:cc:00:11:22"}) == "aa:bb:cc:00:11:22"
    # HaleHound WIFI_STA: mac only, no association
    assert client_assoc_bssid({"mac": "aa:bb:cc:00:11:22", "rssi": -50}) is None


def test_channel_and_encryption_accept_every_spelling():
    assert channel_of({"channel": 6}) == 6            # Marauder/DIV/HaleHound/BW16
    assert channel_of({"ch": 11}) == 11               # LxveOS
    assert channel_of({}) is None
    assert encryption_of({"encryption": "WPA2"}) == "WPA2"   # ESP32-DIV
    assert encryption_of({"auth": "wpa2"}) == "wpa2"          # LxveOS
    assert encryption_of({"enc": "WEP"}) == "WEP"            # tolerant fallback
    assert encryption_of({}) == ""                           # unknown, never fabricated


def test_is_open_only_when_explicit():
    assert is_open("open") and is_open("OPEN") and is_open("none")
    assert not is_open("")            # unknown is NOT open (no fabricated verdict)
    assert not is_open("wpa2")


# ── ingest + aggregation ──
def test_observe_ap_creates_and_updates_by_bssid():
    m = WifiAnalyzerModel()
    a1 = m.observe("ap_found", {"bssid": "AA:BB:CC:DD:EE:FF", "ssid": "Net", "channel": 6,
                                "rssi": -55, "encryption": "WPA2"}, now=100.0)
    assert isinstance(a1, AccessPoint) and len(m) == 1
    assert a1.first_seen == 100.0 and a1.hits == 1 and a1.seen_directly is True
    assert a1.ssid == "Net" and a1.channel == 6 and a1.encryption == "WPA2"
    assert a1.rssi == -55 and a1.rssi_min == -55 and a1.rssi_max == -55

    a2 = m.observe("ap_found",
                   {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "Net", "rssi": -70}, now=101.0)
    assert a2 is a1 and len(m) == 1                     # de-duplicated by normalized BSSID
    assert a1.hits == 2 and a1.last_seen == 101.0
    assert a1.rssi == -70 and a1.rssi_min == -70 and a1.rssi_max == -55   # latest + range tracked


def test_lxveos_ap_fields_ch_and_auth():
    m = WifiAnalyzerModel()
    ap = m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "MyNet", "ch": 6,
                                "rssi": -42, "auth": "wpa2"}, now=1.0)
    assert ap.channel == 6 and ap.encryption == "wpa2" and ap.ssid == "MyNet"


def test_ssid_not_blanked_by_later_hidden_beacon():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "a1:a1:a1:a1:a1:a1", "ssid": "Home", "rssi": -40}, now=1.0)
    m.observe("ap_found", {"bssid": "a1:a1:a1:a1:a1:a1", "ssid": "", "rssi": -42}, now=2.0)
    assert m.get("a1:a1:a1:a1:a1:a1").ssid == "Home"


def test_rogue_ap_flag_is_sticky():
    m = WifiAnalyzerModel()
    m.observe("rogue_ap", {"bssid": "11:22:33:44:55:66", "ssid": "Evil", "rssi": -50}, now=1.0)
    assert m.get("11:22:33:44:55:66").rogue is True
    m.observe("ap_found", {"bssid": "11:22:33:44:55:66", "rssi": -48}, now=2.0)  # later plain hit
    assert m.get("11:22:33:44:55:66").rogue is True                              # not un-flagged


def test_missing_or_bad_rssi_keeps_none():
    m = WifiAnalyzerModel()
    a = m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "N"}, now=1.0)  # no rssi
    assert a.rssi is None and a.bars() == 0
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "rssi": "n/a"}, now=2.0)    # garbage
    assert a.rssi is None
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "rssi": True}, now=3.0)     # bool != rssi
    assert a.rssi is None
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "rssi": -60}, now=4.0)      # real reading
    assert a.rssi == -60 and a.bars() == 3


# ── clients ──
def test_client_found_attributes_to_ap_and_counts_globally():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "Net", "rssi": -50}, now=1.0)
    # Marauder-shaped station associated with that AP
    m.observe("client_found",
              {"client_mac": "11:11:11:11:11:11", "ap_mac": "de:ad:be:ef:00:01"}, 2.0)
    m.observe("client_found", {"mac": "22:22:22:22:22:22", "bssid": "de:ad:be:ef:00:01"}, 2.0)
    ap = m.get("de:ad:be:ef:00:01")
    assert ap.client_count() == 2
    # HaleHound station with no AP association -> counted globally, attributed to no AP
    m.observe("client_found", {"mac": "33:33:33:33:33:33", "rssi": -60}, 3.0)
    assert m.summary()["clients"] == 3 and ap.client_count() == 2


def test_client_for_unseen_ap_creates_placeholder():
    m = WifiAnalyzerModel()
    m.observe("client_found", {"mac": "11:11:11:11:11:11", "ap": "de:ad:be:ef:00:09"}, now=1.0)
    ap = m.get("de:ad:be:ef:00:09")
    assert ap is not None and ap.client_count() == 1
    assert ap.seen_directly is False and ap.ssid == "" and ap.rssi is None


# ── captures (handshake / PMKID) ──
def test_handshake_by_bssid_flags_ap_and_creates_if_new():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "Net", "rssi": -50}, now=1.0)
    m.observe("handshake_captured", {"bssid": "de:ad:be:ef:00:01"}, now=2.0)  # Marauder/DIV
    ap = m.get("de:ad:be:ef:00:01")
    assert ap.handshake is True and ap.has_capture() is True and ap.pmkid is False
    # a handshake for a not-yet-seen BSSID creates a placeholder AP flagged captured
    m.observe("handshake_captured", {"bssid": "de:ad:be:ef:00:02"}, now=3.0)
    ap2 = m.get("de:ad:be:ef:00:02")
    assert ap2 is not None and ap2.handshake is True and ap2.seen_directly is False


def test_pmkid_flags_pmkid_not_handshake():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "N", "rssi": -50}, now=1.0)
    m.observe("pmkid_captured", {"bssid": "de:ad:be:ef:00:01", "pmkid": "abcd"}, now=2.0)
    ap = m.get("de:ad:be:ef:00:01")
    assert ap.pmkid is True and ap.handshake is False and ap.has_capture() is True


def test_lxveos_handshake_by_essid_flags_matching_ap_and_remembers():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:01", "ssid": "MyNet", "rssi": -50}, now=1.0)
    # LxveOS reports a handshake by ESSID only (no bssid), kind=eapol
    m.observe("handshake_captured", {"essid": "MyNet", "kind": "eapol"}, now=2.0)
    assert m.get("de:ad:be:ef:00:01").handshake is True
    # a DIFFERENT AP with the same SSID seen LATER is flagged from the remembered essid capture
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:99", "ssid": "MyNet", "rssi": -55}, now=3.0)
    assert m.get("de:ad:be:ef:00:99").handshake is True
    # a pmkid-kind essid capture sets the pmkid flag
    m.observe("handshake_captured", {"essid": "Other", "kind": "pmkid"}, now=4.0)
    m.observe("ap_found", {"bssid": "de:ad:be:ef:00:aa", "ssid": "Other", "rssi": -60}, now=5.0)
    assert m.get("de:ad:be:ef:00:aa").pmkid is True


# ── sorting ──
def _seed_three(m: WifiAnalyzerModel) -> None:
    m.observe("ap_found", {"bssid": "00:00:00:00:00:01", "ssid": "Zeta", "channel": 1,
                           "rssi": -80}, now=10.0)                              # weak, oldest, ch1
    m.observe("ap_found", {"bssid": "00:00:00:00:00:02", "ssid": "", "channel": 6,
                           "rssi": -40}, now=20.0)  # strong, hidden, ch6
    m.observe("ap_found",
              {"bssid": "00:00:00:00:00:03", "ssid": "Alpha", "channel": 11}, now=30.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:02", "rssi": -42}, now=31.0)  # bump #2 recency
    m.observe("client_found", {"mac": "aa:aa:aa:aa:aa:aa", "bssid": "00:00:00:00:00:02"}, now=31.0)


def test_sort_variants():
    m = WifiAnalyzerModel()
    _seed_three(m)
    assert [a.bssid[-1] for a in m.access_points(sort="rssi")] == ["2", "1", "3"]  # strongest
    assert [a.bssid[-1] for a in m.access_points(sort="recent")] == ["2", "3", "1"]  # 31,30,10
    assert [a.bssid[-1] for a in m.access_points(sort="channel")][:2] == ["1", "2"]  # ch1 then ch6
    assert m.access_points(sort="clients")[0].bssid[-1] == "2"  # #2 has a client
    assert [a.bssid[-1] for a in m.access_points(sort="ssid")] == ["3", "1", "2"]    # named first


def test_fresh_only_filters_stale():
    m = WifiAnalyzerModel()
    _seed_three(m)
    fresh = m.access_points(sort="recent", now=40.0, ttl=15.0, fresh_only=True)
    assert {a.bssid[-1] for a in fresh} == {"2", "3"}  # #2@31 #3@30 fresh; #1@10 is stale
    fresh2 = m.access_points(sort="recent", now=60.0, ttl=15.0, fresh_only=True)
    assert fresh2 == []  # nothing within 15s of now=60


# ── channel view ──
def test_channel_occupancy_and_strongest():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "00:00:00:00:00:01", "channel": 6, "rssi": -70}, now=1.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:02", "channel": 6, "rssi": -50}, now=1.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:03", "channel": 11, "rssi": -60}, now=1.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:04", "rssi": -55}, now=1.0)  # no channel
    occ = m.channel_occupancy(now=1.0)
    assert occ == {6: 2, 11: 1}
    assert m.strongest_on_channel(6, now=1.0) == -50   # the stronger of the two on ch6
    assert m.strongest_on_channel(1, now=1.0) is None
    # a stale AP drops out of fresh occupancy
    assert m.channel_occupancy(now=999.0, ttl=30.0) == {}


# ── freshness / prune / summary ──
def test_freshness_decays_and_prune_drops_stale():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "ab:ab:ab:ab:ab:ab", "rssi": -50}, now=0.0)
    ap = m.get("ab:ab:ab:ab:ab:ab")
    assert ap.freshness(0.0, ttl=10.0) == 1.0
    assert ap.freshness(5.0, ttl=10.0) == 0.5
    assert ap.freshness(10.0, ttl=10.0) == 0.0
    assert ap.freshness(99.0, ttl=10.0) == 0.0 and ap.is_fresh(99.0, ttl=10.0) is False
    assert m.prune(now=99.0, ttl=10.0) == 1 and len(m) == 0


def test_summary_rollup():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "00:00:00:00:00:01", "ssid": "A", "rssi": -40,
                           "encryption": "open"}, now=100.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:02", "ssid": "B", "rssi": -70,
                           "auth": "wpa2"}, now=100.0)
    m.observe("handshake_captured", {"bssid": "00:00:00:00:00:02"}, now=100.0)
    m.observe("client_found", {"mac": "cc:cc:cc:cc:cc:cc", "bssid": "00:00:00:00:00:02"}, now=100.0)
    s = m.summary(now=100.0, ttl=30.0)
    assert s == {"total": 2, "fresh": 2, "open": 1, "handshakes": 1, "clients": 1, "strongest": -40}
    s2 = m.summary(now=200.0, ttl=30.0)   # past ttl: nothing fresh, no strongest
    assert s2["total"] == 2 and s2["fresh"] == 0 and s2["strongest"] is None


# ── memory bounds (a beacon/station flood can't grow the model without limit) ──
def test_ap_cap_evicts_stalest():
    m = WifiAnalyzerModel(max_aps=3)
    for i in range(3):
        m.observe("ap_found", {"bssid": f"00:00:00:00:00:0{i}", "rssi": -50}, now=float(i))
    assert len(m) == 3
    m.observe("ap_found", {"bssid": "00:00:00:00:00:0a", "rssi": -50}, now=99.0)  # evict stalest
    assert len(m) == 3
    assert m.get("00:00:00:00:00:00") is None          # oldest last_seen dropped
    assert m.get("00:00:00:00:00:0a") is not None


def test_client_set_per_ap_capped():
    m = WifiAnalyzerModel(max_clients_per_ap=5)
    for i in range(20):
        m.observe("client_found", {"mac": f"00:00:00:00:00:{i:02d}",
                                   "bssid": "ff:ff:ff:ff:ff:ff"}, now=float(i))
    assert m.get("ff:ff:ff:ff:ff:ff").client_count() == 5   # capped


# ── RSSI → bars / quality helpers (shared with the delegate + graph) ──
def test_rssi_bars_and_quality():
    assert rssi_bars(-30) == 4 and rssi_bars(-60) == 3
    assert rssi_bars(-70) == 2 and rssi_bars(-90) == 1
    assert rssi_bars(None) == 0
    assert rssi_quality(-30) == "strong" and rssi_quality(None) == "—"


def test_non_wifi_event_ignored():
    m = WifiAnalyzerModel()
    assert m.observe("ble_found", {"mac": "aa:bb:cc:dd:ee:ff", "rssi": -50}, now=1.0) is None
    assert m.observe("ap_found", None, now=1.0) is None
    assert len(m) == 0
