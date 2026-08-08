"""Wi-Fi Sense (WS1) host-side verdict parser + honesty schema."""
from __future__ import annotations

from src.core import wifi_sense as ws


def test_parses_the_canonical_verdict():
    v = ws.parse_verdict("csi presence=1 motion=0.42 conf=0.82")
    assert v is not None
    assert v.presence is True
    assert abs(v.motion - 0.42) < 1e-9
    assert abs(v.confidence - 0.82) < 1e-9
    assert v.raw == "csi presence=1 motion=0.42 conf=0.82"


def test_tolerant_order_spelling_and_extra_tokens():
    v = ws.parse_verdict("csi ts=123 confidence:0.9 motion=0.1 presence=0 subc=52")
    assert v.presence is False
    assert abs(v.motion - 0.1) < 1e-9
    assert abs(v.confidence - 0.9) < 1e-9


def test_bare_presence_still_parses():
    v = ws.parse_verdict("csi presence=1")
    assert v.presence is True and v.motion == 0.0 and v.confidence == 0.0


def test_clamps_out_of_range_scalars():
    v = ws.parse_verdict("csi presence=1 motion=1.9 conf=-0.3")
    assert v.motion == 1.0 and v.confidence == 0.0


def test_non_verdict_lines_return_none():
    for line in ("", "ready", "scanning APs...", "csi mode enabled", "boot ok"):
        assert ws.parse_verdict(line) is None


def test_low_confidence_flags_calibration():
    assert ws.parse_verdict("csi presence=1 motion=0.2 conf=0.3").needs_calibration is True
    assert ws.parse_verdict("csi presence=1 motion=0.2 conf=0.8").needs_calibration is False


def test_not_supported_claims_are_refused():
    # The UI uses is_supported_claim to REFUSE imaging/pose/biometric affordances.
    for banned in ("true imaging", "body pose skeleton", "through-wall imaging",
                   "gait identification", "facial recognition"):
        assert ws.is_supported_claim(banned) is False
    for ok in ("presence / occupancy", "coarse motion", "activity level"):
        assert ws.is_supported_claim(ok) is True


def test_disclaimer_and_naming_are_honest():
    assert "not a literal camera" in ws.DISCLAIMER.lower()
    assert ws.FEATURE_NAME == "Wi-Fi Sensing (occupancy)"
    # imaging/pose must be in the hard-blocked tier, never in PROVEN/EXPERIMENTAL
    offered = " ".join(ws.PROVEN + ws.EXPERIMENTAL).lower()
    assert "imag" not in offered
    assert "pose" not in offered
    assert "biometric" not in offered
