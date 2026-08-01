"""Wi-Fi CSI sensing pure core (WS1 P0) — the honesty-tier table + the node verdict parse/validate.

Pure + Qt-free: proves the P0 spine in CI (design brief §A/§E). NO hardware — CI proves parsing +
the tier honesty, never that RF actually senses (verify-never-fake; live sensing is HW-gated in P1).
"""
from __future__ import annotations

from src.core import sensing as S


# ── the honesty table: the physics-locked tiers must not silently drift ──
def test_presence_and_motion_are_proven():
    assert S.capability_tier("presence") == S.PROVEN
    assert S.capability_tier("motion") == S.PROVEN
    assert S.is_supported("presence") and S.is_supported("motion")


def test_zone_breathing_peoplecount_are_experimental():
    for k in ("zone_localization", "breathing", "people_count", "gesture"):
        assert S.capability_tier(k) == S.EXPERIMENTAL, k
        assert S.is_supported(k), k                       # experimental ships labeled, supported


def test_imaging_pose_throughwall_gaitid_are_not_supported():
    # the physically-impossible asks the UI must HARD-REFUSE (no "coming soon" path)
    for k in ("imaging", "pose", "through_wall", "multi_person_separation", "gait_id",
              "metric_tracking"):
        assert S.capability_tier(k) == S.NOT_SUPPORTED, k
        assert not S.is_supported(k), k


def test_unknown_capability_is_not_supported():
    assert S.capability_tier("x-ray_vision") is None
    assert S.is_supported("x-ray_vision") is False        # unknown -> refuse, fail-closed


def test_table_is_internally_consistent():
    keys = [c.key for c in S.SENSING_TIERS]
    assert len(keys) == len(set(keys))                    # no dup keys
    assert all(c.tier in (S.PROVEN, S.EXPERIMENTAL, S.NOT_SUPPORTED) for c in S.SENSING_TIERS)
    assert all(c.label and c.note for c in S.SENSING_TIERS)   # every row is labeled + justified


# ── the node verdict parse (tolerant, never raises) ──
def test_parse_a_normal_verdict():
    v = S.parse_verdict("csi presence=1 motion=0.42 conf=0.82", node_id="n3")
    assert v is not None
    assert v.presence is True and v.motion == 0.42 and v.confidence == 0.82
    assert v.tier == S.PROVEN and v.node_id == "n3"


def test_presence_zero_and_falsey_forms():
    assert S.parse_verdict("csi presence=0 motion=0 conf=0.9").presence is False
    assert S.parse_verdict("csi presence=false").presence is False
    assert S.parse_verdict("csi presence=1").presence is True


def test_motion_and_conf_are_clamped():
    v = S.parse_verdict("csi presence=1 motion=9.9 conf=-3")
    assert v.motion == 1.0 and v.confidence == 0.0        # clamped to 0..1


def test_confidence_accepts_long_key_and_node_token():
    v = S.parse_verdict("csi presence=1 confidence=0.5 node=kitchen")
    assert v.confidence == 0.5 and v.node_id == "kitchen"


def test_tolerant_on_junk():
    assert S.parse_verdict("") is None
    assert S.parse_verdict("not a verdict line") is None      # wrong leader token
    assert S.parse_verdict("csi motion=0.3") is None          # no presence field
    assert S.parse_verdict("csi presence=x motion=nan conf=y") is None  # unparseable nums -> None
    assert S.parse_verdict("flock camera=1") is None          # a different protocol's line


def test_explicit_node_id_arg_wins_over_token():
    v = S.parse_verdict("csi presence=1 node=fromline", node_id="fromarg")
    assert v.node_id == "fromarg"


# ── the transport invariant: a verdict must fit the sealed NodeLink frame ──
def test_verdict_fits_nodelink():
    assert S.verdict_fits_nodelink("csi presence=1 motion=0.42 conf=0.82") is True   # ~35 B
    assert S.verdict_fits_nodelink("x" * S.NODELINK_MAX_PLAINTEXT) is True            # exactly 219
    assert S.verdict_fits_nodelink("x" * (S.NODELINK_MAX_PLAINTEXT + 1)) is False     # 220 -> over
    assert S.NODELINK_MAX_PLAINTEXT == 219                                    # node_crypto cap
