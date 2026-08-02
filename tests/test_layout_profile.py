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
    crack_layout,
    device_layout,
    flash_layout,
    layout_profile,
    macro_layout,
    network_layout,
    nodes_layout,
    operate_home_layout,
    operate_layout,
    settings_layout,
    wardrive_multi_layout,
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


# ── flash_layout (Flash-tab top-row decision — Wave-3 first wire) ──
def test_flash_layout_stacks_on_compact():
    fl = flash_layout(layout_profile(480, 800))  # compact
    assert fl.stack_top_row is True
    assert fl.collapse_chrome is True


def test_flash_layout_is_a_row_on_regular_and_expanded():
    for w in (800, 1600):  # regular, expanded
        fl = flash_layout(layout_profile(w, 700))
        assert fl.stack_top_row is False
        assert fl.collapse_chrome is False


def test_flash_layout_is_size_driven_not_touch():
    # A touch surface that is still roomy keeps the row — depth/density is a separate axis.
    assert flash_layout(layout_profile(1600, 900, touch=True)).stack_top_row is False
    # A compact touch surface stacks (because it's compact, not because it's touch).
    assert flash_layout(layout_profile(400, 800, touch=True)).stack_top_row is True


# ── device_layout (Devices-tab list/detail split decision — Wave-3 second wire) ──
def test_device_layout_stacks_on_compact():
    dl = device_layout(layout_profile(480, 800))  # compact
    assert dl.stack_panels is True
    assert dl.collapse_chrome is True


def test_device_layout_is_side_by_side_on_regular_and_expanded():
    for w in (800, 1600):  # regular, expanded
        dl = device_layout(layout_profile(w, 700))
        assert dl.stack_panels is False
        assert dl.collapse_chrome is False


def test_device_layout_is_size_driven_not_touch():
    # A roomy touch surface stays side-by-side — depth/density is a separate axis from size.
    assert device_layout(layout_profile(1600, 900, touch=True)).stack_panels is False
    # A compact touch surface stacks (because it's compact, not because it's touch).
    assert device_layout(layout_profile(400, 800, touch=True)).stack_panels is True


# ── Batch C: the 7 operator/config-screen deciders share one contract ──
_DECIDERS = [operate_layout, operate_home_layout, crack_layout, settings_layout, macro_layout,
             network_layout, nodes_layout, wardrive_multi_layout]
_SIZES = [(480, 800), (800, 600), (1440, 900)]  # compact / regular / expanded


@pytest.mark.parametrize("decide", _DECIDERS)
@pytest.mark.parametrize("w,h", _SIZES)
@pytest.mark.parametrize("touch", [False, True])
def test_decider_contract(decide, w, h, touch):
    p = layout_profile(w, h, touch=touch)
    out = decide(p)
    # frozen dataclass, like FlashLayout / DeviceLayout
    assert dataclasses.is_dataclass(out) and type(out).__dataclass_params__.frozen
    # pure: identical profile -> equal result
    assert decide(p) == out
    # depth (Simple/Pro) NEVER changes the decision — the user's Ctrl+M choice is a separate axis
    for depth in ("simple", "pro"):
        assert decide(dataclasses.replace(p, depth_hint=depth)) == out
    # the one universal field: every decision's chrome flag == profile.dense_chrome
    assert out.collapse_chrome is p.dense_chrome


def test_operate_home_layout_sheds_chrome_on_compact_only():
    # Zone A metric chips + the Zone C "Go deeper" label show on regular/expanded, hide on compact;
    # the strip hit-target follows density (touch 44 / pointer 28). Pill + STOP never appear here
    # (they are structural, never collapsed).
    compact = operate_home_layout(layout_profile(480, 800))
    regular = operate_home_layout(layout_profile(800, 600))
    assert (compact.show_metric_chips, compact.show_go_deeper_label) == (False, False)
    assert (regular.show_metric_chips, regular.show_go_deeper_label) == (True, True)
    assert compact.stack is True and regular.stack is False
    assert operate_home_layout(layout_profile(800, 600, touch=True)).hit_edge_pt == 44
    assert operate_home_layout(layout_profile(800, 600, touch=False)).hit_edge_pt == 28


def test_operate_layout_columns_stack_and_hit_edge():
    assert operate_layout(layout_profile(500, 800)).columns == 1
    assert operate_layout(layout_profile(800, 600)).columns == 2
    assert operate_layout(layout_profile(1400, 900)).columns == 3
    assert operate_layout(layout_profile(480, 800)).stack is True   # stack only on compact
    assert operate_layout(layout_profile(800, 600)).stack is False
    assert operate_layout(layout_profile(800, 600, touch=True)).hit_edge_pt == 44
    assert operate_layout(layout_profile(800, 600, touch=False)).hit_edge_pt == 28
    # touch never changes the column count (size-only)
    assert operate_layout(layout_profile(800, 600, touch=True)).columns == \
        operate_layout(layout_profile(800, 600, touch=False)).columns


def test_crack_layout_splits_at_1024_not_600():
    assert crack_layout(layout_profile(1023, 900)).stack is True    # still stacked just under 1024
    assert crack_layout(layout_profile(1024, 900)).stack is False   # unstacks at 1024, not 600
    assert crack_layout(layout_profile(400, 800)).stack is True
    assert crack_layout(layout_profile(800, 600)).stack is True     # regular STILL stacks
    assert crack_layout(layout_profile(599, 800)).collapse_chrome is True   # chrome keeps 600 edge
    assert crack_layout(layout_profile(600, 800)).collapse_chrome is False


def test_settings_layout_columns():
    assert settings_layout(layout_profile(480, 800)).columns == 1   # compact == the stack
    assert settings_layout(layout_profile(800, 600)).columns == 2
    assert settings_layout(layout_profile(1400, 900)).columns == 3
    assert settings_layout(layout_profile(800, 600, touch=True)).columns == 2   # density no change


def test_macro_layout_stack_only_on_compact():
    assert macro_layout(layout_profile(480, 800)).stack is True
    assert macro_layout(layout_profile(800, 600)).stack is False
    assert macro_layout(layout_profile(1440, 900)).stack is False   # regular == expanded
    assert not hasattr(macro_layout(layout_profile(800, 600)), "wrap_action_row")


def test_network_layout_geometry():
    reg = network_layout(layout_profile(800, 600))   # the frozen 'regular' geometry
    assert (reg.node_w, reg.node_h, reg.title_chars, reg.sub_chars) == (150, 46, 22, 26)
    for w, h in _SIZES:
        for touch in (False, True):
            p = layout_profile(w, h, touch=touch)
            nl = network_layout(p)
            assert nl.node_h >= p.min_target_pt      # node_h floors the hit-target
            assert nl.sub_chars > nl.title_chars     # sub caption always wider than the title
    assert network_layout(layout_profile(480, 800)).stack is True
    assert network_layout(layout_profile(1440, 900)).stack is False
    # a touch profile lifts compact node_h (base 44) to the 44pt floor
    assert network_layout(layout_profile(480, 800, touch=True)).node_h == 44


def test_nodes_layout_density_driven_columns():
    assert nodes_layout(layout_profile(480, 800, touch=False)).columns == 2   # compact pointer: 3x2
    assert nodes_layout(layout_profile(480, 800, touch=True)).columns == 1    # compact touch:1
    assert nodes_layout(layout_profile(800, 600)).columns == 6                # regular: single row
    assert nodes_layout(layout_profile(1440, 900)).columns == 6
    assert nodes_layout(layout_profile(800, 600, touch=True)).hit_edge_pt == 44
    assert nodes_layout(layout_profile(800, 600, touch=False)).hit_edge_pt == 28


def test_wardrive_multi_layout_stack_edge_density_independent():
    assert wardrive_multi_layout(layout_profile(599, 800)).stack is True
    assert wardrive_multi_layout(layout_profile(600, 800)).stack is False
    assert wardrive_multi_layout(layout_profile(400, 800, touch=True)).stack is True
    assert wardrive_multi_layout(layout_profile(400, 800, touch=False)).stack is True


def test_deciders_dpi_normalized():
    # 1920px @ 192dpi -> 960 ref-pt -> regular; a size-branching decider must see 'regular'
    p = layout_profile(1920, 1080, dpi=192)
    assert p.size == "regular"
    assert settings_layout(p).columns == 2 and operate_layout(p).columns == 2


# ── Spade v2: nav-chrome axis (nav_mode / rail_px / terminal_docked) ──────────────────────────────
def test_nav_mode_desktop_is_docked_sidebar():
    p = layout_profile(1440, 900)                    # expanded pointer desktop
    assert p.nav_mode == "sidebar" and p.terminal_docked is True and p.rail_px == 200


def test_nav_mode_regular_pointer_is_sidebar():
    p = layout_profile(900, 700)          # regular WIDTH, pointer -> still a sidebar
    assert p.nav_mode == "sidebar" and p.terminal_docked is True


def test_nav_mode_touch_deck_collapses_to_rail():
    # THE 7" deck fix: 800x480 is "regular" (<1024) but touch -> an icon rail with
    # an undocked terminal, NOT desktop chrome (the old is_compact-only gate got this wrong).
    p = layout_profile(800, 480, touch=True)
    assert p.size == "regular"
    assert p.nav_mode == "rail" and p.rail_px == 64 and p.terminal_docked is False


def test_nav_mode_phone_is_bottombar():
    p = layout_profile(400, 800, touch=True)         # compact -> bottom tab bar, no side rail
    assert p.nav_mode == "bottombar" and p.rail_px == 0 and p.terminal_docked is False


def test_min_target_qss_from_density():
    from src.ui.qt.layout_profile import min_target_qss
    # touch profile -> 44px floor; pointer -> 28px; non-positive -> a no-op empty string (clears it)
    assert "min-height: 44px" in min_target_qss(44)
    assert "min-height: 28px" in min_target_qss(28)
    assert min_target_qss(0) == "" and min_target_qss(-5) == ""
    # keyed off the profile's min_target_pt (touch=44, pointer=28)
    assert "44px" in min_target_qss(layout_profile(800, 480, touch=True).min_target_pt)
    assert "28px" in min_target_qss(layout_profile(1440, 900).min_target_pt)


def test_nav_fields_default_on_bare_construction():
    # appended defaulted fields: a LayoutProfile built without them still works
    p = LayoutProfile(size="regular", density="pointer", depth_hint="pro", columns=2,
                      min_target_pt=28, dense_chrome=False, ref_width=900.0, ref_height=700.0)
    assert p.nav_mode == "sidebar" and p.rail_px == 200 and p.terminal_docked is True
