"""Breakpoint tests for the pure adaptive-layout resolver (GUI rebuild Wave 3).

The resolver is Qt-free, so these run headless. They pin the breakpoint edges, the dpi
normalization, the input-density mapping, and the robustness of the degenerate inputs — not any
visual rendering.
"""
from __future__ import annotations

import dataclasses

import pytest

from src.ui.qt.layout_profile import (
    COMPACT_MAX,
    REGULAR_MAX,
    LayoutProfile,
    layout_profile,
)


# ── size breakpoints (at reference dpi, so px == reference points) ──────────────────────────────
def test_compact_below_first_breakpoint():
    p = layout_profile(480, 800)
    assert p.size == "compact"
    assert p.is_compact and not p.is_expanded
    assert p.columns == 1
    assert p.dense_chrome is True


def test_regular_between_breakpoints():
    p = layout_profile(800, 600)
    assert p.size == "regular"
    assert p.columns == 2
    assert p.dense_chrome is False


def test_expanded_at_and_above_second_breakpoint():
    p = layout_profile(1600, 900)
    assert p.size == "expanded"
    assert p.is_expanded
    assert p.columns == 3
    assert p.dense_chrome is False


def test_breakpoint_edges_are_half_open():
    # [0, COMPACT_MAX) compact · [COMPACT_MAX, REGULAR_MAX) regular · [REGULAR_MAX, ∞) expanded
    assert layout_profile(COMPACT_MAX - 1, 500).size == "compact"
    assert layout_profile(COMPACT_MAX, 500).size == "regular"
    assert layout_profile(REGULAR_MAX - 1, 500).size == "regular"
    assert layout_profile(REGULAR_MAX, 500).size == "expanded"


# ── dpi normalization ───────────────────────────────────────────────────────────────────────────
def test_high_dpi_shrinks_effective_size():
    # 1920 raw px on a 192-dpi panel is only ~960 reference points → regular, not expanded.
    hi = layout_profile(1920, 1080, dpi=192)
    assert hi.ref_width == pytest.approx(960.0)
    assert hi.size == "regular"
    # The same pixel count at the reference dpi is a full expanded desktop.
    lo = layout_profile(1920, 1080, dpi=96)
    assert lo.ref_width == pytest.approx(1920.0)
    assert lo.size == "expanded"


def test_default_dpi_is_identity():
    p = layout_profile(1000, 700)
    assert p.ref_width == pytest.approx(1000.0)
    assert p.ref_height == pytest.approx(700.0)


# ── input density + hit targets ──────────────────────────────────────────────────────────────────
def test_touch_density_and_target():
    p = layout_profile(1600, 900, touch=True)
    assert p.density == "touch" and p.is_touch
    assert p.min_target_pt == 44


def test_pointer_density_and_target():
    p = layout_profile(1600, 900, touch=False)
    assert p.density == "pointer" and not p.is_touch
    assert p.min_target_pt == 28


# ── depth hint (advisory only) ────────────────────────────────────────────────────────────────────
def test_depth_hint_simple_when_compact():
    assert layout_profile(400, 700).depth_hint == "simple"


def test_depth_hint_simple_when_touch_even_if_large():
    assert layout_profile(1920, 1080, touch=True).depth_hint == "simple"


def test_depth_hint_pro_on_roomy_pointer_desktop():
    assert layout_profile(1440, 900, touch=False).depth_hint == "pro"


# ── degenerate inputs never crash ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("w,h", [(0, 0), (-100, -50), (0, 900)])
def test_nonpositive_dims_clamp_to_compact(w, h):
    p = layout_profile(w, h)
    assert p.size == "compact"
    assert p.ref_width >= 0.0 and p.ref_height >= 0.0


@pytest.mark.parametrize("dpi", [0, -96, None])
def test_bad_dpi_falls_back_to_reference(dpi):
    p = layout_profile(1000, 700, dpi=dpi)  # type: ignore[arg-type]
    assert p.ref_width == pytest.approx(1000.0)  # treated as 96-dpi identity


# ── purity / value semantics ──────────────────────────────────────────────────────────────────────
def test_profile_is_frozen():
    p = layout_profile(1000, 700)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.size = "expanded"  # type: ignore[misc]


def test_resolver_is_deterministic():
    args = dict(touch=True, dpi=120)
    assert layout_profile(1280, 800, **args) == layout_profile(1280, 800, **args)


def test_returns_layout_profile_type():
    assert isinstance(layout_profile(800, 600), LayoutProfile)
