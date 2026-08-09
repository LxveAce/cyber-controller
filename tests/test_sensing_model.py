"""SensingModel (WS1 P2 data layer) — folds sensing_verdict payloads into per-node room state.
Pure + clock-injected: deterministic, no Qt/hardware. Mirrors the DroneWatchModel/BleAnalyzerModel
tests. The payload shape matches CsiSensorProtocol.parse_line's ParsedEvent.data."""
from __future__ import annotations

from src.core import sensing
from src.core.sensing_model import SensingModel


def _verdict(presence=True, motion=0.4, conf=0.8, node="n1", tier=None):
    d = {"presence": presence, "motion": motion, "confidence": conf, "node_id": node}
    if tier is not None:
        d["tier"] = tier
    return d


def test_observe_creates_and_updates_one_row_per_node():
    m = SensingModel()
    r = m.observe(_verdict(node="n1"), now=100.0)
    assert r is not None and r.node_id == "n1" and m.count == 1
    assert r.presence is True and abs(r.motion - 0.4) < 1e-9 and r.verdicts == 1
    # a second verdict for the same node UPDATES (latest-wins), does not add a row
    m.observe(_verdict(presence=False, motion=0.0, conf=0.1, node="n1"), now=101.0)
    assert m.count == 1
    n1 = m.get("n1")
    assert n1.presence is False and n1.motion == 0.0 and n1.verdicts == 2 and n1.last_seen == 101.0


def test_distinct_nodes_are_separate_rows():
    m = SensingModel()
    m.observe(_verdict(node="n1"), now=1.0)
    m.observe(_verdict(node="n2"), now=2.0)
    assert m.count == 2
    # most-recently-seen first
    assert [n.node_id for n in m.nodes()] == ["n2", "n1"]


def test_empty_node_id_keys_under_default_not_dropped():
    m = SensingModel()
    r = m.observe(_verdict(node=""), now=1.0)
    assert r is not None and r.node_id == "node"   # single unnamed node still tracked


def test_non_dict_payload_is_ignored():
    m = SensingModel()
    assert m.observe("not a dict", now=1.0) is None
    assert m.observe(None, now=1.0) is None
    assert m.count == 0


def test_motion_and_confidence_clamp_to_unit_range():
    m = SensingModel()
    r = m.observe(_verdict(motion=5.0, conf=-2.0), now=1.0)
    assert r.motion == 1.0 and r.confidence == 0.0


def test_freshness_and_occupied_decay_with_age():
    m = SensingModel()
    m.observe(_verdict(presence=True, node="n1"), now=100.0)
    n1 = m.get("n1")
    assert n1.is_fresh(100.0, ttl=15.0) and n1.occupied(100.0, ttl=15.0)
    # a stale presence=True must NOT read as live occupancy (the node went quiet)
    assert not n1.is_fresh(120.0, ttl=15.0)
    assert not n1.occupied(120.0, ttl=15.0)
    assert n1.freshness(100.0, ttl=10.0) == 1.0
    assert n1.freshness(115.0, ttl=10.0) == 0.0   # past ttl -> fully faded


def test_summary_counts_fresh_and_occupied():
    m = SensingModel()
    m.observe(_verdict(presence=True, node="occupied-fresh"), now=100.0)
    m.observe(_verdict(presence=False, node="empty-fresh"), now=100.0)
    m.observe(_verdict(presence=True, node="occupied-stale"), now=50.0)
    s = m.summary(now=100.0, ttl=15.0)
    assert s["total"] == 3
    assert s["fresh"] == 2                    # the two seen at now=100
    assert s["occupied"] == 1                 # only the fresh + present one
    assert s["any_occupied"] is True


def test_motion_history_is_bounded():
    m = SensingModel()
    for i in range(200):
        m.observe(_verdict(motion=(i % 10) / 10.0, node="n1"), now=float(i))
    hist = m.get("n1").motion_history
    assert len(hist) == 64                     # bounded ring, keeps the most recent
    assert hist[-1] == (199 % 10) / 10.0


def test_eviction_bounds_node_count():
    m = SensingModel(max_nodes=3)
    for i in range(5):
        m.observe(_verdict(node=f"n{i}"), now=float(i))
    assert m.count == 3
    # the stalest (n0, n1) were evicted; the three most-recent remain
    assert {n.node_id for n in m.nodes()} == {"n2", "n3", "n4"}


def test_default_tier_is_proven():
    m = SensingModel()
    r = m.observe(_verdict(node="n1"), now=1.0)   # no tier in payload
    assert r.tier == sensing.PROVEN
