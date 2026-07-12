"""Capture model — a captured WPA/WPA2 handshake or PMKID with all its associated metadata.

A :class:`CaptureRecord` is what the Crack Lab's Captures list holds and exports. It joins what the
firmware reports on capture (BSSID, the on-SD pcap path, a PMKID) with the pool ``Target`` it
correlates to (SSID / channel / RSSI), the offline extractor's enrichment (capture kind, client MAC,
key version), and the eventual crack outcome. It deliberately mirrors the shape of
:mod:`src.models.target` (dataclass + ``key`` + ``to_dict`` / ``from_dict``) so the serialization
and dedup conventions apply.

Part of punch-list #2 (smarter deauth + exportable handshake capture log), slice 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CaptureRecord:
    """A captured handshake / PMKID and everything associated with it.

    Keyed by :attr:`key` (``capture_type:bssid``) so the same AP re-handshaking — or the same deauth
    fired twice — upserts one row (bumping :attr:`times_seen`) instead of spawning duplicate rows.

    Attributes:
        bssid: AP MAC address (``aa:bb:cc:dd:ee:ff``).
        capture_type: ``"pmkid"`` or ``"eapol"`` (the 4-way handshake).
        ssid: ESSID / the PBKDF2 salt (may be empty until a beacon resolves it).
        channel: Wi-Fi channel.
        sta_mac: Client/station MAC (4-way handshakes only).
        key_version: 1 = WPA/HMAC-MD5, 2 = WPA2/HMAC-SHA1, 0/3 = AES-CMAC (declined natively).
        rssi: Signal strength in dBm.
        gps_lat, gps_lon: Best-effort location; ``None`` when no fix is active (an honest null).
        device_source: Serial port of the device that produced the capture.
        firmware: Firmware that produced it (``"marauder"`` / ``"esp32_div"`` / ...).
        captured_at: When first logged (UTC ingest time; pcap timestamps are discarded upstream).
        last_seen: When last re-observed (UTC).
        times_seen: How many times this capture key has been observed.
        pmkid: Inline PMKID hex (the ESP32-DIV path — directly crackable, no file).
        pcap_path: On-device SD path to the ``.pcap``/``.pcapng`` (or local once retrieved).
        hc22000_path: hashcat-22000 file path (only after a convert).
        hashes_extracted: Extractable hash count from the capture.
        crack_status: ``uncracked`` | ``running`` | ``cracked`` | ``no-key`` | ``unsupported``.
        password: Recovered PSK (when cracked).
        crack_detail: Human-readable crack outcome detail.
        wordlist: Wordlist used for the crack attempt.
        raw: The original firmware serial line (audit).
    """

    # ── identity / dedup ─────────────────────────────────────────────
    bssid: str
    capture_type: str = "eapol"          # "pmkid" | "eapol"

    # ── network identity ─────────────────────────────────────────────
    ssid: str = ""
    channel: int = 0
    sta_mac: str = ""
    key_version: int = 0                 # 1=WPA/MD5, 2=WPA2/SHA1, 0/3=AES-CMAC (declined natively)

    # ── signal / geo (best-effort) ───────────────────────────────────
    rssi: int = 0
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None

    # ── provenance ───────────────────────────────────────────────────
    device_source: str = ""
    firmware: str = ""
    captured_at: datetime = field(default_factory=_utcnow)
    last_seen: datetime = field(default_factory=_utcnow)
    times_seen: int = 1

    # ── crackable material / files ───────────────────────────────────
    pmkid: str = ""
    pcap_path: str = ""                  # on-device SD path (or local once retrieved)
    hc22000_path: str = ""               # only after a convert
    hashes_extracted: int = 0

    # ── crack outcome ────────────────────────────────────────────────
    crack_status: str = "uncracked"      # uncracked | running | cracked | no-key | unsupported
    password: str = ""
    crack_detail: str = ""
    wordlist: str = ""

    raw: str = ""                        # original firmware serial line (audit)

    @property
    def key(self) -> str:
        """Dedup identity — mirrors ``Target.key`` (``type:mac``), with a lowercased BSSID so an
        AP reported in mixed case collapses to one row."""
        return f"{self.capture_type}:{self.bssid.lower()}"

    def update_from(self, other: "CaptureRecord") -> None:
        """Merge a re-observation of the same capture into this record: bump ``times_seen``, refresh
        ``last_seen``, and take the newer value for any field the re-observation actually carries —
        never clobbering a known value with an empty/zero/``None`` one (``0`` / ``""`` are the
        unknown-sentinels here, mirroring ``TargetPool.add``'s latest-wins-on-non-empty semantics).
        ``captured_at`` (first-seen) is intentionally left untouched.
        """
        self.last_seen = _utcnow()
        self.times_seen += 1
        if other.ssid:
            self.ssid = other.ssid
        if other.channel:
            self.channel = other.channel
        if other.sta_mac:
            self.sta_mac = other.sta_mac
        if other.key_version:
            self.key_version = other.key_version
        if other.rssi:
            self.rssi = other.rssi
        if other.gps_lat is not None:
            self.gps_lat = other.gps_lat
        if other.gps_lon is not None:
            self.gps_lon = other.gps_lon
        if other.pmkid:
            self.pmkid = other.pmkid
        if other.pcap_path:
            self.pcap_path = other.pcap_path
        if other.hc22000_path:
            self.hc22000_path = other.hc22000_path
        if other.hashes_extracted:
            self.hashes_extracted = other.hashes_extracted
        if other.firmware:
            self.firmware = other.firmware
        if other.device_source:
            self.device_source = other.device_source

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (datetimes as ISO strings), mirroring ``Target.to_dict``."""
        return {
            "bssid": self.bssid,
            "capture_type": self.capture_type,
            "ssid": self.ssid,
            "channel": self.channel,
            "sta_mac": self.sta_mac,
            "key_version": self.key_version,
            "rssi": self.rssi,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "device_source": self.device_source,
            "firmware": self.firmware,
            "captured_at": self.captured_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "times_seen": self.times_seen,
            "pmkid": self.pmkid,
            "pcap_path": self.pcap_path,
            "hc22000_path": self.hc22000_path,
            "hashes_extracted": self.hashes_extracted,
            "crack_status": self.crack_status,
            "password": self.password,
            "crack_detail": self.crack_detail,
            "wordlist": self.wordlist,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaptureRecord":
        """Deserialize from a dict (ISO strings → datetimes), mirroring ``Target.from_dict``."""
        data = dict(data)
        for k in ("captured_at", "last_seen"):
            if isinstance(data.get(k), str):
                data[k] = datetime.fromisoformat(data[k])
        return cls(**data)
