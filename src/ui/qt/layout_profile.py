"""Pure, Qt-free layout resolver for the adaptive GUI rebuild (Wave 3).

``layout_profile(width, height, touch, dpi)`` maps a window/screen geometry plus the input
modality to a :class:`LayoutProfile` — a small breakpoint descriptor the widgets consult when
they lay themselves out. It carries NO Qt dependency and NO side effects, so it is fully
unit-testable headless and can be reasoned about in isolation from the running app.

Two things this resolver is deliberately NOT:

* It does **not** touch or override the user's Simple/Pro *depth* choice
  (``settings["interface"]["mode"]``, toggled via View ▸ Interface Mode / Ctrl+M). That is a
  progressive-disclosure preference the user owns; the resolver only offers a ``depth_hint`` an
  unset/first-run caller *may* seed from. Pro has zero feature penalty, so a hint never hides a
  control the user asked to see.
* It does **not** import Qt or read global state. Callers pass in the geometry they measured
  (``resizeEvent``/``screenGeometry``) and the resolver returns a value; wiring it into widgets
  is a separate, later step.

DPI handling: ``width``/``height`` are the raw pixel dimensions of the surface; ``dpi`` is its
logical DPI. Breakpoints are evaluated in **reference points** (``px * 96 / dpi``) so a physically
small high-DPI panel (e.g. 1920px at 192 dpi ≈ 960 ref-pt) classifies by its real size, not its
pixel count.
"""
from __future__ import annotations

from dataclasses import dataclass

# Breakpoints in reference points (device-independent; 96 dpi == 1 px per point).
COMPACT_MAX = 600.0   # below this width: phone / tiny floating window — single column, dense chrome
REGULAR_MAX = 1024.0  # below this width: tablet / small desktop — two columns
# at or above REGULAR_MAX: expanded desktop — multi-column, full chrome

REFERENCE_DPI = 96.0

# Minimum interactive hit-target edge, in reference points (callers scale back up by dpi/96).
TOUCH_TARGET_PT = 44    # Apple/Material touch minimum
POINTER_TARGET_PT = 28  # comfortable for a mouse/trackpad


@dataclass(frozen=True)
class LayoutProfile:
    """Immutable description of how the UI should arrange itself for one geometry + input pair."""

    size: str            # "compact" | "regular" | "expanded"
    density: str         # "touch" | "pointer"
    depth_hint: str      # "simple" | "pro" — advisory only; the user's Ctrl+M choice always wins
    columns: int         # suggested column count for grid surfaces
    min_target_pt: int   # minimum hit-target edge in reference points
    dense_chrome: bool   # collapse toolbars / prefer overflow menus when True
    ref_width: float     # dpi-normalized width the classification was made on
    ref_height: float    # dpi-normalized height

    @property
    def is_compact(self) -> bool:
        return self.size == "compact"

    @property
    def is_expanded(self) -> bool:
        return self.size == "expanded"

    @property
    def is_touch(self) -> bool:
        return self.density == "touch"


def _size_class(ref_width: float) -> str:
    if ref_width < COMPACT_MAX:
        return "compact"
    if ref_width < REGULAR_MAX:
        return "regular"
    return "expanded"


def layout_profile(
    width: float,
    height: float,
    touch: bool = False,
    dpi: float = REFERENCE_DPI,
) -> LayoutProfile:
    """Resolve a :class:`LayoutProfile` from a surface geometry and its input modality.

    ``width``/``height`` are raw surface pixels; ``dpi`` is the surface's logical DPI (defaults to
    the 96-dpi reference, i.e. treat the inputs as already-logical points). Non-positive dimensions
    clamp to 0 and a non-positive/absurd ``dpi`` falls back to the reference, so the function never
    divides by zero and always returns a valid profile.
    """
    safe_dpi = dpi if dpi and dpi > 0 else REFERENCE_DPI
    scale = REFERENCE_DPI / safe_dpi
    ref_width = max(0.0, width) * scale
    ref_height = max(0.0, height) * scale

    size = _size_class(ref_width)
    density = "touch" if touch else "pointer"

    # A cramped canvas or a touch surface leans toward the streamlined depth; roomy pointer-driven
    # desktops lean Pro. This is only a seed for callers with no stored preference — never override.
    depth_hint = "simple" if (size == "compact" or touch) else "pro"

    columns = 1 if size == "compact" else 2 if size == "regular" else 3
    min_target_pt = TOUCH_TARGET_PT if touch else POINTER_TARGET_PT
    dense_chrome = size == "compact"

    return LayoutProfile(
        size=size,
        density=density,
        depth_hint=depth_hint,
        columns=columns,
        min_target_pt=min_target_pt,
        dense_chrome=dense_chrome,
        ref_width=ref_width,
        ref_height=ref_height,
    )


# ── Per-screen layout decisions (pure; the widgets apply them) ──────────────────────────────────
# Wave-3 rebuild: keep each screen's "how do I arrange for this profile" decision here as pure data,
# so it's unit-testable without a live Qt widget. The widget only maps the decision to Qt calls.
@dataclass(frozen=True)
class FlashLayout:
    """How the Flash tab arranges its top row (port · profile · actions) for a given profile."""

    stack_top_row: bool   # True = stack the three cards vertically (compact); else a horizontal row
    collapse_chrome: bool  # dense chrome (compact) — collapse toolbars / trim non-essential status


def flash_layout(profile: LayoutProfile) -> FlashLayout:
    """Decide the Flash tab's top-row arrangement from a :class:`LayoutProfile`. Depends only on the
    SIZE axis (a cramped canvas stacks; anything roomier keeps the row). Depth (Simple/Pro) is the
    user's separate choice and is NOT touched here."""
    return FlashLayout(stack_top_row=profile.is_compact, collapse_chrome=profile.dense_chrome)
