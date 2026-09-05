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

# CPU sampling. ``psutil.cpu_percent(interval=None)`` is non-blocking but reports utilisation *since the
# previous call in that context* — sampled from arbitrary web-request threads it reads a false 0.0 in a
# thread that never established a baseline (a per-thread warm flag can't be a process-global). So ONE
# dedicated daemon thread owns the sampling and every reader — any request thread — gets its cached
# value. The reader never touches psutil, so it can't return a cold-thread 0.
#: A cached CPU value older than this (the loop samples every ~2s) means the sampler thread died — the
#: value is stale and the reader/health surface says so, and a fresh sampler is (re)started.
_CPU_STALE_SECONDS = 15.0

_cpu_lock = threading.Lock()
_cpu_value: float = 0.0
_cpu_ts: float = 0.0        # time.monotonic() of the last SUCCESSFUL sample (0 = never sampled)
_cpu_gen = 0               # generation: only the current-gen worker may write the cache
_cpu_launching = False     # a worker is starting and hasn't posted its first sample yet


def _cpu_sampler_loop(gen: int) -> None:
    global _cpu_value, _cpu_ts, _cpu_launching
    try:
        psutil.cpu_percent(interval=None)  # prime the baseline in THIS dedicated thread
        while True:
            v = psutil.cpu_percent(interval=2.0)  # blocking sample, dedicated thread only
            with _cpu_lock:
                if gen != _cpu_gen:
                    return  # a newer worker superseded us — never touch the shared cache
                _cpu_value = v
                _cpu_ts = time.monotonic()   # advance freshness ONLY after a successful measurement
                _cpu_launching = False       # first good sample clears the launch guard
    except Exception:  # noqa: BLE001 — a dead sampler must not take the app down
        log.exception("CPU sampler thread stopped")
    finally:
        with _cpu_lock:
            if gen == _cpu_gen:
                _cpu_launching = False       # allow a future restart; _cpu_ts stays (reads report stale)


def _ensure_cpu_sampler() -> None:
    global _cpu_gen, _cpu_launching, _cpu_value, _cpu_ts
    with _cpu_lock:
        if _cpu_launching:
            return  # a start/restart is already in flight — exactly one replacement, never a pile-up
        fresh = _cpu_ts != 0.0 and (time.monotonic() - _cpu_ts) <= _CPU_STALE_SECONDS
        if fresh:
            return
        _cpu_launching = True
        _cpu_gen += 1
        gen = _cpu_gen
        first_ever = _cpu_ts == 0.0
    if first_ever:
        # First start only: one honest immediate value so the first read isn't a 2 s placeholder. A FAILED
        # seed must NOT advance the timestamp — leaving _cpu_ts at 0 keeps the state stale/unavailable
        # instead of fabricating a fresh 0%.
        try:
            seed = psutil.cpu_percent(interval=0.1)
        except Exception:  # noqa: BLE001
            seed = None
        if seed is not None:
            with _cpu_lock:
                _cpu_value = seed
                _cpu_ts = time.monotonic()
                _cpu_launching = False
    threading.Thread(target=_cpu_sampler_loop, name="cpu-sampler", args=(gen,), daemon=True).start()


def _cpu_sample() -> tuple[float, bool]:
    """Return ``(cpu_percent, stale)`` from the dedicated sampler's cache, starting/restarting it on use.
    ``stale`` is True when there is no successful sample yet or the last one is too old (sampler died) —
    readers never sample psutil themselves (no cold-thread false 0), a frozen value is surfaced as stale,
    and a failed sampler yields stale rather than a fabricated fresh 0."""
    _ensure_cpu_sampler()
    with _cpu_lock:
        stale = _cpu_ts == 0.0 or (time.monotonic() - _cpu_ts) > _CPU_STALE_SECONDS
        return _cpu_value, stale


def _cpu_percent_nonblocking() -> float:
    return _cpu_sample()[0]


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
        cpu, cpu_stale = _cpu_sample()
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
            "cpu_stale": cpu_stale,   # True if the sampler died and this value is old (don't trust it)
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
            # COPY, not a shared reference: the status/last_seen/firmware updates below run OUTSIDE
            # this lock, so mutating the stored dict in place would let get_all_device_health read a
            # torn snapshot. Mutate a local copy, then persist it back atomically at the end.
            cached = dict(self._device_health.get(port, {}))
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
        elif cached.get("status") in ("connected", "no-reply"):
            # The port was previously LIVE but now has no connection object: it was closed/released (e.g.
            # the Devices-tab Disconnect pops it) even though the board may stay physically plugged.
            # Without this the status stayed frozen at "connected", so the Health panel showed a closed
            # device as connected forever. Flip to disconnected; last_seen stays frozen. A never-connected
            # "registered" device keeps its registered status (it was never live).
            cached["status"] = "disconnected"

        # Persist the freshly-computed status/last_seen back atomically (a full-object swap under
        # the lock) so get_all_device_health sees a whole dict, old or new, never a torn mix.
        with self._lock:
            if port in self._device_health:
                self._device_health[port] = cached
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
