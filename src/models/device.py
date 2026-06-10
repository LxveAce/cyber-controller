"""Device model — represents a connected hardware device."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BoardType(Enum):
    """Known board types."""

    ESP32 = "esp32"
    ESP32_S2 = "esp32-s2"
    ESP32_S3 = "esp32-s3"
    ESP32_C3 = "esp32-c3"
    ESP8266 = "esp8266"
    FLIPPER_ZERO = "flipper-zero"
    RASPBERRY_PI = "raspberry-pi"
    ANDROID_ADB = "android-adb"
    UNKNOWN = "unknown"


class Protocol(Enum):
    """Supported firmware protocols."""

    MARAUDER = "marauder"
    GHOST_ESP = "ghost-esp"
    BRUCE = "bruce"
    HALEHOUND = "halehound"
    MESHTASTIC = "meshtastic"
    FLIPPER = "flipper"
    GENERIC = "generic"
    UNKNOWN = "unknown"


@dataclass
class Device:
    """A connected hardware device.

    Attributes:
        port: Serial port path (e.g. COM3, /dev/ttyUSB0).
        name: Human-readable device name.
        firmware: Detected firmware identifier string.
        protocol: Communication protocol enum.
        connected: Whether the device is currently connected.
        serial_number: USB serial number if available.
        board_type: Hardware board type enum.
        baud_rate: Serial baud rate for this device.
        vid: USB vendor ID (hex string).
        pid: USB product ID (hex string).
        description: USB device description string.
    """

    port: str
    name: str = ""
    firmware: str = ""
    protocol: Protocol = Protocol.UNKNOWN
    connected: bool = False
    serial_number: str = ""
    board_type: BoardType = BoardType.UNKNOWN
    baud_rate: int = 115200
    vid: str = ""
    pid: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"Device@{self.port}"

    @property
    def display_name(self) -> str:
        """Formatted display string."""
        status = "connected" if self.connected else "disconnected"
        fw = f" [{self.firmware}]" if self.firmware else ""
        return f"{self.name} ({self.port}){fw} — {status}"

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {
            "port": self.port,
            "name": self.name,
            "firmware": self.firmware,
            "protocol": self.protocol.value,
            "connected": self.connected,
            "serial_number": self.serial_number,
            "board_type": self.board_type.value,
            "baud_rate": self.baud_rate,
            "vid": self.vid,
            "pid": self.pid,
            "description": self.description,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Device:
        """Deserialize from a plain dict."""
        data = dict(data)
        data["protocol"] = Protocol(data.get("protocol", "unknown"))
        data["board_type"] = BoardType(data.get("board_type", "unknown"))
        return cls(**data)
