"""Flock heatmap (FL F4) — an offscreen-capable map of located ALPR-camera detections.

Consumes the portable GeoJSON that F2's :class:`~src.core.flock.FlockSession` produces (or a session directly),
projects each located camera through spherical **web-mercator**, fits the whole set into a `QGraphicsScene`,
and draws each camera as a heat-colored dot (hotter / larger = more sightings) — the "heatmaps" the owner
asked for. Awareness-only: it visualizes WHERE surveillance cameras were seen; it drives no device.

The projection math (``web_mercator`` / :class:`MercatorFit` / ``heat_color``) is pure and unit-testable with
no Qt. The known-cameras layer is a stub until F3 (the offline camera catalog) lands; today it renders the
live-detection layer from a scan's GeoJSON, and can load a saved ``cameras.geojson`` from disk.
"""
from __future__ import annotations

import json
import math
from typing import Any, List, Optional, Tuple

# ── pure projection core (no Qt — unit-testable) ─────────────────────

_MERC_LAT_CLAMP = 85.05112878   # the latitude where the mercator y-extent is ±1 (poles are unrepresentable)


def web_mercator(lat: float, lon: float) -> Tuple[float, float]:
    """Spherical web-mercator, normalized to the unit square. Returns (x, y) in [0, 1] with y increasing
    DOWNWARD (screen convention: north maps to a smaller y / the top)."""
    x = (lon + 180.0) / 360.0
    lat = max(min(lat, _MERC_LAT_CLAMP), -_MERC_LAT_CLAMP)
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)
    return x, y


class MercatorFit:
    """Fit a set of (lat, lon) points into a *width*×*height* pixel canvas: web-mercator, aspect-preserving,
    padded, and centered. Degenerate inputs (one point, or a zero-span axis) are handled without div-by-zero."""

    def __init__(self, points: "List[Tuple[float, float]]", width: int, height: int, pad: int = 24) -> None:
        self.width, self.height, self.pad = width, height, pad
        # Empty is degenerate but must not raise on min()/max() — treat as a single centered point.
        merc = [web_mercator(lat, lon) for lat, lon in points] if points else [(0.5, 0.5)]
        xs = [m[0] for m in merc]
        ys = [m[1] for m in merc]
        self.minx, self.maxx = min(xs), max(xs)
        self.miny, self.maxy = min(ys), max(ys)
        self.spanx = self.maxx - self.minx
        self.spany = self.maxy - self.miny
        aw = max(1, width - 2 * pad)
        ah = max(1, height - 2 * pad)
        sx = aw / self.spanx if self.spanx > 1e-12 else None
        sy = ah / self.spany if self.spany > 1e-12 else None
        cands = [s for s in (sx, sy) if s is not None]
        self._center_only = not cands            # every point coincides -> just center them
        self.scale = min(cands) if cands else 1.0

    def to_pixel(self, lat: float, lon: float) -> Tuple[float, float]:
        if self._center_only:
            return self.width / 2.0, self.height / 2.0
        mx, my = web_mercator(lat, lon)
        aw = max(1, self.width - 2 * self.pad)
        ah = max(1, self.height - 2 * self.pad)
        fitw = self.spanx * self.scale
        fith = self.spany * self.scale
        ox = self.pad + (aw - fitw) / 2.0
        oy = self.pad + (ah - fith) / 2.0
        return ox + (mx - self.minx) * self.scale, oy + (my - self.miny) * self.scale


# A simple perceptual density ramp: cool blue (few sightings) -> hot red (many). Red rises monotonically.
_HEAT_STOPS: "List[Tuple[float, Tuple[int, int, int]]]" = [
    (0.0, (31, 119, 180)),   # blue
    (0.5, (233, 143, 32)),   # amber
    (1.0, (214, 39, 40)),    # red
]


def heat_color(t: float) -> Tuple[int, int, int]:
    """Map a normalized density *t* in [0, 1] to an (r, g, b) heat color."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    for i in range(len(_HEAT_STOPS) - 1):
        t0, c0 = _HEAT_STOPS[i]
        t1, c1 = _HEAT_STOPS[i + 1]
        if t <= t1:
            f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            return tuple(round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))  # type: ignore[return-value]
    return _HEAT_STOPS[-1][1]


def _valid_point(feature: Any) -> bool:
    try:
        if not isinstance(feature, dict):
            return False
        geom = feature.get("geometry") or {}
        if not isinstance(geom, dict) or geom.get("type") != "Point":
            return False
        coords = geom.get("coordinates")
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            return False
        # isinstance(nan/inf, float) is True and bool is an int — reject both: a non-finite coordinate would
        # collapse the whole MercatorFit bbox (min/max with NaN) and silently mislocate every camera.
        return all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
                   for c in coords[:2])
    except Exception:  # noqa: BLE001
        return False


def _as_count(props: Any) -> int:
    """A sightings count for the density ramp — always >= 1, tolerant of missing / null / non-numeric."""
    try:
        return max(1, int((props or {}).get("count", 1) or 1))
    except (TypeError, ValueError, AttributeError):
        return 1


# ── Qt widget (the pure core above stays Qt-free; the widget is optional) ──

try:  # allow importing the pure core (web_mercator/MercatorFit/heat_color) even without PyQt5
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen
    from PyQt5.QtWidgets import (
        QGraphicsScene,
        QGraphicsView,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    _BG = QColor("#0d1117")
    _CANVAS_W, _CANVAS_H = 800, 600

    class FlockHeatmapTab(QWidget):
        """A heatmap of located ALPR cameras from a Flock scan's GeoJSON. Offscreen-renderable."""

        def __init__(self, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._features: "List[dict]" = []
            self._camera_items: list = []

            root = QVBoxLayout(self)
            self._btn_load = QPushButton("Load cameras.geojson…")
            self._btn_load.setToolTip("Open a saved Flock scan (the cameras.geojson a FlockSession writes).")
            self._btn_load.clicked.connect(self._on_load)
            root.addWidget(self._btn_load)

            self._scene = QGraphicsScene(self)
            self._view = QGraphicsView(self._scene)
            self._view.setRenderHint(QPainter.Antialiasing)
            self._view.setBackgroundBrush(QBrush(_BG))
            root.addWidget(self._view, 1)

            self._legend = QLabel("No detections loaded. Blue = few sightings · red = many.")
            self._legend.setStyleSheet("color:#8b949e;")
            root.addWidget(self._legend)

            self.set_geojson({"type": "FeatureCollection", "features": []})

        # ── data in ───────────────────────────────────────────────────
        def set_session(self, session: Any) -> None:
            """Populate from a live :class:`FlockSession` (uses its ``to_geojson``)."""
            self.set_geojson(session.to_geojson())

        def set_geojson(self, gj: dict) -> None:
            feats = gj.get("features", []) if isinstance(gj, dict) else []
            if not isinstance(feats, list):
                feats = []
            self._features = [f for f in feats if _valid_point(f)]
            self._rebuild()

        def load_geojson_file(self, path: str) -> int:
            """Load a cameras.geojson from *path*. Returns the number of cameras loaded (0 on any error)."""
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    gj = json.load(fh)
                self.set_geojson(gj)                        # inside the try -> "0 on ANY error" truly holds
            except Exception:  # noqa: BLE001 — a bad/missing/hostile file must not crash the tab
                self.set_geojson({"type": "FeatureCollection", "features": []})
                self._legend.setText("Could not read that file.")
                return 0
            return len(self._features)

        @property
        def camera_count(self) -> int:
            return len(self._features)

        # ── render ────────────────────────────────────────────────────
        def _rebuild(self) -> None:
            self._scene.clear()
            self._camera_items = []
            if not self._features:
                self._scene.setSceneRect(0, 0, _CANVAS_W, _CANVAS_H)
                self._legend.setText("No detections loaded. Blue = few sightings · red = many.")
                return
            pts = [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]) for f in self._features]
            fit = MercatorFit(pts, _CANVAS_W, _CANVAS_H)
            counts = [_as_count(f.get("properties")) for f in self._features]
            maxc = max(counts)
            for feat, c in zip(self._features, counts):
                lat = feat["geometry"]["coordinates"][1]
                lon = feat["geometry"]["coordinates"][0]
                x, y = fit.to_pixel(lat, lon)
                t = (c - 1) / (maxc - 1) if maxc > 1 else 0.0     # normalized density
                r8, g8, b8 = heat_color(t)
                radius = 6.0 + 12.0 * t                            # hotter -> larger dot
                item = self._scene.addEllipse(
                    x - radius, y - radius, 2 * radius, 2 * radius,
                    QPen(Qt.NoPen), QBrush(QColor(r8, g8, b8)))
                item.setOpacity(0.65)                              # semi-transparent -> overlaps accumulate
                self._camera_items.append(item)
            self._scene.setSceneRect(0, 0, _CANVAS_W, _CANVAS_H)
            self._legend.setText(
                f"{len(self._features)} camera(s) · blue = few sightings · red = many (up to {maxc}).")

        def render_native(self, width: int = _CANVAS_W, height: int = _CANVAS_H) -> "QImage":
            """Render the scene into a QImage — pure, offscreen-testable (no window needed)."""
            img = QImage(width, height, QImage.Format_ARGB32)
            img.fill(_BG)
            p = QPainter(img)
            self._scene.render(p)
            p.end()
            return img

        # ── load button (dialog; not unit-tested) ─────────────────────
        def _on_load(self) -> None:
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Flock scan (cameras.geojson)", "", "GeoJSON (*.geojson *.json);;All files (*)")
            if path:
                self.load_geojson_file(path)

except ImportError:  # PyQt5 unavailable — the pure projection core above is still importable/testable.
    FlockHeatmapTab = None  # type: ignore[assignment,misc]
