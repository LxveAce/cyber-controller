"""OPERATE HOME domain grid (design brief) — the stable, branded radio/domain tile grid.

A fixed set of domain tiles (Wi-Fi / BLE / RF-SubGHz / GPS-Wardrive / Tools / Settings) laid out
responsively by :class:`ResponsiveTileGrid` (real 2-col/3-up by width, not a fixed postage-stamp
grid). Each tile is a Biscuit :class:`OperationCard`; activating one emits
``domain_selected(key)`` so the host shell can open that domain's detail screen. Presentation only —
the shell wires the navigation.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from src.ui.qt.biscuit import OperationCard
from src.ui.qt.widgets.responsive_grid import ResponsiveTileGrid

# (key, icon glyph, title, one-line description) — the brief's domain axis, in a stable order.
_DOMAINS: tuple[tuple[str, str, str, str], ...] = (
    ("wifi", "\U0001F4F6", "Wi-Fi", "Scan, capture, and analyze 802.11 networks"),
    ("ble", "\U0001F4F1", "BLE", "Bluetooth LE scan, GATT, and tracker detection"),
    ("subghz", "\U0001F4E1", "RF / Sub-GHz", "Sub-GHz capture and OOK/FSK decode"),
    ("gps", "\U0001F6F0", "GPS Wardrive", "GPS-tagged wardriving to WiGLE CSV"),
    ("tools", "\U0001F9F0", "Tools", "Crack Lab, OSINT, and utilities"),
    ("settings", "⚙", "Settings", "Device, interface, and accessibility"),
)

# Domains whose real screen lands at P4 (they open in MAP). Shown as honest, greyed ROADMAP tiles —
# non-activating, labelled with where they're going — instead of an in-place browser or a
# navigate-to-nothing placeholder. Spade D6c (Atlas call): grey + "P4 · opens in MAP".
_ROADMAP: dict[str, str] = {"gps": "P4 · opens in MAP", "subghz": "P4 · opens in MAP"}


class DomainGrid(QWidget):
    """The OPERATE HOME domain tile grid. ``domain_selected(key)`` fires when a tile is chosen."""

    domain_selected = pyqtSignal(str)

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, OperationCard] = {}
        tiles: list[QWidget] = []
        for key, icon, title, desc in _DOMAINS:
            roadmap = _ROADMAP.get(key)
            card = OperationCard(icon, title, roadmap or desc)
            if roadmap:
                # Greyed, non-activating roadmap tile: Qt blocks input to a disabled widget, so it
                # can't emit domain_selected — an honest "coming at P4", not a dead-end navigate.
                card.setEnabled(False)
            else:
                # Bind the key per-iteration; activating the card announces which domain was chosen.
                card.activated.connect(lambda _=False, k=key: self.domain_selected.emit(k))
            self._cards[key] = card
            tiles.append(card)
        self._grid = ResponsiveTileGrid(tiles)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._grid)

    def domain_keys(self) -> list[str]:
        """The domain keys, in display order."""
        return [d[0] for d in _DOMAINS]

    def roadmap_keys(self) -> list[str]:
        """Domains rendered as greyed, non-activating roadmap tiles (their screen lands at P4)."""
        return list(_ROADMAP)

    @property
    def grid(self) -> ResponsiveTileGrid:
        return self._grid
