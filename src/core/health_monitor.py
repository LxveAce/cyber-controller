"""Health monitor — system and device health metrics with polling thread."""

from __future__ import annotations

import logging
import threading
import time
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

    Device metrics (via serial query):
        firmware_version, uptime, signal_strength, last_seen
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

        If a serial connection is available, attempts to query the device
        for firmware version and uptime. Otherwise returns cached data.

        Args:
            port: Serial port identifier.

        Returns:
            Dict with firmware_version, uptime, signal_strength, last_seen, status.
        """
        with self._lock:
            cached = self._device_health.get(port, {})
            conn = self._device_connections.get(port)

        if not cached:
            return {
                "port": port,
                "firmware_version": "unknown",
                "uptime": None,
                "signal_strength": None,
                "last_seen": None,
                "status": "not_registered",
            }

        # If we have a connection, try to update firmware_version
        if conn is not None:
            try:
                if hasattr(conn, "is_connected") and conn.is_connected:
                    cached["last_seen"] = datetime.now(timezone.utc).isoformat()
                    cached["status"] = "connected"
                else:
                    cached["status"] = "disconnected"
            except Exception:
                cached["status"] = "error"

        return dict(cached)

    def get_all_device_health(self) -> dict[str, dict[str, Any]]:
        """Return health data for all registered devices."""
        with self._lock:
            return {port: dict(info) for port, info in self._device_health.items()}

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

                # Update device health
                with self._lock:
                    ports = list(self._device_connections.keys())
                for port in ports:
                    health = self.get_device_health(port)
                    with self._lock:
                        self._device_health[port] = health

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
