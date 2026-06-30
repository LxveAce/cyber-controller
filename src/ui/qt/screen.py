"""Screen / cyberdeck scaling helpers.

Cyber Controller is a *cyberdeck* controller — it runs on everything from a ~480p touchscreen panel to a
4K desktop. These small, mostly-pure helpers let the window adapt instead of assuming a desktop:

* ``enable_high_dpi()`` — turn on Qt fractional high-DPI scaling (must run *before* the QApplication is
  built; a no-op if one already exists).
* ``adaptive_minimum_size()`` — shrink the hard 900x600 minimum to fit a small deck screen.
* ``recommended_ui_mode()`` — auto-pick the streamlined Simple interface on small/touch screens unless the
  user explicitly chose a mode.

The size/mode functions are pure (no Qt) so they unit-test without a display.
"""

from __future__ import annotations


def enable_high_dpi() -> bool:
    """Enable high-DPI scaling attributes on the QApplication class.

    MUST be called before the QApplication is constructed (Qt reads these attributes at construction).
    Safe to call from every QApplication-creation site: it no-ops once an instance exists, so the first
    caller wins and the rest are harmless.

    Returns True if the attributes were applied (no app existed yet), else False.
    """
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — PyQt5 not installed (headless/CLI); nothing to do
        return False

    if QApplication.instance() is not None:
        return False

    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:  # noqa: BLE001 — very old Qt; best-effort
        pass
    # Fractional rounding (Qt 5.14+): a 150%/175% 4K panel scales smoothly instead of snapping to 200%.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:  # noqa: BLE001 — attribute absent on older PyQt5
        pass
    return True


def adaptive_minimum_size(
    avail_w: int,
    avail_h: int,
    *,
    want_w: int = 900,
    want_h: int = 600,
    floor_w: int = 480,
    floor_h: int = 320,
    margin_w: int = 40,
    margin_h: int = 80,
) -> tuple[int, int]:
    """Return a window minimum size that actually fits the available screen.

    The desktop ideal is ``want_w x want_h`` (900x600), but a 800x480 / 1024x600 cyberdeck panel can't
    hold a 900-wide window — so clamp the minimum down to ``avail - margin``, never below ``floor`` (480x320,
    small enough for the tiniest common deck screens). Pure; no Qt needed.
    """
    w = max(floor_w, min(want_w, avail_w - margin_w))
    h = max(floor_h, min(want_h, avail_h - margin_h))
    return (w, h)


def adaptive_launch_size(
    avail_w: int,
    avail_h: int,
    *,
    want_w: int = 1280,
    want_h: int = 800,
    margin_w: int = 20,
    margin_h: int = 40,
) -> tuple[int, int]:
    """Return an initial window size clamped to the screen (so it doesn't open larger than a small deck)."""
    return (min(want_w, avail_w - margin_w), min(want_h, avail_h - margin_h))


def recommended_ui_mode(
    avail_h: int,
    explicit: str | None = None,
    *,
    touch: bool = False,
    small_h: int = 600,
) -> str:
    """Pick the interface mode for the current screen.

    If the user explicitly chose ``simple``/``pro``, honor it. Otherwise auto-select **Simple** on a small
    or touch screen (less per-tab clutter, bigger hit targets) and **Pro** on a roomy desktop. Pure.
    """
    e = (explicit or "").lower()
    if e in ("simple", "pro"):
        return e
    if touch or avail_h <= small_h:
        return "simple"
    return "pro"
