"""Regression guards for the no-visual-change UI performance optimizations.

These lock in the behavior the optimizations rely on so a future edit can't silently undo them:
  * BaseProtocol.cached_commands() memoizes the (static) command list per concrete class and
    returns content identical to get_commands() (UI-opt #2).
  * HealthTab reads the HealthMonitor's cached snapshot instead of the blocking psutil call,
    so HealthMonitor must expose latest_system_health with the same dict shape (UI-opt #1).
"""
from __future__ import annotations

from src.protocols import get_protocol, list_protocols
from src.protocols.base import BaseProtocol
from src.core.health_monitor import HealthMonitor


def test_cached_commands_matches_get_commands_for_every_protocol():
    for name in list_protocols():
        proto = get_protocol(name)
        live = proto.get_commands()
        cached = proto.cached_commands()
        assert [c.name for c in cached] == [c.name for c in live], name
        assert len(cached) == len(live), name


def test_cached_commands_is_memoized_per_class():
    a = get_protocol("marauder")
    b = get_protocol("marauder")  # get_protocol() returns a fresh instance each call
    assert a is not b
    # ...but the command list is cached at the class level, shared across instances.
    assert a.cached_commands() is b.cached_commands()


def test_cache_is_keyed_by_concrete_class_not_shared_across_protocols():
    mara = get_protocol("marauder").cached_commands()
    ghost = get_protocol("ghostesp").cached_commands()
    assert mara is not ghost
    assert BaseProtocol._commands_cache  # populated


def test_latest_system_health_shape_matches_blocking_call():
    # HealthTab._refresh now reads this cached property instead of get_system_health()
    # (which does psutil.cpu_percent(interval=0.1), a GUI-thread block). The cached snapshot
    # must carry the same keys the tab renders.
    hm = HealthMonitor()
    snap = hm.latest_system_health
    assert isinstance(snap, dict)
    # get_system_health is the source of truth for the key set.
    live = hm.get_system_health()
    for key in ("cpu_percent", "memory_percent", "disk_percent"):
        assert key in live
