"""Wi-Fi CSI sensing — the pure, Qt-free core (WS1 / P0 feasibility spike).

This is occupancy/motion SENSING, not a camera: no pixels, no image, no faces — only inference from
how a moving body perturbs Wi-Fi multipath. :data:`SENSING_TIERS` below is the honesty spine of the
whole feature: it fixes, up front, what commodity sub-7 GHz Wi-Fi CSI can (PROVEN), might in-domain
(EXPERIMENTAL), and physically cannot (NOT_SUPPORTED) do — so every front-end labels each capability
truthfully and HARD-REFUSES the impossible ones (imaging / pose / through-wall). Physics + refs:
`command-center/projects/cc-app/WIFI-CAMERA-DESIGN-BRIEF-2026-07-29.md` §A.

A node emits only a compact ~35 B verdict line (never raw CSI — it can't ride the 219 B sealed
NodeLink frame, ``node_crypto.py``); this module parses + validates that verdict. numpy-optional:
the parse/validate path is pure stdlib; heavier DSP (P2) degrades gracefully without numpy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The sealed NodeLink (node_crypto, AES-256-GCM) carries at most this many plaintext bytes/frame and
# RAISES rather than truncates - a node NEVER streams raw CSI (~256 B); it emits only the verdict.
NODELINK_MAX_PLAINTEXT = 219

# ── capability honesty tiers (design brief §A) ──────────────────────────────
PROVEN = "proven"                # ship: robust ~99% in-domain, amplitude-only
EXPERIMENTAL = "experimental"    # ship LABELED + consent-gated: good in-domain only, collapses out
NOT_SUPPORTED = "not-supported"  # hard-blocked + refused in the UI: physically out of reach


@dataclass(frozen=True)
class Capability:
    """One row of the honesty table: a capability, the tier it truthfully sits in, and why."""
    key: str
    label: str
    tier: str
    note: str


# The LOCKED honesty table (proven -> experimental -> not-supported). The UI reads ``tier`` to badge
# each capability + HARD-REFUSE anything NOT_SUPPORTED. Don't promote a row without the physics.
SENSING_TIERS: "tuple[Capability, ...]" = (
    Capability("presence", "Presence / occupancy", PROVEN,
               "Is the room occupied. Robust ~99% in-domain; needs empty-room calibration."),
    Capability("motion", "Coarse motion / activity level", PROVEN,
               "Still vs walking vs running, enter/leave. Amplitude-only."),
    Capability("zone_localization", "Zone / room-level localization + occupancy heatmap",
               EXPERIMENTAL, "Multi-node amplitude ratios -> zone (not coords). In-domain only."),
    Capability("breathing", "Single-person breathing rate", EXPERIMENTAL,
               "Fragile to body angle / Fresnel blind spots; heartbeat marginal."),
    Capability("people_count", "Coarse people-count (0-~10)", EXPERIMENTAL,
               "In-domain only, per-site ML; collapses out-of-domain."),
    Capability("gesture", "Fixed-vocab gesture / activity", EXPERIMENTAL,
               "Per-site ML + mandatory on-site enrollment."),
    Capability("imaging", "True imaging / a picture", NOT_SUPPORTED,
               "No pixels exist — physically impossible on commodity sub-7 GHz Wi-Fi."),
    Capability("pose", "Body pose / skeleton", NOT_SUPPORTED,
               "Needs a purpose-built antenna array + heavy DL; not a single node."),
    Capability("through_wall", "Through-wall imaging", NOT_SUPPORTED,
               "The 'Wi-Fi sees through walls' result (RF-Pose) is FMCW radar, not Wi-Fi CSI."),
    Capability("multi_person_separation", "Multi-person separation", NOT_SUPPORTED,
               "All clean results are single-target; ~97-99% feature overlap on ESP32."),
    Capability("gait_id", "Gait / biometric identification", NOT_SUPPORTED,
               "Multi-person gait ID ~39-56% on ESP32 (near chance) — a sensing-quality limit."),
    Capability("metric_tracking", "Metric (x,y,cm) tracking", NOT_SUPPORTED,
               "Range resolution c/2B is meters on buildable bands, not centimeters."),
)

_TIER_BY_KEY = {c.key: c.tier for c in SENSING_TIERS}


def capability_tier(key: str) -> "Optional[str]":
    """The honesty tier for a capability key, or None if the key is unknown."""
    return _TIER_BY_KEY.get(key)


def is_supported(key: str) -> bool:
    """False for a NOT_SUPPORTED (imaging/pose/through-wall/gait/metric) OR an unknown key.
    The UI hard-refuses these - never a 'coming soon' path (honest-functionality is structural)."""
    return _TIER_BY_KEY.get(key) in (PROVEN, EXPERIMENTAL)


# ── the node verdict (the ONLY thing a node emits — never raw CSI) ───────────
@dataclass(frozen=True)
class SensingVerdict:
    """One node's compact sensing verdict.

    presence: room occupied. motion: 0..1 activity level. confidence: 0..1. tier: the honesty tier
    the verdict is claimed under (a node's presence/motion is PROVEN). node_id: which node.
    """
    presence: bool
    motion: float
    confidence: float
    tier: str = PROVEN
    node_id: str = ""


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def parse_verdict(line: str, node_id: str = "") -> "Optional[SensingVerdict]":
    """Parse a node's ~35 B verdict line, e.g. ``csi presence=1 motion=0.42 conf=0.82``.

    TOLERANT - never raises: returns None on a line that isn't ``csi`` or is unparseable, so
    a noisy serial / NodeLink stream can't crash ingest. Extra/unknown tokens are ignored; motion +
    confidence clamp to 0..1. Modeled on the one-line-per-event parsers in ``src/protocols``.
    """
    if not line:
        return None
    toks = line.strip().split()
    if not toks or toks[0].lower() != "csi":
        return None
    fields: "dict[str, str]" = {}
    for t in toks[1:]:
        if "=" in t:
            k, _, v = t.partition("=")
            fields[k.strip().lower()] = v.strip()
    if "presence" not in fields:
        return None
    try:
        presence = fields["presence"] not in ("0", "false", "no", "")
        motion = _clamp01(float(fields.get("motion", "0") or "0"))
        conf = _clamp01(float(fields.get("conf", fields.get("confidence", "0")) or "0"))
    except (ValueError, TypeError):
        return None
    tier = fields.get("tier", PROVEN)
    if tier not in (PROVEN, EXPERIMENTAL, NOT_SUPPORTED):
        tier = PROVEN
    return SensingVerdict(presence=presence, motion=motion, confidence=conf, tier=tier,
                          node_id=node_id or fields.get("node", ""))


def verdict_fits_nodelink(payload: str) -> bool:
    """True if *payload* fits the sealed NodeLink frame (<= 219 B). The node emits only the
    compact verdict so it rides the EXISTING bridge with zero new transport; a payload that
    would overflow must be rejected at build time, never truncated (``node_crypto`` raises)."""
    return len(payload.encode("utf-8")) <= NODELINK_MAX_PLAINTEXT
