"""Wi-Fi analyzer model — the pure, firmware-agnostic core behind the Wi-Fi access-point view.

Stream-A's Wi-Fi counterpart to the BLE analyzer: an output view that reproduces the on-device
access-point list + channel view, not a text dump. This is the Qt-free foundation that view renders.
It ingests the Wi-Fi discovery events every scanning firmware already emits —
``ap_found`` (Marauder,
Ghost ESP, HaleHound, ESP32-DIV, BW16, LxveOS), the HaleHound Guardian ``rogue_ap`` twin,
``client_found`` station sightings, and the ``handshake_captured`` /
``pmkid_captured`` capture events
— into one access-point table keyed by BSSID,
and folds handshake/PMKID captures onto the matching AP.

Pure and unit-testable with no Qt or serial:
observe(event_type, data, now) takes a parsed event dict +
an injected timestamp (it never reads the clock, so tests are deterministic and the view owns it).
Awareness-only: it visualizes what's advertising nearby and drives no device — the view transmits
nothing.

Posture matches the codebase's parsers: never trust the input shape,
bound memory (a beacon-flood can't
grow it without limit), and keep a missing RSSI distinct from a real one — 0 dBm at the antenna is
absurd, so an absent/garbage rssi becomes None and is never counted as a real reading. Firmwares
disagree on field names, so the field extractors accept every spelling the real parsers emit
(channel/ch, encryption/enc/auth, mac/client_mac, bssid/ap_mac/ap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ── bounds (a beacon/station flood must not grow the model without limit) ──
_MAX_APS = 4096            # stalest AP is evicted when a new BSSID arrives at the cap
_MAX_CLIENTS_PER_AP = 512  # per-AP associated-station set cap (a spoofed station flood is bounded)
_MAX_CLIENTS = 8192        # global distinct-station counter cap (bounds the "Clients" rollup)
_DEFAULT_TTL = 30.0        # seconds since last_seen after which an AP is considered stale

# RSSI → signal-bar thresholds, matching SignalBarsDelegate so graph/table agree on "strong":
# > -50 = 4 bars, > -65 = 3, > -75 = 2, else 1. A missing reading is 0 bars.
_BAR_THRESHOLDS = ((-50, 4), (-65, 3), (-75, 2))

# Encryption tokens that mean an OPEN (unencrypted) network. An
# empty/unknown string is NOT counted as
# open — we only claim "open" when a firmware actually said so (no fabricated verdict).
_OPEN_TOKENS = frozenset({"open", "none", "opn", "-"})


def _as_int(value: object) -> Optional[int]:
    """Coerce an event field to int, or None if unusable. None (not 0) keeps a missing RSSI/channel
    distinct from a real one — the view counts real readings only, never a phantom value."""
    # bool subclasses int; a stray True isn't a reading
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError fires on int(inf)/int(nan)
        return None


def _norm_mac(raw: object) -> Optional[str]:
    """Normalize a MAC/BSSID to a lowercase key, or None when it isn't a usable string."""
    if not isinstance(raw, str):
        return None
    mac = raw.strip().lower()
    return mac or None


def normalize_bssid(data: object) -> Optional[str]:
    """Extract the AP's BSSID from an ap_found / capture event and normalize to a lowercase key.
    Returns None when there's no usable BSSID —
    such an event can't be an AP row and is dropped clean.
    (A BW16 Vampire scan prints index + SSID only,
    with no BSSID; those aren't rows in this view.)"""
    if not isinstance(data, dict):
        return None
    return _norm_mac(data.get("bssid"))


def client_assoc_bssid(data: object) -> Optional[str]:
    """The BSSID a station is associated with, across the field names the parsers use: ap_mac
    (Marauder), bssid (ESP32-DIV / its serial fork), ap (LxveOS). None when unattributed (HaleHound
    WIFI_STA reports only the station MAC + RSSI, with no AP)."""
    if not isinstance(data, dict):
        return None
    for key in ("ap_mac", "bssid", "ap"):
        mac = _norm_mac(data.get(key))
        if mac is not None:
            return mac
    return None


def client_mac(data: object) -> Optional[str]:
    """The station's own MAC: client_mac (Marauder) or mac (everyone else)."""
    if not isinstance(data, dict):
        return None
    return _norm_mac(data.get("client_mac")) or _norm_mac(data.get("mac"))


def channel_of(data: dict) -> Optional[int]:
    """Channel from an event: ``channel`` (Marauder/DIV/HaleHound/BW16) or ``ch`` (LxveOS)."""
    ch = _as_int(data.get("channel"))
    return ch if ch is not None else _as_int(data.get("ch"))


def encryption_of(data: dict) -> str:
    """Encryption/auth label from an event: ``encryption`` (ESP32-DIV / its serial fork) or ``auth``
    (LxveOS, e.g. "wpa2"/"open"). ``enc`` is accepted too as a tolerant fallback.
    "" when the firmware
    didn't report it (Marauder/HaleHound/BW16 don't) — an unknown, never a fabricated "open"."""
    for key in ("encryption", "enc", "auth"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def is_open(enc: str) -> bool:
    """True only when the encryption label explicitly says open. An empty/unknown label is NOT open
    (we don't know), so a scan that never reported encryption
    reports zero open networks, not all."""
    return isinstance(enc, str) and enc.strip().lower() in _OPEN_TOKENS


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
class AccessPoint:
    """One de-duplicated Wi-Fi access point, aggregated across every ap_found sighting of its BSSID
    (plus any station associations and handshake/PMKID captures matched to it).

    rssi is the latest reading; rssi_min/rssi_max bound the range. clients is the set of associated
    station MACs (its length is the live client count). handshake / pmkid are sticky-True: once a
    firmware captures crackable material for this AP it stays flagged even after a later
    plain beacon.
    rogue is sticky too (a HaleHound Guardian evil-twin verdict isn't cleared by a later plain hit).
    An AP first seen only via a station association or a handshake
    has an empty ssid / None rssi until
    a real ap_found arrives — it's a real BSSID, shown honestly rather than invented."""
    bssid: str
    ssid: str = ""
    channel: Optional[int] = None
    encryption: str = ""
    rssi: Optional[int] = None
    rssi_min: Optional[int] = None
    rssi_max: Optional[int] = None
    rogue: bool = False
    handshake: bool = False     # EAPOL 4-way handshake captured
    pmkid: bool = False         # PMKID captured (also directly crackable)
    seen_directly: bool = False  # a real ap_found arrived (vs inferred from a client/capture only)
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 0
    clients: Set[str] = field(default_factory=set)

    def display_ssid(self) -> str:
        """Name for the row, or a placeholder for a hidden/nameless network."""
        return self.ssid if self.ssid else "(hidden)"

    def enc_label(self) -> str:
        """Encryption for the row, or a placeholder when the firmware didn't report it."""
        return self.encryption if self.encryption else "?"

    def client_count(self) -> int:
        return len(self.clients)

    def has_capture(self) -> bool:
        """Whether any crackable material (EAPOL handshake or PMKID) was captured for this AP."""
        return self.handshake or self.pmkid

    def is_open(self) -> bool:
        return is_open(self.encryption)

    def age(self, now: float) -> float:
        """Seconds since this AP was last heard (>= 0)."""
        return max(0.0, now - self.last_seen)

    def is_fresh(self, now: float, ttl: float = _DEFAULT_TTL) -> bool:
        return self.age(now) <= ttl

    def freshness(self, now: float, ttl: float = _DEFAULT_TTL) -> float:
        """1.0 just-seen → 0.0 at/after ttl, linear. The view fades a stale row by this factor so an
        AP that has left range visibly decays instead of lingering as if present."""
        if ttl <= 0:
            return 1.0 if self.age(now) <= 0 else 0.0
        return max(0.0, min(1.0, 1.0 - self.age(now) / ttl))

    def bars(self) -> int:
        return rssi_bars(self.rssi)

    def to_dict(self) -> dict:
        return {
            "bssid": self.bssid,
            "ssid": self.ssid,
            "channel": self.channel,
            "encryption": self.encryption,
            "rssi": self.rssi,
            "rssi_min": self.rssi_min,
            "rssi_max": self.rssi_max,
            "rogue": self.rogue,
            "handshake": self.handshake,
            "pmkid": self.pmkid,
            "seen_directly": self.seen_directly,
            "clients": self.client_count(),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "hits": self.hits,
        }


class WifiAnalyzerModel:
    """Live, firmware-agnostic aggregation of Wi-Fi access points for the analyzer view.

    Feed parsed events via observe(event_type, data, now) with an injected timestamp;
    read a ranked AP
    list via access_points(), per-channel occupancy via channel_occupancy(), and a header rollup via
    summary(). Bounded: at max_aps a new BSSID evicts the stalest, each AP keeps at most
    max_clients_per_ap associated stations, and the global station counter is capped — so a
    beacon/station flood is capped, not unbounded growth.

    A handshake or PMKID event that carries only a BSSID folds onto (or creates) that AP. LxveOS
    reports a handshake by ESSID (network name) with no BSSID; those are matched to any AP with the
    same SSID, and remembered so a later-seen AP with that SSID is flagged too."""

    _AP_EVENTS = ("ap_found", "rogue_ap")

    def __init__(self, max_aps: int = _MAX_APS,
                 max_clients_per_ap: int = _MAX_CLIENTS_PER_AP) -> None:
        self._aps: "Dict[str, AccessPoint]" = {}
        self._max_aps = max(1, int(max_aps))
        self._max_clients_per_ap = max(1, int(max_clients_per_ap))
        self._client_macs: "Set[str]" = set()   # global distinct stations (incl. unattributed ones)
        # ESSID → set of capture kinds ({"eapol", "pmkid"}) for BSSID-less captures (LxveOS hs), so
        # an AP with that SSID seen later gets flagged for EACH kind. Bounded on insert (see
        # _remember_essid_capture) so a flood of distinct fake SSIDs can't grow it without limit.
        self._essid_captures: "Dict[str, Set[str]]" = {}

    # ── ingest ───────────────────────────────────────────────────────
    def observe(self, event_type: str, data: object, now: float) -> Optional[AccessPoint]:
        """Fold one parsed event into the table. event_type routes it: ap_found / rogue_ap → an AP
        sighting; client_found → a station association; handshake_captured / pmkid_captured → a
        capture flag. Returns the affected AccessPoint
        (or None when the event carries nothing usable
        — dropped clean, never raised). Any other event_type is ignored."""
        if not isinstance(data, dict):
            return None
        if event_type in self._AP_EVENTS:
            return self._observe_ap(data, now, rogue=event_type == "rogue_ap")
        if event_type == "client_found":
            return self._observe_client(data, now)
        if event_type == "handshake_captured":
            return self._observe_capture(data, now, "eapol")
        if event_type == "pmkid_captured":
            return self._observe_capture(data, now, "pmkid")
        return None

    def _get_or_create(self, bssid: str, now: float) -> AccessPoint:
        ap = self._aps.get(bssid)
        if ap is None:
            if len(self._aps) >= self._max_aps:
                self._evict_stalest()
            ap = AccessPoint(bssid=bssid, first_seen=now)
            self._aps[bssid] = ap
        return ap

    def _observe_ap(self, data: dict, now: float, rogue: bool) -> Optional[AccessPoint]:
        bssid = normalize_bssid(data)
        if bssid is None:
            return None
        ap = self._get_or_create(bssid, now)
        ap.seen_directly = True
        ap.last_seen = now
        ap.hits += 1

        # SSID: overwrite only with a NEW non-empty name;
        # a nameless/hidden re-beacon must not blank it.
        ssid = data.get("ssid")
        if isinstance(ssid, str) and ssid.strip():
            ap.ssid = ssid.strip()
        ch = channel_of(data)
        if ch is not None:
            ap.channel = ch
        enc = encryption_of(data)
        if enc:
            ap.encryption = enc
        if rogue:
            # sticky: a Guardian evil-twin verdict isn't cleared by a later plain hit
            ap.rogue = True

        rssi = _as_int(data.get("rssi"))
        # WiFi RSSI at the antenna is always negative dBm. A 0 or positive value is a firmware
        # "no reading" sentinel (Marauder/GhostESP/FlockYou emit rssi:0 for a missing reading), not
        # a real full-strength signal — drop it to None so it isn't painted as the strongest AP,
        # per this module's own contract (see the docstring).
        if rssi is not None and rssi < 0:
            ap.rssi = rssi
            ap.rssi_min = rssi if ap.rssi_min is None else min(ap.rssi_min, rssi)
            ap.rssi_max = rssi if ap.rssi_max is None else max(ap.rssi_max, rssi)

        # A handshake/PMKID seen earlier for this SSID (BSSID-less LxveOS capture)
        # flags this AP now.
        self._apply_essid_capture(ap)
        return ap

    def _observe_client(self, data: dict, now: float) -> Optional[AccessPoint]:
        mac = client_mac(data)
        if mac is not None and len(self._client_macs) < _MAX_CLIENTS:
            # global distinct-station tally (includes unattributed clients)
            self._client_macs.add(mac)
        bssid = client_assoc_bssid(data)
        if bssid is None:
            # station with no AP association (HaleHound) — counted globally, not to an AP
            return None
        ap = self._get_or_create(bssid, now)
        ap.last_seen = now
        if mac is not None and len(ap.clients) < self._max_clients_per_ap:
            ap.clients.add(mac)
        return ap

    def _observe_capture(self, data: dict, now: float, kind: str) -> Optional[AccessPoint]:
        """Fold a handshake/PMKID capture in. Keys on BSSID when present (Marauder/DIV); LxveOS
        reports only the ESSID, so match every AP with that SSID
        and remember it for later-seen APs."""
        bssid = normalize_bssid(data)
        # A handshake_captured event tagged kind=pmkid is a PMKID regardless of whether it also
        # carries a BSSID — honor the override on BOTH paths, not just the BSSID-less one.
        data_kind = data.get("kind")
        if isinstance(data_kind, str) and data_kind.strip().lower() == "pmkid":
            kind = "pmkid"
        if bssid is not None:
            ap = self._get_or_create(bssid, now)
            ap.last_seen = now
            self._flag_capture(ap, kind)
            ssid = data.get("ssid") or data.get("essid")
            if isinstance(ssid, str) and ssid.strip() and not ap.ssid:
                ap.ssid = ssid.strip()
            return ap
        # BSSID-less capture (LxveOS hs carries essid + kind).
        # Match by SSID; remember (accumulating kinds) for the future.
        essid = data.get("essid") or data.get("ssid")
        if not isinstance(essid, str) or not essid.strip():
            return None
        key = essid.strip().lower()
        self._remember_essid_capture(key, kind)
        matched: Optional[AccessPoint] = None
        for ap in self._aps.values():
            if ap.ssid.strip().lower() == key:
                self._flag_capture(ap, kind)
                ap.last_seen = now
                matched = ap
        return matched

    def _remember_essid_capture(self, key: str, kind: str) -> None:
        """Remember a BSSID-less capture kind for an ESSID so a later-seen AP with that SSID is
        flagged. Accumulates kinds (a network can have both an EAPOL handshake and a PMKID) and is
        bounded: at the AP cap a new ESSID evicts the oldest remembered one, so a flood of distinct
        fake SSIDs can't grow this map without limit."""
        kinds = self._essid_captures.get(key)
        if kinds is None:
            if len(self._essid_captures) >= self._max_aps:
                self._essid_captures.pop(next(iter(self._essid_captures)), None)
            kinds = set()
            self._essid_captures[key] = kinds
        kinds.add(kind)

    @staticmethod
    def _flag_capture(ap: AccessPoint, kind: str) -> None:
        if kind == "pmkid":
            ap.pmkid = True
        else:
            ap.handshake = True

    def _apply_essid_capture(self, ap: AccessPoint) -> None:
        if not ap.ssid:
            return
        for kind in self._essid_captures.get(ap.ssid.strip().lower(), ()):
            self._flag_capture(ap, kind)

    def _evict_stalest(self) -> None:
        """Make room by dropping the least-recently-heard BSSID (most likely to have left range).
        O(n) over the table — n is bounded by max_aps."""
        if not self._aps:
            return
        stalest = min(self._aps.values(), key=lambda a: a.last_seen)
        self._aps.pop(stalest.bssid, None)

    # ── read ─────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._aps)

    @property
    def ap_count(self) -> int:
        return len(self._aps)

    def get(self, bssid: str) -> Optional[AccessPoint]:
        return self._aps.get(bssid.strip().lower()) if isinstance(bssid, str) else None

    def access_points(self, sort: str = "rssi", now: Optional[float] = None,
                      ttl: float = _DEFAULT_TTL, fresh_only: bool = False) -> "List[AccessPoint]":
        """Ranked AP list. sort: rssi (strongest first), recent (most-recent first), channel (by
        channel then strength), clients (busiest first), or ssid (named before hidden, then BSSID).
        fresh_only drops APs older than ttl (needs now)."""
        items = list(self._aps.values())
        if fresh_only and now is not None:
            items = [a for a in items if a.is_fresh(now, ttl)]
        if sort == "recent":
            items.sort(key=lambda a: a.last_seen, reverse=True)
        elif sort == "channel":
            # Channel ascending (unknown channel last), then strongest first within a channel.
            items.sort(key=lambda a: self._rssi_key(a.rssi), reverse=True)
            items.sort(key=lambda a: (a.channel is None, a.channel or 0))
        elif sort == "clients":
            items.sort(key=lambda a: (a.client_count(), self._rssi_key(a.rssi)), reverse=True)
        elif sort == "ssid":
            items.sort(key=lambda a: (a.ssid == "", a.ssid.lower(), a.bssid))
        else:  # "rssi" (default): strongest first; a missing RSSI ranks below any real reading.
            items.sort(key=lambda a: self._rssi_key(a.rssi), reverse=True)
        return items

    @staticmethod
    def _rssi_key(rssi: Optional[int]) -> int:
        """Sort key: a missing RSSI ranks below any real reading."""
        return rssi if rssi is not None else -9999

    def channel_occupancy(self, now: Optional[float] = None, ttl: float = _DEFAULT_TTL,
                          fresh_only: bool = True) -> "Dict[int, int]":
        """AP count per channel (the channel-view data). Only APs with a known channel are counted;
        fresh_only (default) restricts to APs heard within ttl
        so a channel a network left goes quiet.
        Keyed by channel int → count."""
        out: "Dict[int, int]" = {}
        for ap in self._aps.values():
            if ap.channel is None:
                continue
            if fresh_only and now is not None and not ap.is_fresh(now, ttl):
                continue
            out[ap.channel] = out.get(ap.channel, 0) + 1
        return out

    def strongest_on_channel(self, channel: int, now: Optional[float] = None,
                             ttl: float = _DEFAULT_TTL, fresh_only: bool = True) -> Optional[int]:
        """Best (highest) RSSI among APs on a channel — colors the channel bar.
        None when none known."""
        best: Optional[int] = None
        for ap in self._aps.values():
            if ap.channel != channel or ap.rssi is None:
                continue
            if fresh_only and now is not None and not ap.is_fresh(now, ttl):
                continue
            best = ap.rssi if best is None else max(best, ap.rssi)
        return best

    def summary(self, now: Optional[float] = None, ttl: float = _DEFAULT_TTL) -> dict:
        """Header rollup: total APs, fresh (within ttl), open networks, APs with a capture, distinct
        stations seen, and the strongest current signal — the one-line airspace readout."""
        aps = list(self._aps.values())
        fresh = [a for a in aps if now is None or a.is_fresh(now, ttl)]
        strongest = max((a.rssi for a in fresh if a.rssi is not None), default=None)
        return {
            "total": len(aps),
            "fresh": len(fresh),
            "open": sum(1 for a in aps if a.is_open()),
            "handshakes": sum(1 for a in aps if a.has_capture()),
            "clients": len(self._client_macs),
            "strongest": strongest,
        }

    def prune(self, now: float, ttl: float = _DEFAULT_TTL) -> int:
        """Drop APs not heard within ttl. Returns the count removed.
        Housekeeping — the table can also
        just fade stale rows via AccessPoint.freshness and keep them for history."""
        stale = [b for b, a in self._aps.items() if not a.is_fresh(now, ttl)]
        for bssid in stale:
            del self._aps[bssid]
        return len(stale)

    def clear(self) -> None:
        self._aps.clear()
        self._client_macs.clear()
        self._essid_captures.clear()
