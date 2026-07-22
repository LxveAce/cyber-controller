"""Safety engine — danger classification + warning-gate logic for the UI.

This is a **pure-Python** module (no PyQt, no serial, no disk I/O) so it can be
fully unit-tested without hardware or heavy optional dependencies.  The Qt UI
calls into it to decide *whether* to show a confirmation and *what text* to show;
the UI is responsible only for the actual dialog widgets.

Three concerns live here:

1. **Classification** — given a raw command string (and optionally the
   :class:`~src.protocols.base.CommandInfo` that produced it), return a danger
   level: ``""`` (safe), ``"lab-only"`` (RF transmit / deauth / jam / brute /
   spam that must only be run in an authorized, controlled environment), or
   ``"illegal-tx"`` (transmission that is illegal in most jurisdictions, e.g.
   broadband jamming).  When a ``CommandInfo.danger`` is supplied and non-empty
   it is authoritative; otherwise we fall back to a conservative keyword scan of
   the raw string (unknown -> ``""``).

2. **Gating** — pure predicates over a settings dict (the same shape produced by
   :mod:`src.config.settings`) that tell the UI when to confirm a dangerous
   command and when to show the one-time first-run legal disclaimer.  These
   functions never touch disk; the caller loads/saves settings.

3. **Text** — the human-readable bodies for the first-run legal disclaimer and
   the per-command "controlled lab only" confirmation.

Design notes / invariants (reliability-first):

* Unknown input is treated as **safe** for classification (we do not invent
  danger), but the *first-run disclaimer* is shown unconditionally at least once
  regardless of any warning-suppression setting — suppressing per-command
  warnings must never silently skip the one-time legal acknowledgement.
* ``illegal-tx`` is a strict superset of severity over ``lab-only``: any keyword
  in the illegal set also implies lab-only, so a command is never *down*graded.
* All helpers tolerate ``None``/partial settings dicts and missing sections so a
  config written by an older build still behaves safely (fail toward warning).
"""

from __future__ import annotations

import logging
from typing import Any

from src.protocols.base import CommandInfo

log = logging.getLogger(__name__)

# ── Danger levels ────────────────────────────────────────────────────

#: No danger — safe to send without a confirmation.
SAFE: str = ""
#: RF transmit / deauth / jam / brute / spam — authorized controlled-lab use only.
LAB_ONLY: str = "lab-only"
#: Transmission illegal in most jurisdictions (e.g. broadband jamming).
ILLEGAL_TX: str = "illegal-tx"

#: Ordered by ascending severity, used to pick the "worst" of two levels.
_SEVERITY: dict[str, int] = {SAFE: 0, LAB_ONLY: 1, ILLEGAL_TX: 2}


# ── Keyword tables (matched as case-insensitive substrings) ──────────
#
# These are deliberately broad substrings: command surfaces vary across firmware
# (``deauth``, ``attack -t deauth``, ``wifi_deauth`` …), so a substring scan is
# the most robust fallback when no CommandInfo.danger is attached.  Order does
# not matter — every match is collected and the worst level wins.

#: Substrings that imply at least lab-only (RF TX / deauth / jam / brute / spam).
_LAB_ONLY_KEYWORDS: tuple[str, ...] = (
    "deauth",
    "jam",          # also caught by the illegal set; keep here so partials match
    "jammer",
    "beacon",
    "spam",
    "brute",
    "sourapple",
    "sour_apple",
    "protokill",
    "attack",
    "karma",        # rogue/evil-twin AP — RF TX
    "rickroll",     # beacon-spam variant
    "cinder",       # HaleHound BLE attack
    "mousejack",    # keystroke injection over RF
    "replay",       # SubGHz replay TX
    "clone",        # NFC/card clone (copy an access credential) — e.g. LxveOS `nfc … clone <UID>`
)

#: Substrings whose transmission is illegal in most jurisdictions.
#: A match here forces the :data:`ILLEGAL_TX` level (strictly worse than lab).
_ILLEGAL_TX_KEYWORDS: tuple[str, ...] = (
    "broadband",
    # "jam" subsumes "jammer"/"jam_reader"/"jamming"/"signal_jam"/"rf_jam" etc.
    # Jamming is precisely the operation the disclaimer cites as illegal under
    # 47 U.S.C. 333 / FCC, so ANY jam-family command must reach ILLEGAL_TX — never
    # the milder lab-only gate. (Kept the more specific entries too for clarity.)
    "jam",
    "jammer",
    "jam_reader",
    "tag_disrupt",
    "protokill",
    # BlueStress's operate verb. Its ``Flood`` CommandInfo already carries an explicit
    # danger="illegal-tx" (authoritative), but a HAND-TYPED ``flood <band>`` that misses a
    # CommandInfo lookup would otherwise fall through to a plain command-string scan and
    # classify SAFE. Include "flood" here so any free-typed flood is gated regardless. NOTE:
    # this is the COMMAND-STRING illegal set — the free-text DESCRIPTION scan deliberately
    # EXCLUDES "flood" (see _DESC_ILLEGAL_TX_KEYWORDS), because inside a description "flood"
    # qualifies a lab-only attack ("probe request flood", "beacon flood"), not §333 jamming.
    "flood",
)

#: Illegal-tx substrings safe to match inside a free-text DESCRIPTION — the command-string
#: illegal set MINUS "flood". As a standalone command token "flood" is BlueStress's illegal
#: RF-disruption verb, but in a description it merely names a lab-only attack ("probe request
#: flood"), so it must not upgrade a description past lab-only (which would over-flag deauth/
#: beacon/probe-flood commands and break the description-scan invariants).
_DESC_ILLEGAL_TX_KEYWORDS: tuple[str, ...] = tuple(
    kw for kw in _ILLEGAL_TX_KEYWORDS if kw != "flood"
)


def _keyword_level(cmd: str) -> str:
    """Return the worst danger level implied by keyword scanning *cmd*.

    Case-insensitive substring match.  Unknown / benign input returns
    :data:`SAFE` (``""``) — we never invent danger.

    The illegal-tx set is checked such that it *upgrades* the result: a command
    that matches both tables (e.g. ``"jammer"`` / ``"protokill"``) resolves to
    :data:`ILLEGAL_TX`, never the milder lab-only.
    """
    if not cmd:
        return SAFE
    low = cmd.lower()

    level = SAFE
    if any(kw in low for kw in _LAB_ONLY_KEYWORDS):
        level = LAB_ONLY
    if any(kw in low for kw in _ILLEGAL_TX_KEYWORDS):
        level = ILLEGAL_TX
    return level


# ── Metadata (description / category) scan ───────────────────────────
#
# ``_keyword_level`` scans only the command STRING.  Some firmware name an
# offensive command with no danger keyword ("probe", "iot_recon", "sniffpwn",
# "startportal") and carry the danger only in the CommandInfo's description or
# category — those would otherwise present as safe.  The helpers below let
# ``classify`` fold that metadata in (fail TOWARD a warning, never downgrade)
# WITHOUT the two over-flag traps a naive scan hits:
#   * a cease/stop command ("stopscan", "stopportal") shares the vocabulary of
#     the attack it *ends* but is itself safe -> never escalated;
#   * a passive listen ("sniff … beacons") legitimately mentions attack terms in
#     its description -> the description set is a NARROW, unambiguous active-offense
#     subset (no "beacon"/"karma"/"replay"/"airtag").

#: Danger keywords unambiguous enough to match inside a free-text DESCRIPTION.
_DESC_LAB_ONLY_KEYWORDS: tuple[str, ...] = (
    "deauth", "brute", "spam", "flood", "mousejack", "sourapple", "sour_apple",
)

#: Categories whose commands are inherently offensive (active TX / attack / phishing).
#: A match escalates to lab-only — but only for a command that is not a cease action.
_OFFENSIVE_CATEGORIES: frozenset[str] = frozenset(
    {"attack", "attacks", "portal", "evil portal", "jam", "jamming", "spam"}
)

#: Name prefixes that mark a command as a SAFE cease / stop / clear action. Stopping an
#: attack is not itself dangerous, so metadata escalation is suppressed for these. (Kept
#: unambiguous — a bare "off" was dropped as it would also match "offset"/"offline".)
_CEASE_PREFIXES: tuple[str, ...] = ("stop", "disable", "clear")


def _is_cease(name: str) -> bool:
    """True for a stop/clear/disable command — never escalated by metadata."""
    return (name or "").strip().lower().startswith(_CEASE_PREFIXES)


def _description_level(desc: str) -> str:
    """Worst danger implied by a command's free-text description (narrow, unambiguous set)."""
    if not desc:
        return SAFE
    low = desc.lower()
    level = SAFE
    if any(kw in low for kw in _DESC_LAB_ONLY_KEYWORDS):
        level = LAB_ONLY
    if any(kw in low for kw in _DESC_ILLEGAL_TX_KEYWORDS):
        level = ILLEGAL_TX
    return level


def _metadata_level(info: CommandInfo) -> str:
    """Extra danger implied by a CommandInfo's description + category. Returns :data:`SAFE` for a
    cease/stop command (which shares offensive vocabulary but is itself safe)."""
    if info is None or _is_cease(getattr(info, "name", "")):
        return SAFE
    category = (getattr(info, "category", "") or "").strip().lower()
    category_level = LAB_ONLY if category in _OFFENSIVE_CATEGORIES else SAFE
    return worst_of(_description_level(getattr(info, "description", "") or ""), category_level)


def classify(cmd: str, info: CommandInfo | None = None) -> str:
    """Classify the danger of a command.

    Args:
        cmd: Raw command string about to be sent (may be empty).
        info: Optional :class:`~src.protocols.base.CommandInfo` for this command.
            When provided and its ``danger`` field is non-empty, that value is
            authoritative (the protocol author has explicitly annotated it).

    Returns:
        One of :data:`SAFE` (``""``), :data:`LAB_ONLY`, or :data:`ILLEGAL_TX`.

    Resolution order:
        1. ``info.danger`` when ``info`` is supplied and ``info.danger`` is a
           non-empty string -> returned verbatim (normalised/stripped). An
           explicit annotation is authoritative and is never widened below.
        2. Otherwise the worst of a keyword scan of ``cmd`` AND — when ``info``
           is supplied — the danger implied by its description + category
           (:func:`_metadata_level`). This catches an offensive command whose
           name carries no keyword (e.g. ``probe`` / ``iot_recon`` /
           ``startportal``). It only ever *adds* a warning; a cease/stop command
           and a passive listen are not escalated (see :func:`_metadata_level`).
    """
    if info is not None:
        danger = getattr(info, "danger", "") or ""
        danger = danger.strip()
        if danger:
            return danger
    level = _keyword_level(cmd)
    if info is not None:
        level = worst_of(level, _metadata_level(info))
    return level


def worst_of(*levels: str) -> str:
    """Return the highest-severity danger level among *levels*.

    Useful when a single user action expands into several commands (e.g. a macro
    or a batch) and the UI wants a single gate for the whole action.  Unknown
    levels are treated as :data:`SAFE` so a stray value can never *lower* the
    result below a real danger.
    """
    best = SAFE
    best_rank = 0
    for lvl in levels:
        rank = _SEVERITY.get(lvl, 0)
        if rank > best_rank:
            best, best_rank = lvl, rank
    return best


# ── Settings gating (pure predicates over a settings dict) ───────────
#
# The integrator adds, to src/config/settings.py DEFAULTS:
#     "safety": {"confirm_dangerous": True, "suppress_all_warnings": False}
# and a top-level   "_disclaimer_ack": False
# These helpers READ that dict shape but perform no disk I/O.

#: Settings section name and keys (kept as constants so callers/tests don't
#: hard-code strings that could drift from the DEFAULTS the integrator adds).
SAFETY_SECTION: str = "safety"
KEY_CONFIRM_DANGEROUS: str = "confirm_dangerous"
KEY_SUPPRESS_ALL: str = "suppress_all_warnings"
KEY_DISCLAIMER_ACK: str = "_disclaimer_ack"


def _safety_section(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ``safety`` sub-dict from *settings*, or an empty dict."""
    if not isinstance(settings, dict):
        return {}
    section = settings.get(SAFETY_SECTION)
    return section if isinstance(section, dict) else {}


def should_confirm(danger: str, settings: dict[str, Any] | None) -> bool:
    """Return True iff the UI must pop a confirmation before sending.

    True exactly when **all** of:
        * ``danger`` is non-empty (the command is dangerous), AND
        * ``safety.confirm_dangerous`` is truthy (default True), AND
        * ``safety.suppress_all_warnings`` is falsy (default False).

    A safe command (``danger == ""``) never needs confirmation.  When the safety
    section is missing entirely we fail *toward* a warning: ``confirm_dangerous``
    defaults to True and ``suppress_all_warnings`` to False, so a dangerous
    command on a partial/legacy config is still gated.
    """
    if not danger:
        return False
    section = _safety_section(settings)
    confirm = bool(section.get(KEY_CONFIRM_DANGEROUS, True))
    suppressed = bool(section.get(KEY_SUPPRESS_ALL, False))
    return confirm and not suppressed


def tx_hard_block(danger: str, supports_arm: bool, arm_state: str) -> bool:
    """Whether an offensive-TX command must be HARD-BLOCKED (refused outright, not just confirmed).

    The Operate console's armed-lockout only applies to firmwares that actually implement an ARM
    handshake (``supports_arm``): there, a dangerous verb is refused unless the device is ``"armed"``.
    A firmware with no arm concept (Marauder / ESP32-DIV / GhostESP / Bruce / …) has nothing to arm, so
    hard-blocking would leave every offensive button permanently dead. For those, this returns False —
    the command is instead gated by the standard confirm dialog (:func:`should_confirm`) at send time,
    the same posture the Devices tab uses. This is for authorized lab use.

    Returns True only when the command is dangerous AND the firmware arms AND it is not currently armed.
    """
    if not danger:
        return False
    if not supports_arm:
        return False
    return arm_state != "armed"


def needs_first_run_disclaimer(settings: dict[str, Any] | None) -> bool:
    """Return True iff the one-time legal disclaimer has not been acknowledged.

    This is the top-level ``_disclaimer_ack`` flag and is **independent of**
    ``safety.suppress_all_warnings``: suppressing per-command warnings must never
    skip the one-time legal/authorized-use acknowledgement.  Returns True until
    ``_disclaimer_ack`` is set truthy (the UI sets it after the user accepts).
    """
    if not isinstance(settings, dict):
        return True
    return not settings.get(KEY_DISCLAIMER_ACK, False)


# ── Text builders ────────────────────────────────────────────────────

def legal_disclaimer_text() -> str:
    """Return the one-time first-run legal / authorized-use disclaimer body.

    Shown exactly once (gated by :func:`needs_first_run_disclaimer`).  Mentions
    the U.S. jamming statute (47 U.S.C. 333) and the FCC, the authorized /
    controlled-lab-only nature of the offensive features, and that the operator
    bears sole legal responsibility.
    """
    return (
        "AUTHORIZED USE ONLY — LEGAL DISCLAIMER\n"
        "\n"
        "Cyber Controller can drive hardware that transmits radio frequency "
        "(RF) energy and performs offensive security operations (Wi-Fi deauth, "
        "beacon/BLE spam, brute force, SubGHz/NFC replay, and similar).\n"
        "\n"
        "Operating radio jamming or interference equipment is ILLEGAL in most "
        "jurisdictions. In the United States it is prohibited by the "
        "Communications Act — 47 U.S.C. 333 — and the FCC; marketing, using, or "
        "operating a jammer can carry severe civil and criminal penalties.\n"
        "\n"
        "Deauthentication, jamming, brute-force, and spam features are provided "
        "for AUTHORIZED, CONTROLLED-LAB use only — for example on networks and "
        "devices you own or have explicit written permission to test.\n"
        "\n"
        "You, the operator, are SOLELY RESPONSIBLE for ensuring that every "
        "command you send is lawful in your jurisdiction and authorized for the "
        "target. The authors and contributors accept no liability for misuse.\n"
        "\n"
        "Click Accept only if you understand and agree to use this tool lawfully "
        "and only with proper authorization."
    )


def lab_only_warning_text(cmd: str, danger: str) -> str:
    """Return the per-command confirmation body for a dangerous command.

    Args:
        cmd: The command about to be sent (echoed back so the user can confirm
            exactly what will run).
        danger: The danger level from :func:`classify` — :data:`LAB_ONLY` or
            :data:`ILLEGAL_TX`.  Any other/empty value is treated as a generic
            dangerous command (the UI should not normally call this for safe
            commands, but the text stays sensible if it does).

    Returns:
        A multi-line confirmation body suitable for a QMessageBox.  Always
        non-empty and always names the command.
    """
    safe_cmd = cmd.strip() if cmd else "(empty command)"

    if danger == ILLEGAL_TX:
        headline = (
            "ILLEGAL TRANSMISSION WARNING\n"
            "\n"
            "This command transmits RF in a way that is ILLEGAL in most "
            "jurisdictions (e.g. broadband jamming, prohibited in the U.S. under "
            "47 U.S.C. 333 / FCC rules)."
        )
    else:
        headline = (
            "CONTROLLED-LAB-ONLY WARNING\n"
            "\n"
            "This is an offensive RF / attack command (deauth, jamming, brute "
            "force, or spam). Run it ONLY in an authorized, controlled lab "
            "environment, against devices you own or are explicitly permitted "
            "to test."
        )

    return (
        f"{headline}\n"
        "\n"
        f"Command:  {safe_cmd}\n"
        "\n"
        "By continuing you confirm that this operation is lawful in your "
        "jurisdiction and that you have authorization for the target. You are "
        "solely responsible for its use.\n"
        "\n"
        "Send this command?"
    )
