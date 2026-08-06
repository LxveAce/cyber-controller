"""DEVICE ▸ Dashboard — the reform's landing screen (the app opens here).

The reform opens on a real working device screen instead of the Operate-Home bounce-pad the owner
rejected. Per REFORM-DENSITY-SPEC "Dashboard → DEVICE > Dashboard (landing)", this MERGES RIG Health
(host CPU/RAM/Disk/Battery + GPS gauges + the 5-col device-health table) and RIG Devices (the device
list, firmware combo, connect/scan/send, the per-device readouts — caps, telemetry, health chip,
ARM/SAFE lamp, alert, airspace snapshot — the serial terminal, and the BlueJammer + Meshtastic panels)
into one landing, with an optional Cross-Comm summary slot.

It is a PURE RE-COMPOSITION: it RE-PARENTS the already-built ``HealthTab`` / ``DeviceTab`` /
``CrossCommTab`` instances into one layout and never rebuilds their internals, so every existing signal,
safety gate, and test keeps working and nothing from the density spec's Dashboard field set is dropped
(density rule: reorganize, never thin). safety.py is untouched — the ARM/SAFE lamp and the always-on,
ungated BlueJammer STOP live inside the re-parented DeviceTab exactly as before. The composed widgets'
lifecycle (poll timers, shutdown) stays owned by main_window, which keeps its own references to them.

Additive: this is NOT yet wired into main_window. Mounting it as the DEVICE ▸ Dashboard sub-tab and
landing the app on it (instead of Operate-Home) is the shell increment (main_window's lane).
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSplitter, QVBoxLayout, QWidget


class DeviceDashboard(QWidget):
    """DEVICE ▸ Dashboard landing. Composes the passed HealthTab + DeviceTab (+ optional Cross-Comm)
    into one screen by re-parenting them — the device control + terminal dominant, host health beside
    it. The widgets keep every field/gate they already have; this only rearranges them."""

    def __init__(self, health_tab: QWidget, device_tab: QWidget,
                 cross_comm: "Optional[QWidget]" = None, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._health_tab = health_tab
        self._device_tab = device_tab
        self._cross_comm = cross_comm
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._split = QSplitter(Qt.Horizontal)
        self._split.setChildrenCollapsible(False)   # no field is ever fully hidden by a drag
        # Host health (gauges + device-health table) beside the device control + terminal.
        self._split.addWidget(self._health_tab)
        self._split.addWidget(self._device_tab)
        self._split.setStretchFactor(0, 2)
        self._split.setStretchFactor(1, 3)           # device control + terminal dominates
        sizes = [420, 700]
        if self._cross_comm is not None:             # Cross-Comm summary slot (density reconciliation)
            self._split.addWidget(self._cross_comm)
            self._split.setStretchFactor(2, 2)
            sizes = [380, 560, 380]
        self._split.setSizes(sizes)
        outer.addWidget(self._split, 1)

    def set_ui_mode(self, mode: str) -> None:
        """Forward the Simple/Pro depth toggle to the composed widgets — each owns its own Pro-gating,
        so the depth toggle stays honest (Pro-only fields hide/show exactly as they do standalone)."""
        for w in (self._health_tab, self._device_tab, self._cross_comm):
            fn = getattr(w, "set_ui_mode", None)
            if callable(fn):
                fn(mode)
