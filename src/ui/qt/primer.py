"""Primer formula — the reusable visual language from the approved reform mockup.

The owner's directive: "take the HTML example's formula and apply it throughout the actual
app." This module is that formula as reusable PyQt: the card grid, chips, flash badges, note bars, tiles,
buttons and fields the mockup uses, so every reformed tab reads the same way instead of each inventing its
own look. The card factory itself lives in flash_tab._make_card (shared by 8 tabs); this adds the smaller
components + the exact mockup colours so they never drift.

Colours mirror the mockup :root variables exactly.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

# ── mockup palette (:root) ───────────────────────────────────────────────
CARD = "#161b22"
CARD2 = "#0f141b"
CANVAS = "#0d1117"
BORDER = "#30363d"
BORDER2 = "#21262d"
TX = "#c9d1d9"
TX2 = "#f0f6fc"
MUT = "#8b949e"
DIM = "#6e7681"
ACC = "#a371f7"
ACC_DIM = "#5a3ba8"
GREEN = "#3fb950"
AMBER = "#d29922"
ORANGE = "#f0883e"
RED = "#f85149"
BLUE = "#58a6ff"
MONO = "'Cascadia Code','Cascadia Mono',Consolas,monospace"

# Flash-badge tints (mockup .fbadge / .fb-*): (fg, bg).
_FBADGE = {
    "ok":   (GREEN,  "#0f2417"),   # proven binary
    "exp":  (AMBER,  "#2a2109"),   # experimental
    "tool": (BLUE,   "#0d1f33"),   # needs an external tool
    "src":  (RED,    "#2b1416"),   # source-only
}
# Chip tints (mockup .chip / .chip.cap / .chip.on).
_CHIP = {
    "":    (MUT,        CANVAS, BORDER),
    "cap": ("#c8b6f5",  CANVAS, ACC_DIM),
    "on":  (GREEN,      CANVAS, "#238636"),
}


def note_bar(text: str, *, warn: bool = False) -> QLabel:
    """A mockup .notebar (info) / .warnbar (caution) — a full-width tinted note above a section."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    if warn:
        lbl.setStyleSheet("QLabel{background:#1c1408;border:1px solid #3a2c12;color:#e0b970;"
                          "border-radius:7px;padding:8px 12px;font-size:12px;}")
    else:
        lbl.setStyleSheet("QLabel{background:#141a12;border:1px solid #2c3a1c;color:#c6d67f;"
                          "border-radius:7px;padding:8px 12px;font-size:12px;}")
    return lbl


def flash_badge(text: str, kind: str) -> QLabel:
    """A mockup .fbadge — an honest firmware-status pill (kind: ok|exp|tool|src)."""
    fg, bg = _FBADGE.get(kind, (MUT, CANVAS))
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(f"QLabel{{color:{fg};background:{bg};border-radius:5px;padding:2px 8px;"
                      f"font-size:11px;font-weight:600;}}")
    return lbl


def chip(text: str, kind: str = "") -> QLabel:
    """A mockup .chip — a small rounded status pill (kind: ''|cap|on)."""
    fg, bg, bd = _CHIP.get(kind, _CHIP[""])
    lbl = QLabel(text)
    lbl.setStyleSheet(f"QLabel{{color:{fg};background:{bg};border:1px solid {bd};border-radius:20px;"
                      f"padding:2px 8px;font-size:11px;}}")
    return lbl


def footnote(text: str) -> QLabel:
    """A mockup .footnote — dim fine print under a card."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"QLabel{{color:{DIM};font-size:11px;}}")
    return lbl


def stat_tile(number: str, label: str, tone: str = "") -> QWidget:
    """A mockup .tile — a big-number stat cell (tone: ''|green|orange)."""
    col = {"green": GREEN, "orange": ORANGE}.get(tone, TX2)
    w = QFrame()
    w.setStyleSheet(f"QFrame{{background:{CANVAS};border:1px solid {BORDER};border-radius:8px;}}")
    v = QVBoxLayout(w)
    v.setContentsMargins(11, 9, 11, 9)
    v.setSpacing(1)
    n = QLabel(str(number))
    n.setStyleSheet(f"QLabel{{color:{col};font-family:{MONO};font-size:22px;font-weight:600;}}")
    ll = QLabel(label.upper())
    ll.setStyleSheet(f"QLabel{{color:{MUT};font-size:10px;letter-spacing:0.4px;}}")
    v.addWidget(n)
    v.addWidget(ll)
    return w


def tiles_row(items: "list[tuple[str, str, str]]") -> QWidget:
    """A row of stat tiles from (number, label, tone) triples (mockup .tiles)."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(10)
    for number, label, tone in items:
        h.addWidget(stat_tile(number, label, tone), 1)
    return w


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"QFrame{{color:{BORDER2};background:{BORDER2};max-height:1px;}}")
    return f


# QSS applied to a reformed tab's root so its standard widgets pick up the mockup formula (tight tables,
# .btn buttons, .field inputs, mono/dim helpers). Object-name hooks: btnGreen / btnDanger / btnWarn for the
# coloured buttons, mono / dim / muted for text tone. Kept close to the mockup so tabs read identically.
def primer_qss() -> str:
    return (
        f"QLabel#mono{{font-family:{MONO};}}"
        f"QLabel#dim{{color:{DIM};}} QLabel#muted{{color:{MUT};}}"
        # tables: muted small header, hairline rows, zebra
        f"QTableWidget,QTableView{{background:{CANVAS};gridline-color:{BORDER2};font-size:12px;"
        f"alternate-background-color:{CARD2};selection-background-color:{ACC_DIM};}}"
        f"QHeaderView::section{{background:{CANVAS};color:{MUT};border:none;"
        f"border-bottom:1px solid {BORDER};padding:5px 8px;font-size:11px;font-weight:600;}}"
        f"QTableWidget::item{{padding:5px 8px;}}"
        # fields
        f"QLineEdit,QComboBox,QPlainTextEdit,QSpinBox{{background:{CANVAS};color:{TX};"
        f"border:1px solid {BORDER};border-radius:6px;padding:6px 9px;font-size:12px;}}"
        # buttons — default + coloured object-name variants
        f"QPushButton{{background:#21262d;color:{TX};border:1px solid {BORDER};border-radius:6px;"
        f"padding:6px 11px;font-size:12px;}}"
        f"QPushButton:hover{{border-color:{DIM};}}"
        f"QPushButton#btnGreen{{background:#238636;border-color:#2ea043;color:#fff;font-weight:600;}}"
        f"QPushButton#btnDanger{{border-color:#8b2c26;color:{RED};}}"
        f"QPushButton#btnWarn{{border-color:#8a6100;color:{AMBER};}}"
        # Disabled colour-buttons dim to the neutral gray (the global #flash_btn had a :disabled rule;
        # btnGreen/btnDanger need their own since this sheet flattens the objectName-based global one).
        f"QPushButton#btnGreen:disabled,QPushButton#btnDanger:disabled,QPushButton#btnWarn:disabled"
        f"{{background:{BORDER2};border-color:{BORDER2};color:{DIM};font-weight:400;}}"
    )


def apply_primer(widget: QWidget, extra: str = "") -> None:
    """Style a reformed tab (or card) to the primer formula. Call on the tab root AFTER building it so the
    formula reaches its standard widgets; pass *extra* to append tab-specific QSS."""
    widget.setStyleSheet(primer_qss() + (extra or ""))


def card_grid(spacing: int = 12) -> "tuple[QWidget, QGridLayout]":
    """A mockup .grid / .split — a grid host. Returns (widget, layout); add cards with row/col spans.
    A QGridLayout so cards can wrap responsively when the caller reflows (see per-tab reflow helpers)."""
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(spacing)
    g.setVerticalSpacing(spacing)
    return w, g
