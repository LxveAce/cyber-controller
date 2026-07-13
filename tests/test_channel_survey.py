"""Passive channel-occupancy survey + its /api/channels web endpoint."""
from __future__ import annotations

import pytest

from src.core.channel_survey import survey_channels
from src.models.target import Target, TargetType


def _ap(channel: int, mac: str = "AA:BB:CC:00:00:01"):
    return Target(mac=mac, target_type=TargetType.AP, channel=channel)


# ── core survey ─────────────────────────────────────────────────────────────────────────

def test_survey_counts_only_aps_on_real_channels():
    targets = [
        _ap(6, "AA:BB:CC:00:00:01"),
        _ap(6, "AA:BB:CC:00:00:02"),
        _ap(0, "AA:BB:CC:00:00:03"),                                   # unknown channel -> ignored
        Target(mac="AA:BB:CC:00:00:04", target_type=TargetType.CLIENT, channel=6),   # not an AP
        Target(mac="AA:BB:CC:00:00:05", target_type=TargetType.BLE, channel=6),      # not an AP
    ]
    s = survey_channels(targets)
    assert s["total_aps"] == 2
    assert s["per_channel"] == {6: 2}


def test_survey_band_split_24_vs_5():
    s = survey_channels([_ap(1, "AA:BB:CC:00:00:01"), _ap(11, "AA:BB:CC:00:00:02"),
                         _ap(36, "AA:BB:CC:00:00:03"), _ap(149, "AA:BB:CC:00:00:04")])
    assert s["band_24"] == 2 and s["band_5"] == 2   # ch 1/11 -> 2.4 GHz; ch 36/149 -> 5 GHz


def test_survey_recommends_the_clear_24_channel():
    # APs crowd channels 1 and 6; 11 is empty and non-overlapping -> it must be recommended.
    targets = ([_ap(1, f"AA:BB:CC:00:01:{i:02d}") for i in range(3)]
               + [_ap(6, f"AA:BB:CC:00:06:{i:02d}") for i in range(2)])
    s = survey_channels(targets)
    assert s["recommend_24"] == 11
    assert s["recommend_24_load"] == 0        # nothing overlaps channel 11


def test_survey_recommend_none_without_24ghz():
    s = survey_channels([_ap(36, "AA:BB:CC:00:00:01"), _ap(40, "AA:BB:CC:00:00:02")])
    assert s["recommend_24"] is None and s["recommend_24_load"] is None
    assert s["band_5"] == 2


def test_survey_busiest_is_count_ordered():
    targets = ([_ap(6, f"AA:BB:CC:00:06:{i:02d}") for i in range(3)]
               + [_ap(1, "AA:BB:CC:00:01:00")])
    s = survey_channels(targets)
    assert s["busiest"][0] == (6, 3)          # channel 6 is busiest


def test_survey_empty_is_safe():
    s = survey_channels([])
    assert s["total_aps"] == 0 and s["recommend_24"] is None and s["busiest"] == []


# ── /api/channels web endpoint ──────────────────────────────────────────────────────────

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


def test_api_channels_surveys_the_live_pool():
    pool = TargetPool(EventBus())
    for i in range(3):
        pool.add(_ap(1, f"AA:BB:CC:00:01:{i:02d}"))
    pool.add(_ap(6, "AA:BB:CC:00:06:00"))
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), pool)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    r = client.get("/api/channels")
    assert r.status_code == 200
    data = r.get_json()
    assert data["total_aps"] == 4
    assert data["per_channel"] == {"1": 3, "6": 1}   # JSON keys are strings
    assert data["recommend_24"] == 11                # 1 and 6 are busy -> pick clear 11
