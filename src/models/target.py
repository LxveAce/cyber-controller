"""Target model — represents a discovered wireless target."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TargetType(Enum):
    """Types of wireless targets."""

    AP = "ap"
    CLIENT = "client"
    BLE = "ble"
    SUBGHZ = "subghz"
    NFC = "nfc"


@dataclass
class Target:
    """A wireless target discovered during scanning.

    Attributes:
        mac: MAC address (or UID for NFC/SubGHz).
        target_type: Category of target.
        ssid: SSID for AP/client targets, name for BLE.
        rssi: Signal strength in dBm.
        channel: Wi-Fi/BLE channel.
        device_source: Port of the device that discovered this target.
        timestamp: When the target was first seen (UTC).
        last_seen: When the target was last observed (UTC).
        encryption: Encryption type string (e.g. WPA2, OPEN).
        vendor: OUI vendor lookup result.
        extra: Arbitrary metadata dict.
    """

    mac: str
    target_type: TargetType
    ssid: str = ""
    rssi: int = 0
    channel: int = 0
    device_source: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    encryption: str = ""
    vendor: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Unique identity key for deduplication."""
        return f"{self.target_type.value}:{self.mac}"

    @property
    def age_seconds(self) -> float:
        """Seconds since first seen."""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    def update_seen(self, rssi: int | None = None, channel: int | None = None) -> None:
        """Update last-seen time and optional fields."""
        self.last_seen = datetime.now(timezone.utc)
        if rssi is not None:
            self.rssi = rssi
        if channel is not None:
            self.channel = channel

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {
            "mac": self.mac,
            "target_type": self.target_type.value,
            "ssid": self.ssid,
            "rssi": self.rssi,
            "channel": self.channel,
            "device_source": self.device_source,
            "timestamp": self.timestamp.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "encryption": self.encryption,
            "vendor": self.vendor,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Target:
        """Deserialize from a plain dict."""
        data = dict(data)
        data["target_type"] = TargetType(data.get("target_type", "ap"))
        for key in ("timestamp", "last_seen"):
            if isinstance(data.get(key), str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)
