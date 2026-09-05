"""A02 + R05: CPU utilisation is served from ONE dedicated sampler's cache (no cold-thread false 0), with
generation + launch-guard so stale reads start at most one replacement, a failed seed stays stale (not a
fabricated fresh 0), and freshness advances only after a successful sample."""

from __future__ import annotations

import threading
import time

from src.core import health_monitor as hm


def _reset_sampler(monkeypatch):
    monkeypatch.setattr(hm, "_cpu_value", 0.0, raising=False)
    monkeypatch.setattr(hm, "_cpu_ts", 0.0, raising=False)
    monkeypatch.setattr(hm, "_cpu_gen", 0, raising=False)
    monkeypatch.setattr(hm, "_cpu_launching", False, raising=False)


def _slow_cpu(value=37.0):
    def fake(interval=None):
        if interval and interval >= 2:
            time.sleep(30)   # emulate psutil's blocking sample so the loop calls once, never spins
        return value
    return fake


def _counting_threads(starts):
    real = hm.threading.Thread

    def counting(*args, **kwargs):
        if kwargs.get("name") == "cpu-sampler":
            starts["n"] += 1
        return real(*args, **kwargs)
    return counting


def test_reader_never_samples_psutil_itself(monkeypatch):
    _reset_sampler(monkeypatch)
    caller_threads: set[str] = set()

    def fake_cpu_percent(interval=None):
        caller_threads.add(threading.current_thread().name)
        if interval and interval >= 2:
            time.sleep(30)
        return 37.0

    monkeypatch.setattr(hm.psutil, "cpu_percent", fake_cpu_percent)
    assert hm._cpu_percent_nonblocking() == 37.0   # first read seeds on THIS thread + launches the sampler

    results: list[float] = []

    def worker():
        results.append(hm._cpu_percent_nonblocking())

    t = threading.Thread(target=worker, name="reader-xyz")
    t.start()
    t.join()
    assert results == [37.0]
    assert "reader-xyz" not in caller_threads   # the reader thread never sampled psutil


def test_repeated_stale_reads_start_only_one_replacement(monkeypatch):
    _reset_sampler(monkeypatch)
    starts = {"n": 0}
    monkeypatch.setattr(hm.psutil, "cpu_percent", _slow_cpu())
    monkeypatch.setattr(hm.threading, "Thread", _counting_threads(starts))
    # simulate a dead sampler: a stale timestamp, nothing launching
    monkeypatch.setattr(hm, "_cpu_ts", time.monotonic() - hm._CPU_STALE_SECONDS - 5, raising=False)
    for _ in range(5):
        value, stale = hm._cpu_sample()
        assert stale is True            # the old value is surfaced stale on every read
    assert starts["n"] == 1             # ...but only ONE replacement worker was launched


def test_failed_seed_stays_stale_not_fresh_zero(monkeypatch):
    _reset_sampler(monkeypatch)

    def failing(interval=None):
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr(hm.psutil, "cpu_percent", failing)
    value, stale = hm._cpu_sample()
    assert value == 0.0 and stale is True   # a failed sampler is stale/unavailable, not a fresh 0%


def test_first_read_gives_an_immediate_value(monkeypatch):
    _reset_sampler(monkeypatch)
    monkeypatch.setattr(hm.psutil, "cpu_percent", _slow_cpu(55.0))
    value, stale = hm._cpu_sample()
    assert value == 55.0 and stale is False   # first-start seed gives a real, fresh value


def test_health_dict_exposes_cpu_stale(monkeypatch):
    _reset_sampler(monkeypatch)
    monkeypatch.setattr(hm.psutil, "cpu_percent", _slow_cpu(20.0))
    h = hm.HealthMonitor.get_system_health()
    assert "cpu_stale" in h and h["cpu_stale"] is False
