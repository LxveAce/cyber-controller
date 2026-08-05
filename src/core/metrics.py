"""Canonical metrics layer — one reading vocabulary every firmware normalizes up to.

Every firmware speaks its own serial dialect, and the per-protocol parsers already turn those
dialects into ``ParsedEvent``s (``src/protocols/*``). What was missing was a SHARED reading model,
so a Marauder GPS fix, a BFFB CC1101 sub-GHz hit, an LxveOS airspace snapshot and a LxveNode link
telemetry all surface on ONE Dashboard the SAME way, instead of each screen hand-reading a
firmware-specific field. This module is that layer:

- :class:`ReadingKind` — a CLOSED enum of what a reading MEANS (RSSI, GPS fix, battery, a
  detection, …), independent of which firmware produced it.
- :class:`Medium` — the RF / transport domain the reading came from (wifi, ble, sub-GHz, …).
- :class:`Reading` — one normalized value (kind + medium + value + unit + label + source device).
- :class:`MetricsModel` — holds the LATEST reading per (device, kind) and notifies observers.
  Qt-free, so a Qt view wraps it with a signal, exactly like the existing event-observer taps.
- :func:`event_to_readings` — the canonical-events mapping: a ParsedEvent -> zero or more Readings.
  It reads the parsers' EXISTING output; no parser is rewritten. LxveOS / LxveNode's structured
  schema is the canonical shape (the ``caps`` bitmask -> capability slugs, the link
  ``{tier,rssi,snr,latency_ms,dr}``, the airspace snapshot), and every firmware maps up to it.
- :func:`attach_metrics` — wires a MetricsModel to a ``TargetIngestor`` via its existing
  ``add_event_observer`` hook, so the metrics feed is READ-ONLY and adds no branch to routing (the
  ingestor already isolates an observer's failure, so a bad line still can't break ingestion).

Read-only + gate-safe: this layer never sends a command, never touches safety classification, and
never mutates a Target / Capture. It only OBSERVES.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger(__name__)


class ReadingKind(Enum):
    """What a reading MEANS, independent of the firmware that produced it. A CLOSED set: a new kind
    is added deliberately here (and mapped in :func:`event_to_readings`) rather than invented at a
    call site, so the Dashboard renders a fixed, known tile set."""

    DETECTION = "detection"        # something seen (AP/client/BLE/sub-GHz/NFC/RFID/cam)
    CAPTURE = "capture"            # crackable material captured (handshake / PMKID / pcap)
    GPS_FIX = "gps_fix"            # a position fix (lat / lon [/ alt])
    RSSI = "rssi"                  # signal strength, dBm
    CHANNEL = "channel"            # current / observed channel
    BATTERY = "battery"           # battery charge, %
    PACKET_RATE = "packet_rate"   # packet / traffic rate or count
    SPECTRUM = "spectrum"         # a per-channel energy sample (channel + rssi)
    LINK = "link"                 # relay / mesh link state (tier + quality)
    DEVICE_INFO = "device_info"   # board / chip / firmware identity + heap
    CAPABILITIES = "capabilities"  # the device's advertised capability set (LxveOS caps bitmask)
    ARM_STATE = "arm_state"       # armed / safe / pending (safety-relevant; display only here)
    ALERT = "alert"               # a detector fired (surveillance sweep, deauth flood, …)
    AIRSPACE = "airspace"         # occupancy snapshot (aps/open/wps/ble/trackers)


class Medium(Enum):
    """The RF / transport domain a reading came from."""

    WIFI = "wifi"
    BLE = "ble"
    SUBGHZ = "subghz"
    NFC = "nfc"
    RFID = "rfid"
    GPS = "gps"
    LORA = "lora"       # LxveNode LoRa relay tier
    IR = "ir"
    RF24 = "rf24"       # nRF24 / mousejack
    SYSTEM = "system"   # the board itself — identity / battery / heap
    UNKNOWN = "unknown"


@dataclass
class Reading:
    """One normalized reading. ``value`` stays loosely typed (str / int / float) because kinds
    differ; ``unit`` and ``label`` make it renderable without the consumer knowing the source
    firmware. ``extra`` carries the source-specific fields a tile may want (e.g. the full snapshot
    dict) without bloating the core shape. ``seq`` is stamped by the model on insert for
    deterministic ordering without a wall clock (so history / tests don't depend on time)."""

    kind: ReadingKind
    medium: Medium
    value: Any
    unit: str = ""
    label: str = ""
    device_source: str = ""
    extra: dict = field(default_factory=dict)
    seq: int = 0

    @property
    def key(self) -> tuple:
        """Identity for latest-wins storage: one slot per (device, kind)."""
        return (self.device_source, self.kind)


# Enum declaration order → a stable display order for a device's readings (Dashboard tile order).
_KIND_ORDER = {k: i for i, k in enumerate(ReadingKind)}


class MetricsModel:
    """Latest-reading store keyed by (device_source, kind), with change observers.

    A Dashboard reads :meth:`readings_for` (or :meth:`all_latest`) to paint tiles and subscribes to
    be told when a reading changes. Qt-free by design: :meth:`update` may be called on the
    serial-reader thread (that's where the ingestor observer fires), so a Qt consumer wraps this and
    marshals to the GUI thread with a ``pyqtSignal`` — the model itself takes no Qt dependency and
    does no locking beyond a plain dict write (single-writer: the reader thread)."""

    def __init__(self) -> None:
        self._latest: dict[tuple, Reading] = {}
        self._observers: list[Callable[[Reading], None]] = []
        self._seq = 0

    def update(self, reading: Reading) -> Reading:
        """Store *reading* as the latest for its (device, kind), stamp a monotonic seq, and notify
        observers. Returns the stored reading (seq set). Each observer is isolated so one failing
        consumer can't stop the others or the store."""
        self._seq += 1
        reading.seq = self._seq
        self._latest[reading.key] = reading
        for cb in list(self._observers):
            try:
                cb(reading)
            except Exception:
                log.exception("MetricsModel: observer error")
        return reading

    def ingest_event(self, ev: Any, port: str) -> list[Reading]:
        """Map a ParsedEvent to readings and store each — the one call an ingestor observer makes.
        A mapping error on one event is logged and swallowed (returns what mapped cleanly), keeping
        the ingestor's "a bad line never breaks ingestion" invariant even though the ingestor wraps
        this too."""
        try:
            readings = event_to_readings(ev, port)
        except Exception:
            log.exception("MetricsModel: event_to_readings failed on %s", port)
            return []
        return [self.update(r) for r in readings]

    def latest(self, port: str, kind: ReadingKind) -> Reading | None:
        """The most recent reading of *kind* from *port* (or None)."""
        return self._latest.get((port, kind))

    def readings_for(self, port: str) -> list[Reading]:
        """Every latest reading from *port*, in a stable kind order (Dashboard tile order)."""
        rs = [r for (p, _kind), r in self._latest.items() if p == port]
        return sorted(rs, key=lambda r: _KIND_ORDER[r.kind])

    def all_latest(self) -> list[Reading]:
        """Every latest reading across all devices (source, then kind order)."""
        return sorted(self._latest.values(),
                      key=lambda r: (r.device_source, _KIND_ORDER[r.kind]))

    def devices(self) -> list[str]:
        """The device sources that have reported at least one reading."""
        return sorted({p for (p, _kind) in self._latest})

    def clear(self, port: str | None = None) -> None:
        """Drop readings for *port* (e.g. on disconnect), or all readings when *port* is None."""
        if port is None:
            self._latest.clear()
            return
        for key in [k for k in self._latest if k[0] == port]:
            del self._latest[key]

    def subscribe(self, cb: Callable[[Reading], None]) -> None:
        """Register *cb*, called ``cb(reading)`` after each stored update. Fires on the reader
        thread — a Qt consumer MUST marshal to the GUI thread. Duplicate registrations are the
        caller's concern."""
        self._observers.append(cb)

    def unsubscribe(self, cb: Callable[[Reading], None]) -> None:
        """Best-effort removal of a previously-registered observer (no-op if absent)."""
        try:
            self._observers.remove(cb)
        except ValueError:
            pass


# ── event → reading mapping ─────────────────────────────────────────────────────────────────────

# Detection events → the medium they belong to. One DETECTION reading each, plus any rssi/channel
# surfaced as their own kinds so a "latest RSSI"/"channel" tile updates from ANY firmware, not just
# ones with a dedicated signal event.
_DETECTION_MEDIUM: dict[str, Medium] = {
    "ap_found": Medium.WIFI,
    "rogue_ap": Medium.WIFI,
    "client_found": Medium.WIFI,
    "alpr_found": Medium.WIFI,
    "probe_request": Medium.WIFI,
    "ble_found": Medium.BLE,
    "subghz_found": Medium.SUBGHZ,
    "nfc_found": Medium.NFC,
    "rfid_found": Medium.RFID,
    "ir_found": Medium.IR,
    "iot_found": Medium.RF24,
    "mousejack": Medium.RF24,
    "nrf_data": Medium.RF24,
}

_DETECTION_NOUN: dict[str, str] = {
    "ap_found": "AP", "rogue_ap": "rogue AP", "client_found": "client", "alpr_found": "camera",
    "probe_request": "probe", "ble_found": "BLE", "subghz_found": "SubGHz", "nfc_found": "NFC",
    "rfid_found": "RFID", "ir_found": "IR", "iot_found": "IoT", "mousejack": "mousejack",
    "nrf_data": "nRF",
}

# Crackable-material captures → a CAPTURE reading labelled by what landed.
_CAPTURE_LABEL: dict[str, str] = {
    "handshake_captured": "handshake", "pmkid_captured": "pmkid", "pcap_saved": "pcap",
}

_INT_RE = re.compile(r"-?\d+")


def _num(v: Any):
    """Coerce *v* to int/float, or None when it isn't numeric. Bools are non-numeric so a
    ``tracker=1`` flag never masquerades as a signal value."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        return int(s) if _INT_RE.fullmatch(s) else float(s)
    except (ValueError, TypeError):
        return None


def _detection_label(et: str, d: dict) -> str:
    """A short human label for a detection: the noun + the best available identifier."""
    noun = _DETECTION_NOUN.get(et, et)
    for k in ("ssid", "name", "essid", "bssid", "mac", "client_mac", "addr", "uid",
              "frequency", "freq"):
        v = d.get(k)
        if v:
            return f"{noun} {v}"
    return noun


def _device_summary(d: dict) -> str:
    """A one-line board/firmware summary from a device_info event's present fields."""
    parts: list[str] = []
    board = d.get("board") or d.get("chip")
    if board:
        parts.append(str(board))
    fw = d.get("fw") or d.get("fw_version") or d.get("version")
    if fw:
        parts.append(f"fw {fw}")
    heap = _num(d.get("heap"))
    if heap is not None:
        parts.append(f"heap {heap} KB")
    return " · ".join(parts) or "device"


def _airspace_label(d: dict) -> str:
    """The airspace snapshot tile text — only the keys the snapshot actually carried."""
    order = [("aps", "APs"), ("open", "open"), ("wps", "WPS"), ("bles", "BLE"),
             ("trackers", "trackers"), ("stas", "clients"), ("alerts", "alerts")]
    bits = [f"{lbl} {d[k]}" for k, lbl in order if d.get(k) is not None]
    return "airspace: " + " · ".join(bits) if bits else "airspace"


def event_to_readings(ev: Any, port: str) -> list[Reading]:
    """Map one ParsedEvent to zero or more canonical :class:`Reading`s (the CANONICAL_EVENTS layer).

    Reads the parser's existing ``event_type`` / ``data``; rewrites no parser. Events that carry no
    metric (``info`` / ``status`` / ``error`` / ``version`` / …) map to nothing."""
    et = getattr(ev, "event_type", "") or ""
    d = getattr(ev, "data", {}) or {}
    out: list[Reading] = []

    # Detections: a DETECTION reading, plus rssi/channel lifted to their own kinds (cross-firmware).
    if et in _DETECTION_MEDIUM:
        med = _DETECTION_MEDIUM[et]
        label = _detection_label(et, d)
        out.append(Reading(ReadingKind.DETECTION, med, label, "", label, port, extra={"event": et}))
        rssi = _num(d.get("rssi"))
        if rssi is not None:
            out.append(Reading(ReadingKind.RSSI, med, rssi, "dBm", f"{rssi} dBm", port))
        ch = _num(d.get("channel"))
        if ch is not None:
            out.append(Reading(ReadingKind.CHANNEL, med, ch, "", f"ch {ch}", port))
        return out

    if et == "gps_fix":
        lat, lon = _num(d.get("lat")), _num(d.get("lon"))
        if lat is not None and lon is not None:
            label = f"{lat:.5f}, {lon:.5f}"
            value: Any = f"{lat},{lon}"
        else:
            value = d.get("message") or "fix"
            label = str(value)
        extra = {k: d[k] for k in ("lat", "lon", "alt", "sats", "fix") if k in d}
        out.append(Reading(ReadingKind.GPS_FIX, Medium.GPS, value, "", label, port, extra=extra))
        return out

    if et == "spectrum":
        ch, rssi = _num(d.get("channel")), _num(d.get("rssi"))
        out.append(Reading(ReadingKind.SPECTRUM, Medium.WIFI, rssi, "dBm",
                           f"ch {ch}: {rssi} dBm", port, extra={"channel": ch, "rssi": rssi}))
        if ch is not None:
            out.append(Reading(ReadingKind.CHANNEL, Medium.WIFI, ch, "", f"ch {ch}", port))
        if rssi is not None:
            out.append(Reading(ReadingKind.RSSI, Medium.WIFI, rssi, "dBm", f"{rssi} dBm", port))
        return out

    if et == "channel_changed":
        ch = _num(d.get("channel"))
        out.append(Reading(ReadingKind.CHANNEL, Medium.WIFI, ch, "", f"ch {ch}", port))
        return out

    if et == "packet":
        info = d.get("info") or d.get("message") or d.get("rate")
        if info is not None:
            out.append(Reading(ReadingKind.PACKET_RATE, Medium.WIFI, info, "",
                               f"packets: {info}", port))
        return out

    if et == "device_info":
        summary = _device_summary(d)
        out.append(Reading(ReadingKind.DEVICE_INFO, Medium.SYSTEM, summary, "", summary, port,
                           extra=dict(d)))
        caps = d.get("caps_tokens")
        if caps:
            caps_label = " · ".join(str(c) for c in caps)
            out.append(Reading(ReadingKind.CAPABILITIES, Medium.SYSTEM, list(caps), "",
                               caps_label, port, extra={"caps": d.get("caps")}))
        batt = _num(d.get("batt") if d.get("batt") is not None else d.get("battery"))
        if batt is not None:
            out.append(Reading(ReadingKind.BATTERY, Medium.SYSTEM, batt, "%", f"{batt:.0f}%", port))
        return out

    if et == "link_state":
        tier = d.get("tier") or d.get("link_event") or ""
        rssi = _num(d.get("rssi"))
        bits = [str(tier)] if tier else []
        if rssi is not None:
            bits.append(f"{rssi} dBm")
        snr = _num(d.get("snr"))
        if snr is not None:
            bits.append(f"snr {snr}")
        lat_ms = _num(d.get("latency_ms"))
        if lat_ms is not None:
            bits.append(f"{lat_ms} ms")
        out.append(Reading(ReadingKind.LINK, Medium.LORA, tier, "",
                           "link: " + " · ".join(bits) if bits else "link", port, extra=dict(d)))
        if rssi is not None:
            out.append(Reading(ReadingKind.RSSI, Medium.LORA, rssi, "dBm", f"{rssi} dBm", port))
        return out

    if et == "arm_state":
        st = str(d.get("state", ""))
        out.append(Reading(ReadingKind.ARM_STATE, Medium.SYSTEM, st, "",
                           f"arm: {st}" if st else "arm", port, extra=dict(d)))
        return out

    if et == "alert":
        grade, count = d.get("grade"), _num(d.get("count"))
        if grade:
            value, label = grade, f"alert: {grade}"
        elif count is not None:
            value, label = count, f"alert x{count}"
        else:
            value, label = "alert", "alert"
        out.append(Reading(ReadingKind.ALERT, Medium.WIFI, value, "", label, port, extra=dict(d)))
        return out

    if et == "snapshot":
        out.append(Reading(ReadingKind.AIRSPACE, Medium.WIFI, d.get("aps"), "",
                           _airspace_label(d), port, extra=dict(d)))
        return out

    if et in _CAPTURE_LABEL:
        kind_label = _CAPTURE_LABEL[et]
        out.append(Reading(ReadingKind.CAPTURE, Medium.WIFI, kind_label, "",
                           f"capture: {kind_label}", port, extra=dict(d)))
        return out

    return out  # info / status / error / version / … carry no metric


def attach_metrics(ingestor: Any, model: MetricsModel) -> Callable[[Any, str], None]:
    """Feed *model* from *ingestor*'s parsed-event stream via the existing ``add_event_observer``
    hook.

    Read-only: adds NO branch to ``_route`` and sends nothing. The ingestor already isolates an
    observer's failure, so a bad line can't break ingestion. Returns the observer callback — pass it
    to ``ingestor.remove_event_observer`` to detach."""

    def _feed(ev: Any, port: str) -> None:
        model.ingest_event(ev, port)

    ingestor.add_event_observer(_feed)
    return _feed
