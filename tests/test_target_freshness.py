"""Passive target-freshness (staleness) summary + its /api/freshness web endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.target_freshness import summarize_freshness
from src.models.target import Target, TargetType

# A fixed reference "now" so insertion time and staleness are fully deterministic in the core tests.
_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _seen(ago_sec: float, mac: str = "AA:BB:CC:00:00:01") -> Target:
    t = Target(mac=mac, target_type=TargetType.AP)
    t.last_seen = _NOW - timedelta(seconds=ago_sec)
    return t


# ── core summary ────────────────────────────────────────────────────────────────────────

def test_buckets_split_fresh_recent_and_stale():
    targets = [
        _seen(5, "AA:BB:CC:00:00:01"),      # fresh   (<= 30 s)
        _seen(29, "AA:BB:CC:00:00:02"),     # fresh
        _seen(60, "AA:BB:CC:00:00:03"),     # recent  (30 s – 2 min)
        _seen(300, "AA:BB:CC:00:00:04"),    # stale   (> 2 min)
    ]
    s = summarize_freshness(targets, now=_NOW)
    assert s["total"] == 4
    assert (s["fresh"], s["recent"], s["stale"]) == (2, 1, 1)


def test_bucket_boundaries_land_in_the_lower_bucket():
    # 30 s -> still fresh; 120 s -> still recent; 121 s -> stale (thresholds are inclusive).
    s = summarize_freshness(
        [_seen(30, "AA:BB:CC:00:00:01"),
         _seen(120, "AA:BB:CC:00:00:02"),
         _seen(121, "AA:BB:CC:00:00:03")],
        now=_NOW,
    )
    assert (s["fresh"], s["recent"], s["stale"]) == (1, 1, 1)


def test_newest_and_oldest_ages_are_reported():
    s = summarize_freshness(
        [_seen(10, "AA:BB:CC:00:00:01"), _seen(200, "AA:BB:CC:00:00:02")], now=_NOW
    )
    assert s["newest_age_sec"] == 10.0
    assert s["oldest_age_sec"] == 200.0
    assert s["fresh_within_sec"] == 30 and s["recent_within_sec"] == 120


def test_clock_skew_future_last_seen_clamps_to_zero_not_negative():
    s = summarize_freshness([_seen(-5, "AA:BB:CC:00:00:01")], now=_NOW)
    assert s["newest_age_sec"] == 0.0
    assert s["fresh"] == 1


def test_naive_last_seen_is_treated_as_utc():
    t = Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.AP)
    t.last_seen = (_NOW - timedelta(seconds=10)).replace(tzinfo=None)   # tz-naive
    s = summarize_freshness([t], now=_NOW)
    assert s["total"] == 1 and s["fresh"] == 1


def test_target_without_last_seen_is_skipped_not_guessed():
    class _NoSeen:
        last_seen = None

    s = summarize_freshness([_seen(5, "AA:BB:CC:00:00:01"), _NoSeen()], now=_NOW)
    assert s["total"] == 1        # the field-less object is skipped, not counted as age 0


def test_empty_pool_is_safe():
    s = summarize_freshness([], now=_NOW)
    assert s["total"] == 0
    assert s["newest_age_sec"] is None and s["oldest_age_sec"] is None
    assert (s["fresh"], s["recent"], s["stale"]) == (0, 0, 0)


def test_default_now_uses_current_utc():
    # No now= -> datetime.now(utc); a freshly created target is fresh against real wall-clock.
    s = summarize_freshness([Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.AP)])
    assert s["total"] == 1 and s["fresh"] == 1


# ── /api/freshness web endpoint ─────────────────────────────────────────────────────────

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.ui.web.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def test_api_freshness_summarizes_the_live_pool():
    # The endpoint uses real wall-clock now; offsets of 2 s / 600 s sit well inside their buckets,
    # so the few-ms elapsed between building the pool and the request can't flip a count.
    now = datetime.now(timezone.utc)
    pool = TargetPool(EventBus())
    fresh = Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.AP)
    fresh.last_seen = now - timedelta(seconds=2)
    stale = Target(mac="AA:BB:CC:00:00:02", target_type=TargetType.AP)
    stale.last_seen = now - timedelta(seconds=600)
    pool.add(fresh)
    pool.add(stale)

    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), pool)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    r = client.get("/api/freshness")
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 2
    assert data["fresh"] == 1 and data["stale"] == 1
    assert data["oldest_age_sec"] >= 600.0


def test_targets_page_renders_the_freshness_panel():
    pool = TargetPool(EventBus())
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), pool)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    html = client.get("/targets").get_data(as_text=True)
    assert 'id="freshness"' in html        # the staleness panel is present
    assert 'id="fresh-counts"' in html
    assert 'id="fresh-ages"' in html
