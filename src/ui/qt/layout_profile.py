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
    # Spade v2 — nav-chrome axis (defaulted so existing constructors/importers don't churn). Derived
    # from form-factor + density, not size alone, so the 7" deck avoids desktop chrome.
    nav_mode: str = "sidebar"       # "sidebar"|"rail"|"bottombar" — how Axis-1 nav renders
    rail_px: int = 200              # nav rail/sidebar width in reference points (0 when bottombar)
    terminal_docked: bool = True    # docked terminal (True) vs a pull-up/full-screen sheet

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

    # Nav-chrome axis (Spade v2): form-factor + density, not size alone.
    if size == "compact":
        nav_mode, rail_px, terminal_docked = "bottombar", 0, False          # phone: bottom tab bar
    elif size == "regular" and touch:
        # the 7" touch deck (~800x480): regular WIDTH but touch density -> icon rail + undocked
        # terminal. The old is_compact-only collapse missed this and gave the deck desktop chrome.
        nav_mode, rail_px, terminal_docked = "rail", 64, False
    else:
        nav_mode, rail_px, terminal_docked = "sidebar", 200, True   # desktop / regular-pointer

    return LayoutProfile(
        size=size,
        density=density,
        depth_hint=depth_hint,
        columns=columns,
        min_target_pt=min_target_pt,
        dense_chrome=dense_chrome,
        ref_width=ref_width,
        ref_height=ref_height,
        nav_mode=nav_mode,
        rail_px=rail_px,
        terminal_docked=terminal_docked,
    )


def min_target_qss(min_target_pt: int) -> str:
    """A tiny QSS snippet lifting interactive controls to a min hit-target height (Spade v2 touch
    density). Pure; the widget applies it. Returns '' for a non-positive target (a no-op), so a
    pointer profile clears any prior touch styling."""
    pt = int(min_target_pt)
    if pt <= 0:
        return ""
    return f"QPushButton, QToolButton, QComboBox, QLineEdit {{ min-height: {pt}px; }}"


# ── Per-screen layout decisions (pure; the widgets apply them) ──────────────────────────────────
# Wave-3 rebuild: keep each screen's "how do I arrange for this profile" decision here as pure data,
# so it's unit-testable without a live Qt widget. The widget only maps the decision to Qt calls.
@dataclass(frozen=True)
class FlashLayout:
    """How the Firmware view (RIG → Firmware) arranges its top row (port · profile · actions) for a profile."""

    stack_top_row: bool   # True = stack the three cards vertically (compact); else a horizontal row
    collapse_chrome: bool  # dense chrome (compact) — collapse toolbars / trim non-essential status


def flash_layout(profile: LayoutProfile) -> FlashLayout:
    """Decide the Firmware view's top-row arrangement from a :class:`LayoutProfile`. Depends only on the
    SIZE axis (a cramped canvas stacks; anything roomier keeps the row). Depth (Simple/Pro) is the
    user's separate choice and is NOT touched here."""
    return FlashLayout(stack_top_row=profile.is_compact, collapse_chrome=profile.dense_chrome)


@dataclass(frozen=True)
class DeviceLayout:
    """How the Devices tab arranges its device-list / detail split for a given profile."""

    stack_panels: bool     # True = stack the list above the detail (compact); else side-by-side
    collapse_chrome: bool  # dense chrome (compact) — collapse toolbars / trim non-essential status


def device_layout(profile: LayoutProfile) -> DeviceLayout:
    """Decide the Devices tab's split orientation from a :class:`LayoutProfile`. Size-driven only: a
    cramped canvas stacks the device list above the detail panel (the horizontal splitter turns
    vertical); anything roomier keeps them side by side. Depth (Simple/Pro) is the user's separate
    choice and is NOT touched here."""
    return DeviceLayout(stack_panels=profile.is_compact, collapse_chrome=profile.dense_chrome)


# ── Batch C deciders — the 7 operator/config screens ──────
# Per CC-GUI-DECIDER-CONTRACT-2026-07-28: per-screen frozen dataclasses (NOT one god-type), fields
# drawn from ONE vocabulary — `columns` (primary-grid columns), `stack` (flip the primary axis to
# vertical on a cramped canvas), `collapse_chrome` (dense chrome, == profile.dense_chrome), and
# `hit_edge_pt` (min hit-target, == profile.min_target_pt, surfaced only where a screen uses it).
# All pure + depth-invariant; the widgets apply them (Step 2). Two deliberate divergences: Crack
# stacks at 1024 (not 600), and Nodes' columns are density-driven.


@dataclass(frozen=True)
class OperateLayout:
    """Operate console grid + header."""

    columns: int
    stack: bool
    collapse_chrome: bool
    hit_edge_pt: int


def operate_layout(profile: LayoutProfile) -> OperateLayout:
    """Operate console: command grid columns from `profile.columns` (replaces the hard-coded 3 in
    `grid.addWidget(btn, i//3, i%3)`); the device/fw header stacks on compact; dense chrome shrinks
    the log; the grid/arm buttons get a hit-target min-height (they have none today)."""
    return OperateLayout(columns=profile.columns, stack=profile.is_compact,
                         collapse_chrome=profile.dense_chrome, hit_edge_pt=profile.min_target_pt)


@dataclass(frozen=True)
class CrackLayout:
    """Crack Lab panel split."""

    stack: bool
    collapse_chrome: bool
    hit_edge_pt: int


def crack_layout(profile: LayoutProfile) -> CrackLayout:
    """Crack Lab: only a true desktop (>= 1024 ref-pt) gets the controls-left / captures+log-right
    split, so it STACKS unless expanded — `stack = not is_expanded`. The one decider keyed off
    is_expanded (the 1024 breakpoint), not is_compact (600)."""
    return CrackLayout(stack=not profile.is_expanded, collapse_chrome=profile.dense_chrome,
                       hit_edge_pt=profile.min_target_pt)


@dataclass(frozen=True)
class SettingsLayout:
    """Settings card grid."""

    columns: int
    collapse_chrome: bool


def settings_layout(profile: LayoutProfile) -> SettingsLayout:
    """Settings: the nine cards flow into a 1/2/3-column grid keyed off `profile.columns`
    (`columns == 1` IS the stack). Dense chrome tightens the card margins + demotes helper text."""
    return SettingsLayout(columns=profile.columns, collapse_chrome=profile.dense_chrome)


@dataclass(frozen=True)
class MacroLayout:
    """Macro editor splitter."""

    stack: bool
    collapse_chrome: bool


def macro_layout(profile: LayoutProfile) -> MacroLayout:
    """Macro: the left/right `QSplitter` flips to vertical on compact. The proposed
    `wrap_action_row` is dropped as redundant — the toolbar wrap rides on `collapse_chrome`."""
    return MacroLayout(stack=profile.is_compact, collapse_chrome=profile.dense_chrome)


# Network graph node geometry + truncation caps, per size class. The 'regular' row reproduces the
# frozen `_NODE_W=150 / _NODE_H=46 / label[:22] / sub[:26]` the rebuild replaces.
# (node_w, node_h_base, title_chars, sub_chars); sub_chars always exceeds title_chars.
_NETWORK_GEOM = {
    "compact":  (132, 44, 18, 22),
    "regular":  (150, 46, 22, 26),
    "expanded": (176, 52, 26, 30),
}


@dataclass(frozen=True)
class NetworkLayout:
    """Network graph node geometry (the only decider with screen-specific fields)."""

    columns: int
    stack: bool
    collapse_chrome: bool
    node_w: int
    node_h: int
    title_chars: int
    sub_chars: int


def network_layout(profile: LayoutProfile) -> NetworkLayout:
    """Network graph: node size + truncation scale by size; `node_h` floors the hit-target
    (`max(base, min_target_pt)`); `columns` caps the target fan; the target field drops below the
    device column on compact. The only decider carrying screen-specific fields."""
    node_w, node_h_base, title_chars, sub_chars = _NETWORK_GEOM[profile.size]
    return NetworkLayout(columns=profile.columns, stack=profile.is_compact,
                         collapse_chrome=profile.dense_chrome, node_w=node_w,
                         node_h=max(node_h_base, profile.min_target_pt),
                         title_chars=title_chars, sub_chars=sub_chars)


@dataclass(frozen=True)
class NodesLayout:
    """Nodes action-button row."""

    columns: int
    collapse_chrome: bool
    hit_edge_pt: int


def nodes_layout(profile: LayoutProfile) -> NodesLayout:
    """Nodes: the six-button action row is DENSITY-driven — `6` when roomy, else `1` (compact
    touch, a 1-wide stack) or `2` (compact pointer, a 3x2). The single density-driven column count
    in the set (`columns == 1` IS the stack)."""
    columns = 6 if not profile.is_compact else (1 if profile.is_touch else 2)
    return NodesLayout(columns=columns, collapse_chrome=profile.dense_chrome,
                       hit_edge_pt=profile.min_target_pt)


@dataclass(frozen=True)
class WardriveMultiLayout:
    """Multi-Wardrive control rows."""

    stack: bool
    collapse_chrome: bool


def wardrive_multi_layout(profile: LayoutProfile) -> WardriveMultiLayout:
    """Multi-Wardrive: the Boards row + GPS row go vertical on compact (freeing the baud fields);
    dense chrome trims the banner + helper labels. No `columns` — the status-table columns are
    fixed semantic data."""
    return WardriveMultiLayout(stack=profile.is_compact, collapse_chrome=profile.dense_chrome)
