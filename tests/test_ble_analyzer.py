"""Tests for the pure BLE-analyzer model (src/core/ble_analyzer.py).

Drives the model with the ble_found event shapes the real parsers emit (Marauder/Ghost/Flipper send
mac; LxveOS sends addr + company/tracker/type) and an injected clock, so every behaviour —
normalization, aggregation, freshness, sorting, bounds — is deterministic and needs no Qt or serial.
"""
from __future__ import annotations

from src.core.ble_analyzer import (
    BleAnalyzerModel,
    BleDevice,
    normalize_addr,
    rssi_bars,
    rssi_quality,
)


# ── address normalization (the firmware-agnostic key) ──
def test_normalize_addr_accepts_mac_and_addr_and_lowercases():
    assert normalize_addr({"mac": "AA:BB:CC:DD:EE:FF", "rssi": -40}) == "aa:bb:cc:dd:ee:ff"
    assert normalize_addr({"addr": "66:55:44:33:22:11"}) == "66:55:44:33:22:11"  # LxveOS key
    # mac present but empty -> fall back to addr (a firmware could emit both, mac blank)
    assert normalize_addr({"mac": "", "addr": "01:02:03:04:05:06"}) == "01:02:03:04:05:06"


def test_normalize_addr_missing_or_bad_returns_none():
    assert normalize_addr({"name": "x", "rssi": -50}) is None   # no address at all
    assert normalize_addr({"mac": 12345}) is None               # non-string address
    assert normalize_addr("not a dict") is None                 # hostile / wrong type
    assert normalize_addr({"mac": "   "}) is None               # whitespace-only -> empty key


# ── ingest + aggregation ──
def test_observe_creates_and_updates_device():
    m = BleAnalyzerModel()
    d1 = m.observe({"mac": "aa:bb:cc:dd:ee:ff", "name": "Watch", "rssi": -55}, now=100.0)
    assert isinstance(d1, BleDevice) and len(m) == 1
    assert d1.first_seen == 100.0 and d1.last_seen == 100.0 and d1.hits == 1
    assert d1.rssi == -55 and d1.rssi_min == -55 and d1.rssi_max == -55

    d2 = m.observe({"mac": "AA:BB:CC:DD:EE:FF", "rssi": -70}, now=101.0)  # same addr, upcased
    assert d2 is d1 and len(m) == 1                     # de-duplicated by normalized address
    assert d1.hits == 2 and d1.last_seen == 101.0 and d1.first_seen == 100.0
    assert d1.rssi == -70 and d1.rssi_min == -70 and d1.rssi_max == -55  # latest + range tracked


def test_observe_returns_none_on_addressless_or_nondict_event():
    m = BleAnalyzerModel()
    assert m.observe({"name": "nope", "rssi": -50}, now=1.0) is None
    assert m.observe(None, now=1.0) is None
    assert len(m) == 0


def test_name_not_blanked_by_later_nameless_advert():
    m = BleAnalyzerModel()
    m.observe({"mac": "a1:a1:a1:a1:a1:a1", "name": "MyPhone", "rssi": -40}, now=1.0)
    m.observe({"mac": "a1:a1:a1:a1:a1:a1", "name": "", "rssi": -42}, now=2.0)  # nameless re-advert
    assert m.get("a1:a1:a1:a1:a1:a1").name == "MyPhone"
    # a real new name later DOES update
    m.observe({"mac": "a1:a1:a1:a1:a1:a1", "name": "MyPhone 15", "rssi": -41}, now=3.0)
    assert m.get("a1:a1:a1:a1:a1:a1").name == "MyPhone 15"


def test_tracker_flag_is_sticky():
    m = BleAnalyzerModel()
    m.observe({"addr": "11:22:33:44:55:66", "rssi": -50, "tracker": 1}, now=1.0)  # LxveOS tracker
    assert m.get("11:22:33:44:55:66").tracker is True
    m.observe({"addr": "11:22:33:44:55:66", "rssi": -48}, now=2.0)                 # later plain hit
    assert m.get("11:22:33:44:55:66").tracker is True                             # not un-flagged


def test_lxveos_company_and_type_captured():
    m = BleAnalyzerModel()
    # LxveOS ble event: addr + numeric company id + address type
    d = m.observe({"addr": "66:55:44:33:22:11", "type": "random", "rssi": -55,
                   "name": "My", "company": 76}, now=1.0)
    assert d.addr_type == "random"
    assert d.company == "76"           # numeric company id kept as a string tag
    assert d.name == "My"


# ── RSSI sentinel handling (missing != a real reading) ──
def test_missing_or_bad_rssi_adds_no_sample_and_zero_bars():
    m = BleAnalyzerModel()
    d = m.observe({"mac": "de:ad:be:ef:00:01"}, now=1.0)          # no rssi field at all
    assert d.rssi is None and d.samples == [] and d.bars() == 0
    m.observe({"mac": "de:ad:be:ef:00:01", "rssi": "n/a"}, now=2.0)  # garbage rssi
    assert d.rssi is None and d.samples == []
    m.observe({"mac": "de:ad:be:ef:00:01", "rssi": True}, now=3.0)   # bool is not an RSSI
    assert d.rssi is None and d.samples == []
    # a real reading now records a sample
    m.observe({"mac": "de:ad:be:ef:00:01", "rssi": -60}, now=4.0)
    assert d.rssi == -60 and d.samples == [(4.0, -60)] and d.bars() == 3


def test_series_returns_time_ordered_samples():
    m = BleAnalyzerModel()
    for i, r in enumerate((-40, -50, -45)):
        m.observe({"mac": "c0:c0:c0:c0:c0:c0", "rssi": r}, now=float(i))
    assert m.series("c0:c0:c0:c0:c0:c0") == [(0.0, -40), (1.0, -50), (2.0, -45)]
    assert m.series("unknown:addr") == []


# ── sorting ──
def _seed_three(m: BleAnalyzerModel) -> None:
    m.observe({"mac": "00:00:00:00:00:01", "name": "Zeta", "rssi": -80}, now=10.0)  # weak, oldest
    m.observe({"mac": "00:00:00:00:00:02", "name": "", "rssi": -40}, now=20.0)  # strong, unnamed
    m.observe({"mac": "00:00:00:00:00:03", "name": "Alpha"}, now=30.0)  # no rssi, newest
    m.observe({"mac": "00:00:00:00:00:02", "rssi": -42}, now=31.0)  # bump #2's hits/recency


def test_sort_by_rssi_puts_strongest_first_unknown_last():
    m = BleAnalyzerModel()
    _seed_three(m)
    order = [d.addr[-1] for d in m.devices(sort="rssi")]
    assert order == ["2", "1", "3"]   # -40 > -80 > (no rssi ranks last)


def test_sort_by_recent_and_hits_and_name():
    m = BleAnalyzerModel()
    _seed_three(m)
    assert [d.addr[-1] for d in m.devices(sort="recent")] == ["2", "3", "1"]  # #2 bumped last
    assert m.devices(sort="hits")[0].addr[-1] == "2"                          # #2 seen twice
    # named first (Alpha, Zeta), unnamed ("") last
    assert [d.addr[-1] for d in m.devices(sort="name")] == ["3", "1", "2"]


def test_fresh_only_filters_stale():
    m = BleAnalyzerModel()
    _seed_three(m)
    fresh = m.devices(sort="recent", now=40.0, ttl=15.0, fresh_only=True)
    # at now=40 with ttl=15: #2 (31) and #3 (30) are fresh; #1 (10) is stale
    assert {d.addr[-1] for d in fresh} == {"2", "3"}


# ── freshness / prune / summary ──
def test_freshness_decays_linearly_and_prune_drops_stale():
    m = BleAnalyzerModel()
    m.observe({"mac": "ab:ab:ab:ab:ab:ab", "rssi": -50}, now=0.0)
    dev = m.get("ab:ab:ab:ab:ab:ab")
    assert dev.freshness(0.0, ttl=10.0) == 1.0
    assert dev.freshness(5.0, ttl=10.0) == 0.5
    assert dev.freshness(10.0, ttl=10.0) == 0.0
    assert dev.freshness(99.0, ttl=10.0) == 0.0 and dev.is_fresh(99.0, ttl=10.0) is False
    assert m.prune(now=99.0, ttl=10.0) == 1 and len(m) == 0


def test_summary_rollup():
    m = BleAnalyzerModel()
    m.observe({"mac": "00:00:00:00:00:01", "name": "A", "rssi": -40}, now=100.0)
    m.observe({"addr": "00:00:00:00:00:02", "rssi": -70, "tracker": 1}, now=100.0)
    m.observe({"mac": "00:00:00:00:00:03"}, now=100.0)                   # nameless, no rssi
    s = m.summary(now=100.0, ttl=30.0)
    assert s == {"total": 3, "fresh": 3, "trackers": 1, "named": 1, "strongest": -40}
    # advance past ttl: nothing fresh, strongest becomes None
    s2 = m.summary(now=200.0, ttl=30.0)
    assert s2["total"] == 3 and s2["fresh"] == 0 and s2["strongest"] is None


# ── memory bounds (a BLE flood can't grow the model without limit) ──
def test_device_cap_evicts_stalest():
    m = BleAnalyzerModel(max_devices=3)
    for i in range(3):
        m.observe({"mac": f"00:00:00:00:00:0{i}", "rssi": -50}, now=float(i))
    assert len(m) == 3
    m.observe({"mac": "00:00:00:00:00:0a", "rssi": -50}, now=99.0)  # 4th -> evicts stalest (i=0)
    assert len(m) == 3
    assert m.get("00:00:00:00:00:00") is None                       # oldest last_seen was dropped
    assert m.get("00:00:00:00:00:0a") is not None


def test_sample_ring_buffer_capped():
    m = BleAnalyzerModel(max_samples=5)
    for i in range(20):
        m.observe({"mac": "ff:ff:ff:ff:ff:ff", "rssi": -50 - i}, now=float(i))
    dev = m.get("ff:ff:ff:ff:ff:ff")
    assert len(dev.samples) == 5                        # capped
    assert dev.samples[0] == (15.0, -65) and dev.samples[-1] == (19.0, -69)  # newest kept
    assert dev.hits == 20                               # hit count is not capped


# ── RSSI → bars / quality helpers (shared with the delegate + graph) ──
def test_rssi_bars_thresholds():
    assert rssi_bars(-30) == 4 and rssi_bars(-50) == 3   # > -50 = 4; == -50 falls to next band
    assert rssi_bars(-60) == 3 and rssi_bars(-70) == 2 and rssi_bars(-90) == 1
    assert rssi_bars(None) == 0


def test_rssi_quality_labels():
    assert rssi_quality(-30) == "strong"
    assert rssi_quality(-60) == "good"
    assert rssi_quality(-70) == "fair"
    assert rssi_quality(-90) == "weak"
    assert rssi_quality(None) == "—"
