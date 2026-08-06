"""Pure, Qt-free navigation model for the Spade GUI (v2) — the single source of the app's IA.

The Spade design collapses the app to ~5 operator-job **surfaces** rendered as one two-axis
navigation (Axis 1 = the surface rail; Axis 2 = a one-level drill-down in a surface). This module
is that IA expressed once, as data, with **no Qt import and no side effects** — so it is fully
unit-testable headless and every renderer (Qt sidebar/rail/bottom-bar, the web PWA, the tui) paints
the SAME tree. It mirrors :mod:`src.ui.qt.layout_profile`: a small pure descriptor the UI consults.

Two properties the design leans on:

* **Verb surfaces, hard-capped at 5 + a pinned Settings.** ``RIG · HUNT · OPERATE · CRACK · MAP``
  read left-to-right as the mission arc. (The verb labels vs the older WS-6 nouns are an owner call;
  the ``key``s are stable, so a label flip never churns wiring.)
* **Honest-functionality is structural.** A node may carry a ``capability_key``; :func:`visible_nav`
  drops any node whose capability has no registered provider, so a surface with no real backing is
  *absent from the tree* rather than shipped as a "coming soon" tile. The reserved **Sense**
  (counter-surveillance) surface uses this: designed into the IA now but hidden until the
  node-firmware provider exists.

Nothing here wires into the app yet — P0 of the Spade plan is substrate only. ``_tab_registry``
and the renderers consume this in a later phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class NavNode:
    """One node in the navigation tree — a surface (Axis 1) or a drill-down view (Axis 2).

    ``key`` is the stable identifier wiring keys off (never user-facing); ``label`` is the display
    text (may flip verb<->noun without churning anything). ``capability_key`` (when set) gates the
    node's *presence*: :func:`visible_nav` removes it unless a provider is registered for that
    capability. ``children`` are the drill-down views in a surface (depth capped at 1 level of
    children by the design, but the structure does not enforce that — the renderer does).
    """

    key: str
    label: str
    icon: str = ""              # renderer-mapped icon hint (name or glyph), never a Qt object
    capability_key: Optional[str] = None  # None => always present; else gated on a real provider
    primary_action: Optional[str] = None  # the surface's headline op (e.g. the floating Start/Stop)
    children: tuple["NavNode", ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """JSON-serializable form (the web/api renders the identical tree from this)."""
        d: dict = {"key": self.key, "label": self.label}
        if self.icon:
            d["icon"] = self.icon
        if self.capability_key is not None:
            d["capability_key"] = self.capability_key
        if self.primary_action is not None:
            d["primary_action"] = self.primary_action
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _n(key: str, label: str, icon: str = "", children: tuple = (), **kw) -> NavNode:
    return NavNode(key=key, label=label, icon=icon, children=tuple(children), **kw)


# The canonical Spade surface set (Axis 1). Verb labels are the default (owner may flip to the
# WS-6 nouns Flash/Connect/Operate/Survey/Analyze — keys stay). Children are the sub-views that
# re-parent into each surface (per CC-SPADE-DESIGN §2). nrf/NFC browse tiles are absent
# (honest-functionality: no real screen). Sense is capability-gated until node firmware.
_SURFACES: tuple[NavNode, ...] = (
    _n("rig", "DEVICE", "rig", primary_action="connect", children=(
        _n("dashboard", "Dashboard", "gauge"),   # the reform landing (re-homes Devices + Health)
        _n("flash_firmware", "Firmware", "chip"),
        _n("flash_os", "Software OS", "disk"),
        _n("cross_comm", "Mesh", "link"),        # Nodes re-homes here (follow-up)
    )),
    _n("hunt", "HUNT", "radar", primary_action="scan", children=(
        _n("wifi", "Wi-Fi", "wifi"),
        _n("ble", "BLE", "bluetooth"),
        _n("targets", "Targets", "crosshair"),
        _n("graph", "Graph", "graph"),
    )),
    _n("operate", "OPERATE", "console", primary_action="operate", children=(
        _n("home", "Home", "operate"),        # the dual-axis launcher (leads the surface)
        _n("control", "Control", "console"),  # QA-1 (owner #9): fan-out Broadcast + single-device Console, merged
        _n("macros", "Macros", "macro"),
    )),
    _n("crack", "CRACK", "key", primary_action="crack", children=(
        _n("crack_lab", "Crack Lab", "key"),
    )),
    _n("map", "MAP", "map", primary_action="drive", children=(
        _n("wardrive", "Wardrive", "route"),
        _n("multi_wardrive", "Multi-Wardrive", "route"),
        _n("flock", "Flock Map", "camera"),
    )),
    # Reserved 6th surface — designed into the IA now, hidden until a real counter-surveil provider
    # (node firmware) registers "sense". Until then it renders as an orchid filter/layer
    # across HUNT + MAP (see the design doc), not as an inert tab.
    _n("sense", "SENSE", "eye", capability_key="sense", primary_action="detect", children=()),
)

# Settings is utility nav (a pinned gear), not one of the 5 job-surfaces — kept apart so the
# rail stays hard-capped at 5 thumb-slots with no overflow logic.
_SETTINGS = _n("settings", "Settings", "gear")


def surfaces() -> tuple[NavNode, ...]:
    """The canonical job-surface tuple (Axis 1), excluding the pinned Settings gear."""
    return _SURFACES


def settings_node() -> NavNode:
    """The pinned Settings utility node (rendered apart from the 5 job-surfaces)."""
    return _SETTINGS


def nav_spec() -> dict:
    """The whole IA as a JSON-serializable dict: the surface list + the pinned settings node.

    The web ``GET /api/nav`` and any non-Qt renderer build their nav from exactly this, so the
    IA never diverges per frontend (today the web hand-builds a different tree).
    """
    return {
        "version": 2,
        "surfaces": [s.to_dict() for s in _SURFACES],
        "settings": _SETTINGS.to_dict(),
    }


def visible_nav(capabilities: Optional[set] = None) -> tuple[NavNode, ...]:
    """The surfaces visible given the set of capabilities a real provider backs.

    A node whose ``capability_key`` is not in *capabilities* is dropped (recursively) — so
    "wire-it-or-it-doesn't-appear" is a property of the tree, not a review promise. ``capabilities``
    of None/empty means only always-present nodes show (Sense hidden until "sense" is provided).
    """
    caps = capabilities or set()

    def keep(node: NavNode) -> Optional[NavNode]:
        if node.capability_key is not None and node.capability_key not in caps:
            return None
        kids = tuple(k for k in (keep(c) for c in node.children) if k is not None)
        # frozen dataclass: rebuild with the filtered children
        return NavNode(
            key=node.key, label=node.label, icon=node.icon,
            capability_key=node.capability_key, primary_action=node.primary_action, children=kids,
        )

    return tuple(k for k in (keep(s) for s in _SURFACES) if k is not None)
