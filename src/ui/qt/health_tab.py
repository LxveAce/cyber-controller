"""Health tab — system and device health dashboard."""

from __future__ import annotations

import logging
from typing import Any

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.health_monitor import HealthMonitor

log = logging.getLogger(__name__)


def _make_bar(label: str, color: str = "#39ff14") -> tuple[QLabel, QProgressBar]:
    """Create a labelled progress bar."""
    lbl = QLabel(label)
    lbl.setMinimumWidth(100)
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    bar.setTextVisible(True)
    bar.setStyleSheet(
        f"QProgressBar::chunk {{ background-color: {color}; }}"
        "QProgressBar { text-align: center; }"
    )
    return lbl, bar


class HealthTab(QWidget):
    """System and device health dashboard tab.

    Displays CPU, RAM, Disk, and Battery bars plus a table of
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
        sys_group = QGroupBox("System Health")
        sys_layout = QVBoxLayout(sys_group)

        # CPU
        cpu_row = QHBoxLayout()
        self._cpu_label, self._cpu_bar = _make_bar("CPU:", "#39ff14")
        cpu_row.addWidget(self._cpu_label)
        cpu_row.addWidget(self._cpu_bar)
        self._cpu_detail = QLabel("")
        self._cpu_detail.setMinimumWidth(80)
        cpu_row.addWidget(self._cpu_detail)
        sys_layout.addLayout(cpu_row)

        # RAM
        ram_row = QHBoxLayout()
        self._ram_label, self._ram_bar = _make_bar("RAM:", "#00bfff")
        ram_row.addWidget(self._ram_label)
        ram_row.addWidget(self._ram_bar)
        self._ram_detail = QLabel("")
        self._ram_detail.setMinimumWidth(80)
        ram_row.addWidget(self._ram_detail)
        sys_layout.addLayout(ram_row)

        # Disk
        disk_row = QHBoxLayout()
        self._disk_label, self._disk_bar = _make_bar("Disk:", "#ff8c00")
        disk_row.addWidget(self._disk_label)
        disk_row.addWidget(self._disk_bar)
        self._disk_detail = QLabel("")
        self._disk_detail.setMinimumWidth(80)
        disk_row.addWidget(self._disk_detail)
        sys_layout.addLayout(disk_row)

        # Battery
        batt_row = QHBoxLayout()
        self._batt_label, self._batt_bar = _make_bar("Battery:", "#ffd700")
        batt_row.addWidget(self._batt_label)
        batt_row.addWidget(self._batt_bar)
        self._batt_detail = QLabel("")
        self._batt_detail.setMinimumWidth(80)
        batt_row.addWidget(self._batt_detail)
        sys_layout.addLayout(batt_row)

        # GPS status
        gps_row = QHBoxLayout()
        gps_label = QLabel("GPS:")
        gps_label.setMinimumWidth(100)
        gps_row.addWidget(gps_label)
        self._gps_status = QLabel("No Fix")
        self._gps_status.setStyleSheet("color: #888;")
        gps_row.addWidget(self._gps_status)
        gps_row.addStretch()
        sys_layout.addLayout(gps_row)

        root.addWidget(sys_group)

        # ── Device Health Section ────────────────────────────────────
        dev_group = QGroupBox("Device Health")
        dev_layout = QVBoxLayout(dev_group)

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

        root.addWidget(dev_group)

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
        """Update system health bars."""
        cpu = health.get("cpu_percent", 0)
        self._cpu_bar.setValue(int(cpu))
        self._cpu_detail.setText(f"{cpu:.1f}%")
        self._color_bar(self._cpu_bar, cpu)

        mem = health.get("memory_percent", 0)
        used_mb = health.get("memory_used_mb", 0)
        total_mb = health.get("memory_total_mb", 0)
        self._ram_bar.setValue(int(mem))
        self._ram_detail.setText(f"{used_mb}/{total_mb} MB")
        self._color_bar(self._ram_bar, mem)

        disk = health.get("disk_percent", 0)
        used_gb = health.get("disk_used_gb", 0)
        total_gb = health.get("disk_total_gb", 0)
        self._disk_bar.setValue(int(disk))
        self._disk_detail.setText(f"{used_gb}/{total_gb} GB")
        self._color_bar(self._disk_bar, disk)

        batt = health.get("battery_percent")
        if batt is not None:
            self._batt_bar.setValue(int(batt))
            self._batt_detail.setText(f"{batt:.0f}%")
            self._batt_bar.setEnabled(True)
            self._color_bar(self._batt_bar, 100 - batt)  # Invert: low battery = red
        else:
            self._batt_bar.setValue(0)
            self._batt_detail.setText("N/A")
            self._batt_bar.setEnabled(False)

        gps = health.get("gps_fix", False)
        if gps:
            self._gps_status.setText("Fix Acquired")
            self._gps_status.setStyleSheet("color: #39ff14; font-weight: bold;")
        else:
            self._gps_status.setText("No Fix")
            self._gps_status.setStyleSheet("color: #888;")

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
            color = QColor("#39ff14") if status == "connected" else QColor("#888")
            for col in range(5):
                item = self._device_table.item(row, col)
                if item:
                    item.setForeground(color)

    @staticmethod
    def _color_bar(bar: QProgressBar, value: float) -> None:
        """Set bar chunk color based on value threshold."""
        if value >= 90:
            color = "#ff4444"
        elif value >= 70:
            color = "#ff8c00"
        elif value >= 50:
            color = "#ffd700"
        else:
            color = "#39ff14"
        bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
            "QProgressBar { text-align: center; }"
        )

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
