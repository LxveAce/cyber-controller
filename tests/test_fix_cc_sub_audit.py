"""Regression guards for the 2026-07-12 protocol/core adversarial audit (cc-sub-audit).

Four CONFIRMED robustness defects in the non-UI serial path, each triggered by realistic
device output and each fixed minimally:

    #1 serial_handler._reader_loop — a device that streams with NO CR/LF grew the line buffer
       without bound (memory exhaustion). It is now capped and flushed at _MAX_LINE_CHARS.
    #2 flipper.parse_line — a huge-magnitude SubGHz RSSI is a valid float *shape* (passes the RSSI
       regex) but float() of it is inf and int(inf) raises OverflowError, killing the reader thread.
       Now guarded -> the signal still reports, only the RSSI degrades to unknown.
    #3 cross_comm.TargetPool.add — rotating BLE/Wi-Fi MACs (or a SubGHz flood) grew the pool without
       limit (prune() was never called on a timer). At the cap the least-recently-seen target is
       evicted and a target.removed event fires.
    #4 target_ingest._event_to_target — a SubGHz target keyed on frequency ALONE collapsed every
       distinct signal on a shared band (433.92 MHz) into one entry. Now keyed on
       frequency+protocol+signal so distinct signals stay distinct and the same signal still dedupes.

All pure logic / fakes — no hardware, no real port opened.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest


# ── #1 serial_handler: unbounded line buffer is capped ───────────────────────────────────────────

serial_handler = pytest.importorskip("src.core.serial_handler")  # imports `serial` at module top
pytest.importorskip("serial")
SerialConnection = serial_handler.SerialConnection


class _Scripted:
    """Minimal fake serial: returns scripted chunks, then idles returning b"" (no hot-spin)."""

    def __init__(self, chunks) -> None:
        self.is_open = True
        self._chunks = list(chunks)

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        time.sleep(0.005)
        return b""

    def close(self) -> None:
        self.is_open = False


def _run(conn) -> threading.Thread:
    conn._set_state(serial_handler.ConnectionState.CONNECTED)
    th = threading.Thread(target=conn._reader_loop, daemon=True)
    th.start()
    return th


def test_reader_loop_caps_unbounded_buffer(monkeypatch) -> None:
    # A device streaming with NO line terminator must not grow buf forever. Once past the cap the
    # buffer is flushed as one line (bounded memory) rather than accumulating until OOM.
    monkeypatch.setattr(serial_handler, "_MAX_LINE_CHARS", 64)
    conn = SerialConnection("X")
    got: list[str] = []
    done = threading.Event()
    conn.on_line(lambda ln: (got.append(ln), done.set()))
    conn._serial = _Scripted([b"Z" * 200])  # 200 bytes, no CR/LF, over the patched 64-char cap
    th = _run(conn)
    assert done.wait(2.0), "oversized unterminated buffer was never flushed (would grow unbounded)"
    conn._stop_event.set()
    th.join(timeout=2.0)
    assert got and set(got[0]) == {"Z"} and len(got[0]) > 64


def test_reader_loop_below_cap_stays_buffered(monkeypatch) -> None:
    # A sub-cap fragment with no terminator must still wait for its line ending — the cap must not
    # flush a normal partial line early.
    monkeypatch.setattr(serial_handler, "_MAX_LINE_CHARS", 1024)
    conn = SerialConnection("X")
    got: list[str] = []
    conn.on_line(got.append)
    conn._serial = _Scripted([b"partial-no-terminator"])  # 21 bytes, below the cap
    th = _run(conn)
    time.sleep(0.15)  # let the loop consume the chunk and idle
    conn._stop_event.set()
    th.join(timeout=2.0)
    assert got == []  # unterminated sub-cap fragment stays buffered, not emitted


# ── #2 flipper: huge-magnitude SubGHz RSSI must not raise OverflowError ───────────────────────────

_SUBGHZ_BASE = "SubGhz: Protocol: Princeton | Bit: 24 | Key: 0x001234 | Freq: 433.92 MHz | RSSI: "


@pytest.mark.parametrize("rssi", ["9" * 400, "-" + "9" * 400])
def test_huge_magnitude_subghz_rssi_does_not_raise(rssi: str) -> None:
    # A few-hundred-digit RSSI is a valid float SHAPE, so it passes the RSSI regex — but float() of it
    # is +/-inf and int(inf) raises OverflowError, which pre-fix killed the reader thread. It must
    # degrade to "no rssi" while still reporting the signal.
    from src.protocols.base import ParsedEvent
    from src.protocols.flipper import FlipperProtocol

    ev = FlipperProtocol().parse_line(_SUBGHZ_BASE + rssi)
    assert isinstance(ev, ParsedEvent)
    assert ev.event_type == "subghz_found"
    assert "rssi" not in ev.data  # unconvertible RSSI dropped, not crashed


# ── #3 cross_comm.TargetPool: bounded, evicts least-recently-seen ─────────────────────────────────

def test_pool_evicts_oldest_when_over_cap(monkeypatch) -> None:
    # A stream of never-before-seen keys (rotating MACs / SubGHz flood) must not grow the pool without
    # bound: at the cap the least-recently-seen target is evicted to admit the new one, and a
    # target.removed event fires so subscribers stay consistent.
    import src.core.cross_comm as cross_comm
    from src.models.target import Target, TargetType

    monkeypatch.setattr(cross_comm, "_MAX_TARGETS", 3)
    bus = cross_comm.EventBus()
    removed: list[dict] = []
    bus.subscribe("target.removed", lambda _topic, payload: removed.append(payload))
    pool = cross_comm.TargetPool(bus)

    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        t = Target(mac=f"AA:BB:CC:00:00:0{i}", target_type=TargetType.BLE)
        t.last_seen = base + timedelta(seconds=i)  # ...:00 oldest ... ...:02 newest
        pool.add(t)
    assert pool.count == 3

    newest = Target(mac="AA:BB:CC:00:00:99", target_type=TargetType.BLE)
    newest.last_seen = base + timedelta(seconds=99)
    assert pool.add(newest) is True

    assert pool.count == 3  # stayed at the cap
    assert pool.get("ble:AA:BB:CC:00:00:00") is None      # least-recently-seen evicted
    assert pool.get("ble:AA:BB:CC:00:00:99") is not None  # new target admitted
    assert any(p.get("mac") == "AA:BB:CC:00:00:00" for p in removed)  # removal was broadcast


def test_pool_update_of_existing_never_evicts(monkeypatch) -> None:
    # Re-observing an EXISTING target updates in place — it must not count as growth or trigger eviction
    # even when the pool sits exactly at the cap.
    import src.core.cross_comm as cross_comm
    from src.models.target import Target, TargetType

    monkeypatch.setattr(cross_comm, "_MAX_TARGETS", 2)
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    pool.add(Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.BLE))
    pool.add(Target(mac="AA:BB:CC:00:00:02", target_type=TargetType.BLE))
    assert pool.count == 2
    # Update the first (same key) — no new slot needed, both must survive.
    assert pool.add(Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.BLE, rssi=-30)) is False
    assert pool.count == 2
    assert pool.get("ble:AA:BB:CC:00:00:01") is not None
    assert pool.get("ble:AA:BB:CC:00:00:02") is not None


def test_pool_reobservation_refreshes_recency_and_shields_from_eviction(monkeypatch) -> None:
    # Re-observing a target must shield it from being the next eviction victim. FREEZE the clock so
    # every last_seen is identical: now insertion-order and recency-order diverge. The old
    # min()-over-last_seen ties and evicts the FIRST-inserted key (:01, just re-observed — wrong);
    # the O(1) OrderedDict (move_to_end on touch) evicts the true LRU (:02). Fails on the old code.
    import src.core.cross_comm as cross_comm
    import src.models.target as target_mod
    from src.models.target import Target, TargetType

    frozen = datetime(2020, 1, 1, tzinfo=timezone.utc)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(target_mod, "datetime", _FrozenDT)  # freeze update_seen's last_seen stamp
    monkeypatch.setattr(cross_comm, "_MAX_TARGETS", 3)
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    for suffix in ("01", "02", "03"):
        pool.add(Target(mac=f"AA:BB:CC:00:00:{suffix}", target_type=TargetType.BLE))

    # Re-observe the OLDEST (first-inserted) key. The clock is frozen, so its last_seen does NOT
    # advance — only the OrderedDict recency order moves it off the eviction front.
    assert pool.add(Target(mac="AA:BB:CC:00:00:01", target_type=TargetType.BLE, rssi=-40)) is False

    # A new key now evicts :02 (the true LRU), NOT the just-re-observed :01.
    assert pool.add(Target(mac="AA:BB:CC:00:00:99", target_type=TargetType.BLE)) is True
    assert pool.count == 3
    assert pool.get("ble:AA:BB:CC:00:00:01") is not None  # shielded (fails on old code)
    assert pool.get("ble:AA:BB:CC:00:00:02") is None      # the genuine least-recently-seen went
    assert pool.get("ble:AA:BB:CC:00:00:99") is not None


# ── #4 target_ingest: SubGHz keyed by freq+protocol+signal, not freq alone ────────────────────────

def _ingestor():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.target_ingest import TargetIngestor

    return TargetIngestor(TargetPool(EventBus()))


class _SubGhzEv:
    event_type = "subghz_found"

    def __init__(self, **data) -> None:
        self.data = data


def test_subghz_distinct_signals_on_same_band_do_not_collapse() -> None:
    # Two different remotes on the SAME band (433.92 MHz) must produce DISTINCT targets. Pre-fix both
    # keyed on the frequency alone, so the second merely "updated" the first and vanished.
    ing = _ingestor()
    a = ing._event_to_target(_SubGhzEv(frequency="433.92", protocol="Princeton", key="0x001234"), "COM9")
    b = ing._event_to_target(_SubGhzEv(frequency="433.92", protocol="CAME", key="0x00ABCD"), "COM9")
    assert a is not None and b is not None
    assert a.mac != b.mac and a.key != b.key  # distinct identities
    ing._pool.add(a)
    ing._pool.add(b)
    assert ing._pool.count == 2  # both survive, neither swallowed the other


def test_subghz_same_signal_still_dedupes() -> None:
    # A genuine RE-observation of the SAME signal (same freq+protocol+key) must still dedupe to one.
    ing = _ingestor()
    first = ing._event_to_target(_SubGhzEv(frequency="433.92", protocol="Princeton", key="0x001234"), "COM9")
    again = ing._event_to_target(_SubGhzEv(frequency="433.92", protocol="Princeton", key="0x001234"), "COM9")
    assert first.key == again.key
    ing._pool.add(first)
    ing._pool.add(again)
    assert ing._pool.count == 1


def test_subghz_preserves_label_data_and_frequency() -> None:
    # The composite key must not cost the human-facing fields: protocol label + signal payload survive
    # (the original test_subghz_ingest contract) AND the raw frequency stays recoverable in extra.
    ing = _ingestor()
    t = ing._event_to_target(_SubGhzEv(frequency="433.92", protocol="Princeton", key="0x001234", rssi=-40), "COM9")
    assert t.ssid == "Princeton"
    assert t.extra["data"] == "0x001234"
    assert t.extra["frequency"] == "433.92"
    assert t.rssi == -40


def test_subghz_freq_only_device_degrades_to_freq_key() -> None:
    # A device that emits only a frequency (no protocol/data) degrades to the old freq-only key —
    # backward compatible, no composite noise.
    ing = _ingestor()
    t = ing._event_to_target(_SubGhzEv(frequency="315.0"), "COM8")
    assert t.mac == "315.0"
    assert t.extra["frequency"] == "315.0"
