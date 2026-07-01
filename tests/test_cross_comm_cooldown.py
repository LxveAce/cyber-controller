"""AutoRouter cooldown must be atomic across device reader threads and bounded in size.

_on_target runs in EACH connected radio's serial reader thread; an unlocked check-and-stamp let two
threads see the same target and BOTH fire the routed command (double deauth). _cooldowns also grew
without bound over a long scan/wardrive. Pure logic, no hardware."""

from __future__ import annotations

import threading

import pytest

cross_comm = pytest.importorskip("src.core.cross_comm")


def _rule(**kw):
    from src.core.cross_comm import RoutingRule, TargetType
    base = dict(name="r", target_type=TargetType.AP, ssid_pattern="", min_rssi=-100,
                device_port="COMX", command_template="deauth {mac}", cooldown=30.0, enabled=True)
    base.update(kw)
    return RoutingRule(**base)


def test_cooldown_atomic_under_concurrency():
    bus = cross_comm.EventBus()
    sends, sl = [], threading.Lock()

    def send(port, cmd):
        with sl:
            sends.append((port, cmd))

    router = cross_comm.AutoRouter(bus, send)
    router.add_rule(_rule(cooldown=30.0))
    payload = {"target_type": "ap", "mac": "AA:BB:CC:DD:EE:FF", "ssid": "x", "rssi": -30, "channel": 1}

    n = 16
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()  # release all threads at once to race the cooldown
        router._on_target("target.added", dict(payload))

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sends) == 1, f"cooldown must dedupe concurrent same-target hits, got {len(sends)}"


def test_cooldowns_bounded():
    bus = cross_comm.EventBus()
    router = cross_comm.AutoRouter(bus, lambda p, c: None)
    router.add_rule(_rule(cooldown=0.0))
    for i in range(6000):
        bus.publish("target.added", {
            "target_type": "ap",
            "mac": f"AA:BB:CC:DD:{(i >> 8) & 0xFF:02x}:{i & 0xFF:02x}",
            "ssid": "s", "rssi": -30, "channel": 1,
        })
    assert len(router._cooldowns) <= 4096
