"""Health tab — system and device health dashboard."""

from __future__ import annotations

import logging
from typing import Any

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.health_monitor import HealthMonitor
from src.ui.qt.widgets.arc_gauge import ArcGauge

log = logging.getLogger(__name__)


def _make_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    """Create a card-styled QFrame with optional title label."""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("card_title")
        layout.addWidget(lbl)
    return card, layout


class HealthTab(QWidget):
    """System and device health dashboard tab.

    Displays CPU, RAM, Disk, and Battery arc gauges plus a table of
    connected devices with firmware version, uptime, and signal.
    Auto-refreshes every 5 seconds via QTimer.
    """

    def __init__(self, health_monitor: HealthMonitor) -> None:
        super().__init__()
        self._monitor = health_monitor
        self._build_ui()

        # Refresh timer (5 seconds)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(5000)

        # Initial refresh
        self._refresh()

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ── System Health Section ────────────────────────────────────
        sys_card, sys_layout = _make_card("System Health")

        # Arc gauges in a horizontal row
        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(16)

        self._cpu_gauge = ArcGauge(value=0, label="CPU")
        gauge_row.addWidget(self._cpu_gauge)

        self._ram_gauge = ArcGauge(value=0, label="RAM")
        gauge_row.addWidget(self._ram_gauge)

        self._disk_gauge = ArcGauge(value=0, label="Disk")
        gauge_row.addWidget(self._disk_gauge)

        self._batt_gauge = ArcGauge(value=0, label="Battery")
        gauge_row.addWidget(self._batt_gauge)

        sys_layout.addLayout(gauge_row)

        # Detail labels row
        detail_row = QHBoxLayout()
        self._cpu_detail = QLabel("")
        self._cpu_detail.setAlignment(Qt.AlignCenter)
        self._cpu_detail.setObjectName("muted")
        detail_row.addWidget(self._cpu_detail)

        self._ram_detail = QLabel("")
        self._ram_detail.setAlignment(Qt.AlignCenter)
        self._ram_detail.setObjectName("muted")
        detail_row.addWidget(self._ram_detail)

        self._disk_detail = QLabel("")
        self._disk_detail.setAlignment(Qt.AlignCenter)
        self._disk_detail.setObjectName("muted")
        detail_row.addWidget(self._disk_detail)

        self._batt_detail = QLabel("")
        self._batt_detail.setAlignment(Qt.AlignCenter)
        self._batt_detail.setObjectName("muted")
        detail_row.addWidget(self._batt_detail)

        sys_layout.addLayout(detail_row)

        # GPS status
        gps_row = QHBoxLayout()
        gps_label = QLabel("GPS:")
        gps_label.setMinimumWidth(40)
        gps_row.addWidget(gps_label)
        self._gps_status = QLabel("No Fix")
        self._gps_status.setObjectName("muted")
        gps_row.addWidget(self._gps_status)
        gps_row.addStretch()
        sys_layout.addLayout(gps_row)

        root.addWidget(sys_card)

        # ── Device Health Section ────────────────────────────────────
        dev_card, dev_layout = _make_card("Device Health")

        self._device_table = QTableWidget()
        self._device_table.setColumnCount(5)
        self._device_table.setHorizontalHeaderLabels([
            "Port", "Firmware", "Uptime", "Signal", "Last Seen",
        ])
        self._device_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._device_table.setAlternatingRowColors(True)
        self._device_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._device_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._device_table.verticalHeader().setVisible(False)
        dev_layout.addWidget(self._device_table)

        root.addWidget(dev_card)

    # ── Refresh ──────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Poll health monitor and update all widgets."""
        try:
            system = self._monitor.get_system_health()
            self._update_system(system)
        except Exception:
            log.exception("HealthTab: system health error")

        try:
            devices = self._monitor.get_all_device_health()
            self._update_devices(devices)
        except Exception:
            log.exception("HealthTab: device health error")

    def _update_system(self, health: dict[str, Any]) -> None:
        """Update system health gauges."""
        cpu = health.get("cpu_percent", 0)
        self._cpu_gauge.set_value(int(cpu))
        self._cpu_detail.setText(f"{cpu:.1f}%")

        mem = health.get("memory_percent", 0)
        used_mb = health.get("memory_used_mb", 0)
        total_mb = health.get("memory_total_mb", 0)
        self._ram_gauge.set_value(int(mem))
        self._ram_detail.setText(f"{used_mb}/{total_mb} MB")

        disk = health.get("disk_percent", 0)
        used_gb = health.get("disk_used_gb", 0)
        total_gb = health.get("disk_total_gb", 0)
        self._disk_gauge.set_value(int(disk))
        self._disk_detail.setText(f"{used_gb}/{total_gb} GB")

        batt = health.get("battery_percent")
        if batt is not None:
            self._batt_gauge.set_value(int(batt))
            self._batt_detail.setText(f"{batt:.0f}%")
            # Invert for color: low battery = high danger
            self._batt_gauge._color_override = None
        else:
            self._batt_gauge.set_value(0)
            self._batt_detail.setText("N/A")
            self._batt_gauge._color_override = "#484f58"
            self._batt_gauge.update()

        gps = health.get("gps_fix", False)
        if gps:
            self._gps_status.setText("Fix Acquired")
            self._gps_status.setObjectName("gps_fix")
            self._gps_status.setStyleSheet("color: #39ff14; font-weight: bold;")
        else:
            self._gps_status.setText("No Fix")
            self._gps_status.setObjectName("muted")
            self._gps_status.setStyleSheet("color: #8b949e;")

    def _update_devices(self, devices: dict[str, dict[str, Any]]) -> None:
        """Update device health table."""
        self._device_table.setRowCount(len(devices))
        for row, (port, info) in enumerate(devices.items()):
            self._device_table.setItem(row, 0, QTableWidgetItem(port))
            self._device_table.setItem(
                row, 1, QTableWidgetItem(info.get("firmware_version", "unknown"))
            )

            uptime = info.get("uptime")
            uptime_str = self._format_uptime(uptime) if uptime is not None else "--"
            self._device_table.setItem(row, 2, QTableWidgetItem(uptime_str))

            signal = info.get("signal_strength")
            signal_str = f"{signal} dBm" if signal is not None else "--"
            self._device_table.setItem(row, 3, QTableWidgetItem(signal_str))

            last_seen = info.get("last_seen", "--")
            if last_seen and last_seen != "--":
                # Show just time portion
                try:
                    last_seen = last_seen.split("T")[1][:8]
                except (IndexError, AttributeError):
                    pass
            self._device_table.setItem(row, 4, QTableWidgetItem(str(last_seen)))

            # Color status
            status = info.get("status", "unknown")
            color = QColor("#39ff14") if status == "connected" else QColor("#8b949e")
            for col in range(5):
                item = self._device_table.item(row, col)
                if item:
                    item.setForeground(color)

    @staticmethod
    def _format_uptime(seconds: float | None) -> str:
        """Format uptime seconds into human-readable string."""
        if seconds is None:
            return "--"
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        hours = s // 3600
        mins = (s % 3600) // 60
        return f"{hours}h {mins}m"
