"""Shared Link-strip rendering + tier-aware bandwidth logic for the LxveNode relay link.

A LxveNode relay reports its live link state over serial (an ``LXVENODE/1 link``/``tier``/``stats``
line -> a ``link_state`` event -> ``Device.apply_link_state`` -> ``Device.link``). This module turns
that ``Device.link`` dict into three things the Operate console consumes:

  * a compact one-line Link strip — tier badge + link quality + the most-recent failover + role/peer
    (:func:`link_strip_render`);
  * the tier-aware status-poll cadence — don't hammer a constrained LoRa mesh link with the auto-
    ``status`` probe (:func:`poll_interval_ms`);
  * the tier-aware stream-command gate — high-bandwidth "stream" verbs off on LoRa (:func:`stream_blocked`).

All three are PURE functions (no Qt, no I/O), mirroring :func:`src.ui.qt.arm_lamp.arm_lamp_render` and the
``src.core.safety`` predicates, so the OperateTab widget stays a thin renderer and the decisions are
unit-tested in isolation. This is READ-ONLY telemetry — nothing here transmits or drives the relay; it only
describes the link so the operator understands the bandwidth they are working with.

Design SSOT: ``LxveNode-in-Cyber-Controller.md`` §2b (Link strip) + §2c (honoring LoRa's bandwidth).
"""
from __future__ import annotations

from dataclasses import dataclass

# tier slug -> (badge label, color). Colors read like a signal-strength scale: Wi-Fi (near, full rate)
# green · ESP-NOW (mid) amber · LoRa (far, constrained) blue. An unknown/blank tier renders verbatim in
# muted grey (a future tier is never lost — same forward-compat posture as the arm lamp).
_TIER_BADGES: dict[str, tuple[str, str]] = {
    "wifi":   ("Wi-Fi",   "#3fb950"),
    "espnow": ("ESP-NOW", "#d29922"),
    "lora":   ("LoRa",    "#58a6ff"),
}
_MUTED = "#8b949e"
_DOWN = "#f85149"

# Tiers CC treats as low-bandwidth far/relayed links: the status poll is lengthened AND high-bandwidth
# "stream" verbs are gated. LoRa is the constrained far link (a few hundred bps of compact frames);
# Wi-Fi/ESP-NOW are near/mid and carry full rate. A firmware ``mode=compact`` flag is an explicit "I am
# bandwidth-constrained right now" signal and throttles too, even on an unexpected tier (see _is_constrained).
_LOW_BANDWIDTH_TIERS = frozenset({"lora"})

# Default status-poll cadences (ms). The base 2 s matches the console's historical fixed interval; the
# throttled 15 s sits inside the design's 10-20 s LoRa band so the poll itself can't saturate the link.
POLL_BASE_MS = 2000
POLL_THROTTLED_MS = 15000

# A relay reports its link roughly on the status-poll cadence. If no frame has been heard for this long
# the link is treated as STALE — a relay gone silent without an explicit DOWN frame — so the strip stops
# showing a live green tier and CC stops trusting the (now old) tier for the poll/stream decisions.
# Generous enough to clear the slowest (LoRa, ~15 s) cadence without false-flagging a healthy slow link.
LINK_STALE_S = 45.0


@dataclass(frozen=True)
class LinkStripView:
    """What the Operate console paints for the Link strip. ``visible`` is False when the device has
    reported no link (a plain USB target, not relayed) — the strip is hidden entirely then."""

    visible: bool
    text: str
    color: str


def _current_tier(link: dict) -> str:
    """The effective current tier, lowercased ('' if unknown).

    A ``tier`` failover frame carries the NEW tier in ``to=`` and no ``tier=`` field, while
    :meth:`Device.apply_link_state` merges frames latest-wins — so right after a failover the merged
    dict still holds the PREVIOUS steady ``link`` frame's ``tier=``. Prefer the failover ``to=`` when the
    most-recent event was a ``tier`` change, so the badge/throttle/gate track the real current tier
    instead of lagging a frame behind."""
    if link.get("link_event") == "tier" and link.get("to"):
        return str(link["to"]).strip().lower()
    return str(link.get("tier", "")).strip().lower()


def _is_constrained(link: dict) -> bool:
    """True when the active link is a low-bandwidth far/relayed link — the shared predicate behind both
    the poll-throttle and the stream gate. Driven by the effective tier (LoRa) OR an explicit firmware
    ``mode=compact`` signal (which the firmware sets when it drops to compact framing)."""
    if not isinstance(link, dict) or not link:
        return False
    if _current_tier(link) in _LOW_BANDWIDTH_TIERS:
        return True
    return str(link.get("mode", "")).strip().lower() == "compact"


def poll_interval_ms(link: dict, base_ms: int = POLL_BASE_MS,
                     throttled_ms: int = POLL_THROTTLED_MS, stale: bool = False) -> int:
    """Tier-aware status-poll cadence in ms.

    On a low-bandwidth far/relayed link (LoRa, or a firmware-declared ``mode=compact``) CC lengthens the
    poll so the ``status`` probe itself doesn't saturate a constrained mesh link; on Wi-Fi/ESP-NOW-near —
    or a plain non-relayed device with no link dict at all — it polls at the normal cadence. A ``stale``
    link (a relay gone silent) also throttles — don't hammer a relay that may be gone. Pure."""
    return throttled_ms if (stale or _is_constrained(link)) else base_ms


def stream_blocked(link: dict, stale: bool = False) -> bool:
    """Whether high-bandwidth "stream" verbs (live pcap / packet monitor / sniff dump / wardrive tail /
    video) should be disabled for the active link.

    True on a constrained LoRa/compact relay link OR a ``stale`` link (a relay gone silent — don't start
    a firehose over a link that may be dead), False on a fresh Wi-Fi/ESP-NOW-near or plain non-relayed
    device. The relayed target's console text still streams back either way (it is compact line text) —
    only the firehose verbs the link can't carry are gated. Pure."""
    return stale or _is_constrained(link)


def _badge_label(tier: str) -> str:
    """The display label for a tier slug ('Wi-Fi'/'ESP-NOW'/'LoRa'), or the raw slug for an unknown
    tier (never dropped), or '?' when blank."""
    known = _TIER_BADGES.get(tier)
    if known is not None:
        return known[0]
    return tier or "?"


def _quality_str(link: dict) -> str:
    """RSSI / SNR / latency / data-rate summary — present-only, so a partial ``stats`` line still formats.
    On LoRa the data-rate (``dr=sf9bw125``) is the spreading-factor/bandwidth, so the operator sees the
    speed the link is actually giving them."""
    bits: list[str] = []
    rssi = link.get("rssi")
    if isinstance(rssi, int):
        bits.append(f"{rssi} dBm")
    snr = link.get("snr")
    if isinstance(snr, int):
        bits.append(f"snr {snr}")
    latency = link.get("latency_ms")
    if isinstance(latency, int):
        bits.append(f"{latency} ms")
    dr = link.get("dr")
    if dr:
        bits.append(str(dr))
    return " ".join(bits)


def _failover_str(link: dict) -> str:
    """The most-recent tier change from a ``tier`` frame: '⇄ Wi-Fi -> LoRa (rssi)'. Empty unless the most-
    recent link event was a failover — CC says WHY the tier changed instead of looking hung."""
    if link.get("link_event") != "tier":
        return ""
    frm = str(link.get("from", "")).strip().lower()
    to = str(link.get("to", "")).strip().lower()
    if not frm and not to:
        return ""
    reason = link.get("reason")
    tail = f" ({reason})" if reason else ""
    return f"⇄ {_badge_label(frm)} -> {_badge_label(to)}{tail}"


def _role_peer_str(link: dict) -> str:
    """Role + peer/hop so a multi-node repeater chain is legible: 'relay > nodeA'. Present-only."""
    role = str(link.get("role", "")).strip()
    peer = str(link.get("peer", "")).strip()
    if role and peer:
        return f"{role} > {peer}"
    return role or peer


def link_strip_render(link: dict, stale: bool = False) -> LinkStripView:
    """Derive the Link strip from a ``Device.link`` dict. ``stale`` (set by the caller from the link's
    last-heard time) renders the last-known tier muted and marked "stale" — a relay gone silent without a
    DOWN frame — instead of a live green tier.

    Hidden (``visible=False``) when the device reported no link — a plain USB target that isn't relayed
    never grows a strip. Otherwise a compact, single line: an active-tier badge, the link quality, the
    most-recent failover (if the last event was one), and the role/peer. A link the node reports DOWN
    (``up=0``) renders red and says DOWN rather than showing a stale-looking green tier. Read-only."""
    if not isinstance(link, dict) or not link:
        return LinkStripView(False, "", _MUTED)

    tier = _current_tier(link)
    badge = _badge_label(tier)
    known = _TIER_BADGES.get(tier)
    color = known[1] if known is not None else _MUTED

    parts: list[str] = []
    if link.get("up") is False:
        parts.append(f"⛌ {badge} DOWN")
        color = _DOWN
    elif stale:
        # A relay that stopped reporting without an explicit DOWN frame: show the last-known tier
        # muted + marked stale, not a live green tier — it may be gone and the quality below is old.
        parts.append(f"⚠ {badge} stale")
        color = _MUTED
    else:
        parts.append(f"▮ {badge}")

    for extra in (_quality_str(link), _failover_str(link), _role_peer_str(link)):
        if extra:
            parts.append(extra)

    return LinkStripView(True, "link: " + "  ·  ".join(parts), color)
