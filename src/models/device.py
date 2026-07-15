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
    #: True when the operator manually FORCED this firmware (Broadcast force-firmware / Devices combo).
    #: A forced choice must survive the post-probe re-autodetect (which otherwise overwrites it).
    firmware_forced: bool = False
    protocol: Protocol = Protocol.UNKNOWN
    connected: bool = False
    serial_number: str = ""
    board_type: BoardType = BoardType.UNKNOWN
    baud_rate: int = 115200
    vid: str = ""
    pid: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    # Handshake/liveness state, set by the connect-time probe (src/core/handshake.py). Distinct from
    # ``connected`` (is the serial link open) — this is "did the firmware actually answer over that link":
    #   "unknown"  -- not probed yet (the default)
    #   "alive"    -- the firmware replied to a probe command
    #   "no-reply" -- probed over an open text-CLI link but got silence (dead / wrong baud / not really a CLI)
    #   "no-cli"   -- driver_type is stream/controlmap, so there is no text CLI to probe (honest, not a failure)
    health: str = "unknown"
    fw_banner: str = ""  # an identifying line captured from the probe reply (best-effort)
    # Capabilities the firmware reports about ITSELF at runtime over serial (a `device_info` event —
    # e.g. LxveOS's `status` line `caps=` bitmask, decoded to tokens), as opposed to the static
    # per-firmware map below. LxveOS reports its real radios at runtime, so a headless build that
    # statically declares nothing still surfaces wifi/ble once spoken. Empty until one lands.
    runtime_capabilities: "frozenset[str]" = frozenset()
    #: Live device telemetry from the same device_info (fw version, board, chip, ui, panel, the
    #: ready/planned/unavailable ops tally, free heap). Display-only; refreshed on each device_info.
    telemetry: dict = field(default_factory=dict)
    #: Offensive-TX arm state a firmware reports over serial (an ``arm_state`` event from LxveOS
    #: ``arm``/``disarm``): "" (unknown / never reported), "safe", "pending", "armed", or
    #: "tx_disabled" (offensive TX compiled out). Drives the device tab's ARM/SAFE lamp. Display-only.
    arm_state: str = ""
    #: The most recent detector/watchlist alert this device reported (a parsed ``alert`` event's data:
    #: kind + its fields) and a session counter. A LxveOS detector firing (deauth attack, evil twin,
    #: tracker, watchlist hit, ...) would otherwise scroll past in the terminal; these let the device tab
    #: surface "a detector fired". Display-only; refreshed on each alert.
    last_alert: dict = field(default_factory=dict)
    alert_count: int = 0

    #: device_info keys kept as telemetry — identifying status-line fields EXCEPT the
    #: raw caps bitmask + its decoded tokens (those drive runtime_capabilities instead).
    _TELEMETRY_KEYS = ("fw", "board", "chip", "ui", "panel", "ops", "heap", "proto_version")

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"Device@{self.port}"

    def apply_device_info(self, data: dict) -> bool:
        """Absorb a parsed ``device_info`` event's data — from a firmware reporting its identity
        over serial (e.g. LxveOS ``status``/``info``). Sets :attr:`runtime_capabilities` from the
        decoded ``caps_tokens`` and refreshes :attr:`telemetry` (fw/board/chip/ui/panel/ops/heap).
        Returns True if anything changed. Tolerant: absent keys are skipped, so both the rich
        ``status`` line and the smaller ``info`` block are accepted, and a firmware that reports no
        capabilities leaves ``runtime_capabilities`` untouched rather than clearing it."""
        if not isinstance(data, dict):
            return False
        changed = False
        tokens = data.get("caps_tokens")
        if isinstance(tokens, (list, tuple, set, frozenset)):
            new_caps = frozenset(str(t) for t in tokens)
            if new_caps != self.runtime_capabilities:
                self.runtime_capabilities = new_caps
                changed = True
        for key in self._TELEMETRY_KEYS:
            if key in data and self.telemetry.get(key) != data[key]:
                self.telemetry[key] = data[key]
                changed = True
        return changed

    #: Arm-state tokens a firmware may report (LxveOS EVENT-PROTOCOL `arm` event `state=`). Any other
    #: string is still stored (forward-compat) — the UI just renders it verbatim rather than color-coding.
    _ARM_STATES = ("safe", "pending", "armed", "tx_disabled")

    def apply_arm_state(self, data: dict) -> bool:
        """Absorb a parsed ``arm_state`` event (LxveOS ``arm``/``disarm``): store the offensive-TX arm
        state string so the device tab can show a prominent ARM/SAFE lamp. Returns True if it changed.
        A missing/blank ``state`` is ignored (leaves the prior state intact), so a malformed line can
        never silently clear a live "armed" indicator."""
        if not isinstance(data, dict):
            return False
        state = data.get("state")
        if isinstance(state, str) and state and state != self.arm_state:
            self.arm_state = state
            return True
        return False

    def apply_alert(self, data: dict) -> bool:
        """Absorb a parsed ``alert`` event (a LxveOS detector firing: deauth / evil-twin / weak-AP /
        BLE tracker / rogue-HID / watchlist hit / ...). Stores it as the most-recent alert and bumps the
        session counter so the device tab can surface it rather than leaving it buried in the terminal
        scroll. Returns True (every alert is a distinct detection = news); a non-dict is ignored."""
        if not isinstance(data, dict):
            return False
        self.last_alert = dict(data)
        self.alert_count += 1
        return True

    @property
    def display_name(self) -> str:
        """Formatted display string."""
        status = "connected" if self.connected else "disconnected"
        fw = f" [{self.firmware}]" if self.firmware else ""
        return f"{self.name} ({self.port}){fw} — {status}"

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capability tokens this node's firmware supports (wifi / ble / subghz / nfc / ir / gps / lora / …).
        A read-only view over the protocol capability map, so a connected device can be treated as a node with
        known abilities (network view + Broadcast/AutoRouter applicability). Empty for an unknown firmware or
        one that declares none. The firmware identifier is the lookup key; the protocol enum is the fallback.

        Prefers the firmware's RUNTIME-reported capabilities when a device_info has landed
        (:attr:`runtime_capabilities`) — a board reporting its radios over serial (LxveOS) is
        described by what it actually said, not by a static map that may declare nothing for it."""
        if self.runtime_capabilities:
            return self.runtime_capabilities
        from src.protocols import (
            capabilities_for,  # lazy: keep models independent of the protocols package
        )
        return capabilities_for(self.firmware or self.protocol.value)

    @property
    def driver_type(self) -> str:
        """How CC talks to this node: "text-cli" (a line-based command shell — the default), "stream" (a
        binary/framed link with no text command channel, e.g. Meshtastic protobuf), or "controlmap" (no serial
        command channel at all, e.g. BlueJammer's web UI). Read-only view over the protocol map — lets a node
        say honestly whether it even has a sendable command channel. The firmware identifier is the lookup key;
        the protocol enum is the fallback."""
        from src.protocols import (
            driver_type_for,  # lazy: keep models independent of the protocols package
        )
        return driver_type_for(self.firmware or self.protocol.value)

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
            "health": self.health,
            "fw_banner": self.fw_banner,
            # frozenset isn't JSON-serializable; persist as a sorted list, restored in from_dict.
            "runtime_capabilities": sorted(self.runtime_capabilities),
            "telemetry": dict(self.telemetry),
            "arm_state": self.arm_state,
            "last_alert": dict(self.last_alert),
            "alert_count": self.alert_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Device:
        """Deserialize from a plain dict."""
        data = dict(data)
        data["protocol"] = Protocol(data.get("protocol", "unknown"))
        data["board_type"] = BoardType(data.get("board_type", "unknown"))
        # runtime_capabilities round-trips through a list (see to_dict) -> back to a frozenset.
        if "runtime_capabilities" in data:
            data["runtime_capabilities"] = frozenset(data["runtime_capabilities"])
        return cls(**data)
