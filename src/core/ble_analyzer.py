"""BLE analyzer model — the pure, firmware-agnostic core behind the Bluetooth-analyzer output view.

Stream-A's archetype is an output view that reproduces the on-device Bluetooth analyzer: a live
signal-strength graph + device table, not a text dump. This is the Qt-free foundation that view
renders. It ingests the ble_found events every BLE-capable firmware already emits (Marauder, Ghost
ESP, Flipper, HaleHound and ESP32-DIV send mac/name/rssi; LxveOS sends the same shape keyed addr,
plus company/tracker/type) into one device table, and keeps a bounded per-device RSSI series.

Pure and unit-testable with no Qt or serial: observe(data, now) takes a parsed event dict + an
injected timestamp (it never reads the clock, so tests are deterministic and the view owns it).
Awareness-only: it visualizes what's advertising nearby and drives no device.

Posture matches the codebase's parsers: never trust the input shape, bound memory (a spam flood
can't grow it without limit), and keep a missing RSSI distinct from a real one — 0 dBm at the
antenna is absurd, so an absent/garbage rssi becomes None and is never plotted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.core.ble_numbers import lookup_company

# ── bounds (a BLE-advert flood must not grow the model without limit) ──
_MAX_DEVICES = 4096      # stalest device is evicted when a new address arrives at the cap
_MAX_SAMPLES = 240       # per-device RSSI ring buffer for the graph — a few minutes at ~1 Hz
_DEFAULT_TTL = 30.0      # seconds since last_seen after which a device is considered stale

# RSSI → signal-bar thresholds, matching SignalBarsDelegate so graph/table agree on "strong":
# > -50 = 4 bars, > -65 = 3, > -75 = 2, else 1. A missing reading is 0 bars.
_BAR_THRESHOLDS = ((-50, 4), (-65, 3), (-75, 2))


def _as_int(value: object) -> Optional[int]:
    """Coerce an event field to int, or None if unusable. None (not 0) keeps a missing RSSI distinct
    from a real one — the graph plots real samples only, never a phantom 0 dBm."""
    if value is None or isinstance(value, bool):  # bool subclasses int; a stray True isn't RSSI
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError fires on int(inf)/int(nan)
        return None


def _truthy(value: object) -> bool:
    """Tolerant truthiness for a flag arriving as 1/"1"/True/"true"/"yes" across firmwares."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def normalize_addr(data: object) -> Optional[str]:
    """Extract the BLE address from a ble_found / LxveOS ble event and normalize to a lowercase key.
    Accepts mac (Marauder/Ghost/Flipper/etc) or addr (LxveOS). Returns None when neither is
    usable — such an event can't be a table row and is dropped clean."""
    if not isinstance(data, dict):
        return None
    raw = data.get("mac")
    if raw is None or raw == "":
        raw = data.get("addr")
    if not isinstance(raw, str):
        return None
    addr = raw.strip().lower()
    return addr or None


def rssi_bars(rssi: Optional[int]) -> int:
    """Map an RSSI to 0-4 signal bars (0 = unknown/missing). Pure so graph, table, and header agree;
    thresholds mirror SignalBarsDelegate."""
    if rssi is None:
        return 0
    for floor, bars in _BAR_THRESHOLDS:
        if rssi > floor:
            return bars
    return 1


def rssi_quality(rssi: Optional[int]) -> str:
    """A one-word signal label for the readout: strong / good / fair / weak / — (unknown)."""
    return {0: "—", 1: "weak", 2: "fair", 3: "good", 4: "strong"}[rssi_bars(rssi)]


@dataclass
class BleDevice:
    """One de-duplicated BLE advertiser, aggregated across every ble_found sighting of its address.

    rssi is the latest reading; rssi_min/rssi_max bound the range. samples is a bounded (timestamp,
    rssi) ring buffer for the graph — real readings only, so a device that only ever advertised
    without a parseable RSSI has an empty series (0 bars), not a flat line. tracker is sticky-True:
    once a firmware flags an address as a tracker, a later plain hit doesn't clear it."""
    addr: str
    name: str = ""
    vendor: str = ""
    company: str = ""          # raw company/manufacturer id when a firmware sends one (LxveOS ble)
    company_name: str = ""     # the id resolved to a Bluetooth-SIG vendor name (e.g. 76 -> "Apple, Inc.")
    addr_type: str = ""        # "public" / "random" when reported
    tracker: bool = False
    rssi: Optional[int] = None
    rssi_min: Optional[int] = None
    rssi_max: Optional[int] = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 0
    samples: List[Tuple[float, int]] = field(default_factory=list)

    def display_name(self) -> str:
        """Name for the row, or a placeholder when the advert is nameless (most are)."""
        return self.name if self.name else "(unknown)"

    def age(self, now: float) -> float:
        """Seconds since this device was last heard (>= 0)."""
        return max(0.0, now - self.last_seen)

    def is_fresh(self, now: float, ttl: float = _DEFAULT_TTL) -> bool:
        return self.age(now) <= ttl

    def freshness(self, now: float, ttl: float = _DEFAULT_TTL) -> float:
        """1.0 just-seen → 0.0 at/after ttl, linear. The view fades a stale row by this factor so a
        device that has left the area visibly decays instead of lingering as if present."""
        if ttl <= 0:
            return 1.0 if self.age(now) <= 0 else 0.0
        return max(0.0, min(1.0, 1.0 - self.age(now) / ttl))

    def bars(self) -> int:
        return rssi_bars(self.rssi)

    def to_dict(self) -> dict:
        return {
            "addr": self.addr,
            "name": self.name,
            "vendor": self.vendor,
            "company": self.company,
            "company_name": self.company_name,
            "addr_type": self.addr_type,
            "tracker": self.tracker,
            "rssi": self.rssi,
            "rssi_min": self.rssi_min,
            "rssi_max": self.rssi_max,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "hits": self.hits,
            "sample_count": len(self.samples),
        }


class BleAnalyzerModel:
    """Live, firmware-agnostic aggregation of BLE advertisers for the analyzer view.

    Feed parsed ble_found / ble event data via observe() with an injected timestamp; read a ranked
    device list via devices(), per-device RSSI history via series(), a header rollup via summary().
    Bounded: at max_devices a new address evicts the stalest, and each device keeps at most
    max_samples graph points — so a BLE-spam flood is capped, not unbounded growth."""

    def __init__(self, max_devices: int = _MAX_DEVICES, max_samples: int = _MAX_SAMPLES) -> None:
        self._devices: "Dict[str, BleDevice]" = {}
        self._max_devices = max(1, int(max_devices))
        self._max_samples = max(1, int(max_samples))

    # ── ingest ───────────────────────────────────────────────────────
    def observe(self, data: object, now: float) -> Optional[BleDevice]:
        """Fold one BLE sighting into the table. data is a parsed ble_found / LxveOS ble dict;
        now is the caller's timestamp. Returns the updated/created BleDevice, or None when the event
        carries no usable address (dropped clean, never raised)."""
        addr = normalize_addr(data)
        if addr is None:
            return None
        assert isinstance(data, dict)  # normalize_addr already guaranteed this
        rssi = _as_int(data.get("rssi"))

        dev = self._devices.get(addr)
        if dev is None:
            if len(self._devices) >= self._max_devices:
                self._evict_stalest()
            dev = BleDevice(addr=addr, first_seen=now)
            self._devices[addr] = dev

        dev.last_seen = now
        dev.hits += 1

        # Name: overwrite only with a NEW non-empty name; a nameless re-advert must not blank it.
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            dev.name = name.strip()
        for src_key, dst in (("vendor", "vendor"), ("company", "company"), ("type", "addr_type")):
            val = data.get(src_key)
            if isinstance(val, str) and val.strip():
                setattr(dev, dst, val.strip())
            elif val is not None and not isinstance(val, str):
                setattr(dev, dst, str(val))  # LxveOS company is numeric — keep as a string tag
        if dev.company:
            # Resolve the numeric SIG company id to a vendor name (all 3998 companies, vs the ~6 the
            # firmware names). Keep the raw id; only set the name when the lookup is certain (never blank a
            # prior name with an "" from an unknown id).
            resolved = lookup_company(dev.company)
            if resolved:
                dev.company_name = resolved
        if _truthy(data.get("tracker")):
            dev.tracker = True  # sticky: a tracker verdict isn't un-flagged by a later plain hit

        if rssi is not None:
            dev.rssi = rssi
            dev.rssi_min = rssi if dev.rssi_min is None else min(dev.rssi_min, rssi)
            dev.rssi_max = rssi if dev.rssi_max is None else max(dev.rssi_max, rssi)
            dev.samples.append((now, rssi))
            if len(dev.samples) > self._max_samples:
                # Drop the oldest sample(s); keep a plain list the graph iterates directly.
                del dev.samples[: len(dev.samples) - self._max_samples]
        return dev

    def _evict_stalest(self) -> None:
        """Make room by dropping the least-recently-heard address (most likely to have left
        the area). O(n) over the table — n is bounded by max_devices."""
        if not self._devices:
            return
        stalest = min(self._devices.values(), key=lambda d: d.last_seen)
        self._devices.pop(stalest.addr, None)

    # ── read ─────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._devices)

    @property
    def device_count(self) -> int:
        return len(self._devices)

    def get(self, addr: str) -> Optional[BleDevice]:
        return self._devices.get(addr.strip().lower()) if isinstance(addr, str) else None

    def devices(self, sort: str = "rssi", now: Optional[float] = None,
                ttl: float = _DEFAULT_TTL, fresh_only: bool = False) -> "List[BleDevice]":
        """Ranked device list. sort: rssi (strongest first), recent (most-recent first),
        name (named before unknown, then address), or hits (busiest first). fresh_only drops devices
        older than ttl (needs now)."""
        items = list(self._devices.values())
        if fresh_only and now is not None:
            items = [d for d in items if d.is_fresh(now, ttl)]
        if sort == "recent":
            items.sort(key=lambda d: d.last_seen, reverse=True)
        elif sort == "hits":
            items.sort(key=lambda d: (d.hits, self._rssi_key(d.rssi)), reverse=True)
        elif sort == "name":
            # Named devices first (a real name beats "(unknown)"), then by name, then address.
            items.sort(key=lambda d: (d.name == "", d.name.lower(), d.addr))
        else:  # "rssi" (default): strongest first; a missing RSSI ranks below any real reading.
            items.sort(key=lambda d: self._rssi_key(d.rssi), reverse=True)
        return items

    @staticmethod
    def _rssi_key(rssi: Optional[int]) -> int:
        """Sort key: a missing RSSI ranks below any real reading (flock's sentinel)."""
        return rssi if rssi is not None else -9999

    def series(self, addr: str) -> "List[Tuple[float, int]]":
        """The (timestamp, rssi) samples for one device (graph data). Empty list for an unknown
        address or a device that never had a parseable RSSI."""
        dev = self.get(addr)
        return list(dev.samples) if dev is not None else []

    def summary(self, now: Optional[float] = None, ttl: float = _DEFAULT_TTL) -> dict:
        """Header rollup: total tracked, fresh (within ttl), trackers, named, and the strongest
        current signal — the one-line airspace readout above the graph."""
        devs = list(self._devices.values())
        fresh = [d for d in devs if now is None or d.is_fresh(now, ttl)]
        strongest = max((d.rssi for d in fresh if d.rssi is not None), default=None)
        return {
            "total": len(devs),
            "fresh": len(fresh),
            "trackers": sum(1 for d in devs if d.tracker),
            "named": sum(1 for d in devs if d.name),
            "strongest": strongest,
        }

    def prune(self, now: float, ttl: float = _DEFAULT_TTL) -> int:
        """Drop devices not heard within ttl. Returns the count removed. Housekeeping — the
        table can also just fade stale rows via BleDevice.freshness and keep them for history."""
        stale = [a for a, d in self._devices.items() if not d.is_fresh(now, ttl)]
        for addr in stale:
            del self._devices[addr]
        return len(stale)

    def clear(self) -> None:
        self._devices.clear()
