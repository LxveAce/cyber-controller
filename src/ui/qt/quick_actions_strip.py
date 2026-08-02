"""Operate-Home Zone B: QuickActionsStrip (WS3) - hero one-tap actions for the connected firmware.

A row of curated action tiles (from ``operate_featured.featured_actions``) plus an always-present
two-mode STOP. Every tap reuses CC's guarded send verbatim (the ``run_fn`` / ``send`` / ``ready_fn``
/ ``safe_state_fn`` from ``OperateTab``); this widget never re-implements ``safety.py`` or the send
path. A no-arg verb fires on tap; an arg verb opens an inline ``OpPanel`` via the same guarded
``send``. Dangerous verbs are danger-labeled (red illegal-tx / amber lab-only) and readiness-gated
(disabled-with-reason from ``ready_fn(ci)()`` on the poll), never one-tap TX. STOP is never gated.
Honest-empty: no curated verbs -> a hint + STOP only, no invented tiles.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt5.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from src.ui.qt.theme import colors as C

_ILLEGAL = "#f85149"   # illegal-tx danger cue (matches the Operate grid)
_LABONLY = "#d29922"   # lab-only danger cue


class QuickActionsStrip(QWidget):
    """The one-tap action strip. Rebuilt (via :meth:`set_actions`) only on connect / disconnect /
    firmware-change; readiness refreshed cheaply on the host poll (:meth:`refresh_readiness`)."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._run_fn: "Optional[Callable]" = None
        self._send: "Optional[Callable]" = None
        self._ready_fn: "Optional[Callable]" = None
        self._safe_state_fn: "Optional[Callable]" = None
        self._supports_arm = False
        self._stop_ci: Any = None
        self._min_target = 44               # tile/STOP hit-target (pt); bumped on touch
        self._tiles: "list[tuple[Any, QPushButton]]" = []
        self._stop_btn: "Optional[QPushButton]" = None
        self._open_panel: Any = None
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(8)
        self._grid_host = QWidget()
        self._grid_lay = QVBoxLayout(self._grid_host)
        self._grid_lay.setContentsMargins(0, 0, 0, 0)
        self._outer.addWidget(self._grid_host)
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
        self._hint.setVisible(False)
        self._outer.addWidget(self._hint)

    def set_actions(self, cis: "list[Any]", run_fn: Callable, send: Callable, ready_fn: Callable,
                    safe_state_fn: Callable, supports_arm: bool = False,
                    stop_ci: Any = None) -> None:
        """Rebuild the strip for the connected firmware. Call ONLY on connect / disconnect /
        firmware-change — a rebuild on the poll would tear down an open OpPanel mid-interaction."""
        self._run_fn, self._send = run_fn, send
        self._ready_fn, self._safe_state_fn = ready_fn, safe_state_fn
        self._supports_arm, self._stop_ci = supports_arm, stop_ci
        self._build(list(cis or []))

    # ── build ────────────────────────────────────────────────────────────
    def _build(self, cis: "list[Any]") -> None:
        self._close_panel()
        while self._grid_lay.count():                       # clear the old grid
            w = self._grid_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._tiles = []
        tiles: "list[QWidget]" = []
        for ci in cis:
            btn = self._make_tile(ci)
            self._tiles.append((ci, btn))
            tiles.append(btn)
        self._stop_btn = self._make_stop()
        tiles.append(self._stop_btn)
        from src.ui.qt.widgets.responsive_grid import ResponsiveTileGrid
        self._grid_lay.addWidget(ResponsiveTileGrid(tiles))
        self._hint.setText(
            "" if cis else "No one-tap actions here; use Go deeper or the Devices terminal.")
        self._hint.setVisible(not cis)
        self.refresh_readiness()

    def _make_tile(self, ci: Any) -> QPushButton:
        from src.core import safety
        danger = safety.classify(getattr(ci, "name", "") or "", ci)
        btn = QPushButton(getattr(ci, "name", "") or "?")
        btn.setMinimumHeight(self._min_target)
        tip = getattr(ci, "description", "") or getattr(ci, "name", "") or ""
        if getattr(ci, "args", ""):
            tip += f"\nargs: {ci.args}"
        if danger:
            tip += f"\n[{danger}]"
            color = _ILLEGAL if danger == "illegal-tx" else _LABONLY
            btn.setStyleSheet(f"QPushButton {{ border:1px solid {color}; color:{color}; }}")
        btn.setToolTip(tip)
        btn.setProperty("base_tip", tip)
        btn.clicked.connect(lambda _=False, c=ci: self._on_tile(c))
        return btn

    def _make_stop(self) -> QPushButton:
        """STOP / safe-state, two-mode (never gated). Arming fw -> disarm via safe_state_fn; else a
        matched stop verb via run_fn; else a disabled 'no stop verb' chip (honest, not fake)."""
        btn = QPushButton("STOP")
        btn.setMinimumHeight(self._min_target)
        btn.setStyleSheet("QPushButton { font-weight:bold; }")
        if self._supports_arm and self._safe_state_fn is not None:
            btn.setToolTip("Return the device to SAFE (disarm).")
            btn.clicked.connect(lambda: self._safe_state_fn())
        elif self._stop_ci is not None and self._run_fn is not None:
            btn.setToolTip(f"Stop: {getattr(self._stop_ci, 'name', '')}")
            btn.clicked.connect(lambda _=False, c=self._stop_ci: self._run_fn(c))
        else:
            btn.setEnabled(False)
            btn.setToolTip("No stop verb for this firmware.")
        return btn

    # ── run ──────────────────────────────────────────────────────────────
    def _on_tile(self, ci: Any) -> None:
        self._close_panel()
        if getattr(ci, "args", ""):            # arg verb -> inline OpPanel (guarded send)
            from src.ui.qt.op_panel import OpPanel
            ready = self._ready_fn(ci) if self._ready_fn is not None else None
            self._open_panel = OpPanel(ci, self._send, ready_fn=ready)
            self._outer.addWidget(self._open_panel)
        elif self._run_fn is not None:         # no-arg verb -> one tap via run_curated
            self._run_fn(ci)

    def refresh_readiness(self) -> None:
        """Poll-safe: refresh each tile's enabled + disabled-reason from ``ready_fn(ci)()``. No
        rebuild, no teardown (cheap on the 2 s poll). STOP is never gated."""
        if self._ready_fn is None:
            return
        for ci, btn in self._tiles:
            try:
                ok, reason = self._ready_fn(ci)()
            except Exception:  # noqa: BLE001 — a readiness hiccup must not disable a tile silently
                ok, reason = True, ""
            btn.setEnabled(bool(ok))
            base = btn.property("base_tip") or ""
            btn.setToolTip(base + ("" if ok else f"\n{reason}"))
        if self._open_panel is not None:
            self._open_panel.refresh_ready()

    def set_min_target(self, pt: int) -> None:
        """Lift the tile + STOP hit-target to *pt* reference-points (touch density). Applied to the
        live tiles + STOP now, and stored so the next :meth:`set_actions` rebuild re-applies it."""
        self._min_target = max(1, int(pt))
        for _ci, btn in self._tiles:
            btn.setMinimumHeight(self._min_target)
        if self._stop_btn is not None:
            self._stop_btn.setMinimumHeight(self._min_target)

    def _close_panel(self) -> None:
        if self._open_panel is not None:
            self._open_panel.setParent(None)
            self._open_panel.deleteLater()
            self._open_panel = None
