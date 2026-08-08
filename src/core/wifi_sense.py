"""Wi-Fi Sensing (occupancy / motion) — host-side verdict parser + capability schema (WS1).

This is the shared, firmware-agnostic host layer for the "Wi-Fi Sense" feature (design brief:
command-center/projects/cc-app/WIFI-CAMERA-DESIGN-BRIEF-2026-07-29.md). Both build targets — an
ESP32 CSI node and a router/AP capture box — stream the SAME compact verdict to CC; this module
parses it and carries the honesty schema the UI must enforce.

HONESTY (hard rules — do not soften in any UI built on this):
  * There is NO camera and NO image. Everything is inference from how a moving body perturbs Wi-Fi
    multipath. Ship as "Wi-Fi Sensing (occupancy)"; "wifi-camera" is only a colloquial hook and
    always carries the "not a literal camera — occupancy/motion, no images of people" disclaimer.
  * The firmware emits a compact verdict ONLY (``csi presence=1 motion=0.42 conf=0.82``), never raw
    CSI and never anything resembling an image, pose, or identity.
  * NOT_SUPPORTED capabilities (imaging, body pose/skeleton, through-wall imaging, multi-person
    separation, gait/biometric ID, metric x/y tracking) are physically out of reach on commodity
    single-antenna nodes and MUST be hard-refused in the UI, not offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Colloquial hook only — always paired with the disclaimer below.
FEATURE_HOOK = "wifi-camera"
#: The real, shippable name + view label.
FEATURE_NAME = "Wi-Fi Sensing (occupancy)"
VIEW_LABEL = "Sense"
DISCLAIMER = (
    "Not a literal camera — Wi-Fi occupancy/motion sensing only. No images, no faces, no pose. "
    "It infers presence and movement from how a body perturbs Wi-Fi multipath."
)

#: Capability tiers from the brief §A. The UI shows PROVEN plainly, labels + consent-gates
#: EXPERIMENTAL, and HARD-REFUSES anything in NOT_SUPPORTED (never renders a control implying it).
PROVEN = (
    "presence / occupancy (empty vs occupied)",
    "coarse motion / activity level (still vs walking vs running; enter/leave)",
)
EXPERIMENTAL = (
    "zone/room-level localization + occupancy heatmap (multi-node)",
    "single-person breathing rate",
    "coarse people-count (0-~10, in-domain)",
    "fixed-vocabulary gesture/activity (per-site ML)",
)
NOT_SUPPORTED = (
    "true imaging / a picture",
    "body pose / skeleton",
    "through-wall imaging",
    "multi-person separation",
    "gait / biometric identification",
    "metric (x, y, cm) tracking",
)


def is_supported_claim(claim: str) -> bool:
    """False for any capability in NOT_SUPPORTED (case/substring-insensitive) — the UI uses this to
    REFUSE rendering imaging/pose/biometric affordances rather than implying they exist."""
    c = (claim or "").strip().lower()
    if not c:
        return False
    banned = ("imag", "picture", "pose", "skeleton", "through-wall", "through wall",
              "biometric", "gait", "identif", "facial", "face recognition")
    return not any(b in c for b in banned)


@dataclass(frozen=True)
class SenseVerdict:
    """One occupancy/motion verdict from a CSI node. Amplitude-only inference — never an image.

    presence:  is the monitored zone occupied.
    motion:    coarse activity level in [0, 1] (0 = still, higher = more movement).
    confidence: model confidence in [0, 1]; low confidence usually means out-of-domain / needs
                (re)calibration against an empty-room baseline.
    raw:       the original verdict line (for the terminal/log).
    """

    presence: bool
    motion: float
    confidence: float
    raw: str = ""

    @property
    def needs_calibration(self) -> bool:
        """Low confidence is the honest signal to (re)run the empty-room calibration — domain shift
        (changed layout/furniture/person/NIC), not sensor noise, is the dominant error source."""
        return self.confidence < 0.5


_KV = re.compile(r"(presence|motion|conf|confidence)\s*[=:]\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def parse_verdict(line: str) -> "SenseVerdict | None":
    """Parse a CSI node's compact verdict (e.g. ``csi presence=1 motion=0.42 conf=0.82``) into a
    :class:`SenseVerdict`, or return None if the line carries no recognizable sensing fields.

    Tolerant by design: key order is free, ``conf``/``confidence`` both accepted, extra tokens are
    ignored, and a bare ``presence`` with no motion/conf still parses (motion/conf default to 0.0).
    Pure + firmware-agnostic — the same parser serves the ESP32-node and router/AP capture paths.
    NEVER interprets anything as an image/pose; it only reads presence/motion/confidence scalars.
    """
    if not line:
        return None
    fields = {}
    for key, val in _KV.findall(line):
        k = key.lower()
        k = "confidence" if k in ("conf", "confidence") else k
        try:
            fields[k] = float(val)
        except ValueError:
            continue
    if "presence" not in fields and "motion" not in fields:
        return None  # not a sensing verdict — don't fabricate a reading
    return SenseVerdict(
        presence=bool(fields.get("presence", 0.0) >= 0.5),
        motion=_clamp01(fields.get("motion", 0.0)),
        confidence=_clamp01(fields.get("confidence", 0.0)),
        raw=line.strip(),
    )
