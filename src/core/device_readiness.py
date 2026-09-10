"""Device-readiness explanation — a PURE derivation from already-collected status facts.

The Health tab renders each device's raw ``status`` string and colours it green only when it reads
``"connected"`` (see ``ui/qt/health_tab.py``); every other state collapses into one undifferentiated
grey row. So a device that is simply unplugged, one CC has no driver for, one that needs an external
host tool CC does not bundle, and one that is plugged in but whose firmware has never actually answered
all look identical, and none of them explain *what is missing*, *what is still unknown*, or *what the
owner can safely inspect next*.

This module fills that gap without any I/O. It consumes facts the app has ALREADY collected for an
explicitly selected owner device and returns one honest, distinct readiness verdict plus plain-language
lists. It never probes, connects, installs, executes, enables, or transmits — it only classifies
supplied strings and returns text. The live serial/handshake/flash paths remain the sole authorities;
this is a read-only explanation layer, the same way ``flash_badges.badge_for`` is an honest static hint
over the flash path.

Reused vocabulary (no new status type is invented here — these are the exact strings the existing
layers already produce; a caller passes them straight through):

* ``status`` — from ``health_monitor.get_device_health()``: one of ``registered`` / ``connected`` /
  ``no-reply`` / ``disconnected`` / ``not_registered`` / ``error``.
* ``health`` — from the connect-time handshake stored on ``models.device.Device.health``:
  ``unknown`` (not probed) / ``alive`` (a text-CLI firmware actually replied) / ``no-reply`` (open text
  CLI, silence) / ``no-cli`` (a stream/control-map node with NO text channel — the handshake sends no
  write and reads no reply, so it is a transport distinction, NOT evidence the firmware answered).
* ``driver_type`` — ``models.device.Device.driver_type``: ``text-cli`` / ``stream`` / ``controlmap``,
  or the ``unsupported`` sentinel from ``core.drivers.UnsupportedDriver`` (CC has no driver for it). A
  value that is none of those is UNRECOGNISED and is classified honestly as unclear, never as ready.
* ``missing_dependency`` — an external host tool the caller already knows is absent, named exactly as
  ``flash_badges.BACKEND_TOOL`` / the ``NEEDS_TOOL`` badge names it (CC bundles none of those). ``""``
  when nothing is missing / unknown.

Deterministic, stdlib-only, side-effect-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

# ── Reused status vocabularies (mirror the producers named in the module docstring) ──────────────

#: health_monitor statuses that mean a live serial link is open on the port.
_LINK_OPEN_STATUSES = frozenset({"connected", "no-reply"})
#: health_monitor statuses that positively mean NO live link (detected/registered/closed/errored).
#: The empty string is a recognised "no link" value (a freshly detected, never-connected row).
_LINK_CLOSED_STATUSES = frozenset({"disconnected", "not_registered", "registered", "error", ""})

#: Tri-state link results. ``link_state()`` returns exactly one so that ``link_open()`` and
#: ``explain_readiness`` reason over the SAME normalised decision instead of re-deriving it twice.
_LINK_OPEN = "open"
_LINK_CLOSED = "closed"
_LINK_INDETERMINATE = "indeterminate"  # status is neither a recognised open nor closed value

#: Device.health handshake state that VERIFIABLY means the firmware answered: a text-CLI board replied
#: (``handshake.classify_reply`` returns ``"alive"`` only when a non-empty reply line came back). This is
#: the sole genuinely "answered" / READY case.
_HEALTH_ANSWERED = frozenset({"alive"})
#: Device.health states that leave the hardware response UNVERIFIED while the link is open:
#:   * ``unknown``  — never probed;
#:   * ``no-reply`` — probed over a text CLI, silence;
#:   * ``no-cli``   — a stream/control-map node with no text channel; the handshake makes NO write and
#:                    reads NO reply, so a text handshake is INAPPLICABLE and response evidence is
#:                    UNAVAILABLE (honest, not a fault — but NOT proof the firmware answered).
_HEALTH_LINK_OPEN_UNVERIFIED = frozenset({"unknown", "no-reply", "no-cli"})

#: driver_type values CC actually has a working driver for (mirror models.device.Device.driver_type).
_DRIVER_SUPPORTED = frozenset({"text-cli", "stream", "controlmap"})
#: driver_type sentinel meaning CC has no driver for this device kind at all.
_DRIVER_UNSUPPORTED = "unsupported"

#: firmware identifiers that are really "not identified" (mirrors handshake._has_known_firmware).
_UNIDENTIFIED_FIRMWARE = frozenset({"", "generic", "raw", "unknown"})


class ReadinessState(Enum):
    """One distinct readiness verdict for a selected device. The four middle members are the states
    the Health tab currently cannot tell apart."""

    READY = "ready"                              # link open, supported driver, firmware answered (alive)
    DISCONNECTED = "disconnected"                # no live serial link — nothing device-side is knowable
    UNSUPPORTED = "unsupported"                  # CC has no driver for this device kind
    MISSING_DEPENDENCY = "missing_dependency"    # an external host tool CC does not bundle is absent
    HARDWARE_UNVERIFIED = "hardware_unverified"  # link open, but the firmware response is not verified
    UNKNOWN = "unknown"                          # facts are malformed/unrecognised — cannot classify


# Human labels for display; the enum ``value`` stays the stable machine key.
STATE_LABEL: dict[ReadinessState, str] = {
    ReadinessState.READY: "Ready",
    ReadinessState.DISCONNECTED: "Disconnected",
    ReadinessState.UNSUPPORTED: "Unsupported device",
    ReadinessState.MISSING_DEPENDENCY: "Missing a required tool",
    ReadinessState.HARDWARE_UNVERIFIED: "Hardware not verified",
    ReadinessState.UNKNOWN: "Status unclear",
}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class DeviceStatusFacts:
    """The already-collected facts about ONE explicitly selected owner device. Every field is a value
    the app has in hand (no I/O to gather them); defaults describe a freshly detected, un-probed device.

    ``connected`` is an optional explicit override for "is the serial link open"; when left None the
    link state is read from ``status`` alone. It exists so a caller holding ``Device.connected`` can pass
    it without having to reconcile it against the health string.
    """

    status: str = ""            # health_monitor.get_device_health()['status']
    health: str = "unknown"     # Device.health (connect-time handshake)
    firmware: str = ""          # Device.firmware
    driver_type: str = "text-cli"  # Device.driver_type
    missing_dependency: str = ""   # an absent external host tool, named as flash_badges does; "" = none
    connected: bool | None = None  # optional explicit link-open override; None => derive from status

    def link_state(self) -> str:
        """The single normalised link decision, shared by ``link_open()`` and ``explain_readiness`` so
        the two never disagree (e.g. a padded ``" CONNECTED "``). Explicit ``connected`` wins; otherwise
        the NORMALISED ``status`` decides. A status that is neither a recognised open nor a recognised
        closed value stays ``_LINK_INDETERMINATE`` — it is never guessed into "closed"."""
        if self.connected is not None:
            return _LINK_OPEN if self.connected else _LINK_CLOSED
        status = _norm(self.status)
        if status in _LINK_OPEN_STATUSES:
            return _LINK_OPEN
        if status in _LINK_CLOSED_STATUSES:
            return _LINK_CLOSED
        return _LINK_INDETERMINATE

    def link_open(self) -> bool:
        """True iff a live serial link is open — i.e. ``link_state()`` is open. Reads the SAME
        normalised decision ``explain_readiness`` uses, so both agree on a padded/cased status."""
        return self.link_state() == _LINK_OPEN


@dataclass(frozen=True)
class ReadinessReport:
    """The explanation for one device: a single distinct state, a one-line summary, and three plain,
    display-ready lists — what is missing, what is still unknown, and what can be safely INSPECTED next.
    ``inspect_next`` items are read-only guidance (which existing panel to look at); they never tell the
    owner — or the app — to probe, connect, install, or run anything."""

    state: ReadinessState
    summary: str
    missing: tuple[str, ...] = field(default_factory=tuple)
    unknown: tuple[str, ...] = field(default_factory=tuple)
    inspect_next: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return STATE_LABEL[self.state]

    def to_dict(self) -> dict:
        """Plain-dict form for a web/JSON surface (mirrors the ``to_dict`` convention in models)."""
        return {
            "state": self.state.value,
            "label": self.label,
            "summary": self.summary,
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "inspect_next": list(self.inspect_next),
        }


def _firmware_identified(firmware: str) -> bool:
    return _norm(firmware) not in _UNIDENTIFIED_FIRMWARE


def explain_readiness(facts: DeviceStatusFacts) -> ReadinessReport:
    """Classify a selected device's supplied facts into one distinct :class:`ReadinessState` with a
    plain explanation. Pure: it reads only ``facts`` and returns a report.

    Precedence (first match wins) — deliberately ordered most-fundamental first, so exactly one honest
    verdict comes back and the four ambiguous states stay distinct:

      1. DISCONNECTED         — a recognised "no live link" state; nothing device-side is assessable.
      1b. UNKNOWN             — the link state itself is indeterminate (an unrecognised status with no
                                explicit ``connected`` override): do NOT collapse it into DISCONNECTED.
      2. UNSUPPORTED          — CC structurally has no driver for this device kind (no tool fixes it).
      2b. UNKNOWN             — the driver_type is unrecognised: CC cannot tell which transport this is,
                                so readiness cannot be classified (only a recognised driver can be).
      3. MISSING_DEPENDENCY   — supported in principle, but an external host tool CC does not bundle is absent.
      4. HARDWARE_UNVERIFIED  — link open + supported + tool present, but the firmware response is not
                                verified: never probed (unknown), silent (no-reply), or a no-text-CLI
                                node (no-cli — a handshake is inapplicable, response evidence unavailable).
      5. READY                — the firmware verifiably answered for its device kind (alive).
      6. UNKNOWN              — the health value is unrecognised; classify honestly as unclear.
    """
    link = facts.link_state()
    status = _norm(facts.status)
    health = _norm(facts.health)
    driver_type = _norm(facts.driver_type) or "text-cli"
    dependency = str(facts.missing_dependency or "").strip()
    fw_known = _firmware_identified(facts.firmware)

    # 1) A recognised "no live link" state — the most fundamental gap. Nothing else is knowable offline.
    if link == _LINK_CLOSED:
        if status == "error":
            summary = "The link to this port errored; the device is not reachable right now."
            first_missing = "a working serial link (the last open attempt errored)"
        elif status in ("registered", "not_registered"):
            summary = "This device was detected but has never been connected."
            first_missing = "an open serial link (detected, but never connected)"
        else:
            summary = "This device is not connected."
            first_missing = "an open serial link to the device"
        return ReadinessReport(
            state=ReadinessState.DISCONNECTED,
            summary=summary,
            missing=(first_missing,),
            unknown=(
                "firmware identity, capabilities, and hardware response — all unknown until connected",
            ),
            inspect_next=(
                "Confirm the device is plugged in and the correct port is selected in the Devices tab.",
                "No firmware, capability, or hardware facts can be read while the link is closed.",
            ),
        )

    # 1b) The link state is INDETERMINATE — an unrecognised status with no explicit connection override.
    #     "Not a recognised open state" is NOT the same as "disconnected"; stay honestly UNKNOWN.
    if link != _LINK_OPEN:  # _LINK_INDETERMINATE
        return ReadinessReport(
            state=ReadinessState.UNKNOWN,
            summary="This device's connection status is not one CC recognises, so its readiness is unclear.",
            missing=(),
            unknown=(
                f"an unrecognised connection status ({facts.status!r}) — the link cannot be confirmed "
                "open or closed, so nothing device-side can be classified",
            ),
            inspect_next=(
                "Review this device's raw status in the Devices / Health panel.",
                "No readiness can be derived until the connection status is a recognised value "
                "(or an explicit connected/disconnected fact is supplied).",
            ),
        )

    # From here the link is open.

    # 2) CC has no driver for this transport/firmware kind — a structural mismatch a tool or a reconnect
    #    cannot resolve.
    if driver_type == _DRIVER_UNSUPPORTED:
        return ReadinessReport(
            state=ReadinessState.UNSUPPORTED,
            summary="CC has no driver for this device kind, so it cannot exchange commands with it.",
            missing=("a CC driver for this device's transport type",),
            unknown=("whether this device exposes any command channel CC can use",),
            inspect_next=(
                "Review the device's firmware and driver type in the Devices tab.",
                "An unsupported driver type is fail-closed by design; CC will not send commands to it.",
            ),
        )

    # 2b) The driver_type is neither a recognised transport nor the unsupported sentinel. CC cannot tell
    #     which transport this is, so it must NOT be reasoned into READY — stay honestly UNKNOWN.
    if driver_type not in _DRIVER_SUPPORTED:
        return ReadinessReport(
            state=ReadinessState.UNKNOWN,
            summary="This device's driver type is not one CC recognises, so its readiness cannot be classified.",
            missing=(),
            unknown=(
                f"an unrecognised driver type ({facts.driver_type!r}) — CC cannot tell which transport "
                "this is, so readiness cannot be derived",
            ),
            inspect_next=(
                "Review this device's driver type in the Devices tab.",
                "Only a recognised driver (text-cli / stream / control-map) can be assessed for readiness.",
            ),
        )

    # 3) Supported in principle, but a required external host tool CC does not bundle is absent.
    if dependency:
        return ReadinessReport(
            state=ReadinessState.MISSING_DEPENDENCY,
            summary=(
                f"A required host tool ('{dependency}') is not present, so CC cannot work with this "
                "device yet."
            ),
            missing=(f"the '{dependency}' host tool (CC does not bundle it)",),
            unknown=("what this device can do once the required tool is available",),
            inspect_next=(
                f"The Flash tab badges which external tool a device needs ('{dependency}' here); "
                "CC bundles none of them.",
                "Installing host tooling is an owner decision made outside CC.",
            ),
        )

    # From here the link is open, the driver kind is recognised and supported, and no host tool is
    # missing. The only remaining question is whether the hardware has actually, verifiably answered.

    # 4) Firmware response NOT verified: never probed (unknown), silent (no-reply), or a no-text-CLI
    #    stream/control-map node (no-cli). ``no-cli`` is NOT an answered firmware — the handshake made no
    #    write and read no reply — so it does not reach READY; its transport distinction stays VISIBLE.
    if health in _HEALTH_LINK_OPEN_UNVERIFIED:
        if health == "no-cli":
            summary = (
                "The link is open to a stream / control-map node that has no text command channel, so a "
                "text handshake is inapplicable and the firmware's response cannot be confirmed here."
            )
            missing = ()  # a text reply is not "missing" here — it is inapplicable to this transport
            unknown = (
                "the firmware's response — this transport has no text CLI to answer on, so a handshake "
                "is inapplicable and response evidence is unavailable (silence is expected, not a fault)",
            )
            inspect_next = (
                "This device uses a stream / control-map transport (no text CLI); see its driver type in "
                "the Devices tab.",
                "Such nodes are exercised through their own backend/panel, not a text handshake — treat "
                "the firmware response as unconfirmed here rather than answered.",
            )
        elif health == "no-reply":
            summary = (
                "The link is open but the firmware has not replied; the hardware is not verified."
            )
            missing = ("a reply from the firmware over the open link",)
            unknown = (
                "whether the board is running, at the right baud, or correctly flashed",
            )
            inspect_next = (
                "Review baud and firmware for this port in the Devices tab.",
                "A hung, wrong-baud, or mis-flashed board presents exactly like this (open link, no reply).",
            )
        else:  # "unknown" — the connect-time handshake result has not been read yet
            summary = "The link is open, but the connect-time handshake has not been read yet."
            missing = ("a completed connect-time handshake result",)
            unknown = ("whether the firmware responds, and its identity, until the handshake is read",)
            inspect_next = (
                "The connect-time handshake result appears in the Devices / Health panel once available.",
                "Until then, treat this device's hardware response as unverified.",
            )
        extra_unknown: tuple[str, ...] = ()
        if health != "no-cli" and not fw_known:
            extra_unknown = ("firmware identity is not yet determined",)
        return ReadinessReport(
            state=ReadinessState.HARDWARE_UNVERIFIED,
            summary=summary,
            missing=missing,
            unknown=unknown + extra_unknown,
            inspect_next=inspect_next,
        )

    # 5) The firmware verifiably answered for its device kind (alive). Ready — surface any residual
    #    unknown honestly (an identified-as-responsive board whose firmware name is still generic).
    if health in _HEALTH_ANSWERED:
        residual_unknown: tuple[str, ...] = ()
        if not fw_known:
            residual_unknown = ("the specific firmware name is not identified (the board did respond)",)
        return ReadinessReport(
            state=ReadinessState.READY,
            summary="This device is connected and its firmware has answered for its kind.",
            missing=(),
            unknown=residual_unknown,
            inspect_next=(),
        )

    # 6) The health value is not one CC recognises — do not guess. Report it plainly as unclear.
    return ReadinessReport(
        state=ReadinessState.UNKNOWN,
        summary="This device's health state is not recognised, so its readiness is unclear.",
        missing=(),
        unknown=(f"an unrecognised health value ({facts.health!r}) — cannot classify readiness",),
        inspect_next=(
            "Review this device's raw status in the Devices / Health panel.",
        ),
    )


def facts_from_health(
    health_row: Mapping[str, object],
    *,
    health: str = "unknown",
    driver_type: str = "text-cli",
    missing_dependency: str = "",
    connected: bool | None = None,
) -> DeviceStatusFacts:
    """Build :class:`DeviceStatusFacts` from a ``health_monitor.get_device_health()`` row plus the few
    device fields that row does not carry. Convenience only — pure, no I/O.

    ``health_row`` supplies ``status`` and ``firmware_version`` (the health row's key for the firmware
    string). The handshake ``health``, ``driver_type`` (both on ``Device``), and any known
    ``missing_dependency`` are passed by the caller because the health row does not include them.
    """
    return DeviceStatusFacts(
        status=str(health_row.get("status", "") or ""),
        health=health,
        firmware=str(health_row.get("firmware_version", "") or ""),
        driver_type=driver_type,
        missing_dependency=missing_dependency,
        connected=connected,
    )


__all__ = [
    "ReadinessState",
    "STATE_LABEL",
    "DeviceStatusFacts",
    "ReadinessReport",
    "explain_readiness",
    "facts_from_health",
]
