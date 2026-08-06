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

from src.core import flock, flock_osm, map_tiles

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


def zoom_step(angle_delta: int, base: float = 1.2) -> float:
    """Map a mouse-wheel ``angleDelta().y()`` to a zoom multiplier: one notch (±120) scales by *base*
    (in) or ``1/base`` (out); 0 is a no-op (1.0). Pure + Qt-free so the slippy-map zoom is unit-testable."""
    return base ** (angle_delta / 120.0)


def clamped_zoom_factor(cur_scale: float, factor: float, min_scale: float, max_scale: float) -> float:
    """Wheel-zoom factor to actually apply, given the view's current transform scale. Blocks a notch ONLY
    when the view is already at/past a limit AND the notch would push further past it — so a fit-scale that
    lands OUTSIDE [min,max] can still be zoomed back toward the band. Returns 1.0 (no-op) when blocked.

    This fixes "I can't scroll to zoom": the scene spans the whole world, so ``fitInView`` on a wide camera
    set (a city spans well under 1/1000th of it) — and especially with the world basemap on — settles at a
    scale far BELOW ``min_scale``. The old clamp rejected ANY result outside [min,max], which trapped that
    case in BOTH directions (zoom-in still landed below min, zoom-out went further below) => zoom dead."""
    if factor > 1.0 and cur_scale >= max_scale:
        return 1.0
    if factor < 1.0 and cur_scale <= min_scale:
        return 1.0
    return factor


def dots_in_rect(dots, left, top, right, bottom):
    """Indices of the ``(x, y, radius, ...)`` dots whose bounding box intersects the [left,top,right,bottom]
    rect. This is the viewport cull: the camera layer paints only these, so with thousands of cameras the
    off-screen ones cost nothing to draw. Pure + Qt-free so it's unit-testable without a scene."""
    out = []
    for i, d in enumerate(dots):
        x, y, r = d[0], d[1], d[2]
        if x + r < left or x - r > right or y + r < top or y - r > bottom:
            continue
        out.append(i)
    return out


# The full web-mercator world [0,1]^2 mapped to a fixed pixel square, so every layer — the cameras now,
# the world basemap next — lives in ONE shared coordinate space that stays aligned at any pan/zoom. The
# constant is Earth's equatorial circumference in metres, so scene units are ~metres near the equator.
_WORLD_PX = 40_075_016.0

# "You are here" GPS marker fill: bright cyan while the GPS is live-streaming a fix, muted grey once the fix
# goes stale (the live scan stopped) so a stale position doesn't read as your current one.
_GPS_LIVE_FILL = "#22d3ee"
_GPS_STALE_FILL = "#6e7681"

# Wi-Fi AP dots for the wardrive layer (Slice D): flat green, off the cameras' blue->red heat scale
# so the two layers on the one map read as distinct sources at a glance.
_AP_FILL = "#3fb950"

# Drive-trail polyline (wardriving-v2 S3): amber, distinct from the AP green, the pin cyan, and the
# cameras' blue->red heat -- so the breadcrumb behind you reads as its own layer at a glance.
_TRAIL_FILL = "#f59e0b"

# Max view span (degrees) for a one-shot OSM import — refuses whole-planet queries so the shared
# free Overpass API is never asked for the world; the user zooms to a real area first.
_OSM_MAX_SPAN_DEG = 2.0


def world_px(lat: float, lon: float, world: float = _WORLD_PX) -> Tuple[float, float]:
    """Project (lat, lon) into the shared global-mercator pixel plane [0, world]. Pure + Qt-free — the
    single projection both the camera layer and (Phase B) the world basemap are placed through."""
    x, y = web_mercator(lat, lon)
    return x * world, y * world


def world_px_inv(px_x: float, px_y: float, world: float = _WORLD_PX) -> Tuple[float, float]:
    """Inverse of :func:`world_px`: a shared-plane pixel (x, y) back to (lat, lon). Pure + Qt-free —
    lets a caller read the geographic bbox of the current view for an OSM/Overpass query."""
    x, y = px_x / world, px_y / world
    lon = x * 360.0 - 180.0
    s = math.tanh((0.5 - y) * 2.0 * math.pi)          # exact inverse of web_mercator's y
    lat = math.degrees(math.asin(max(-1.0, min(1.0, s))))
    return lat, lon


def basemap_paths(geojson: Any, world: float = _WORLD_PX) -> "List[List[Tuple[float, float]]]":
    """Project a Polygon/MultiPolygon FeatureCollection's rings into shared-plane point lists — each inner
    list is one closed ring's (x, y) world_px points, ready for a QPainterPath. Pure + Qt-free so the
    basemap projection is unit-testable. GeoJSON coords are [lon, lat]; non-polygon / short / non-finite
    rings are skipped (a hostile/partial world file can't crash the map)."""
    rings: "List[List[Tuple[float, float]]]" = []
    feats = geojson.get("features") if isinstance(geojson, dict) else None
    for feat in feats or []:
        geom = (feat or {}).get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue
        for poly in polys or []:
            for ring in poly or []:
                pts: "List[Tuple[float, float]]" = []
                for c in ring or []:
                    if not (isinstance(c, (list, tuple)) and len(c) >= 2):
                        continue
                    lon, lat = c[0], c[1]
                    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                           for v in (lat, lon)):
                        pts.append(world_px(lat, lon, world))
                if len(pts) >= 3:
                    rings.append(pts)
    return rings


def load_world_basemap() -> dict:
    """Load the bundled Natural Earth 110m world basemap (public domain). Returns an empty
    FeatureCollection if it's missing/unreadable — the basemap layer simply won't draw."""
    try:
        from src.core.resources import resource_path
        with open(resource_path("src", "config", "maps", "world_110m.geojson"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"type": "FeatureCollection", "features": []}


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


# ── Drive trail (wardriving-v2 S3) ────────────────────────────────────
# A breadcrumb of accepted GPS fixes, drawn behind the "you are here" pin. Append + decimate logic
# is PURE + Qt-free (unit-testable headless, like world_px); the _TrailLayer projects each stored
# (lat, lon) through world_px at paint, into the same shared plane as the cameras + AP dots.
_TRAIL_MIN_MOVE_DEG = 3.0e-5    # ~3.3 m at the equator: coarse enough to skip a parked cloud,
_TRAIL_MAX_POINTS = 5000        # fine enough to trace a street. Cap a long drive, then decimate.


def trail_accept(last: "Optional[Tuple[float, float]]", lat: float, lon: float,
                 min_move_deg: float = _TRAIL_MIN_MOVE_DEG) -> bool:
    """Whether a GPS fix (lat, lon) should be the next trail breadcrumb, given the trail's current
    *last* point (or None when empty). False for a non-finite / out-of-range fix, for Null-Island
    (0, 0) (the "no fix" sentinel), or a fix within *min_move_deg* (Chebyshev -- max |dlat|,|dlon|,
    no trig) of *last*, so standing still never grows the trail. Pure + Qt-free."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    if last is None:
        return True
    return max(abs(lat - last[0]), abs(lon - last[1])) >= min_move_deg


def trail_decimate(trail: "List[Tuple[float, float]]",
                   max_points: int = _TRAIL_MAX_POINTS) -> "List[Tuple[float, float]]":
    """Down-sample *trail* to <= *max_points*: keep every k-th point (k = ceil(n / max_points)) so a
    very long drive stays bounded in memory + paint while keeping the trail's shape (the live pin,
    drawn separately, still marks the exact current position). Pure; unchanged when within the cap;
    a max_points < 1 is treated as 1."""
    n = len(trail)
    cap = max(1, int(max_points))
    if n <= cap:
        return trail
    k = (n + cap - 1) // cap          # ceil(n / cap): the stride that brings len down to <= cap
    return trail[::k]


def _is_sqlite_db(path: str) -> bool:
    """True if *path* starts with the 16-byte SQLite header magic. Kismet's native ``.kismet`` is
    a SQLite DB that can't ride the text ``wardrive_points`` dispatcher, so the import handler
    sniffs this to route it to ``kismet_db_to_points`` (by path). Any read error -> False. Pure."""
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except Exception:  # noqa: BLE001 — a missing/unreadable file simply isn't a SQLite DB
        return False


def trail_to_geojson(trail: "List[Tuple[float, float]]") -> dict:
    """Serialize a drive trail [(lat, lon), ...] as a GeoJSON LineString FeatureCollection (coords
    [lon, lat] per the GeoJSON spec) so a saved drive can be reloaded + replayed, or opened
    in any GIS tool. Pure + Qt-free (owner-call #2's persist/replay foundation)."""
    coords = [[lon, lat] for lat, lon in trail]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "drive-trail"},
         "geometry": {"type": "LineString", "coordinates": coords}}]}


def trail_from_geojson(gj: Any) -> "List[Tuple[float, float]]":
    """Parse a GeoJSON (as :func:`trail_to_geojson` writes) back to [(lat, lon), ...]: reads every
    LineString's coordinates ([lon, lat]) across the collection. Tolerant -- a non-dict feature,
    a missing/short/non-finite or out-of-range coord is skipped; returns [] on anything unusable,
    never raises. Pure + Qt-free, so replay is unit-testable headless."""
    out: "List[Tuple[float, float]]" = []
    feats = gj.get("features", []) if isinstance(gj, dict) else []
    for feat in (feats if isinstance(feats, list) else []):
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            continue
        for c in (geom.get("coordinates") or []):
            if not (isinstance(c, (list, tuple)) and len(c) >= 2):
                continue
            lon, lat = c[0], c[1]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for v in (lat, lon)):
                continue
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                out.append((float(lat), float(lon)))
    return out


def _flock_pump(session: Any, gps_line: str, dev_line: str, checkpoint_path: str = "") -> bool:
    """One live-capture step — shared by the driving worker and unit-testable with no Qt or serial.

    Feed an optional GPS NMEA line (updates the session's sticky fix), then an optional Flock-You device
    line, into *session*. If the device line records a new or relocated camera, checkpoint the run to
    *checkpoint_path* (best-effort) and return True; otherwise return False.
    """
    if gps_line:
        session.update_gps(gps_line)
    added = bool(dev_line) and session.observe(dev_line)
    if added and checkpoint_path:
        try:
            session.checkpoint(checkpoint_path)
        except OSError:
            pass
    return added


def _fix_status_text(fix: Any, has: bool) -> str:
    """One-line GPS status for the live-scan readout: ``'<lat>, <lon>  ·  N sats · HDOP x.x'`` (the quality
    suffix is shown only when the receiver reports it — 0/0 on an RMC-only or older receiver), or ``'No Fix'``.
    Pure + unit-tested; the worker loops that build this run under serial I/O and are not covered."""
    if not has or fix is None:
        return "No Fix"
    txt = f"{fix.lat:.5f}, {fix.lon:.5f}"
    if fix.sats or fix.hdop:
        txt += f"  ·  {fix.sats} sats · HDOP {fix.hdop:.1f}"
    return txt


# ── Qt widget (the pure core above stays Qt-free; the widget is optional) ──

try:  # allow importing the pure core (web_mercator/MercatorFit/heat_color) even without PyQt5
    from PyQt5.QtCore import Qt, QThread, QRectF, QTimer, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
    from PyQt5.QtWidgets import (
        QCheckBox,
        QComboBox,
        QGraphicsItem,
        QGraphicsItemGroup,
        QGraphicsPathItem,
        QGraphicsPixmapItem,
        QGraphicsScene,
        QGraphicsView,
        QHBoxLayout,
        QLabel,
        QPlainTextEdit,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    _BG = QColor("#0d1117")
    _CANVAS_W, _CANVAS_H = 800, 600

    class _PannableGraphicsView(QGraphicsView):
        """A QGraphicsView you can drag to pan and wheel to zoom toward the cursor — a slippy map, not a
        static fit. Total zoom is clamped to the transform scale so the scene can't be flung off-screen or
        zoomed into the void; the clamp reads the live transform, so fitInView()/reset compose cleanly."""

        # _MAX_SCALE caps zoom-IN. The zoom-OUT floor is NOT a fixed number — it's whatever scale makes the
        # whole scene fit (_min_zoom), so you can always pull back to see everything (the world with the
        # basemap on; the cameras + margin without). _MIN_SCALE is only a hard numerical floor / the fallback
        # when the view isn't sized yet. (A fixed 0.15 floor used to TRAP zoom-out: a real camera spread — and
        # the world basemap — fits BELOW 0.15, so every zoom-out notch was rejected: "I can't zoom out.")
        _MIN_SCALE, _MAX_SCALE = 1e-6, 60.0

        def __init__(self, scene) -> None:
            super().__init__(scene)
            self.setDragMode(QGraphicsView.ScrollHandDrag)          # click-drag to pan
            self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)  # zoom toward the cursor
            self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
            self._user_zoomed = False   # once the user wheels, stop auto-refitting on resize
            self._pending_fit = None    # QRectF to (re)fit until then
            self._refitting = False     # resizeEvent re-entrancy guard
            self.on_view_changed = None  # optional callback fired after a zoom (the tab reloads map tiles)

        def fit(self, rect) -> None:
            """Frame *rect* now AND remember it, so the first REAL resize after the tab is shown re-fits
            it correctly. Fixes the launch-render bug: the tab is built while hidden, so at construction
            the viewport has no size and fitInView frames against a near-zero viewport (wrong scale) and
            was never recomputed. Re-fitting on the next resize (below) makes it correct on first paint."""
            from PyQt5.QtCore import QRectF
            self._pending_fit = QRectF(rect)
            self._user_zoomed = False
            self.fitInView(rect, Qt.KeepAspectRatio)

        def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt override)
            super().resizeEvent(ev)
            if self._pending_fit is not None and not self._user_zoomed and not self._refitting:
                self._refitting = True
                try:
                    self.fitInView(self._pending_fit, Qt.KeepAspectRatio)
                finally:
                    self._refitting = False

        def _min_zoom(self) -> float:
            """Zoom-OUT floor: the scale at which the whole scene rect fits the viewport, so you can pull all
            the way back to see everything but not zoom out into empty space beyond it. Falls back to the hard
            floor when the scene/viewport isn't sized yet."""
            sr, vp = self.sceneRect(), self.viewport().rect()
            if sr.width() > 0 and sr.height() > 0 and vp.width() > 0 and vp.height() > 0:
                return max(self._MIN_SCALE, min(vp.width() / sr.width(), vp.height() / sr.height()))
            return self._MIN_SCALE

        def wheelEvent(self, ev) -> None:  # noqa: N802 (Qt override)
            f = clamped_zoom_factor(self.transform().m11(), zoom_step(ev.angleDelta().y()),
                                    self._min_zoom(), self._MAX_SCALE)
            if f != 1.0:
                self.scale(f, f)
                self._user_zoomed = True   # the user took control — stop auto-refitting on resize
                if callable(self.on_view_changed):
                    self.on_view_changed()   # zoom changed the scale -> the tab reloads tiles for the new zoom
            ev.accept()   # consume the notch so it can't fall through to the scrollbars as a pan

    class _CameraLayer(QGraphicsItem):
        """Every camera dot drawn as ONE scene item instead of N ellipse items. For a big scan (a nationwide
        DeFlock export is tens of thousands of points) that means one entry in the scene's BSP index and only
        a float list in memory, not thousands of QGraphicsItems. paint() draws only the dots inside the
        exposed region, so QGraphicsView's own scroll/zoom repaint gives free viewport culling — off-screen
        cameras are never processed. dots = list of (x, y, radius, QColor); bounds = full extent QRectF."""

        def __init__(self, dots, bounds) -> None:
            super().__init__()
            self._dots = dots
            self._bounds = bounds

        def boundingRect(self):  # noqa: N802 (Qt override)
            return self._bounds

        def paint(self, painter, option, widget=None) -> None:  # noqa: N802 (Qt override)
            e = option.exposedRect
            painter.setPen(Qt.NoPen)
            painter.setOpacity(0.65)                              # overlaps accumulate, as before
            for i in dots_in_rect(self._dots, e.left(), e.top(), e.right(), e.bottom()):
                x, y, r, color = self._dots[i]
                painter.setBrush(color)
                painter.drawEllipse(QRectF(x - r, y - r, 2 * r, 2 * r))

    class _TrailLayer(QGraphicsItem):
        """The drive trail as ONE scene item: a polyline through the GPS breadcrumbs in the shared
        world_px plane (a float list + a cosmetic-pen path, not N line items). points = list of
        (x, y) world_px; bounds = the full extent. A COSMETIC pen keeps the line a fixed on-screen
        width at any zoom, so it reads the same framed on a block or a whole drive."""

        def __init__(self, points, bounds) -> None:
            super().__init__()
            self._points = points
            self._bounds = bounds

        def boundingRect(self):  # noqa: N802 (Qt override)
            return self._bounds

        def paint(self, painter, option, widget=None) -> None:  # noqa: N802 (Qt override)
            if len(self._points) < 2:
                return
            pen = QPen(QColor(_TRAIL_FILL))
            pen.setCosmetic(True)                                # fixed on-screen width at any zoom
            pen.setWidthF(2.5)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(self._points[0][0], self._points[0][1])
            for x, y in self._points[1:]:
                path.lineTo(x, y)
            painter.drawPath(path)

    class _FlockWorker(QThread):
        """Drive a live Flock scan on its own thread: read GPS + the Flock-You device serial, feed them
        through a FlockSession, checkpoint on each new/relocated camera, and emit the updated cameras GeoJSON
        so the map can redraw. Mirrors wardrive_tab._WardriveWorker's lifecycle (a stop flag + finally-close
        of both ports). The Flock-You firmware is a passive receiver, so nothing is ever written to it.
        """
        status = pyqtSignal(str, int)     # gps-fix text, camera count
        updated = pyqtSignal(dict)        # cameras GeoJSON, emitted on each new/relocated camera
        location = pyqtSignal(float, float, bool)  # gps lat, lon, has_fix -> drives the "you are here" marker
        line = pyqtSignal(str)
        stopped = pyqtSignal()

        def __init__(self, gps_port: str, gps_baud: int, dev_port: str, dev_baud: int,
                     checkpoint_path: str = "") -> None:
            super().__init__()
            self._gps_port, self._gps_baud = gps_port, gps_baud
            self._dev_port, self._dev_baud = dev_port, dev_baud
            self._checkpoint_path = checkpoint_path
            self._stop = False
            self.session = flock.FlockSession()

        def stop(self) -> None:
            self._stop = True

        def run(self) -> None:
            try:
                import serial
            except Exception as exc:  # noqa: BLE001
                self.line.emit(f"pyserial unavailable: {exc}")
                self.stopped.emit()
                return
            gps = dev = None
            last = ("", -1)
            last_pos = None   # position-only dedup for the marker, kept separate from the status text (which
                              # also carries GPS quality) so sat-count/HDOP noise doesn't churn the pin
            try:
                if self._gps_port:
                    gps = serial.Serial(self._gps_port, self._gps_baud, timeout=0.5)
                dev = serial.Serial(self._dev_port, self._dev_baud, timeout=0.5)
                self.line.emit("Flock scan started — waiting for a GPS fix and detections")
                while not self._stop:
                    gl = ""
                    if gps is not None:
                        try:
                            gl = gps.readline().decode("ascii", "replace").strip()
                        except Exception:  # noqa: BLE001
                            gl = ""
                    try:
                        dl = dev.readline().decode("utf-8", "replace").strip()
                    except Exception:  # noqa: BLE001
                        dl = ""
                    if _flock_pump(self.session, gl, dl, self._checkpoint_path):
                        self.updated.emit(self.session.to_geojson())
                        self.line.emit(f"+ camera ({self.session.camera_count} located)")
                    fix = self.session.fix
                    has = bool(fix and fix.has_fix)
                    # Status shows position + GPS quality (sats/HDOP) and re-emits whenever any of that changes.
                    ftxt = _fix_status_text(fix, has)
                    status_cur = (ftxt, self.session.camera_count)
                    if status_cur != last:
                        self.status.emit(ftxt, self.session.camera_count)
                        last = status_cur
                    # The "you are here" marker only cares about POSITION, so dedup its feed separately — a
                    # stationary receiver whose sat count / HDOP flickers must not churn the pin (or, with
                    # Follow on, keep re-centring). Host-side toggle decides whether the marker is shown.
                    pos_cur = (round(fix.lat, 6), round(fix.lon, 6), True) if has else (None, None, False)
                    if pos_cur != last_pos:
                        self.location.emit(fix.lat if has else 0.0, fix.lon if has else 0.0, has)
                        last_pos = pos_cur
            except Exception as exc:  # noqa: BLE001
                self.line.emit(f"flock scan error: {exc}")
            finally:
                for port in (dev, gps):
                    try:
                        if port is not None:
                            port.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.line.emit(f"Flock scan stopped — {self.session.camera_count} camera(s) located")
                self.stopped.emit()

    class _GpsWorker(QThread):
        """Standalone GPS reader — opens ONLY the NMEA port, parses fixes, emits ``location``. Lets the
        'My location (GPS)' pin track your position WITHOUT a full Flock scan (the scan is otherwise the only
        thing that opens the GPS port). Mirrors _FlockWorker's stop-flag + finally-close lifecycle; it reads
        nothing but GPS and writes nothing. Never share the port with a running scan — the tab guards that."""
        location = pyqtSignal(float, float, bool)   # lat, lon, has_fix
        line = pyqtSignal(str)
        stopped = pyqtSignal()

        def __init__(self, gps_port: str, gps_baud: int = 9600) -> None:
            super().__init__()
            self._gps_port, self._gps_baud = gps_port, gps_baud
            self._stop = False
            self.session = flock.FlockSession()

        def stop(self) -> None:
            self._stop = True

        def run(self) -> None:  # pragma: no cover — serial I/O loop (the parse it drives is unit-tested)
            try:
                import serial
            except Exception as exc:  # noqa: BLE001 — pyserial missing
                self.line.emit(f"GPS tracking unavailable (pyserial): {exc}")
                self.stopped.emit()
                return
            gps = None
            last = None
            try:
                gps = serial.Serial(self._gps_port, self._gps_baud, timeout=0.5)
                self.line.emit(f"GPS tracking started on {self._gps_port} — waiting for a fix")
                while not self._stop:
                    try:
                        gl = gps.readline().decode("ascii", "replace").strip()
                    except Exception:  # noqa: BLE001 — a bad line must not kill the reader
                        gl = ""
                    if gl:
                        self.session.update_gps(gl)
                    fix = self.session.fix
                    has = bool(fix and fix.has_fix)
                    cur = (round(fix.lat, 6), round(fix.lon, 6)) if has else None
                    if cur != last:
                        self.location.emit(fix.lat if has else 0.0, fix.lon if has else 0.0, has)
                        last = cur
            except Exception as exc:  # noqa: BLE001 — port busy/denied/unplugged
                self.line.emit(f"GPS tracking error: {exc}")
            finally:
                if gps is not None:
                    try:
                        gps.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.line.emit("GPS tracking stopped")
                self.stopped.emit()

    class _TileFetchWorker(QThread):
        """Fetch a batch of missing street-map tiles off the GUI thread, emitting each as it lands. Only
        spawned when online tiles are enabled AND some visible tiles aren't cached — cache hits are read
        synchronously on the GUI thread and never reach here. Mirrors the other workers' stop-flag lifecycle;
        it does network+disk I/O only through the passed TileCache, which swallows every error (a failed
        tile is just skipped, so the map leaves that square blank)."""
        tile = pyqtSignal(int, int, int, bytes)   # x, y, z, PNG/JPEG bytes
        done = pyqtSignal()

        def __init__(self, cache, tiles) -> None:
            super().__init__()
            self._cache = cache
            self._tiles = list(tiles)
            self._stop = False

        def stop(self) -> None:
            self._stop = True

        def run(self) -> None:  # pragma: no cover — network/disk loop; the cache + tile math are unit-tested
            for (x, y, z) in self._tiles:
                if self._stop:
                    break
                data = self._cache.get_or_fetch(x, y, z, allow_network=True)
                if data and not self._stop:
                    self.tile.emit(x, y, z, data)
            self.done.emit()

    class _OsmImportWorker(QThread):
        """User-initiated OSM/Overpass ALPR import off the GUI thread (mirrors _TileFetchWorker).

        Runs ONE gated Overpass query via flock_osm.fetch_alpr_geojson (cached/rate-limited there)
        and emits the cameras GeoJSON; a network/parse error is reported, not raised."""
        imported = pyqtSignal(dict)   # cameras GeoJSON
        failed = pyqtSignal(str)

        def __init__(self, bbox, cache_path, fetcher=None) -> None:
            super().__init__()
            self._bbox = bbox
            self._cache_path = cache_path
            self._fetcher = fetcher

        def run(self) -> None:
            try:
                gj = flock_osm.fetch_alpr_geojson(
                    self._bbox, self._cache_path, fetcher=self._fetcher)
                self.imported.emit(gj)
            except Exception as exc:  # noqa: BLE001 — a network/parse error must not crash the tab
                self.failed.emit(str(exc))

    class FlockHeatmapTab(QWidget):
        """A heatmap of located ALPR cameras from a Flock scan's GeoJSON. Offscreen-renderable."""

        def __init__(self, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._features: "List[dict]" = []
            self._camera_layer = None             # the single QGraphicsItem holding every camera dot (or None)
            self._camera_bounds = QRectF()        # full extent of the camera set, for reset_view/render framing
            # Slice D wardrive AP layer: (lat, lon, ssid, bssid) points from a WiGLE CSV, a second
            # _CameraLayer in the SAME world_px plane; retained like _features across unload/reload.
            self._wardrive_points: "List[tuple]" = []
            self._wardrive_layer = None           # every AP dot in one QGraphicsItem (or None)
            self._wardrive_bounds = QRectF()      # full extent of the AP set, for framing/render
            self._live_worker = None
            self._latest_gj: "Optional[dict]" = None
            self._visible = False
            self._unloaded = False   # True while the scene is freed for a backgrounded tab (see hideEvent)
            self._osm_workers: "List[_OsmImportWorker]" = []   # retained so a run isn't GC'd
            self._osm_fetcher = None   # None -> real network; tests inject a fake fetcher(url)->str

            root = QVBoxLayout(self)
            # Honest WIP notice: the offline known-camera catalog (F3) is not built yet — the map
            # only shows detections that come from a live scan, a loaded file, or an OSM import.
            self._wip_banner = QLabel(
                "⚠ Work in progress — the offline known-camera catalog (F3) isn't built yet. "
                "Today this maps only live-scan, loaded, and OSM-imported detections.")
            self._wip_banner.setObjectName("flockWipBanner")
            self._wip_banner.setWordWrap(True)
            self._wip_banner.setStyleSheet(
                "color:#d29922;background:#1c1908;border:1px solid #493c0e;"
                "border-radius:4px;padding:5px 8px;font-weight:600;")
            root.addWidget(self._wip_banner)
            self._last_flock_size: "Optional[str]" = None   # Wave-3: size class (debounce)
            from src.ui.qt import primer
            primer.apply_primer(self)   # mockup formula: control-row .field/.btn (map canvas untouched)
            _note = QLabel(
                "Cameras appear from a live scan or a loaded cameras.geojson, on a real street "
                "basemap. The map stays offline by default; turn on “Online tiles” once, with "
                "internet, and pan your area to cache the streets — after that it works offline. "
                "“Import from OSM” pulls crowdsourced ALPR locations for the view from OSM "
                "(awareness-only; ODbL).")
            _note.setWordWrap(True)
            _note.setStyleSheet("color:#8b949e;padding:4px 2px;")
            root.addWidget(_note)
            file_row = QHBoxLayout()
            self._file_row = file_row   # Wave-3: control rows stack on a compact canvas
            self._btn_load = QPushButton("Load cameras.geojson…")
            self._btn_load.setToolTip("Open a saved Flock scan (the cameras.geojson a FlockSession writes).")
            self._btn_load.clicked.connect(self._on_load)
            self._btn_folder = QPushButton("Open data folder")
            self._btn_folder.setToolTip("Open the folder where live Flock scans are saved (~/.cyber-controller/flock).")
            self._btn_folder.clicked.connect(self._open_data_folder)
            self._btn_export = QPushButton("Export CSV…")
            self._btn_export.setToolTip("Save the cameras currently on the map as a spreadsheet-friendly CSV "
                                        "(lat, lon, MAC, SSID, RSSI, channel, first/last seen, count).")
            self._btn_export.clicked.connect(self._on_export_csv)
            self._btn_import_osm = QPushButton("Import from OSM")
            self._btn_import_osm.setToolTip(
                "Import crowdsourced ALPR camera locations for the current view from OpenStreetMap "
                "(DeFlock/Overpass). Awareness-only; © OpenStreetMap (ODbL). Zoom in first.")
            self._btn_import_osm.clicked.connect(self._on_import_osm)
            self._btn_import_wd = QPushButton("Import wardrive log…")
            self._btn_import_wd.setToolTip("Load a wardrive log (WiGLE, Kismet .netxml, Biscuit) "
                                           "onto the map as located Wi-Fi APs. Awareness-only.")
            self._btn_import_wd.clicked.connect(self._on_import_wardrive)
            self._btn_save_trail = QPushButton("Save trail…")
            self._btn_save_trail.setToolTip("Save the drive trail as GeoJSON, to replay later "
                                            "or open in a GIS tool. Awareness-only.")
            self._btn_save_trail.clicked.connect(self._on_save_trail)
            self._btn_load_trail = QPushButton("Load trail…")
            self._btn_load_trail.setToolTip("Replay a saved drive trail (GeoJSON) on the map.")
            self._btn_load_trail.clicked.connect(self._on_load_trail)
            file_row.addWidget(self._btn_load)
            file_row.addWidget(self._btn_folder)
            file_row.addWidget(self._btn_export)
            file_row.addWidget(self._btn_import_osm)
            file_row.addWidget(self._btn_import_wd)
            file_row.addWidget(self._btn_save_trail)
            file_row.addWidget(self._btn_load_trail)
            file_row.addStretch(1)
            root.addLayout(file_row)

            # ── Live scan controls (F5 live driving loop) ──
            live_row = QHBoxLayout()
            self._live_row = live_row
            self._gps_combo = QComboBox()
            self._gps_combo.setToolTip("GPS (NMEA) serial port — optional; without a fix the map stays empty.")
            self._dev_combo = QComboBox()
            self._dev_combo.setToolTip("Flock-You device serial port (the passive 2.4 GHz sniffer).")
            self._btn_ports = QPushButton("⟳")
            self._btn_ports.setToolTip("Rescan serial ports.")
            self._btn_ports.clicked.connect(self._refresh_ports)
            self._btn_live = QPushButton("Start scan")
            self._btn_live.setToolTip("Drive a live Flock scan — cameras drop onto the map as they're located.")
            self._btn_live.clicked.connect(self._toggle_live)
            self._live_status = QLabel("Idle")
            self._live_status.setStyleSheet("color:#8b949e;")
            for w in (QLabel("GPS:"), self._gps_combo, QLabel("Device:"), self._dev_combo,
                      self._btn_ports, self._btn_live):
                live_row.addWidget(w)
            live_row.addWidget(self._live_status, 1)
            root.addLayout(live_row)
            self._refresh_ports()

            self._scene = QGraphicsScene(self)
            self._view = _PannableGraphicsView(self._scene)
            self._view.setRenderHint(QPainter.Antialiasing)
            self._view.setBackgroundBrush(QBrush(_BG))
            root.addWidget(self._view, 1)

            # Map controls: the view is now a slippy map (drag to pan, wheel to zoom); "Reset view"
            # re-frames every camera so you can always get back to the whole set after exploring.
            map_row = QHBoxLayout()
            self._map_row = map_row
            self._btn_reset_view = QPushButton("Reset view")
            self._btn_reset_view.setToolTip("Re-frame all cameras (drag to pan · scroll to zoom).")
            self._btn_reset_view.clicked.connect(self.reset_view)
            self._chk_basemap = QCheckBox("World basemap")
            self._chk_basemap.setToolTip("Show a muted world-countries outline under the cameras, so a scan sits "
                                         "in real-world context. Zoom/pan out to see it; toggle off for a plain map.")
            self._chk_basemap.setChecked(True)
            self._chk_basemap.stateChanged.connect(lambda _s: self._rebuild())
            # Slice D layer toggles (owner-call #3: a Wi-Fi / Flock / both control). Cameras + APs
            # show/hide independently; both default on, so the pre-Slice-D behavior is unchanged.
            self._chk_flock = QCheckBox("Flock cameras")
            self._chk_flock.setToolTip("Show the located Flock ALPR cameras (blue->red heatmap). "
                                       "Turn off to see only the Wi-Fi APs.")
            self._chk_flock.setChecked(True)
            self._chk_flock.stateChanged.connect(lambda _s: self._rebuild())
            self._chk_wardrive = QCheckBox("Wi-Fi APs")
            self._chk_wardrive.setToolTip("Show wardrive Wi-Fi APs (from a WiGLE CSV) as a green layer "
                                          "over the cameras. Awareness-only: maps APs, drives no device.")
            self._chk_wardrive.setChecked(True)   # a loaded wardrive shows at once
            self._chk_wardrive.stateChanged.connect(lambda _s: self._rebuild())
            self._chk_trail = QCheckBox("Trail")
            self._chk_trail.setToolTip("Draw a drive trail behind the live GPS position as you go "
                                       "(session-only). Awareness-only -- traces where you went.")
            self._chk_trail.setChecked(True)
            self._chk_trail.stateChanged.connect(lambda _s: self._update_trail_layer())
            self._chk_streetmap = QCheckBox("Street map")
            self._chk_streetmap.setToolTip("Show a real street basemap (OpenStreetMap tiles) under the cameras, "
                                           "so a scan sits on actual roads. Offline-first: cached tiles render "
                                           "with no network. On by default.")
            self._chk_streetmap.setChecked(True)
            self._chk_streetmap.stateChanged.connect(lambda _s: self._on_streetmap_toggled())
            self._chk_online = QCheckBox("Online tiles")
            self._chk_online.setToolTip("OFF by default (airgapped). Turn on, with internet, to download the street "
                                        "tiles for the area you're viewing and cache them for offline use later. "
                                        "Only the current view is fetched — never a bulk download. Off = fully "
                                        "offline (cached tiles only, no network).")
            self._chk_online.setChecked(False)     # airgapped-by-default (owner 2026-07-21): no network unless asked
            self._chk_online.stateChanged.connect(lambda _s: self._schedule_tiles())
            self._provider_combo = QComboBox()
            self._provider_combo.setToolTip("Basemap style. CARTO's muted maps read cleanest; "
                                            "the map stays offline unless 'Online tiles' is on.")
            for _pkey, _prov in map_tiles.PROVIDERS.items():
                self._provider_combo.addItem(_prov.label, _pkey)
            self._chk_mylocation = QCheckBox("My location (GPS)")
            self._chk_mylocation.setToolTip("When a GPS is streaming (during a live scan), drop a 'you are here' "
                                            "marker at your real-world position. Off by default; needs a GPS fix.")
            self._chk_mylocation.setChecked(False)
            self._chk_mylocation.stateChanged.connect(lambda _s: self._on_mylocation_toggled())
            self._chk_follow = QCheckBox("Follow")
            self._chk_follow.setToolTip("Keep the map centred on your GPS position as it updates (like a car "
                                        "sat-nav). Needs 'My location (GPS)' on.")
            self._chk_follow.setChecked(False)
            self._chk_follow.stateChanged.connect(lambda _s: self.center_on_me())
            self._btn_center = QPushButton("Center on me")
            self._btn_center.setToolTip("Recentre the map on your GPS position once (after you've panned away).")
            self._btn_center.clicked.connect(self.center_on_me)
            self._chk_unload = QCheckBox("Unload when off-tab")
            self._chk_unload.setToolTip("Free the map's memory (cameras + basemap) while you're on another tab, then "
                                        "rebuild it when you return — so a big scan doesn't keep eating CPU/RAM in the "
                                        "background. A live scan keeps recording either way. On by default.")
            self._chk_unload.setChecked(True)
            map_row.addWidget(self._btn_reset_view)
            map_row.addWidget(self._chk_streetmap)
            map_row.addWidget(self._chk_online)
            map_row.addWidget(self._provider_combo)
            map_row.addWidget(self._chk_basemap)
            map_row.addWidget(self._chk_flock)
            map_row.addWidget(self._chk_wardrive)
            map_row.addWidget(self._chk_trail)
            map_row.addWidget(self._chk_mylocation)
            map_row.addWidget(self._chk_follow)
            map_row.addWidget(self._btn_center)
            map_row.addWidget(self._chk_unload)
            map_row.addStretch(1)
            root.addLayout(map_row)

            # Street basemap (WS-4): real XYZ map tiles under the cameras, in the SAME world_px plane, so a scan
            # sits on actual roads. Offline-first — a disk cache serves tiles with no network; "Online tiles"
            # opts into fetching the current view's missing tiles and caching them. The loader is viewport-driven
            # and debounced so panning/zooming coalesces into one refresh; fetches run on _TileFetchWorker. Built
            # BEFORE the attribution label below, which reads the active provider's required credit.
            self._tile_cache = map_tiles.TileCache()
            self._tile_group = None                  # QGraphicsItemGroup holding placed tile pixmaps
            self._tile_items: "dict" = {}            # (x,y,z) -> QGraphicsPixmapItem currently placed
            self._tile_needed: "set" = set()         # (x,y,z) the last refresh wants — guards stale async placement
            self._tile_worker = None                 # the ACTIVE tile fetcher (or None)
            self._tile_workers: "list" = []          # every live fetcher incl. superseded-but-still-finishing ones,
            #                                          retained so a running QThread is never GC'd mid-run (reaped
            #                                          on its finished signal); shutdown() joins them all
            self._tile_timer = QTimer(self)
            self._tile_timer.setSingleShot(True)
            self._tile_timer.setInterval(180)        # coalesce a pan/zoom burst into one tile refresh
            self._tile_timer.timeout.connect(self._update_tiles)
            self._view.on_view_changed = self._schedule_tiles
            self._view.horizontalScrollBar().valueChanged.connect(self._schedule_tiles)
            self._view.verticalScrollBar().valueChanged.connect(self._schedule_tiles)

            self._legend = QLabel("No detections loaded. Blue = few sightings · red = many.")
            self._legend.setStyleSheet("color:#8b949e;")
            root.addWidget(self._legend)

            # Map-tile attribution — OSM's ODbL requires visible credit whenever its tiles are shown. Kept in
            # sync with the active provider; hidden when the street basemap is off.
            self._attribution = QLabel(self._tile_cache.provider.attribution)
            self._attribution.setStyleSheet("color:#6e7681;font-size:8pt;")
            self._attribution.setVisible(self._chk_streetmap.isChecked())
            root.addWidget(self._attribution)
            # Sync the picker to the active provider (carto-dark), THEN connect -- so this initial
            # selection doesn't fire the change handler during construction.
            _pidx = self._provider_combo.findData(self._tile_cache.provider.key)
            if _pidx >= 0:
                self._provider_combo.setCurrentIndex(_pidx)
            self._provider_combo.currentIndexChanged.connect(lambda _i: self._on_provider_changed())

            # Live-scan diagnostics surface. The worker emits every notice (start/stop, per-camera, and
            # the failure paths — pyserial-missing / busy-or-denied COM port) on its `line` signal; without
            # a place to show them the operator gets no clue WHY a scan didn't start (the transient status
            # label is immediately reset to "Idle" by _on_live_stopped). Mirrors wardrive_tab's log pane.
            self._live_log = QPlainTextEdit()
            self._live_log.setReadOnly(True)
            self._live_log.setMaximumHeight(90)
            self._live_log.setPlaceholderText("Live scan messages appear here.")
            root.addWidget(self._live_log)

            # World basemap (Natural Earth 110m, public domain): loaded + projected into the shared world_px
            # plane ONCE here, reused every _rebuild. Empty FeatureCollection if the bundle is missing -> the
            # layer just doesn't draw. self._basemap_group holds the current QGraphicsItemGroup (or None).
            self._basemap_rings = basemap_paths(load_world_basemap())
            self._basemap_group = None

            # "You are here" GPS marker: last known (lat,lon) + its scene item (recreated each _rebuild since
            # scene.clear() drops it). Fed by the live worker's `location` signal; only drawn when the toggle is on.
            # _gps_live tracks whether the fix is currently streaming (bright) or stale after a scan stop (grey).
            self._my_location = None
            self._location_marker = None
            self._gps_live = True
            # Drive trail (S3): breadcrumb of (lat, lon) fixes behind the pin, session-only.
            # Data retained across unload; the layer (a polyline in world_px) is rebuilt on demand.
            self._trail: "List[Tuple[float, float]]" = []
            self._trail_layer = None
            self._trail_bounds = QRectF()   # extent of the trail, unioned into the framed content
            self._gps_worker = None      # standalone NMEA reader (F3) — GPS tracking without a full scan

            self.set_geojson({"type": "FeatureCollection", "features": []})
            self._relayout_flock(force=True)   # seed the control-row orientation

        # ── responsive layout (Wave-3) ──
        def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
            super().resizeEvent(event)
            self._relayout_flock()

        def _relayout_flock(self, force: bool = False) -> None:
            """Stack the file / live-scan / map control rows vertically on a compact canvas so their
            many buttons + checkboxes don't overflow a narrow deck; horizontal otherwise."""
            from src.ui.qt.layout_profile import layout_profile
            from src.ui.qt.touch_mode import touch_active
            dpi = self.logicalDpiX() or 96
            p = layout_profile(max(1, self.width()), max(1, self.height()),
                               touch=touch_active(), dpi=dpi)
            if not force and p.size == self._last_flock_size:   # debounce on the size class
                return
            self._last_flock_size = p.size
            from PyQt5.QtWidgets import QBoxLayout
            direction = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
            for row in (self._file_row, self._live_row, self._map_row):
                row.setDirection(direction)

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
            self.reset_view()                               # frame the freshly-loaded cameras
            return len(self._features)

        def _show_wardrive_points(self, points) -> int:
            """Plot *points* [(lat,lon,ssid,bssid)] as the Wi-Fi AP layer: store, show the layer,
            rebuild, and frame with the cameras. Returns the count. The ONE place a wardrive import
            lands on the map (both load_wardrive_csv and load_wardrive_log route through here)."""
            self._wardrive_points = points
            self._chk_wardrive.setChecked(True)             # make the freshly-loaded APs visible
            self._rebuild()                                 # setChecked may have; 2nd is a no-op
            self.reset_view()                               # frame cameras + APs together
            return len(self._wardrive_points)

        def load_wardrive_csv(self, path: str) -> int:
            """Load a wardrive WiGLE CSV from *path* as a Wi-Fi AP layer on the Flock map (Slice D's
            "View on map"). Returns the AP count (0 on any error). Awareness-only: plots located APs
            in the shared plane as the cameras, driving no device -- a map, not a send."""
            from src.core.wardrive import wigle_csv_to_points
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    points = wigle_csv_to_points(fh.read())   # parse in the try
            except Exception:  # noqa: BLE001 — huge field/binary file; an escaped raise aborts the app
                self._wardrive_points = []
                self._rebuild()
                self._legend.setText("Could not read that wardrive CSV.")
                return 0
            return self._show_wardrive_points(points)

        def load_wardrive_log(self, path: str) -> int:
            """Import ANY wardrive log (WiGLE CSV, Kismet .netxml, Kismet .kismet SQLite, a Biscuit
            export) as the Wi-Fi AP layer (S1). A binary .kismet SQLite DB routes to
            kismet_db_to_points BY PATH; every text format dispatches by content sniff via
            wardrive_points. 0 on a bad/unknown file (never crashes). Awareness-only; drives no
            device."""
            from src.core.wardrive_import import kismet_db_to_points, wardrive_points
            try:
                if _is_sqlite_db(path):
                    points = kismet_db_to_points(path)        # binary .kismet -> by path, not text
                else:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        points = wardrive_points(fh.read())   # parse in the try
            except Exception:  # noqa: BLE001 — huge field/binary/locked file; an escaped raise aborts the app
                self._legend.setText("Could not read that wardrive log.")
                return 0
            n = self._show_wardrive_points(points)
            if n == 0:
                self._legend.setText("No mappable points in that wardrive log.")
            return n

        def save_trail(self, path: str) -> int:
            """Persist the current drive trail to *path* as GeoJSON (owner-call #2). Returns the
            count written; 0 (nothing written) when the trail is empty or the file can't be written.
            Never raises. The saved file replays via load_trail or opens in any GIS tool."""
            if not self._trail:
                return 0
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(trail_to_geojson(self._trail), fh)
            except OSError:
                self._legend.setText("Could not save the trail.")
                return 0
            return len(self._trail)

        def load_trail(self, path: str) -> int:
            """Replay a saved drive trail from *path* (GeoJSON): parse -> set self._trail -> render.
            Returns the point count (0 on a bad/unreadable file, never crashes). Awareness-only:
            it draws a past drive; it touches no device."""
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    gj = json.load(fh)
            except (OSError, ValueError):
                self._legend.setText("Could not read that trail file.")
                return 0
            points = trail_from_geojson(gj)
            if not points:
                self._legend.setText("No drive trail in that file.")   # keep the current trail
                return 0
            self._trail = points
            self._chk_trail.setChecked(True)                # make the replayed trail visible
            self._rebuild()                                 # rebuild so the rect frames the trail
            self.reset_view()
            return len(self._trail)

        @property
        def camera_count(self) -> int:
            return len(self._features)

        @property
        def wardrive_count(self) -> int:
            return len(self._wardrive_points)

        def _wardrive_dots(self):
            """Project the loaded wardrive points into the shared world_px plane as AP dots, reusing
            _CameraLayer's (x, y, radius, QColor) format, a green palette; returns (dots, bounds).
            The radius scales with the AP set's own extent so dots stay visible at any scan size."""
            if not self._wardrive_points:
                return [], QRectF()
            proj = [world_px(lat, lon) for lat, lon, _ssid, _bssid in self._wardrive_points]
            xs = [p[0] for p in proj]
            ys = [p[1] for p in proj]
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
            radius = span * 0.010
            color = QColor(_AP_FILL)
            dots = [(x, y, radius, color) for x, y in proj]
            return dots, QRectF(min(xs) - radius, min(ys) - radius,
                                (max(xs) - min(xs)) + 2 * radius, (max(ys) - min(ys)) + 2 * radius)

        # ── render ────────────────────────────────────────────────────
        def _draw_basemap(self) -> None:
            """Draw the world-countries outline (self._basemap_rings, already projected into world_px) as a
            muted background layer UNDER the cameras. No-op if the bundle didn't load. Cosmetic pen so the
            coastline stays 1px on screen at any zoom (the rings span the whole 40M-unit world)."""
            self._basemap_group = None
            if not self._basemap_rings:
                return
            group = QGraphicsItemGroup()
            group.setZValue(-1000)                              # firmly beneath the camera dots
            pen = QPen(QColor("#30363d"))                       # muted land stroke (dark-theme border grey)
            pen.setCosmetic(True)
            brush = QBrush(QColor(23, 30, 40, 90))              # faint semi-transparent land fill
            for ring in self._basemap_rings:
                path = QPainterPath()
                path.moveTo(ring[0][0], ring[0][1])
                for x, y in ring[1:]:
                    path.lineTo(x, y)
                path.closeSubpath()
                item = QGraphicsPathItem(path, group)           # parent=group -> travels with it
                item.setPen(pen)
                item.setBrush(brush)
            self._scene.addItem(group)
            self._basemap_group = group

        # ── street basemap tiles (WS-4) ──────────────────────────────
        def _schedule_tiles(self, *_a) -> None:
            """Debounced request to refresh the street tiles for the current view — pan/zoom bursts coalesce
            into one refresh. Fires only when the tab is up; showEvent re-schedules on return. Takes ``*_a`` so
            it can connect straight to a scrollbar's ``valueChanged(int)`` and the view's zoom callback alike."""
            timer = getattr(self, "_tile_timer", None)
            if timer is not None:
                timer.start()

        def _on_streetmap_toggled(self) -> None:
            """Street-map toggle: show + refresh the tiles, or clear them and hide the attribution."""
            on = self._chk_streetmap.isChecked()
            self._attribution.setVisible(on)
            if on:
                self._schedule_tiles()
            else:
                self._clear_tiles()

        def _on_provider_changed(self) -> None:
            """Switch the basemap provider: repoint the tile cache, refresh its attribution, drop
            old tiles, and reload the view. Cache-first -- the airgap "Online tiles" stance is
            unchanged, so nothing is fetched from the new provider unless that toggle is on."""
            key = self._provider_combo.currentData()
            if not key:
                return
            self._tile_cache.provider = map_tiles.get_provider(key)
            self._attribution.setText(self._tile_cache.provider.attribution)
            self._clear_tiles()
            if self._chk_streetmap.isChecked():
                self._schedule_tiles()

        def _stop_tile_worker(self) -> None:
            """Ask the current tile fetcher to stop, but KEEP it referenced (in _tile_workers) until it
            actually finishes. This runs on every pan/zoom supersede, so a blocking wait() here would freeze
            the GUI up to a per-tile network timeout; and dropping a running QThread's last Python reference
            risks a 'QThread: Destroyed while thread is still running' abort. The worker reaps itself via its
            finished -> _reap_tile_worker connection once its C++ thread has fully exited."""
            w = self._tile_worker
            if w is not None:
                w.stop()
                self._tile_worker = None   # no longer the ACTIVE worker; still retained in _tile_workers till done

        def _reap_tile_worker(self, w) -> None:
            """A tile worker finished (naturally or after stop()): drop our retained reference and schedule
            its deletion. Fires on the GUI thread via the queued finished signal, so the C++ thread has
            already exited — safe to release."""
            try:
                self._tile_workers.remove(w)
            except ValueError:
                pass
            try:
                w.deleteLater()
            except Exception:  # noqa: BLE001 — already torn down
                pass

        def _clear_tiles(self) -> None:
            """Remove every placed tile item and stop any in-flight fetch. Safe if none exist."""
            self._stop_tile_worker()
            grp = self._tile_group
            if grp is not None:
                try:
                    self._scene.removeItem(grp)
                except Exception:  # noqa: BLE001 — scene may already have dropped it via scene.clear()
                    pass
            self._tile_group = None
            self._tile_items = {}
            self._tile_needed = set()

        def _reset_tile_state(self) -> None:
            """Drop tile REFERENCES after a scene.clear()/free (which already deleted the C++ items) and stop
            the worker — so a later _update_tiles rebuilds cleanly instead of touching deleted items."""
            self._stop_tile_worker()
            self._tile_group = None
            self._tile_items = {}
            self._tile_needed = set()

        def _update_tiles(self) -> None:
            """Place the street tiles covering the current view: cache hits now, misses fetched off-thread
            when Online tiles is on. Prunes tiles that scrolled off. No-op when the street map is off or the
            tab isn't visible. The group sits below the cameras and above the country outline."""
            if not self._chk_streetmap.isChecked() or not self._visible or self._unloaded:
                return
            scale = self._view.transform().m11()
            if scale <= 0:
                return
            vp = self._view.viewport().rect()
            vis = self._view.mapToScene(vp).boundingRect()
            if vis.isEmpty():
                return
            # Cap the tile count to what THIS viewport actually needs (+ a margin), not a fixed 80 — a
            # maximized 4K/ultrawide window is a scale-matched ~130-150 tiles, which the old default dropped
            # to an empty list, blanking the whole basemap exactly when zoomed in to street level.
            cap = (vp.width() // map_tiles.TILE_SIZE + 3) * (vp.height() // map_tiles.TILE_SIZE + 3)
            z, needed = map_tiles.tiles_for_viewport(
                vis.left(), vis.top(), vis.right(), vis.bottom(), 1.0 / scale, cap=max(80, cap))
            self._tile_needed = set(needed)
            if self._tile_group is None:
                self._tile_group = QGraphicsItemGroup()
                self._tile_group.setZValue(-900)          # above the country outline (-1000), below cameras (0)
                self._scene.addItem(self._tile_group)
            # Prune tiles no longer needed (panned out of view, or a stale zoom).
            for key in list(self._tile_items):
                if key not in self._tile_needed:
                    item = self._tile_items.pop(key)
                    try:
                        self._scene.removeItem(item)
                    except Exception:  # noqa: BLE001
                        pass
            # Cache hits paint immediately; misses queue for a fetch.
            misses = []
            for (x, y, zz) in needed:
                if (x, y, zz) in self._tile_items:
                    continue
                data = self._tile_cache.get(x, y, zz)
                if data is not None:
                    self._place_tile(x, y, zz, data)
                else:
                    misses.append((x, y, zz))
            if misses and self._chk_online.isChecked():
                self._stop_tile_worker()                  # supersede an older batch (the view moved on)
                w = _TileFetchWorker(self._tile_cache, misses)
                self._tile_workers.append(w)              # retain so a running QThread is never GC'd mid-run
                w.tile.connect(self._on_tile_ready)
                w.finished.connect(lambda w=w: self._reap_tile_worker(w))
                self._tile_worker = w
                w.start()

        def _place_tile(self, x: int, y: int, z: int, data: bytes) -> None:
            """Draw one tile pixmap at its world_px rectangle (a 256-px tile scaled to the tile's world size),
            parented to the tile group. No-op if the bytes don't decode or the group is gone."""
            if self._tile_group is None:
                return
            pm = QPixmap()
            if not pm.loadFromData(data) or pm.isNull() or pm.width() <= 0:
                return
            wx, wy, size = map_tiles.tile_world_rect(x, y, z)
            item = QGraphicsPixmapItem(pm, self._tile_group)
            item.setPos(wx, wy)
            item.setScale(size / pm.width())              # 256-px tile -> `size` world units, aligned to world_px
            item.setTransformationMode(Qt.SmoothTransformation)
            self._tile_items[(x, y, z)] = item

        def _on_tile_ready(self, x: int, y: int, z: int, data: bytes) -> None:
            """A fetched tile arrived — place it only if the street map is still on and this tile is still in
            view (the view may have panned/zoomed while it downloaded)."""
            if not self._chk_streetmap.isChecked() or (x, y, z) not in self._tile_needed:
                return
            if (x, y, z) not in self._tile_items:
                self._place_tile(x, y, z, data)

        # ── "you are here" GPS marker ────────────────────────────────
        def set_my_location(self, lat: float, lon: float) -> None:
            """Record the live GPS position, mark the fix live, and (if the toggle is on) draw/move the
            'you are here' marker — recentring on it when Follow is on. Public so the live worker's
            `location` signal and tests can drive it without a serial port."""
            lat, lon = float(lat), float(lon)
            # Extend the drive trail past the move-threshold (pure gate), then cap it.
            # _draw_location_marker rebuilds the layer from self._trail below.
            if trail_accept(self._trail[-1] if self._trail else None, lat, lon):
                self._trail.append((lat, lon))
                self._trail = trail_decimate(self._trail)
            self._my_location = (lat, lon)
            self._gps_live = True
            self._draw_location_marker()
            if self._chk_follow.isChecked():
                self.center_on_me()

        def clear_my_location(self) -> None:
            """Forget the GPS position and remove the marker (e.g. GPS lost / scan stopped)."""
            self._my_location = None
            self._draw_location_marker()

        def mark_gps_stale(self) -> None:
            """The live GPS feed stopped: keep the last position but grey the pin so a stale fix doesn't
            read as your current one."""
            self._gps_live = False
            self._draw_location_marker()

        def center_on_me(self) -> None:
            """Recentre the view on the GPS marker. No-op if the toggle is off or no fix is known."""
            if self._my_location is None or not self._chk_mylocation.isChecked():
                return
            x, y = world_px(*self._my_location)
            self._view.centerOn(x, y)

        # ── standalone GPS tracking (F3: "My location" without a full scan) ──
        def _on_mylocation_toggled(self) -> None:
            """Toggling 'My location (GPS)' on/off: draw or hide the pin, and start/stop a standalone GPS
            reader so the pin works even when no Flock scan is running."""
            if self._chk_mylocation.isChecked():
                self._draw_location_marker()
                self._maybe_start_gps_tracking()
            else:
                self._stop_gps_tracking()
                self._draw_location_marker()          # removes the pin

        def _maybe_start_gps_tracking(self) -> None:
            """Start the standalone GPS reader IF the toggle is on, a GPS port is selected, none is already
            running, and no full scan is streaming GPS (that would double-open the same port)."""
            if self._gps_worker is not None or self._live_worker is not None:
                return
            if not self._chk_mylocation.isChecked():
                return
            port = self._gps_combo.currentText().strip()
            if not port:
                self._live_log.appendPlainText("My location: pick a GPS port to track without a scan.")
                return
            self._gps_worker = _GpsWorker(port, 9600)
            self._gps_worker.location.connect(self._on_location_fix)
            self._gps_worker.line.connect(self._on_live_line)
            # Bind the emitting worker into the slot so a superseded worker's late (queued cross-thread)
            # 'stopped' can be told apart from the current one — see _on_gps_tracking_stopped.
            self._gps_worker.stopped.connect(lambda w=self._gps_worker: self._on_gps_tracking_stopped(w))
            self._gps_worker.start()

        def _stop_gps_tracking(self) -> None:
            """Ask the standalone GPS reader to stop + wait for its finally-block to release the port (so a
            following scan can open it). Safe if none is running."""
            w = self._gps_worker
            if w is not None:
                w.stop()
                w.wait(1500)
                self._gps_worker = None

        def _on_gps_tracking_stopped(self, worker: "Optional[object]" = None) -> None:
            # `stopped` is a queued cross-thread signal, so a superseded reader's stop can land AFTER a scan
            # or a fresh reader has taken over. Only clear the handle if THIS is still the tracked worker, and
            # only grey the pin when nothing else is feeding it — otherwise a stale stop would orphan the live
            # reader or briefly grey a pin that a running scan/new reader is actively updating.
            if worker is None or worker is self._gps_worker:
                self._gps_worker = None
            if self._gps_worker is None and self._live_worker is None:
                self.mark_gps_stale()                 # GPS feed ended -> grey the last pin

        def _draw_location_marker(self) -> None:
            """(Re)draw the 'you are here' marker at ``self._my_location``. Removes any existing one first;
            no-op if the toggle is off or no fix is known. Uses ItemIgnoresTransformations so the marker stays
            a fixed on-screen size (a real map pin) at any zoom, drawn above the dots + basemap. Cyan while the
            fix is live, grey once it's stale (scan stopped)."""
            # The drive trail travels with the pin: fires on each per-location redraw (a new
            # fix and each _rebuild); _update_trail_layer has its own toggle, run before the pin's.
            self._update_trail_layer()
            if self._location_marker is not None:
                self._scene.removeItem(self._location_marker)
                self._location_marker = None
            if self._my_location is None or not self._chk_mylocation.isChecked():
                return
            x, y = world_px(*self._my_location)
            r = 8.0
            fill = _GPS_LIVE_FILL if self._gps_live else _GPS_STALE_FILL
            item = self._scene.addEllipse(
                -r, -r, 2 * r, 2 * r,
                QPen(QColor("#ffffff"), 3), QBrush(QColor(fill)))         # white ring + cyan/grey core
            item.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)  # fixed screen size = a map pin
            item.setZValue(10000)                                         # above every other layer
            item.setPos(x, y)
            self._location_marker = item

        def _update_trail_layer(self) -> None:
            """Rebuild the drive-trail layer from self._trail (one polyline in the world_px plane,
            above cameras/APs, below the pin). Cheap: removes + re-adds a single item; projects the
            capped list. No-op when the "Trail" toggle is off or the trail has < 2 points -- the
            DATA is retained either way, so toggling or an unload/reload keeps the drive so far."""
            if self._trail_layer is not None:
                try:
                    self._scene.removeItem(self._trail_layer)
                except RuntimeError:
                    pass                                    # already dropped by a scene.clear()
                self._trail_layer = None
            self._trail_bounds = QRectF()
            if not self._chk_trail.isChecked() or len(self._trail) < 2:
                return
            pts = [world_px(lat, lon) for lat, lon in self._trail]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bounds = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
            self._trail_bounds = bounds
            self._trail_layer = _TrailLayer(pts, bounds)
            self._trail_layer.setZValue(2)     # above cameras (0) + APs (1); the pin is on top
            self._scene.addItem(self._trail_layer)

        def _rebuild(self) -> None:
            self._scene.clear()
            self._camera_layer = None
            self._camera_bounds = QRectF()
            self._wardrive_layer = None                        # scene.clear() freed it; a handle
            self._wardrive_bounds = QRectF()                   # would dangle if not nulled here
            self._trail_layer = None                           # ditto; re-added below
            self._trail_bounds = QRectF()
            self._basemap_group = None
            self._location_marker = None                       # scene.clear() dropped it; redraw below
            self._tile_group = None                            # scene.clear() dropped the tiles too
            self._tile_items = {}
            show_base = self._chk_basemap.isChecked() and bool(self._basemap_rings)
            # The scene spans the whole world whenever a global layer is on (country outline OR street tiles),
            # so you can pan/zoom anywhere on real streets; reset_view still re-frames the cameras.
            world_scene = show_base or self._chk_streetmap.isChecked()
            if show_base:
                self._draw_basemap()
            # Camera (Flock) layer -- the blue->red density heatmap, projected into the ONE shared
            # world_px plane so the basemap + wardrive AP layer stay aligned at every zoom.
            maxc = 0
            if self._chk_flock.isChecked() and self._features:
                counts = [_as_count(f.get("properties")) for f in self._features]
                maxc = max(counts)
                proj: "List[Tuple[float, float, float]]" = []
                for feat, c in zip(self._features, counts):
                    lon = feat["geometry"]["coordinates"][0]
                    lat = feat["geometry"]["coordinates"][1]
                    x, y = world_px(lat, lon)
                    t = (c - 1) / (maxc - 1) if maxc > 1 else 0.0     # normalized density
                    proj.append((x, y, t))
                xs = [p[0] for p in proj]
                ys = [p[1] for p in proj]
                span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)  # floor avoids a 0-size dot
                # Position is absolute (world_px); the dot RADIUS scales with the set's extent so a dot
                # stays visible whether the scan spans a city block or a continent. Hotter -> larger.
                dots = []
                minx = miny = float("inf")
                maxx = maxy = float("-inf")
                for x, y, t in proj:
                    r8, g8, b8 = heat_color(t)
                    radius = span * (0.010 + 0.014 * t)
                    dots.append((x, y, radius, QColor(r8, g8, b8)))
                    minx = min(minx, x - radius); miny = min(miny, y - radius)
                    maxx = max(maxx, x + radius); maxy = max(maxy, y + radius)
                # ONE item for the whole set: a single BSP entry + a float list (not N items);
                # _CameraLayer.paint() draws only the dots in the exposed viewport, so off-screen
                # cameras cost nothing on pan/zoom. Radius keys off the FULL span so dots don't resize.
                self._camera_bounds = QRectF(minx, miny, maxx - minx, maxy - miny)
                self._camera_layer = _CameraLayer(dots, self._camera_bounds)
                self._scene.addItem(self._camera_layer)

            # Wardrive (Wi-Fi AP) layer -- Slice D: a SECOND _CameraLayer in the plane, above cameras.
            # Additive + awareness-only; the projection math (world_px) is shared + byte-unchanged.
            if self._chk_wardrive.isChecked() and self._wardrive_points:
                ap_dots, ap_bounds = self._wardrive_dots()
                if ap_dots:
                    self._wardrive_bounds = ap_bounds
                    self._wardrive_layer = _CameraLayer(ap_dots, ap_bounds)
                    self._wardrive_layer.setZValue(1)              # above cameras (default z 0)
                    self._scene.addItem(self._wardrive_layer)

            self._update_trail_layer()             # build the trail HERE so its extent is in the
            content = self._content_bounds()       # framed content + scene rect (trail-only replay)
            if content.isEmpty():
                if world_scene:
                    self._scene.setSceneRect(0, 0, _WORLD_PX, _WORLD_PX)   # whole world, pannable
                    self._legend.setText("Street basemap · no detections loaded. Blue = few · red = many.")
                    self._view.fit(self._scene.sceneRect())   # frame the globe (re-fits on first resize)
                else:
                    self._scene.setSceneRect(0, 0, _CANVAS_W, _CANVAS_H)
                    self._legend.setText("No detections loaded. Blue = few sightings · red = many.")
                self._draw_location_marker()                   # marker can show over an empty/basemap-only map
                self._schedule_tiles()
                return
            # Scene rect: with a global layer on, the whole world is the scene so you can pan/zoom out
            # to it (reset_view still re-frames the content). Without it, just the content extent + a
            # margin so pan/zoom has room and edge dots aren't clipped.
            if world_scene:
                self._scene.setSceneRect(0, 0, _WORLD_PX, _WORLD_PX)
            else:
                cspan = max(content.width(), content.height(), 1.0)
                margin = cspan * (0.05 + 0.024)
                self._scene.setSceneRect(content.adjusted(-margin, -margin, margin, margin))
            base_note = (" · street basemap" if self._chk_streetmap.isChecked()
                         else " · world basemap" if show_base else "")
            parts = []
            if self._camera_layer is not None:
                parts.append(f"{len(self._features)} cameras · blue few · red many (up to {maxc})")
            if self._wardrive_layer is not None:
                parts.append(f"{len(self._wardrive_points)} Wi-Fi AP(s) · green")
            self._legend.setText(" · ".join(parts) + f"{base_note}. Drag to pan · scroll to zoom.")
            self._draw_location_marker()                       # keep the "you are here" pin above the redraw
            self._schedule_tiles()                             # paint street tiles under the cameras

        def _content_bounds(self) -> "QRectF":
            """Union of on-map layer extents (cameras + wardrive APs + trail), for framing/sizing.
            Skips empty layers so a lone empty QRectF at the origin can't drag the frame to 0,0."""
            out = QRectF()
            for b in (self._camera_bounds, self._wardrive_bounds, self._trail_bounds):
                if not b.isEmpty():
                    out = QRectF(b) if out.isEmpty() else out.united(b)
            return out

        def reset_view(self) -> None:
            """Re-frame the CONTENT: drop any pan/zoom and fit the whole detection set -- cameras AND
            the wardrive AP layer -- into the view (so the world basemap, which spans the globe,
            doesn't hijack the framing). Falls back to the scene rect when empty; safe on empty."""
            self._view.resetTransform()
            rect = self._content_bounds()
            if rect.isEmpty():
                rect = self._scene.sceneRect()
            if rect.isValid() and not rect.isEmpty():
                self._view.fit(rect)
            self._schedule_tiles()                             # reframe changed the view -> reload tiles

        def render_native(self, width: int = _CANVAS_W, height: int = _CANVAS_H) -> "QImage":
            """Render the scene into a QImage — pure, offscreen-testable (no window needed). Frames the
            CAMERAS when present (so a snapshot shows the detections, not the whole globe once the basemap
            makes the scene rect world-sized); otherwise renders the full scene rect."""
            img = QImage(width, height, QImage.Format_ARGB32)
            img.fill(_BG)
            p = QPainter(img)
            src = self._camera_bounds if self._camera_layer is not None else QRectF()
            if src.isValid() and not src.isEmpty():
                self._scene.render(p, QRectF(0, 0, width, height), src)
            else:
                self._scene.render(p)                          # no cameras -> whole scene (globe or empty)
            p.end()
            return img

        # ── live scan (F5 live driving loop) ──────────────────────────
        def _refresh_ports(self) -> None:
            try:
                from serial.tools import list_ports
                ports = [p.device for p in list_ports.comports()]
            except Exception:  # noqa: BLE001 — pyserial missing or enumeration failure
                ports = []
            for combo in (self._gps_combo, self._dev_combo):
                cur = combo.currentText()
                combo.clear()
                combo.addItem("")                      # blank option (the GPS port is optional)
                combo.addItems(ports)
                i = combo.findText(cur)
                if i >= 0:
                    combo.setCurrentIndex(i)

        def _flock_data_dir(self) -> str:
            """The one canonical folder for saved Flock scans: ``~/.cyber-controller/flock``.

            Live drives checkpoint here, the Load dialog opens here, and "Open data folder" reveals it — so
            captures always live in one predictable place instead of scattered next to the working directory."""
            from pathlib import Path
            d = Path.home() / ".cyber-controller" / "flock"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return str(d)

        def _default_checkpoint_path(self) -> str:
            from pathlib import Path
            return str(Path(self._flock_data_dir()) / "live-drive.geojson")

        def _new_checkpoint_path(self) -> str:
            """A UNIQUE per-scan checkpoint file. Each _FlockWorker starts a fresh empty FlockSession and
            os.replace()s its checkpoint file on the first located camera; with a single fixed filename a
            second drive would silently overwrite (destroy) the first drive's saved cameras. Timestamping
            per scan means a new drive can never clobber a prior one, and the Load dialog can reopen any of
            them. (Nothing auto-loads the old fixed name, so keeping it for a fresh scan is unnecessary.)"""
            from datetime import datetime
            from pathlib import Path
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = Path(self._flock_data_dir()) / f"live-drive-{stamp}.geojson"
            path, n = base, 1
            while path.exists():  # guard the rare same-second restart
                path = base.with_name(f"live-drive-{stamp}-{n}.geojson")
                n += 1
            return str(path)

        def _toggle_live(self) -> None:
            if self._live_worker is not None:            # running -> ask it to stop
                self._live_worker.stop()
                self._btn_live.setEnabled(False)
                self._btn_live.setText("Stopping…")
                return
            dev = self._dev_combo.currentText().strip()
            if not dev:
                self._live_status.setText("Pick the Flock device port first.")
                return
            gps = self._gps_combo.currentText().strip()
            self._stop_gps_tracking()                    # a full scan opens the GPS port — release the standalone reader first
            # A UNIQUE per-scan checkpoint file so this drive can't os.replace over a prior drive's cameras.
            self._live_worker = _FlockWorker(gps, 9600, dev, 115200, self._new_checkpoint_path())
            self._live_worker.updated.connect(self._on_live_update)
            self._live_worker.status.connect(self._on_live_status)
            self._live_worker.location.connect(self._on_location_fix)
            self._live_worker.line.connect(self._on_live_line)
            self._live_worker.stopped.connect(self._on_live_stopped)
            self._live_worker.start()
            self._btn_live.setText("Stop scan")
            self._live_status.setText("Scanning — waiting for a fix…")

        def _on_live_update(self, gj: dict) -> None:
            # Record/render split: keep the newest data always (the worker + its checkpoint keep running even
            # while this tab is hidden), but only repaint the scene while visible — showEvent replays the latest.
            self._latest_gj = gj
            if self._visible:
                self.set_geojson(gj)

        def _on_live_status(self, fix_text: str, count: int) -> None:
            self._live_status.setText(f"Fix: {fix_text} · {count} camera(s)")

        def _on_location_fix(self, lat: float, lon: float, has_fix: bool) -> None:
            # Drive the "you are here" marker from the live GPS fix. set_my_location honours the toggle, so
            # this is a no-op on screen until the user turns "My location (GPS)" on.
            if has_fix:
                self.set_my_location(lat, lon)

        def _on_live_line(self, msg: str) -> None:
            # Surface every worker diagnostic (esp. the failure paths) so a scan that never starts —
            # busy/denied COM port, pyserial missing — is visible instead of silently swallowed.
            self._live_log.appendPlainText(msg)
            # Tee to the app-wide terminal too, so the persistent console reflects Flock live-scan
            # activity instead of it dead-ending in this tab's own live-log pane.
            from src.core.activity_log import activity_log
            activity_log().emit_line("flock", msg)

        def _on_live_stopped(self) -> None:
            self._live_worker = None
            self._btn_live.setEnabled(True)
            self._btn_live.setText("Start scan")
            self._live_status.setText("Idle")
            self.mark_gps_stale()            # GPS feed ended -> grey the last-known pin so it doesn't look live
            if self._chk_mylocation.isChecked():
                self._maybe_start_gps_tracking()   # resume standalone GPS tracking now the scan freed the port

        # ── wake/sleep: keep recording while hidden, catch the map up on show ──
        def _free_scene(self) -> None:
            """Drop every QGraphicsItem so a backgrounded tab stops costing CPU/RAM. Resets the item handles
            exactly like _rebuild's head, so a later _draw_location_marker (a live GPS fix can arrive while
            hidden) never calls removeItem() on an already-deleted C++ object. The parsed data
            (_features/_latest_gj), toggles, and the live worker all survive — showEvent rebuilds from them."""
            self._scene.clear()
            self._camera_layer = None
            self._camera_bounds = QRectF()
            self._wardrive_layer = None
            self._wardrive_bounds = QRectF()
            self._trail_layer = None               # scene.clear() freed it; self._trail survives
            self._trail_bounds = QRectF()
            self._basemap_group = None
            self._location_marker = None
            self._reset_tile_state()               # drop tile refs + stop the fetch worker (scene.clear() freed them)
            self._unloaded = True

        def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
            self._visible = True
            if self._latest_gj is not None:
                self.set_geojson(self._latest_gj)   # live: catch up to the newest data + rebuild the scene
            elif self._unloaded:
                self._rebuild()                       # loaded-from-file map: rebuild from the retained _features
            self._unloaded = False
            self._schedule_tiles()                    # repaint street tiles for the current view on return
            super().showEvent(ev)

        def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
            # Deliberately do NOT stop the worker: detections must keep accumulating and checkpointing while the
            # tab is backgrounded (_on_live_update records into _latest_gj without repainting). With the toggle
            # on (default), also FREE the scene so an idle Flock tab holding a big camera set drops CPU/RAM.
            self._visible = False
            if self._chk_unload.isChecked():
                self._free_scene()
            super().hideEvent(ev)

        # ── real shutdown (app close) — the ONE place the live worker is stopped ──
        def shutdown(self) -> None:
            """Stop the live Flock scan and wait for its thread to exit before the tab is destroyed.

            hideEvent keeps the worker running on purpose; this is the only hook that actually tears it down.
            Without it, closing the main window destroys the still-looping QThread wrapper ('QThread:
            Destroyed while thread is still running') and leaks the GPS + device serial ports, which are
            closed only in run()'s finally-block. Waiting lets that finally-block close both ports cleanly.
            Invoked from MainWindow.closeEvent."""
            w = self._live_worker
            if w is not None:
                w.stop()
                w.wait()
            self._stop_gps_tracking()        # also tear down the standalone GPS reader + free its port
            # Join EVERY live tile fetcher (the active one + any superseded-but-still-finishing), so no
            # QThread is left running at teardown — this is the one place a bounded blocking wait is right.
            for tw in list(self._tile_workers):
                tw.stop()
                tw.wait(1500)
            self._tile_workers = []
            self._tile_worker = None

        # ── export (the write is unit-tested; the dialog wrapper is not) ──
        def export_csv_to(self, path: str) -> int:
            """Write the cameras CURRENTLY on the map to *path* as CSV. Returns the row count written.
            SSIDs are untrusted broadcast strings, so the shared converter neutralizes CSV formula injection."""
            gj = {"type": "FeatureCollection", "features": list(self._features)}
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(flock.cameras_geojson_to_csv(gj))
            return len(self._features)

        def _on_export_csv(self) -> None:
            from PyQt5.QtWidgets import QFileDialog
            from pathlib import Path
            if not self._features:
                self._legend.setText("No cameras to export yet — load or run a scan first.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self, "Export cameras to CSV", str(Path(self._flock_data_dir()) / "flock-cameras.csv"),
                "CSV (*.csv);;All files (*)")
            if not path:
                return
            try:
                n = self.export_csv_to(path)
                self._legend.setText(f"Exported {n} camera(s) to CSV.")
            except OSError as exc:
                self._legend.setText(f"Could not write CSV: {exc}")

        # ── load button (dialog; not unit-tested) ─────────────────────
        def _on_import_wardrive(self) -> None:
            """File-import UI (S1): pick a wardrive log and plot its APs. The dialog is the only
            non-testable bit; load_wardrive_log dispatches by CONTENT (not extension) -- WiGLE CSV /
            .netxml / .kismet SQLite / Biscuit. Marauder writes WigleWifi to `wardrive_N.log`, so
            `*.log` is listed too (content still routes it). Awareness-only -- drives no device."""
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "Import wardrive log", "",
                "Wardrive logs (*.csv *.wiglecsv *.log *.netxml *.kismet);;All files (*)")
            if path:
                self.load_wardrive_log(path)

        def _on_save_trail(self) -> None:
            """Persist (owner-call #2): save the current trail to a GeoJSON file for later replay.
            The dialog is the only non-testable bit; it delegates to save_trail."""
            from PyQt5.QtWidgets import QFileDialog
            if not self._trail:
                self._legend.setText("No trail to save yet -- drive with GPS on first.")
                return
            path, _ = QFileDialog.getSaveFileName(self, "Save drive trail", "trail.geojson",
                                                  "GeoJSON (*.geojson *.json);;All files (*)")
            if path:
                self.save_trail(path)

        def _on_load_trail(self) -> None:
            """Replay a saved drive trail: pick a GeoJSON and draw it. Delegates to load_trail."""
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(self, "Load drive trail", "",
                                                  "GeoJSON (*.geojson *.json);;All files (*)")
            if path:
                self.load_trail(path)

        def _on_load(self) -> None:
            from PyQt5.QtCore import Qt
            from PyQt5.QtWidgets import QApplication, QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Flock scan (cameras.geojson)", self._flock_data_dir(),
                "GeoJSON (*.geojson *.json);;All files (*)")
            if not path:
                return
            # A nationwide DeFlock export is tens of thousands of points; json.load + reprojecting every
            # one runs on the GUI thread (the QGraphics scene build has to). It can't be moved to a worker,
            # so give visible feedback + block re-entry: a busy cursor so the pause reads as "working" not
            # "hung", and a disabled Load button so a second click can't stack another parse on the first.
            self._btn_load.setEnabled(False)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.load_geojson_file(path)
            finally:
                QApplication.restoreOverrideCursor()
                self._btn_load.setEnabled(True)

        # ── OSM/DeFlock import (user-initiated, off the GUI thread) ──
        def _view_bbox(self) -> "Tuple[float, float, float, float]":
            """The current view's geographic bbox as (south, west, north, east) — the visible scene
            rect (world_px) inverse-projected. world_px y grows south, so top-left maps to NW."""
            r = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
            n_lat, w_lon = world_px_inv(r.left(), r.top())       # top-left  -> north-west
            s_lat, e_lon = world_px_inv(r.right(), r.bottom())   # bot-right -> south-east
            clamp = lambda v, lo, hi: max(lo, min(hi, v))        # noqa: E731 — tiny local clamp
            return (clamp(s_lat, -85.0, 85.0), clamp(w_lon, -180.0, 180.0),
                    clamp(n_lat, -85.0, 85.0), clamp(e_lon, -180.0, 180.0))

        def _osm_cache_path(self) -> str:
            import os
            return os.path.join(self._flock_data_dir(), "osm-alpr-cache.geojson")

        def _on_import_osm(self) -> None:
            """User-initiated: import ALPR cameras for the current view from OSM/Overpass, off the
            GUI thread. Never auto-run. Refuses a too-large view so the shared free Overpass API
            isn't asked for the whole planet — the cached fetch is otherwise rate-limit-friendly."""
            s, w, n, e = self._view_bbox()
            if (n - s) > _OSM_MAX_SPAN_DEG or (e - w) > _OSM_MAX_SPAN_DEG:
                self._legend.setText("Zoom in to import OSM cameras — the view is too large.")
                return
            self._btn_import_osm.setEnabled(False)
            self._legend.setText("Importing ALPR cameras from OpenStreetMap…")
            worker = _OsmImportWorker(
                (s, w, n, e), self._osm_cache_path(), fetcher=self._osm_fetcher)
            self._osm_workers.append(worker)   # retain so a running QThread is never GC'd mid-run
            worker.imported.connect(self._on_osm_imported)
            worker.failed.connect(self._on_osm_failed)
            worker.finished.connect(lambda w=worker: self._reap_osm_worker(w))
            worker.start()

        def _on_osm_imported(self, gj: dict) -> None:
            count = len(gj.get("features", [])) if isinstance(gj, dict) else 0
            self.set_geojson(gj)
            self.reset_view()
            self._legend.setText(
                f"{count} ALPR camera(s) from OpenStreetMap · {flock_osm.ODBL_ATTRIBUTION}")
            self._btn_import_osm.setEnabled(True)

        def _on_osm_failed(self, msg: str) -> None:
            # An HONEST error, never a silent empty result: a failed fetch must NOT read as
            # "0 cameras found". set_geojson is NOT called, so whatever is on the map stays.
            self._legend.setText(f"Couldn't reach OpenStreetMap / Overpass — import failed. {msg}")
            self._btn_import_osm.setEnabled(True)

        def _reap_osm_worker(self, worker) -> None:
            try:
                self._osm_workers.remove(worker)
            except ValueError:
                pass
            worker.deleteLater()

        def _open_data_folder(self) -> None:
            """Reveal the canonical Flock data folder in the OS file manager (best-effort)."""
            from PyQt5.QtGui import QDesktopServices
            from PyQt5.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._flock_data_dir()))

except ImportError:  # PyQt5 unavailable — the pure projection core above is still importable/testable.
    FlockHeatmapTab = None  # type: ignore[assignment,misc]
