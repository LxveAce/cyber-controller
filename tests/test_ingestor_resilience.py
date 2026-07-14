"""Ingestion-resilience invariant.

A single bad EVENT — a raising ``_event_to_target`` (e.g. a non-numeric rssi hitting ``int()``), a
pool / capture-store ``add`` that throws, or a device-info update that throws — must never break
serial ingestion for the port: it is logged and swallowed, and the NEXT line still routes. Before
the beat-234 red-team hardening only ``parse_line`` was protected; the downstream routing ran
unguarded in ``on_line``, so one exception propagated into the serial reader and dropped every later
line on the connection. This locks the gap between the ingestor's stated "a bad line can never break
serial ingestion" invariant and its actual enforcement.
"""
from __future__ import annotations

from src.core.target_ingest import TargetIngestor
from src.protocols.base import ParsedEvent


class _Conn:
    port = "COM_X"

    def __init__(self) -> None:
        self.cb = None

    def on_line(self, fn) -> None:
        self.cb = fn


class _ApProto:
    """Emits a routable ap_found for every line."""

    def parse_line(self, line):
        return ParsedEvent(
            event_type="ap_found",
            data={"bssid": "AA:BB:CC:00:11:22", "ssid": "n", "rssi": -50, "channel": 1},
            raw=line,
        )


class _BoomPool:
    """A pool whose add() always raises — stands in for any downstream routing failure."""

    def __init__(self) -> None:
        self.calls = 0

    def add(self, target) -> None:
        self.calls += 1
        raise RuntimeError("downstream boom")


def test_downstream_add_exception_is_swallowed_not_propagated():
    pool = _BoomPool()
    ing = TargetIngestor(pool=pool)
    conn = _Conn()
    ing.attach(conn, _ApProto())
    # pool.add raises inside routing; on_line MUST NOT propagate it, and ingestion keeps running.
    conn.cb("scanap-1")
    conn.cb("scanap-2")
    assert pool.calls == 2, "routing stopped after a swallowed downstream exception"


def test_bad_event_does_not_stop_the_next_good_event():
    added = []

    class _OkPool:
        def add(self, t) -> None:
            added.append(t)

    class _FlakyProto:
        """First line makes _event_to_target raise (non-numeric rssi -> int() ValueError); the
        second is a clean ap_found that must still route."""

        def __init__(self) -> None:
            self.n = 0

        def parse_line(self, line):
            self.n += 1
            rssi = "NaN" if self.n == 1 else -40
            return ParsedEvent(
                event_type="ap_found",
                data={"bssid": "AA:BB:CC:00:11:22", "rssi": rssi, "channel": 1},
                raw=line,
            )

    ing = TargetIngestor(pool=_OkPool())
    conn = _Conn()
    ing.attach(conn, _FlakyProto())
    conn.cb("bad")   # _event_to_target raises on int("NaN") -> swallowed
    conn.cb("good")  # routes normally
    assert len(added) == 1 and added[0].mac == "AA:BB:CC:00:11:22"
