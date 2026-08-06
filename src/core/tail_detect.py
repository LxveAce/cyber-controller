"""Follower / tail detection — a persistence tracker over the shared detection stream.

Counter-surveillance: a device that keeps reappearing near you across time (and, with GPS, across
locations) may be following you. This is CC's own take on the method from ArgeliusLabs'
**Chasing-Your-Tail-NG** (MIT) — time-window persistence scoring over probe-request / BLE / client
detections, with an ignore list for your own devices. The algorithm here is a clean reimplementation
of that documented approach (rolling fixed-size time windows; a device present across many windows
scores high), attributed to ArgeliusLabs/Chasing-Your-Tail-NG.

Two pieces:

- :class:`PersistenceTracker` — pure, wall-clock-free (the caller supplies timestamps): buckets each
  observation into a fixed-size window and scores a device by the fraction of the last N windows
  it appeared in. An ignore set filters your own known devices out.
- :func:`attach_tail_detector` — wires a tracker to a ``TargetIngestor`` via its existing
  ``add_event_observer`` hook (the same read-only tap the metrics layer uses) to feed the personal/
  mobile detections (probe_request / ble_found / client_found) into the tracker.

Awareness-first + read-only: this only OBSERVES and SCORES. It flags "a device keeps reappearing";
it never acts on that device — no attack, no transmit. safety.py is untouched.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class TailHit:
    """One device scored as a possible tail: its key, a human label, its persistence [0..1], and the
    count of distinct recent windows it appeared in."""

    device: str
    label: str
    persistence: float
    windows: int


class PersistenceTracker:
    """Time-window persistence scorer. Wall-clock-free: every method takes the timestamp explicitly,
    so behavior is deterministic and testable (the app passes ``time.time()``).

    A device's observations are bucketed into fixed ``window_seconds`` slots. Persistence at time
    ``now`` is ``(# of the last num_windows slots it appeared in) / num_windows`` — 1.0 means it was
    seen in every recent window (a strong tail signal), 0.0 means not seen recently."""

    def __init__(self, window_seconds: int = 300, num_windows: int = 4,
                 ignore: "set[str] | None" = None) -> None:
        self.window_seconds = max(1, int(window_seconds))
        self.num_windows = max(1, int(num_windows))
        self.ignore: set[str] = set(ignore or ())
        self._seen: dict[str, set[int]] = {}   # device -> set of bucket indices seen
        self._label: dict[str, str] = {}       # device -> best human label

    def _bucket(self, ts: float) -> int:
        return int(ts // self.window_seconds)

    def observe(self, device: str, ts: float, label: str = "") -> None:
        """Record that *device* was seen at *ts*. Ignored devices are dropped. A non-empty *label*
        updates the device's display label."""
        if not device or device in self.ignore:
            return
        self._seen.setdefault(device, set()).add(self._bucket(ts))
        if label:
            self._label[device] = label

    def prune(self, now: float) -> None:
        """Drop observations older than the last ``num_windows`` windows (bounded memory)."""
        oldest = self._bucket(now) - self.num_windows + 1
        for device in list(self._seen):
            kept = {b for b in self._seen[device] if b >= oldest}
            if kept:
                self._seen[device] = kept
            else:
                self._seen.pop(device, None)
                self._label.pop(device, None)

    def score(self, device: str, now: float) -> float:
        """Fraction of the last ``num_windows`` windows *device* was in, at time *now* ([0..1])."""
        cur = self._bucket(now)
        oldest = cur - self.num_windows + 1
        recent = {b for b in self._seen.get(device, set()) if oldest <= b <= cur}
        return len(recent) / self.num_windows

    def tails(self, now: float, min_persistence: float = 0.5) -> list[TailHit]:
        """Every non-ignored device whose persistence at *now* is >= *min_persistence*, strongest
        first. Prunes stale data first, so a device that stopped following drops off."""
        self.prune(now)
        hits: list[TailHit] = []
        cur = self._bucket(now)
        oldest = cur - self.num_windows + 1
        for device, buckets in self._seen.items():
            if device in self.ignore:
                continue
            s = self.score(device, now)
            if s >= min_persistence:
                recent = sum(1 for b in buckets if oldest <= b <= cur)
                hits.append(TailHit(device, self._label.get(device, ""), s, recent))
        return sorted(hits, key=lambda h: (-h.persistence, h.device))

    def add_ignore(self, device: str) -> None:
        """Mark *device* as one of your own — drop it and never score it again (the ignore list)."""
        self.ignore.add(device)
        self._seen.pop(device, None)
        self._label.pop(device, None)


# Detections that represent a personal / mobile device that could be following you. APs are excluded
# (a stationary access point isn't a tail); probe requests + BLE + client stations are the signal.
_TAIL_EVENTS = ("probe_request", "ble_found", "client_found")


def _device_key(event_type: str, data: dict) -> str:
    """A stable per-device key for a tail-relevant detection (its MAC/addr), or "" to skip."""
    if event_type == "probe_request":
        return str(data.get("mac") or data.get("client_mac") or "").strip()
    if event_type == "ble_found":
        return str(data.get("mac") or data.get("addr") or "").strip()
    if event_type == "client_found":
        return str(data.get("client_mac") or data.get("mac") or "").strip()
    return ""


def _device_label(event_type: str, data: dict) -> str:
    kinds = {"probe_request": "probe", "ble_found": "BLE", "client_found": "client"}
    kind = kinds.get(event_type, "")
    name = str(data.get("name") or data.get("ssid") or "").strip()
    return f"{kind} {name}".strip() if name else kind


def attach_tail_detector(ingestor: Any, tracker: PersistenceTracker,
                         now_fn: "Callable[[], float] | None" = None) -> Callable[[Any, str], None]:
    """Feed *tracker* from *ingestor*'s parsed-event stream via ``add_event_observer`` — read-only,
    no routing change, no send path. Only personal/mobile detections (probe_request / ble_found /
    client_found) are counted. *now_fn* supplies the observation time (defaults to ``time.time``).
    Returns the observer callback — pass it to ``ingestor.remove_event_observer`` to detach."""
    clock = now_fn or time.time

    def _feed(ev: Any, port: str) -> None:
        et = getattr(ev, "event_type", "") or ""
        if et not in _TAIL_EVENTS:
            return
        d = getattr(ev, "data", {}) or {}
        key = _device_key(et, d)
        if key:
            tracker.observe(key, clock(), label=_device_label(et, d))

    ingestor.add_event_observer(_feed)
    return _feed
