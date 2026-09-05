"""A02: CPU utilisation is served from ONE dedicated sampler's cache, so a web-request thread can't read
a spurious 0 from psutil's per-context non-blocking baseline."""

from __future__ import annotations

import threading
import time

from src.core import health_monitor as hm


def _reset_sampler(monkeypatch):
    monkeypatch.setattr(hm, "_cpu_started", False, raising=False)
    monkeypatch.setattr(hm, "_cpu_value", 0.0, raising=False)


def test_reader_never_samples_psutil_itself(monkeypatch):
    _reset_sampler(monkeypatch)
    caller_threads: set[str] = set()

    def fake_cpu_percent(interval=None):
        caller_threads.add(threading.current_thread().name)
        if interval and interval >= 2:
            time.sleep(30)   # emulate psutil's blocking sample so the loop calls once, never spins
        return 37.0

    monkeypatch.setattr(hm.psutil, "cpu_percent", fake_cpu_percent)

    assert hm._cpu_percent_nonblocking() == 37.0   # first read seeds + starts the dedicated sampler

    results: list[float] = []

    def worker():
        results.append(hm._cpu_percent_nonblocking())

    t = threading.Thread(target=worker, name="reader-xyz")
    t.start()
    t.join()
    assert results == [37.0]                 # the reader got the cached value
    assert "reader-xyz" not in caller_threads  # ...without the reader thread ever sampling psutil


def test_sampler_starts_only_once(monkeypatch):
    _reset_sampler(monkeypatch)
    starts = {"n": 0}
    real_thread = hm.threading.Thread

    def counting_thread(*args, **kwargs):
        if kwargs.get("name") == "cpu-sampler":
            starts["n"] += 1
        return real_thread(*args, **kwargs)

    def fake_cpu_percent(interval=None):
        if interval and interval >= 2:
            time.sleep(30)
        return 5.0

    monkeypatch.setattr(hm.psutil, "cpu_percent", fake_cpu_percent)
    monkeypatch.setattr(hm.threading, "Thread", counting_thread)
    for _ in range(5):
        hm._cpu_percent_nonblocking()
    assert starts["n"] == 1
