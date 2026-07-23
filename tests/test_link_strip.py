"""Pure link-tier logic for the LxveNode relay integration (no Qt needed).

Covers the three pure decisions the Operate console consumes: the Link-strip state derivation
(:func:`link_strip_render`), the tier-aware status-poll cadence (:func:`poll_interval_ms`), and the
tier-aware stream-command gate (:func:`stream_blocked`). These mirror the ``arm_lamp_render`` /
``safety`` predicates — decisions live here, unit-tested in isolation, so the widget stays a thin
renderer. The dicts below are exactly what ``Device.link`` holds after ``apply_link_state`` merges a
parsed ``link_state`` event.
"""
from __future__ import annotations

from src.ui.qt.link_strip import (
    POLL_BASE_MS,
    POLL_THROTTLED_MS,
    link_strip_render,
    poll_interval_ms,
    stream_blocked,
)


# ── Link-strip state derivation ──────────────────────────────────────────────

def test_no_link_hides_the_strip():
    view = link_strip_render({})
    assert view.visible is False
    assert view.text == ""


def test_non_dict_link_hides_the_strip():
    # A malformed value must never raise or show a stale strip.
    assert link_strip_render(None).visible is False  # type: ignore[arg-type]


def test_wifi_link_is_green_and_labelled():
    view = link_strip_render({"link_event": "link", "tier": "wifi", "rssi": -42, "up": True})
    assert view.visible is True
    assert "Wi-Fi" in view.text
    assert "-42 dBm" in view.text
    assert view.color == "#3fb950"


def test_lora_link_is_blue_and_shows_data_rate():
    view = link_strip_render({
        "link_event": "link", "tier": "lora", "rssi": -104, "snr": -7,
        "dr": "sf9bw125", "latency_ms": 620, "up": True, "peer": "nodeA", "role": "relay",
    })
    assert "LoRa" in view.text
    assert "sf9bw125" in view.text          # spreading-factor/data-rate shown on LoRa
    assert "620 ms" in view.text
    assert "relay > nodeA" in view.text     # role/peer legible
    assert view.color == "#58a6ff"


def test_down_link_renders_red_and_says_down():
    view = link_strip_render({"link_event": "link", "tier": "lora", "up": False})
    assert "DOWN" in view.text
    assert view.color == "#f85149"


def test_failover_frame_shows_arrow_and_tracks_new_tier():
    # A `tier` frame carries the NEW tier in `to=` (no `tier=`); after merge the strip must track `to`,
    # not lag on the previous steady frame's `tier=wifi`.
    view = link_strip_render({"link_event": "tier", "tier": "wifi", "from": "wifi",
                              "to": "lora", "reason": "rssi"})
    assert "⇄ Wi-Fi -> LoRa (rssi)" in view.text
    assert view.color == "#58a6ff"          # colored for the tier we failed over TO (LoRa)


def test_unknown_tier_renders_verbatim_muted():
    view = link_strip_render({"link_event": "link", "tier": "starlink"})
    assert "starlink" in view.text
    assert view.color == "#8b949e"


# ── Tier-aware poll cadence ──────────────────────────────────────────────────

def test_no_link_polls_at_base():
    assert poll_interval_ms({}) == POLL_BASE_MS


def test_wifi_and_espnow_poll_at_base():
    assert poll_interval_ms({"tier": "wifi"}) == POLL_BASE_MS
    assert poll_interval_ms({"tier": "espnow"}) == POLL_BASE_MS


def test_lora_link_throttles_the_poll():
    assert poll_interval_ms({"tier": "lora"}) == POLL_THROTTLED_MS


def test_compact_mode_throttles_even_on_an_odd_tier():
    # The firmware's explicit "I'm bandwidth-constrained" signal throttles regardless of tier.
    assert poll_interval_ms({"tier": "espnow", "mode": "compact"}) == POLL_THROTTLED_MS


def test_failover_to_lora_throttles_immediately():
    # Same lag concern as the strip: a `tier` failover to lora must throttle now, off the `to=` field.
    assert poll_interval_ms({"link_event": "tier", "tier": "wifi", "to": "lora"}) == POLL_THROTTLED_MS


def test_poll_interval_honours_custom_bounds():
    assert poll_interval_ms({"tier": "lora"}, base_ms=1000, throttled_ms=30000) == 30000
    assert poll_interval_ms({"tier": "wifi"}, base_ms=1000, throttled_ms=30000) == 1000


# ── Tier-aware stream gate ───────────────────────────────────────────────────

def test_stream_allowed_off_a_relay_and_on_wifi():
    assert stream_blocked({}) is False
    assert stream_blocked({"tier": "wifi"}) is False
    assert stream_blocked({"tier": "espnow"}) is False


def test_stream_blocked_on_lora_and_compact():
    assert stream_blocked({"tier": "lora"}) is True
    assert stream_blocked({"tier": "wifi", "mode": "compact"}) is True
    assert stream_blocked({"link_event": "tier", "to": "lora"}) is True
