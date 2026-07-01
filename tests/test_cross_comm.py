"""Tests for ``src.core.cross_comm`` rendering/sanitisation helpers.

Covered (pure, no device, no heavy deps):
    * ``_safe_render`` substitutes ONLY {mac}/{ssid}/{channel}, leaves unknown
      placeholders untouched, and strips control chars from a newline-bearing SSID
      (command-injection defense — 'a\\nreboot' must not produce a newline);
    * ``_sanitize_value`` removes control characters and caps the length.

``cross_comm`` imports only the standard library plus the pure ``Target`` model,
so no optional dep is required; the ``importorskip`` is belt-and-suspenders.
"""

from __future__ import annotations

import pytest

cross_comm = pytest.importorskip("src.core.cross_comm")

_MAX = cross_comm._MAX_VALUE_LEN


def test_pool_add_refreshes_stale_index_on_mac_keyed_rescan() -> None:
    # A MAC-keyed AP (BW16 bracketed fork prints index + BSSID) that reorders between scans must
    # refresh extra['index'] (and device_source) on update, else an {index} deauth hits the WRONG AP.
    from src.models.target import Target, TargetType
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="CoffeeShop",
                    channel=6, rssi=-42, device_source="COM7", extra={"index": 1}))
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="CoffeeShop",
                    channel=6, rssi=-40, device_source="COM7", extra={"index": 0}))  # reordered -> idx 0
    t = pool.get("ap:AA:BB:CC:DD:EE:FF")
    assert t.extra["index"] == 0 and t.device_source == "COM7"


def test_pool_add_keeps_index_when_reobserved_without_one() -> None:
    # A later index-less re-observation (e.g. Marauder sees the same BSSID) must NOT wipe a known index.
    from src.models.target import Target, TargetType
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM7", extra={"index": 2}))
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM3"))  # no index
    t = pool.get("ap:AA:BB:CC:DD:EE:FF")
    assert t.extra["index"] == 2 and t.device_source == "COM7"


# ── _safe_render ─────────────────────────────────────────────────────

def test_safe_render_substitutes_all_placeholders() -> None:
    out = cross_comm._safe_render(
        "deauth {mac} ssid={ssid} ch={channel}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="CoffeeShop",
        channel=6,
    )
    assert out == "deauth AA:BB:CC:DD:EE:FF ssid=CoffeeShop ch=6"


def test_safe_render_ignores_unknown_placeholders() -> None:
    # Only {mac}/{ssid}/{channel} are recognised; anything else is left verbatim
    # (NOT passed through str.format, which would enable attribute traversal).
    out = cross_comm._safe_render(
        "{mac} {unknown} {ssid.__class__}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="net",
        channel=1,
    )
    assert "{unknown}" in out
    assert "{ssid.__class__}" in out
    assert out.startswith("AA:BB:CC:DD:EE:FF ")


def test_safe_render_strips_newline_bearing_ssid() -> None:
    # A crafted SSID 'a\nreboot' must not inject a second serial command.
    out = cross_comm._safe_render(
        "attack {ssid}",
        mac="AA:BB:CC:DD:EE:FF",
        ssid="a\nreboot",
        channel=1,
    )
    assert "\n" not in out
    assert "\r" not in out
    # The control char is removed; the surrounding text remains joined.
    assert out == "attack areboot"


def test_safe_render_non_numeric_channel_blanks() -> None:
    out = cross_comm._safe_render("ch={channel}", mac="", ssid="", channel="not-a-number")
    assert out == "ch="


# ── _sanitize_value ──────────────────────────────────────────────────

def test_sanitize_value_removes_control_chars() -> None:
    assert cross_comm._sanitize_value("a\nb\tc\r\x00d") == "abcd"


def test_sanitize_value_caps_length() -> None:
    long = "x" * (_MAX + 50)
    out = cross_comm._sanitize_value(long)
    assert len(out) == _MAX


def test_sanitize_value_coerces_non_str() -> None:
    assert cross_comm._sanitize_value(12345) == "12345"


# ── TargetPool.add update semantics (bug-hunt fixes #4, #14, #16) ─────────────────────────────────

def _ap(mac, ssid="", channel=0, rssi=0):
    from src.models.target import Target, TargetType
    return Target(mac=mac, target_type=TargetType.AP, ssid=ssid, channel=channel, rssi=rssi)


def test_pool_add_does_not_clobber_known_channel_rssi_with_zero() -> None:
    # A re-observation that omits channel/rssi (the 0 sentinel) must NOT erase the learned values.
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    pool.add(_ap("AA:BB:CC:DD:EE:FF", ssid="Net", channel=6, rssi=-40))
    pool.add(_ap("AA:BB:CC:DD:EE:FF", ssid="", channel=0, rssi=0))  # re-seen, no channel line
    t = pool.get("ap:AA:BB:CC:DD:EE:FF")
    assert t.channel == 6 and t.rssi == -40 and t.ssid == "Net"


def test_pool_add_latest_wins_ssid_for_synthetic_index_key() -> None:
    # Synthetic idx:<port>:<index> keys are reused across re-ordered scans -> SSID must be latest-wins,
    # not stuck on the first label (else the display SSID mismatches the channel/rssi).
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    pool.add(_ap("idx:COM3:0", ssid="HomeWiFi", channel=1, rssi=-42))
    pool.add(_ap("idx:COM3:0", ssid="CoffeeShop", channel=6, rssi=-50))
    t = pool.get("ap:idx:COM3:0")
    assert t.ssid == "CoffeeShop" and t.channel == 6 and t.rssi == -50


def test_pool_add_publishes_update_outside_lock_no_deadlock() -> None:
    # The update is published OUTSIDE the pool lock, so a subscriber that reads the pool in its
    # callback must not deadlock (this call would hang forever with the old in-lock publish).
    pool = cross_comm.TargetPool(cross_comm.EventBus())
    snapshots: list[int] = []
    pool.bus.subscribe("target.updated", lambda _topic, _p: snapshots.append(len(pool.all())))
    pool.add(_ap("AA:BB:CC:DD:EE:FF", ssid="Net", channel=6, rssi=-40))
    pool.add(_ap("AA:BB:CC:DD:EE:FF", ssid="Net", channel=6, rssi=-41))  # triggers target.updated
    assert snapshots == [1]  # pool.all() succeeded inside the callback
