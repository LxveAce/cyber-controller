"""Health monitor — system and device health metrics with polling thread."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

log = logging.getLogger(__name__)

HealthCallback = Callable[[dict[str, Any]], None]

_DEFAULT_INTERVAL = 5.0


class HealthMonitor:
    """Monitor system and device health metrics.

    Runs a background polling thread that calls registered callbacks
    with updated metrics every ``interval`` seconds.

    System metrics (via psutil):
        cpu_percent, memory_percent, disk_percent, battery_percent, gps_fix

    Device metrics:
        ``last_seen`` and ``status`` are tracked live from each registered device's
        connection; ``firmware_version``/``uptime``/``signal_strength`` are placeholder
        fields (not yet queried over serial) that stay at their registered defaults.
    """

    def __init__(self, interval: float = _DEFAULT_INTERVAL) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Latest cached metrics
        self._system_health: dict[str, Any] = {}
        self._device_health: dict[str, dict[str, Any]] = {}  # port -> metrics

        # Callbacks
        self._callbacks: list[HealthCallback] = []

        # Device connections for querying (port -> serial connection)
        self._device_connections: dict[str, Any] = {}

        # DeviceManager this monitor is wired to (set via attach_device_manager),
        # used to re-resolve each registered port's live connection on every poll.
        self._dm: Any = None

    # ── Callback registration ────────────────────────────────────────

    def on_update(self, callback: HealthCallback) -> None:
        """Register a callback fired on each polling cycle.

        The callback receives a dict with keys ``system`` and ``devices``.
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: HealthCallback) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    # ── Device registration ──────────────────────────────────────────

    def register_device(self, port: str, connection: Any = None) -> None:
        """Register a device port for health monitoring.

        Args:
            port: Serial port identifier.
            connection: Optional SerialConnection instance for firmware queries.
        """
        with self._lock:
            self._device_connections[port] = connection
            self._device_health[port] = {
                "port": port,
                "firmware_version": "unknown",
                "uptime": None,
                "signal_strength": None,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "status": "registered",
            }
        log.debug("HealthMonitor: registered device %s", port)

    def unregister_device(self, port: str) -> None:
        """Remove a device from monitoring."""
        with self._lock:
            self._device_connections.pop(port, None)
            self._device_health.pop(port, None)
        log.debug("HealthMonitor: unregistered device %s", port)

    def attach_device_manager(self, device_manager: Any) -> None:
        """Wire this monitor to a :class:`~src.core.device_manager.DeviceManager`.

        This is the cross-module link that actually populates the device-health
        table: every device already known to the manager is registered now, and the
        set is kept in sync by (un)registering on the manager's device connect /
        disconnect events. The manager is also retained so each poll can re-resolve a
        port's live ``SerialConnection`` — a device is detected (connect event) before
        any serial port is opened on it, so the connection appears only later.
        """
        self._dm = device_manager
        for dev in device_manager.list_devices():
            self.register_device(dev.port, device_manager.get_connection(dev.port))
        device_manager.on_device_connected(
            lambda d: self.register_device(d.port, device_manager.get_connection(d.port))
        )
        device_manager.on_device_disconnected(lambda d: self.unregister_device(d.port))

    # ── System health ────────────────────────────────────────────────

    @staticmethod
    def get_system_health() -> dict[str, Any]:
        """Collect current system health metrics.

        Returns:
            Dict with cpu_percent, memory_percent, disk_percent,
            battery_percent (None if no battery), gps_fix (always False
            unless gpsd is available).
        """
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/") if not hasattr(psutil.disk_usage, "__wrapped__") else psutil.disk_usage("C:\\")

        # Handle cross-platform disk usage
        try:
            disk = psutil.disk_usage("C:\\")
        except Exception:
            try:
                disk = psutil.disk_usage("/")
            except Exception:
                disk = None

        battery_pct = None
        battery = psutil.sensors_battery()
        if battery is not None:
            battery_pct = battery.percent

        # GPS: would require gpsd integration, always False for now
        gps_fix = False

        return {
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "memory_used_mb": round(mem.used / (1024 * 1024)),
            "memory_total_mb": round(mem.total / (1024 * 1024)),
            "disk_percent": disk.percent if disk else 0.0,
            "disk_used_gb": round(disk.used / (1024 ** 3), 1) if disk else 0.0,
            "disk_total_gb": round(disk.total / (1024 ** 3), 1) if disk else 0.0,
            "battery_percent": battery_pct,
            "gps_fix": gps_fix,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Device health ────────────────────────────────────────────────

    def get_device_health(self, port: str) -> dict[str, Any]:
        """Get health metrics for a specific device.

        If a live serial connection is available for the port, refreshes
        ``last_seen``/``status`` from its connection state and reads ``firmware_version``
        from the Device's connect-time handshake; otherwise returns the cached data.
        ``uptime``/``signal_strength`` are not yet queried over serial and stay at their
        registered defaults.

        Args:
            port: Serial port identifier.

        Returns:
            Dict with firmware_version, uptime, signal_strength, last_seen, status.
        """
        with self._lock:
            cached = self._device_health.get(port, {})
            conn = self._device_connections.get(port)
            dm = self._dm

        if not cached:
            return {
                "port": port,
                "firmware_version": "unknown",
                "uptime": None,
                "signal_strength": None,
                "last_seen": None,
                "status": "not_registered",
            }

        # Surface the firmware banner + probe health from the registered Device (set by the
        # connect-time handshake), so the panel shows the real firmware instead of a permanent
        # "unknown". The DeviceManager stand-in used by some tests has no get_device -> guard it.
        dev = None
        get_device = getattr(dm, "get_device", None)
        if callable(get_device):
            try:
                dev = get_device(port)
            except Exception:
                dev = None
        if dev is not None:
            fw = getattr(dev, "firmware", "") or getattr(dev, "fw_banner", "")
            if fw:
                cached["firmware_version"] = fw
        dev_health = getattr(dev, "health", "unknown") if dev is not None else "unknown"

        # Status/last_seen must reflect whether the FIRMWARE answered, not merely that the port is
        # open. A hung or mis-flashed board keeps its CDC link open but never replies ("no-reply"):
        # report that honestly (non-green) and FREEZE last_seen so it stops ticking. Any other live
        # link (alive / no-cli / not-yet-probed) reads connected and refreshes last_seen.
        if conn is not None:
            try:
                if hasattr(conn, "is_connected") and conn.is_connected:
                    if dev_health == "no-reply":
                        cached["status"] = "no-reply"
                    else:
                        cached["status"] = "connected"
                        cached["last_seen"] = datetime.now(timezone.utc).isoformat()
                else:
                    cached["status"] = "disconnected"
            except Exception:
                cached["status"] = "error"

        return dict(cached)

    def get_all_device_health(self) -> dict[str, dict[str, Any]]:
        """Return health data for all registered devices."""
        with self._lock:
            return {port: dict(info) for port, info in self._device_health.items()}

    def _refresh_device_health(self) -> None:
        """Refresh cached health for every registered device.

        Re-resolves each port's live connection from the attached DeviceManager (if
        any) so a serial link opened AFTER the device was detected is reflected in its
        ``status``/``last_seen``, then recomputes and caches per-device metrics. This
        is the per-cycle body the polling thread runs; split out so it is directly
        testable without starting the thread.
        """
        with self._lock:
            ports = list(self._device_connections.keys())
            dm = self._dm
        for port in ports:
            if dm is not None:
                conn = dm.get_connection(port)
                with self._lock:
                    if port in self._device_connections:
                        self._device_connections[port] = conn
            health = self.get_device_health(port)
            with self._lock:
                self._device_health[port] = health

    # ── Polling thread ───────────────────────────────────────────────

    def start(self) -> None:
        """Start the background health polling thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="health-monitor",
            daemon=True,
        )
        self._thread.start()
        log.info("HealthMonitor started (%.1fs interval)", self._interval)

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 2)
        self._thread = None
        log.info("HealthMonitor stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _poll_loop(self) -> None:
        """Background loop: collect metrics and fire callbacks."""
        while not self._stop_event.is_set():
            try:
                system = self.get_system_health()
                with self._lock:
                    self._system_health = system

                # Update device health (re-resolves live connections from the
                # attached DeviceManager so a link opened after detection shows up).
                self._refresh_device_health()

                # Fire callbacks
                payload = {
                    "system": system,
                    "devices": self.get_all_device_health(),
                }
                with self._lock:
                    callbacks = list(self._callbacks)
                for cb in callbacks:
                    try:
                        cb(payload)
                    except Exception:
                        log.exception("HealthMonitor callback error")

            except Exception:
                log.exception("HealthMonitor poll error")

            self._stop_event.wait(self._interval)

    # ── Cached access ────────────────────────────────────────────────

    @property
    def latest_system_health(self) -> dict[str, Any]:
        """Return the most recent system health snapshot."""
        with self._lock:
            return dict(self._system_health)
