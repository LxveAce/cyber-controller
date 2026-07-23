"""Regression tests for the QA-6 hardening sweep of this session's Wi-Fi-analyzer + Link-strip code.

Each test pins a bug the adversarial sweep confirmed (2026-07-23): a firmware rssi:0 sentinel painted
as full strength; a BSSID-less capture memory that overwrote/leaked one kind and grew unbounded;
a kind=pmkid override honoured on only one code path; and a Link strip that showed a silent relay
as live forever.
"""
from __future__ import annotations

from src.core.wifi_analyzer import WifiAnalyzerModel
from src.ui.qt.link_strip import (
    POLL_BASE_MS,
    POLL_THROTTLED_MS,
    link_strip_render,
    poll_interval_ms,
    stream_blocked,
)


# ── rssi=0 is a firmware "no reading" sentinel, not a full-strength signal ──
def test_rssi_zero_is_dropped_not_full_strength():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "X", "rssi": 0}, now=1.0)
    ap = m.get("aa:bb:cc:dd:ee:ff")
    assert ap is not None
    assert ap.rssi is None          # 0 dBm sentinel dropped to None per the module contract
    assert ap.bars() == 0
    assert m.summary(now=1.0)["strongest"] is None
    # a real negative reading is still stored
    m.observe("ap_found", {"bssid": "aa:bb:cc:dd:ee:ff", "rssi": -55}, now=2.0)
    assert m.get("aa:bb:cc:dd:ee:ff").rssi == -55


# ── BSSID-less captures: accumulate every kind, and stay bounded ──
def test_essid_capture_accumulates_both_kinds_for_a_later_seen_ap():
    m = WifiAnalyzerModel()
    m.observe("handshake_captured", {"essid": "Net", "kind": "eapol"}, now=1.0)  # no AP yet
    m.observe("pmkid_captured", {"essid": "Net"}, now=2.0)  # no AP yet, a different kind
    m.observe("ap_found", {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "Net"}, now=3.0)
    ap = m.get("aa:bb:cc:dd:ee:ff")
    assert ap.handshake is True and ap.pmkid is True   # both kinds survive, not just the last


def test_essid_captures_bounded_under_bssid_less_flood():
    m = WifiAnalyzerModel(max_aps=8)
    for i in range(100):
        m.observe("handshake_captured", {"essid": f"net-{i}", "kind": "eapol"}, now=float(i))
    assert len(m._essid_captures) <= 8   # capped by max_aps, not unbounded growth


# ── a kind=pmkid override is honoured whether or not a BSSID is present ──
def test_kind_pmkid_override_honoured_on_the_bssid_path():
    m = WifiAnalyzerModel()
    m.observe("handshake_captured", {"bssid": "aa:bb:cc:dd:ee:ff", "kind": "pmkid"}, now=1.0)
    ap = m.get("aa:bb:cc:dd:ee:ff")
    assert ap.pmkid is True and ap.handshake is False


# ── a silent relay reads as STALE, not live, and gates the poll + streams ──
def test_stale_link_renders_muted_and_gates_poll_and_streams():
    link = {"tier": "wifi", "rssi": -42, "up": True}
    fresh = link_strip_render(link, stale=False)
    assert fresh.visible and "Wi-Fi" in fresh.text and "stale" not in fresh.text
    stale = link_strip_render(link, stale=True)
    assert stale.visible and "stale" in stale.text
    # stale throttles the poll + gates streams even on an unconstrained Wi-Fi tier
    assert poll_interval_ms(link, stale=False) == POLL_BASE_MS
    assert poll_interval_ms(link, stale=True) == POLL_THROTTLED_MS
    assert stream_blocked(link, stale=False) is False
    assert stream_blocked(link, stale=True) is True


# ── Device stamps a last-heard time so the strip can decay a silent relay ──
def test_apply_link_state_stamps_link_ts():
    from src.models.device import Device
    d = Device(port="COM4", firmware="lxveos")
    assert d.link_ts == 0.0
    assert d.apply_link_state({"tier": "wifi", "rssi": -42}) is True
    assert d.link_ts > 0.0
    # an identical frame still refreshes last-heard (the relay is alive) even if nothing changed
    prev = d.link_ts
    d.apply_link_state({"tier": "wifi", "rssi": -42})
    assert d.link_ts >= prev
