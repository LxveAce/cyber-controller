"""AutoRouter proximity floor: the unknown-RSSI sentinel (0) must NOT slip past an explicit min_rssi
floor, or a "nearby/strong APs only" rule fires its command (deauth/etc.) on out-of-range targets."""

from __future__ import annotations

import pytest

cross_comm = pytest.importorskip("src.core.cross_comm")
from src.core.cross_comm import AutoRouter, RoutingRule, TargetType  # noqa: E402


def _rule(**kw):
    base = dict(name="r", target_type=TargetType.AP, ssid_pattern="", min_rssi=-100,
                device_port="COMX", command_template="deauth {mac}", cooldown=30.0, enabled=True)
    base.update(kw)
    return RoutingRule(**base)


def test_explicit_floor_rejects_unknown_and_weak_rssi():
    rule = _rule(min_rssi=-45)  # "nearby/strong APs only"
    m = AutoRouter._matches
    assert m(rule, TargetType.AP, "net", -30) is True   # strong -> fires
    assert m(rule, TargetType.AP, "net", -60) is False  # weak -> rejected
    assert m(rule, TargetType.AP, "net", 0) is False    # UNKNOWN (sentinel) -> must NOT slip past the floor


def test_default_floor_still_matches_unknown_rssi():
    # A rule left at the default -100 imposes no real proximity requirement, so an unknown reading matches.
    assert AutoRouter._matches(_rule(min_rssi=-100), TargetType.AP, "net", 0) is True
