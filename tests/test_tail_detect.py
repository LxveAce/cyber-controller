"""Follower/tail detection (``src/core/tail_detect.py``) — time-window persistence scoring over the
shared detection stream (CC's take on ArgeliusLabs' Chasing-Your-Tail-NG method).

The tracker is wall-clock-free (timestamps are passed in), so every case is deterministic. The
ingestor wiring is exercised with a fake ingestor + a controllable clock: personal/mobile detections
(probe/BLE/client) are counted; a stationary AP is not.
"""
from __future__ import annotations

from src.core.metrics import MetricsModel, ReadingKind
from src.core.tail_detect import PersistenceTracker, attach_tail_detector, tails_to_alerts
from src.protocols.base import ParsedEvent

W = 100  # window seconds used across these tests


class _FakeIngestor:
    """Minimal TargetIngestor stand-in: stores observers, lets a test fire events."""

    def __init__(self) -> None:
        self._obs: list = []

    def add_event_observer(self, cb):
        self._obs.append(cb)

    def remove_event_observer(self, cb):
        self._obs.remove(cb)

    def fire(self, ev, port="COM4"):
        for cb in list(self._obs):
            cb(ev, port)


def _ev(event_type: str, data: dict) -> ParsedEvent:
    return ParsedEvent(event_type=event_type, data=data, raw="")


# ── PersistenceTracker scoring ──────────────────────────────────────────────────────────────────

def test_score_is_fraction_of_recent_windows_seen():
    t = PersistenceTracker(window_seconds=W, num_windows=4)
    for b in (0, 1, 2):                 # seen in buckets 0,1,2
        t.observe("aa:bb", b * W + 5)
    now = 3 * W + 5                     # bucket 3; last 4 windows = buckets 0..3 -> seen 3 of 4
    assert t.score("aa:bb", now) == 0.75


def test_tails_applies_threshold_and_sorts_strongest_first():
    t = PersistenceTracker(window_seconds=W, num_windows=4)
    for b in (0, 1, 2, 3):
        t.observe("A", b * W + 5)      # every window -> 1.0
    for b in (2, 3):
        t.observe("B", b * W + 5)      # 2 of 4 -> 0.5
    t.observe("C", 3 * W + 5)          # 1 of 4 -> 0.25 (below threshold)
    hits = t.tails(3 * W + 5, min_persistence=0.5)
    assert [h.device for h in hits] == ["A", "B"]        # C excluded; A before B
    assert hits[0].persistence == 1.0 and hits[0].windows == 4


def test_ignore_list_devices_are_never_scored():
    t = PersistenceTracker(window_seconds=W, num_windows=4, ignore={"me"})
    t.observe("me", 5)
    assert t.score("me", 5) == 0.0
    assert t.tails(3 * W + 5, min_persistence=0.0) == []   # ignored device never enters the store


def test_add_ignore_drops_an_already_tracked_device():
    t = PersistenceTracker(window_seconds=W, num_windows=4)
    for b in (0, 1, 2, 3):
        t.observe("A", b * W + 5)
    t.add_ignore("A")
    assert t.tails(3 * W + 5, min_persistence=0.5) == []


def test_prune_drops_stale_windows():
    t = PersistenceTracker(window_seconds=W, num_windows=2)
    t.observe("A", 0 * W + 5)          # bucket 0 (stale)
    t.observe("A", 5 * W + 5)          # bucket 5
    t.prune(5 * W + 5)                 # keep buckets >= 5-2+1 = 4 -> only bucket 5 survives
    assert t.score("A", 5 * W + 5) == 0.5


# ── attach_tail_detector: wiring over the ingestor stream ────────────────────────────────────────

def test_attach_counts_personal_detections_not_aps():
    clock = {"t": 0.0}
    ing = _FakeIngestor()
    trk = PersistenceTracker(window_seconds=W, num_windows=4)
    cb = attach_tail_detector(ing, trk, now_fn=lambda: clock["t"])

    for b in (0, 1, 2, 3):             # a client device seen across every window
        clock["t"] = b * W + 5
        ing.fire(_ev("client_found", {"client_mac": "de:ad"}))
    ing.fire(_ev("ap_found", {"bssid": "aa:bb", "ssid": "x"}))   # an AP must NOT be counted

    hits = trk.tails(3 * W + 5, min_persistence=0.5)
    assert [h.device for h in hits] == ["de:ad"]

    # detach stops feeding: a later BLE hit isn't tracked.
    ing.remove_event_observer(cb)
    clock["t"] = 4 * W + 5
    ing.fire(_ev("ble_found", {"mac": "ff:ee"}))
    assert not any(h.device == "ff:ee" for h in trk.tails(5 * W + 5, min_persistence=0.0))


def test_attach_reads_probe_and_ble_device_keys_with_labels():
    ing = _FakeIngestor()
    trk = PersistenceTracker(window_seconds=W, num_windows=2)
    attach_tail_detector(ing, trk, now_fn=lambda: 50.0)     # bucket 0
    ing.fire(_ev("probe_request", {"mac": "11:22", "ssid": "home"}))
    ing.fire(_ev("ble_found", {"addr": "33:44", "name": "Tile"}))
    hits = {h.device: h.label for h in trk.tails(150.0, min_persistence=0.0)}
    assert "11:22" in hits and "33:44" in hits
    assert "probe" in hits["11:22"] and "BLE" in hits["33:44"]


# ── tails_to_alerts: a persistent tail -> a Dashboard ALERT reading ───────────────────────────────

def test_tails_to_alerts_emits_alert_only_for_flagged_tails():
    t = PersistenceTracker(window_seconds=W, num_windows=4)
    for b in (0, 1, 2, 3):
        t.observe("A", b * W + 5)      # persistence 1.0 -> flagged
    for b in (2, 3):
        t.observe("B", b * W + 5)      # 0.5 -> flagged
    t.observe("C", 3 * W + 5)          # 0.25 -> below threshold
    m = MetricsModel()
    tails_to_alerts(t, m, 3 * W + 5, min_persistence=0.5)
    assert m.latest("A", ReadingKind.ALERT) is not None
    assert m.latest("B", ReadingKind.ALERT) is not None
    assert m.latest("C", ReadingKind.ALERT) is None          # below threshold -> no alert
    a = m.latest("A", ReadingKind.ALERT)
    assert a.extra.get("tail") is True and a.extra.get("windows") == 4
    assert "possible tail" in a.label


def test_tails_to_alerts_skips_ignored_devices():
    t = PersistenceTracker(window_seconds=W, num_windows=4, ignore={"me"})
    for b in (0, 1, 2, 3):
        t.observe("me", b * W + 5)     # my own device -> never scored, never alerted
    m = MetricsModel()
    tails_to_alerts(t, m, 3 * W + 5, min_persistence=0.5)
    assert m.latest("me", ReadingKind.ALERT) is None
