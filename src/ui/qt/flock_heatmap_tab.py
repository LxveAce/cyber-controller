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

from src.core import flock

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


def world_px(lat: float, lon: float, world: float = _WORLD_PX) -> Tuple[float, float]:
    """Project (lat, lon) into the shared global-mercator pixel plane [0, world]. Pure + Qt-free — the
    single projection both the camera layer and (Phase B) the world basemap are placed through."""
    x, y = web_mercator(lat, lon)
    return x * world, y * world


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
    from PyQt5.QtCore import Qt, QThread, QRectF, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen
    from PyQt5.QtWidgets import (
        QCheckBox,
        QComboBox,
        QGraphicsItem,
        QGraphicsItemGroup,
        QGraphicsPathItem,
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

    class FlockHeatmapTab(QWidget):
        """A heatmap of located ALPR cameras from a Flock scan's GeoJSON. Offscreen-renderable."""

        def __init__(self, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._features: "List[dict]" = []
            self._camera_layer = None             # the single QGraphicsItem holding every camera dot (or None)
            self._camera_bounds = QRectF()        # full extent of the camera set, for reset_view/render framing
            self._live_worker = None
            self._latest_gj: "Optional[dict]" = None
            self._visible = False
            self._unloaded = False   # True while the scene is freed for a backgrounded tab (see hideEvent)

            root = QVBoxLayout(self)
            _wip = QLabel(
                "\U0001F6A7  Work in progress — the Flock map is still being built. Cameras only appear "
                "from a live scan or a loaded cameras.geojson; a bundled dataset + one-click update are "
                "still coming.")
            _wip.setWordWrap(True)
            _wip.setStyleSheet("background:#3d2c00;color:#f0c000;border:1px solid #7a5c00;"
                               "border-radius:6px;padding:8px 10px;font-weight:600;")
            root.addWidget(_wip)
            file_row = QHBoxLayout()
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
            file_row.addWidget(self._btn_load)
            file_row.addWidget(self._btn_folder)
            file_row.addWidget(self._btn_export)
            file_row.addStretch(1)
            root.addLayout(file_row)

            # ── Live scan controls (F5 live driving loop) ──
            live_row = QHBoxLayout()
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
            self._btn_reset_view = QPushButton("Reset view")
            self._btn_reset_view.setToolTip("Re-frame all cameras (drag to pan · scroll to zoom).")
            self._btn_reset_view.clicked.connect(self.reset_view)
            self._chk_basemap = QCheckBox("World basemap")
            self._chk_basemap.setToolTip("Show a muted world-countries outline under the cameras, so a scan sits "
                                         "in real-world context. Zoom/pan out to see it; toggle off for a plain map.")
            self._chk_basemap.setChecked(True)
            self._chk_basemap.stateChanged.connect(lambda _s: self._rebuild())
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
            map_row.addWidget(self._chk_basemap)
            map_row.addWidget(self._chk_mylocation)
            map_row.addWidget(self._chk_follow)
            map_row.addWidget(self._btn_center)
            map_row.addWidget(self._chk_unload)
            map_row.addStretch(1)
            root.addLayout(map_row)

            self._legend = QLabel("No detections loaded. Blue = few sightings · red = many.")
            self._legend.setStyleSheet("color:#8b949e;")
            root.addWidget(self._legend)

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
            self._gps_worker = None      # standalone NMEA reader (F3) — GPS tracking without a full scan

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
            self.reset_view()                               # frame the freshly-loaded cameras
            return len(self._features)

        @property
        def camera_count(self) -> int:
            return len(self._features)

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

        # ── "you are here" GPS marker ────────────────────────────────
        def set_my_location(self, lat: float, lon: float) -> None:
            """Record the live GPS position, mark the fix live, and (if the toggle is on) draw/move the
            'you are here' marker — recentring on it when Follow is on. Public so the live worker's
            `location` signal and tests can drive it without a serial port."""
            self._my_location = (float(lat), float(lon))
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

        def _rebuild(self) -> None:
            self._scene.clear()
            self._camera_layer = None
            self._camera_bounds = QRectF()
            self._basemap_group = None
            self._location_marker = None                       # scene.clear() dropped it; redraw below
            show_base = self._chk_basemap.isChecked() and bool(self._basemap_rings)
            if show_base:
                self._draw_basemap()
            if not self._features:
                if show_base:
                    self._scene.setSceneRect(0, 0, _WORLD_PX, _WORLD_PX)   # whole world, pannable
                    self._legend.setText("World basemap · no detections loaded. Blue = few · red = many.")
                    self._view.fit(self._scene.sceneRect())   # frame the globe (re-fits on first resize)
                else:
                    self._scene.setSceneRect(0, 0, _CANVAS_W, _CANVAS_H)
                    self._legend.setText("No detections loaded. Blue = few sightings · red = many.")
                self._draw_location_marker()                   # marker can show over an empty/basemap-only map
                return
            counts = [_as_count(f.get("properties")) for f in self._features]
            maxc = max(counts)
            # Project every camera into the ONE shared global-mercator plane (world_px), so the (Phase B)
            # world basemap will align with these dots at every zoom. Then find the set's extent.
            proj: "List[Tuple[float, float, float]]" = []
            for feat, c in zip(self._features, counts):
                lon = feat["geometry"]["coordinates"][0]
                lat = feat["geometry"]["coordinates"][1]
                x, y = world_px(lat, lon)
                t = (c - 1) / (maxc - 1) if maxc > 1 else 0.0     # normalized density
                proj.append((x, y, t))
            xs = [p[0] for p in proj]
            ys = [p[1] for p in proj]
            spanx, spany = max(xs) - min(xs), max(ys) - min(ys)
            span = max(spanx, spany, 1.0)                          # floor avoids a zero-size dot/rect
            # Position is absolute (world_px); the dot RADIUS scales with the set's extent so cameras stay
            # visible whether the scan spans a city block or a continent. Hotter -> larger.
            dots = []
            minx = miny = float("inf")
            maxx = maxy = float("-inf")
            for x, y, t in proj:
                r8, g8, b8 = heat_color(t)
                radius = span * (0.010 + 0.014 * t)
                dots.append((x, y, radius, QColor(r8, g8, b8)))
                minx = min(minx, x - radius); miny = min(miny, y - radius)
                maxx = max(maxx, x + radius); maxy = max(maxy, y + radius)
            # ONE item for the whole set: a single BSP entry + a float list in memory (not N QGraphicsItems),
            # and _CameraLayer.paint() draws only the dots in the exposed viewport, so off-screen cameras cost
            # nothing on pan/zoom. Radius still keys off the FULL set's span (computed above) so dots don't
            # resize as you pan.
            self._camera_bounds = QRectF(minx, miny, maxx - minx, maxy - miny)
            self._camera_layer = _CameraLayer(dots, self._camera_bounds)
            self._scene.addItem(self._camera_layer)
            # Scene rect: with the basemap on, the whole world is the scene so you can pan/zoom out to it
            # (reset_view still re-frames the cameras). Without it, just the cameras' extent + a margin so
            # pan/zoom has room and edge dots aren't clipped.
            if show_base:
                self._scene.setSceneRect(0, 0, _WORLD_PX, _WORLD_PX)
            else:
                margin = span * (0.05 + 0.024)
                self._scene.setSceneRect(min(xs) - margin, min(ys) - margin,
                                         spanx + 2 * margin, spany + 2 * margin)
            base_note = " · world basemap" if show_base else ""
            self._legend.setText(
                f"{len(self._features)} camera(s) · blue = few sightings · red = many (up to {maxc}){base_note}. "
                f"Drag to pan · scroll to zoom.")
            self._draw_location_marker()                       # keep the "you are here" pin above the redraw

        def reset_view(self) -> None:
            """Re-frame the CAMERAS: drop any pan/zoom and fit the whole camera set into the view (so the
            world basemap, which spans the globe, doesn't hijack the framing). Falls back to the scene rect
            when there are no cameras; safe on an empty scene (nothing valid to fit → view left as-is)."""
            self._view.resetTransform()
            if self._camera_layer is not None and not self._camera_bounds.isEmpty():
                rect = self._camera_bounds
            else:
                rect = self._scene.sceneRect()
            if rect.isValid() and not rect.isEmpty():
                self._view.fit(rect)

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
            self._live_worker = _FlockWorker(gps, 9600, dev, 115200, self._default_checkpoint_path())
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
            self._basemap_group = None
            self._location_marker = None
            self._unloaded = True

        def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
            self._visible = True
            if self._latest_gj is not None:
                self.set_geojson(self._latest_gj)   # live: catch up to the newest data + rebuild the scene
            elif self._unloaded:
                self._rebuild()                       # loaded-from-file map: rebuild from the retained _features
            self._unloaded = False
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
        def _on_load(self) -> None:
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Flock scan (cameras.geojson)", self._flock_data_dir(),
                "GeoJSON (*.geojson *.json);;All files (*)")
            if path:
                self.load_geojson_file(path)

        def _open_data_folder(self) -> None:
            """Reveal the canonical Flock data folder in the OS file manager (best-effort)."""
            from PyQt5.QtGui import QDesktopServices
            from PyQt5.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._flock_data_dir()))

except ImportError:  # PyQt5 unavailable — the pure projection core above is still importable/testable.
    FlockHeatmapTab = None  # type: ignore[assignment,misc]
