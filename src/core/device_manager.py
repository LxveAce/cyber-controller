"""Device manager — auto-detect, track, and manage serial hardware devices."""

from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Callable

import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

from src.core.serial_handler import SerialConnection
from src.models.device import BoardType, Device, Protocol

log = logging.getLogger(__name__)

# Callback type aliases
DeviceCallback = Callable[[Device], None]


def _guess_board_type(info: ListPortInfo) -> BoardType:
    """Heuristic board-type guess from USB VID/PID and description."""
    vid = info.vid or 0
    desc = (info.description or "").lower()
    # Espressif VID
    if vid == 0x303A:
        if "s3" in desc:
            return BoardType.ESP32_S3
        if "s2" in desc:
            return BoardType.ESP32_S2
        if "c3" in desc:
            return BoardType.ESP32_C3
        return BoardType.ESP32
    # Silicon Labs CP210x (classic ESP32 / ESP8266 devkits)
    if vid == 0x10C4:
        return BoardType.ESP32
    # FTDI / CH340 — common on ESP32 boards
    if vid in (0x0403, 0x1A86):
        return BoardType.ESP32
    # Flipper Zero
    if vid == 0x0483 and "flipper" in desc:
        return BoardType.FLIPPER_ZERO
    return BoardType.UNKNOWN


class DeviceManager:
    """Central registry for connected serial devices.

    Provides:
    - Manual add/remove/list of devices.
    - :class:`HotPlugMonitor` background thread that polls for USB
      serial ports every *poll_interval* seconds and fires callbacks
      on connect/disconnect.
    - Managed :class:`SerialConnection` instances per device.
    """

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}  # keyed by port
        self._connections: dict[str, SerialConnection] = {}
        self._lock = threading.Lock()

        # Callbacks
        self._on_connected: list[DeviceCallback] = []
        self._on_disconnected: list[DeviceCallback] = []

        self._hotplug: HotPlugMonitor | None = None

    # ── Callback registration ────────────────────────────────────────

    def on_device_connected(self, cb: DeviceCallback) -> None:
        """Register a callback fired when a new device is detected."""
        self._on_connected.append(cb)

    def on_device_disconnected(self, cb: DeviceCallback) -> None:
        """Register a callback fired when a device is removed."""
        self._on_disconnected.append(cb)

    # ── Device registry ──────────────────────────────────────────────

    def add_device(self, device: Device) -> None:
        """Add or update a device in the registry."""
        with self._lock:
            self._devices[device.port] = device
        log.info("Device added: %s", device.display_name)

    def remove_device(self, port: str) -> Device | None:
        """Remove a device by port, closing its connection if open."""
        with self._lock:
            device = self._devices.pop(port, None)
            conn = self._connections.pop(port, None)
        if conn:
            conn.disconnect()
        if device:
            log.info("Device removed: %s", device.display_name)
        return device

    def get_device(self, port: str) -> Device | None:
        """Look up a device by port."""
        with self._lock:
            return self._devices.get(port)

    def list_devices(self) -> list[Device]:
        """Return a snapshot of all registered devices."""
        with self._lock:
            return list(self._devices.values())

    def list_connected(self) -> list[Device]:
        """Return only devices that are currently connected."""
        with self._lock:
            return [d for d in self._devices.values() if d.connected]

    # ── Serial connections ───────────────────────────────────────────

    def open_connection(self, port: str, baud: int = 115200) -> SerialConnection:
        """Open (or return existing) SerialConnection for *port*.

        Raises:
            KeyError: If port is not in the device registry.
        """
        with self._lock:
            if port not in self._devices:
                raise KeyError(f"No registered device on port {port}")
            if port in self._connections and self._connections[port].is_connected:
                return self._connections[port]

        conn = SerialConnection(port, baud=baud)
        conn.connect()
        with self._lock:
            self._connections[port] = conn
            self._devices[port].connected = True
        return conn

    def close_connection(self, port: str) -> None:
        """Close the serial connection for *port*."""
        with self._lock:
            conn = self._connections.pop(port, None)
            dev = self._devices.get(port)
        if conn:
            conn.disconnect()
        if dev:
            dev.connected = False

    def get_connection(self, port: str) -> SerialConnection | None:
        """Return the active SerialConnection for *port*, if any."""
        with self._lock:
            return self._connections.get(port)

    # ── Hot-plug monitor ─────────────────────────────────────────────

    def start_hotplug(self, poll_interval: float = 2.0) -> None:
        """Start the background USB hot-plug monitor."""
        if self._hotplug and self._hotplug.is_alive():
            return
        self._hotplug = HotPlugMonitor(self, poll_interval)
        self._hotplug.start()
        log.info("HotPlug monitor started (%.1fs interval)", poll_interval)

    def stop_hotplug(self) -> None:
        """Stop the background monitor."""
        if self._hotplug:
            self._hotplug.stop()
            self._hotplug = None
            log.info("HotPlug monitor stopped")

    # ── Scanning ─────────────────────────────────────────────────────

    @staticmethod
    def scan_ports() -> list[Device]:
        """Enumerate currently visible USB serial ports.

        Returns:
            A list of :class:`Device` objects (not yet registered).
        """
        devices: list[Device] = []
        for info in serial.tools.list_ports.comports():
            dev = Device(
                port=info.device,
                name=info.description or info.name or info.device,
                serial_number=info.serial_number or "",
                board_type=_guess_board_type(info),
                vid=f"{info.vid:04X}" if info.vid else "",
                pid=f"{info.pid:04X}" if info.pid else "",
                description=info.description or "",
            )
            devices.append(dev)
        return devices

    # USB VID -> serial-bridge family; presence strongly implies a flashable board.
    _ESP_BRIDGE_VIDS = {
        0x10C4: "CP210x",
        0x1A86: "CH340/CH9102",
        0x0403: "FTDI",
        0x303A: "Espressif USB-JTAG",
    }

    @classmethod
    def autodetect_esp_port(cls) -> str | None:
        """Return the most-likely ESP / security-board serial port, or None.

        Scores ports by USB VID (known ESP/serial-bridge chips win) and refuses to
        guess a bare port (e.g. a Bluetooth COM or ``/dev/ttyS0``) — mirroring the
        proven "just plug it in" autodetect from the headless-marauder lineage.
        """
        best: str | None = None
        best_score = 0
        for info in serial.tools.list_ports.comports():
            vid = info.vid or 0
            desc = (info.description or "").lower()
            if vid in cls._ESP_BRIDGE_VIDS:
                score = 3
            elif "usb" in desc and ("serial" in desc or "uart" in desc):
                score = 1
            else:
                score = 0
            if score > best_score:
                best_score, best = score, info.device
        return best

    # ── Internal callbacks ───────────────────────────────────────────

    def _fire_connected(self, device: Device) -> None:
        for cb in self._on_connected:
            try:
                cb(device)
            except Exception:
                log.exception("on_connected callback error")

    def _fire_disconnected(self, device: Device) -> None:
        for cb in self._on_disconnected:
            try:
                cb(device)
            except Exception:
                log.exception("on_disconnected callback error")

    # ── Cleanup ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop hotplug monitor and close all connections."""
        self.stop_hotplug()
        with self._lock:
            ports = list(self._connections.keys())
        for port in ports:
            self.close_connection(port)
        log.info("DeviceManager shut down")


class HotPlugMonitor(threading.Thread):
    """Background thread that polls for USB serial device changes.

    Fires :meth:`DeviceManager.on_device_connected` and
    :meth:`DeviceManager.on_device_disconnected` callbacks.
    """

    def __init__(self, manager: DeviceManager, interval: float = 2.0) -> None:
        super().__init__(name="hotplug-monitor", daemon=True)
        self._manager = manager
        self._interval = interval
        self._stop_event = threading.Event()
        self._known_ports: set[str] = set()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=self._interval + 1)

    def run(self) -> None:
        # Seed with currently visible ports
        self._known_ports = {d.port for d in self._manager.scan_ports()}
        while not self._stop_event.is_set():
            try:
                current = self._manager.scan_ports()
                current_ports = {d.port for d in current}
                current_map = {d.port: d for d in current}

                # New devices
                for port in current_ports - self._known_ports:
                    dev = current_map[port]
                    self._manager.add_device(dev)
                    self._manager._fire_connected(dev)
                    log.info("HotPlug: device connected — %s", dev.display_name)

                # Removed devices
                for port in self._known_ports - current_ports:
                    dev = self._manager.remove_device(port)
                    if dev:
                        self._manager._fire_disconnected(dev)
                        log.info("HotPlug: device disconnected — %s", port)

                self._known_ports = current_ports
            except Exception:
                log.exception("HotPlug monitor error")

            self._stop_event.wait(self._interval)
