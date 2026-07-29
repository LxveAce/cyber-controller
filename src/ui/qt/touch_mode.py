"""Shared touch-mode resolution for the responsive layout deciders (GUI rebuild Wave-3).

Every tab's ``_relayout_*`` needs to know whether to build touch-sized chrome — bigger hit targets,
the Nodes action row's 1-wide stack, the Crack panels' stacking — via ``layout_profile(touch=?)``.
Before this they all hard-coded ``touch=False``, so ALL of that touch responsiveness was dead code
on a real touchscreen (e.g. a CYD deck). This is the one place that decision is made:

    a user OVERRIDE (Settings "Touch mode" = auto / on / off), layered over runtime touch DETECTION.

- ``on`` / ``off`` force it (so the touch axis is reachable even where detection is unreliable);
- ``auto`` (default) uses ``QTouchDevice.devices()`` — a non-empty list means a touch device is
  present. The override is a process global set once from settings at startup (and by the Settings
  control), so ``touch_active()`` stays cheap enough to call on every debounced relayout (no disk).

NOTE: the auto-detection path is not yet validated on real touch hardware — confirm on a touchscreen
board before trusting ``auto`` there; ``on`` is the reliable manual path meanwhile.
"""
from __future__ import annotations

_VALID = ("auto", "on", "off")
_TOUCH_MODE = "auto"   # user override; set_touch_mode() updates it (from settings + the UI control)


def set_touch_mode(mode: str) -> None:
    """Set the override to one of auto/on/off (anything else is ignored, keeping the current)."""
    global _TOUCH_MODE
    m = str(mode).lower().strip()
    if m in _VALID:
        _TOUCH_MODE = m


def get_touch_mode() -> str:
    """The current override (auto/on/off)."""
    return _TOUCH_MODE


def _has_touch_device() -> bool:
    """True when Qt reports a touch input device. Defensive: any failure -> False (pointer)."""
    try:
        from PyQt5.QtGui import QTouchDevice
        return bool(QTouchDevice.devices())
    except Exception:  # noqa: BLE001 — no Qt / no touch API -> treat as no touch
        return False


def touch_active() -> bool:
    """Whether the layout deciders should build touch-sized chrome. ``on``/``off`` force it;
    ``auto`` detects. Tabs pass this as ``layout_profile(..., touch=touch_active())``."""
    if _TOUCH_MODE == "on":
        return True
    if _TOUCH_MODE == "off":
        return False
    return _has_touch_device()
