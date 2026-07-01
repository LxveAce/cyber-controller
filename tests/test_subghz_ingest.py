"""Cross-firmware SubGHz ingest (src/core/target_ingest.py).

Regression: the subghz_found ingest branch read only 'modulation'/'data' (HaleHound's field names), so
a Flipper capture — which emits 'protocol'/'key' — landed in the pool with a blank label and lost its
Key payload (the field that identifies the specific signal). Pure logic, no hardware."""

from __future__ import annotations


class _Ev:
    event_type = "subghz_found"
    data = {"protocol": "Princeton", "key": "0x001234", "frequency": "433.92", "rssi": -40}


def test_subghz_ingest_keeps_flipper_protocol_and_key():
    from src.core.target_ingest import TargetIngestor
    from src.core.cross_comm import TargetPool, EventBus

    ing = TargetIngestor(TargetPool(EventBus()))
    t = ing._event_to_target(_Ev(), "COM9")
    assert t is not None
    assert t.ssid == "Princeton"          # decoded protocol label preserved (was blank)
    assert t.extra["data"] == "0x001234"  # the Key / signal payload preserved (was dropped)


def test_subghz_ingest_still_honors_halehound_fields():
    from src.core.target_ingest import TargetIngestor
    from src.core.cross_comm import TargetPool, EventBus

    class _HH:
        event_type = "subghz_found"
        data = {"modulation": "AM650", "data": "AABBCC", "frequency": "315.0"}

    ing = TargetIngestor(TargetPool(EventBus()))
    t = ing._event_to_target(_HH(), "COM8")
    assert t.ssid == "AM650" and t.extra["data"] == "AABBCC"
