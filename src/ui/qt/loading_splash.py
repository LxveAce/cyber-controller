"""Animated startup/loading screen for the PyQt5 desktop (the heaviest UI variant only).

Shown by :func:`launch_qt` while the dashboard is constructed, then cross-faded to the main window.
The lightweight UIs (Tkinter / Textual / web) deliberately do NOT use this — per the project decision to
reserve the richer startup motion for the full GUI.

Motion follows the project's motion-design tokens (low-frequency / illustrative interaction → richer,
slower motion is acceptable):
  * fade-in   — OutQuart, ~320ms   (enter; large surface, rare)
  * progress  — linear, looping    (time-based indeterminate indicator)
  * logo pulse— InOutSine, ~1300ms (subtle delight)
  * fade-out  — OutCubic, ~260ms   (exit; faster than enter)
Honors a reduced-motion opt-out (settings ``interface.reduced_motion`` or env ``CC_REDUCED_MOTION``):
no fades/pulse, just show→close.
"""

from __future__ import annotations

import os

from PyQt5.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
)
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

_BG = QColor(13, 17, 23)
_PANEL = QColor(22, 27, 34)
_BORDER = QColor(48, 54, 61)
_ACCENT = QColor(57, 255, 20)
_TRACK = QColor(33, 38, 45)


def reduced_motion() -> bool:
    if os.environ.get("CC_REDUCED_MOTION"):
        return True
    try:
        from src.config.settings import load_settings
        return bool(load_settings().get("interface", {}).get("reduced_motion", False))
    except Exception:
        return False


class _Progress(QWidget):
    """Indeterminate progress strip — a highlight sweeps left→right on a linear loop."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1100)            # linear, matches a real "working" cadence
        self._anim.setEasingCurve(QEasingCurve.Linear)
        self._anim.setLoopCount(-1)

    def start(self):
        self._anim.start()

    def stop(self):
        self._anim.stop()

    def getPos(self):
        return self._pos

    def setPos(self, v):
        self._pos = v
        self.update()

    pos = pyqtProperty(float, fget=getPos, fset=setPos)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(_TRACK)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        seg = w * 0.32
        x = (w + seg) * self._pos - seg
        grad = QLinearGradient(x, 0, x + seg, 0)
        grad.setColorAt(0.0, QColor(57, 255, 20, 0))
        grad.setColorAt(0.5, _ACCENT)
        grad.setColorAt(1.0, QColor(57, 255, 20, 0))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(max(0, x), 0, min(seg, w - max(0, x)), h), h / 2, h / 2)


class LoadingSplash(QWidget):
    """Frameless animated loading panel shown before the dashboard appears."""

    def __init__(self, logo_path: str | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._reduced = reduced_motion()
        self.setFixedSize(440, 300)

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 30, 36, 26)
        root.setSpacing(14)
        root.addStretch(1)

        self._logo = QLabel(alignment=Qt.AlignCenter)
        if logo_path and os.path.isfile(logo_path):
            pm = QPixmap(logo_path)
            if not pm.isNull():
                self._logo.setPixmap(pm.scaledToWidth(190, Qt.SmoothTransformation))
        else:
            self._logo.setText("CYBER CONTROLLER")
            self._logo.setStyleSheet("color:#a371f7;font:bold 18px 'JetBrains Mono';")
        root.addWidget(self._logo)

        self._status = QLabel("Starting Cyber Controller…", alignment=Qt.AlignCenter)
        self._status.setStyleSheet("color:#8b949e;font:11px 'Segoe UI';background:transparent;")
        root.addWidget(self._status)

        self._bar = _Progress(self)
        root.addWidget(self._bar)
        root.addStretch(1)

        # A single opacity effect on the whole panel drives the fade-in/out. NOTE: do NOT also put an
        # opacity effect on a child (e.g. the logo) — nested QGraphicsOpacityEffects make the child fail
        # to render in Qt. The motion here is the fade transitions + the progress sweep (logo stays put).
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._eff.setOpacity(1.0 if self._reduced else 0.0)
        self._fade = QPropertyAnimation(self._eff, b"opacity", self)

    # ── painting the rounded panel ───────────────────────────────────
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        p.setPen(Qt.NoPen)
        p.setBrush(_PANEL)
        p.drawRoundedRect(r, 14, 14)
        p.setPen(QPen(_BORDER, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 14, 14)

    def _center_on_screen(self):
        scr = QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.center() - QPoint(self.width() // 2, self.height() // 2))

    def set_status(self, text: str):
        self._status.setText(text)
        QApplication.processEvents()

    def start(self):
        self._center_on_screen()
        self.show()
        self.raise_()
        self._bar.start()
        if self._reduced:
            QApplication.processEvents()
            return
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(320)
        self._fade.setEasingCurve(QEasingCurve.OutQuart)
        self._fade.start()
        QApplication.processEvents()

    def finish(self, on_done):
        """Fade out, then call on_done() and close. Honors reduced-motion (instant)."""
        self._bar.stop()
        if self._reduced:
            on_done()
            self.close()
            return
        self._fade.stop()
        self._fade.setStartValue(self._eff.opacity())
        self._fade.setEndValue(0.0)
        self._fade.setDuration(260)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        def _end():
            try:
                self._fade.finished.disconnect(_end)
            except Exception:
                pass
            on_done()
            self.close()

        self._fade.finished.connect(_end)
        self._fade.start()


def fade_in_window(win, duration: int = 320) -> None:
    """Fade the main window in (OutQuart). No-op under reduced motion."""
    if reduced_motion():
        return
    eff = QGraphicsOpacityEffect(win)
    win.setGraphicsEffect(eff)
    eff.setOpacity(0.0)
    anim = QPropertyAnimation(eff, b"opacity", win)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setDuration(duration)
    anim.setEasingCurve(QEasingCurve.OutQuart)

    def _clear():
        win.setGraphicsEffect(None)  # remove the effect so normal painting/perf resumes

    anim.finished.connect(_clear)
    win._cc_fadein_anim = anim  # keep a ref so it isn't GC'd
    anim.start()
