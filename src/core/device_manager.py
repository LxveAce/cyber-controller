"""Device manager — auto-detect, track, and manage serial hardware devices."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

from src.core.serial_handler import SerialConnection
from src.models.device import BoardType, Device

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
        self._conn_owners: dict[str, set[str]] = {}  # port -> UI owners sharing the connection
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
            self._conn_owners.pop(port, None)  # a physical removal drops all owners
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

    def open_connection(self, port: str, baud: int = 115200, owner: str | None = None) -> SerialConnection:
        """Open (or return the existing) SerialConnection for *port*.

        Multiple UI panels can share one connection on a port; pass an *owner* tag (e.g. ``"devices_tab"``,
        ``"pterm"``) and the connection is only torn down when the LAST owner releases it via
        :meth:`close_connection` — so disconnecting in one panel can't kill another panel's in-use
        connection. A physical unplug (:meth:`remove_device`) or :meth:`shutdown` still force-closes.

        Raises:
            KeyError: If port is not in the device registry, or the device was hot-unplugged mid-connect.
        """
        with self._lock:
            if port not in self._devices:
                raise KeyError(f"No registered device on port {port}")
            existing = self._connections.get(port)
            if existing is not None and existing.is_connected:
                if owner:
                    self._conn_owners.setdefault(port, set()).add(owner)
                self._devices[port].connected = True
                return existing
            stale = existing  # a dead/errored conn for this port, if any
            fw = self._devices[port].firmware or ""

        # Build + connect OUTSIDE the lock (connect() can block / join a thread). Release any stale handle
        # first so the exclusive COM port is free, and seed the per-firmware terminator at open time.
        if stale is not None:
            try:
                stale.disconnect()
            except Exception:
                pass
        try:
            from src.protocols import line_ending_for
            terminator = line_ending_for(fw) if fw else "\n"
        except Exception:
            terminator = "\n"
        conn = SerialConnection(port, baud=baud, line_ending=terminator)

        def _reconcile(_state, _conn=conn, _port=port):
            # Reflect the connection's OWN state changes back onto the Device so the indicator can't
            # lie. A mid-session ERROR (cable brown-out / firmware reboot that drops the CDC endpoint
            # but keeps the COM name) doesn't make the port disappear, so HotPlug never fires and
            # Device.connected would otherwise stay True forever — the sidebar shows green while the
            # AutoRouter silently drops every routed command to the dead port. A late callback after a
            # remove_device finds no device and no-ops.
            with self._lock:
                d = self._devices.get(_port)
                if d is not None:
                    d.connected = _conn.is_connected

        conn.on_state_change(_reconcile)
        conn.connect()

        to_close: SerialConnection | None = None
        removed = False
        with self._lock:
            dev = self._devices.get(port)
            if dev is None:
                # Hot-unplugged during connect(): discard the new conn cleanly rather than KeyError- on a
                # popped key (and never leak the freshly opened port).
                to_close = conn
                removed = True
            else:
                self._connections[port] = conn
                dev.connected = True
                if owner:
                    self._conn_owners.setdefault(port, set()).add(owner)
        if to_close is not None:
            to_close.disconnect()  # outside the lock (disconnect joins the reader thread)
        if removed:
            raise KeyError(f"Device on port {port} was removed during connect")
        return conn

    def close_connection(self, port: str, owner: str | None = None) -> None:
        """Release the serial connection for *port*. With an *owner* tag only the LAST owner's release
        tears the connection down (so one panel disconnecting can't kill another's in-use connection); a
        release with no owner (shutdown / hot-unplug) force-closes regardless."""
        with self._lock:
            owners = self._conn_owners.get(port)
            if owner and owners is not None:
                owners.discard(owner)
                if owners:
                    return  # other owners still using it -> keep it alive
            conn = self._connections.pop(port, None)
            self._conn_owners.pop(port, None)
            dev = self._devices.get(port)
        if conn:
            conn.disconnect()
        if dev:
            dev.connected = False

    def get_connection(self, port: str) -> SerialConnection | None:
        """Return the active SerialConnection for *port*, if any."""
        with self._lock:
            return self._connections.get(port)

    def probe(self, port: str, *, timeout: float = 0.8):
        """Run the connect-time handshake probe on *port* and set the device's ``health``/``fw_banner``.

        The entry point for the S3-c handshake (src/core/handshake.py): it sends the firmware's probe command
        over the open link, learns liveness + banner + live vocabulary, and returns a ``HandshakeResult`` (or
        ``None`` if the port has no device / no live connection). Intentionally NOT called from
        :meth:`open_connection` — a probe writes to the port and blocks briefly, so the caller runs it when it
        makes sense (e.g. a UI connect flow, in a background thread), rather than on every open.
        """
        dev = self.get_device(port)
        conn = self.get_connection(port)
        if dev is None or conn is None or not conn.is_connected:
            return None
        from src.core.handshake import probe_device
        return probe_device(conn, dev, timeout=timeout)

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
