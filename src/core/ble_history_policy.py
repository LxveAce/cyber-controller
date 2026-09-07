"""Pure BLE-history configuration policy boundary (runtime-policy slice, pre-journal).

Decide, from an already-loaded settings snapshot, whether BLE history is ``disabled``, an in-memory
journal, or an *eligible* persistent journal — and, for an eligible persistent request, validate a
caller-supplied directory override as absolute-path *syntax* only. This is a configuration
**eligibility** decision, not journal activation: a selected persistent policy still needs later
directory admission, a started + owned journal, and live security-transition handling before it is
ever running / available / durable.

Strictly pure. It does not load settings, inspect ``os.environ``, resolve a home/config directory,
call ``secure_store``, create or start a journal, or do any filesystem / process / network work. It
reads only the mapping it is handed and returns a frozen decision built from finite constants — a
reason never contains a path, raw settings, or exception text, no arbitrary supplied mode is echoed,
and no HTTP serialization is introduced here (the private ``override_path`` intentionally retains a
valid supplied path). The decision retains no reference to the source mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Optional

# Known / selected modes (exact strings).
MODE_DISABLED = "disabled"
MODE_MEMORY = "memory"
MODE_PERSISTENT = "persistent"
_KNOWN_MODES = (MODE_DISABLED, MODE_MEMORY, MODE_PERSISTENT)

# Caller-supplied path-syntax flavors, so both platforms are testable without touching a real path.
FLAVOR_POSIX = "posix"
FLAVOR_WINDOWS = "windows"
_KNOWN_FLAVORS = (FLAVOR_POSIX, FLAVOR_WINDOWS)

# Finite reason constants. A reason never contains a path, raw settings, or exception text.
REASON_MALFORMED_SETTINGS = "malformed_settings"
REASON_MALFORMED_SECTION = "malformed_ble_history_section"
REASON_MALFORMED_MODE = "malformed_mode"
REASON_UNKNOWN_MODE = "unknown_mode"
REASON_MALFORMED_ACK = "malformed_plaintext_ack"
REASON_PERSISTENT_REQUIRES_ACK = "persistent_requires_plaintext_ack"
REASON_SECURE_CONTAINER_CONFLICT = "secure_container_conflict"
REASON_MALFORMED_SECURITY = "malformed_security_section"
REASON_MALFORMED_OVERRIDE = "malformed_override_path"
REASON_MALFORMED_FLAVOR = "malformed_path_flavor"


@dataclass(frozen=True)
class BleHistoryPolicy:
    """An immutable BLE-history *eligibility* decision — never running / available / durable.

    ``selected`` is the usable policy: one of the known modes, or ``None`` when the request is
    refused or malformed (``reason`` then names why, as a finite constant). ``requested`` is the
    recognized requested mode, or ``None`` for an unknown/malformed request (an arbitrary supplied
    value is never echoed). ``override_path`` is a caller-supplied directory kept as *private*
    configuration for an eligible persistent selection only; it is validated as absolute-path syntax
    and preserved verbatim — never resolved, created, opened, or serialized into status here.
    """

    requested: Optional[str]
    selected: Optional[str]
    reason: Optional[str] = None
    override_path: Optional[str] = None

    @property
    def eligible(self) -> bool:
        """True when a usable policy was selected (``disabled`` / ``memory`` / ``persistent``)."""
        return self.selected is not None


def _is_exact_bool(value: Any) -> bool:
    """Exact ``bool`` identity — not truthiness, and not a bool-like ``0``/``1``/``"true"``."""
    return value is True or value is False


def _is_absolute_path_syntax(value: str, flavor: str) -> bool:
    """Pure syntax check: is *value* an absolute path for *flavor*? No filesystem access.

    Rejects empty / whitespace-only and embedded NUL, then defers to the pure ``PurePosixPath`` /
    ``PureWindowsPath`` ``is_absolute()`` predicate for the flavor (so complete drive / UNC /
    extended-length absolute forms are accepted and host-only UNC, drive-relative, root-relative and
    relative forms are rejected). The value is never normalized, resolved, or touched on disk.
    """
    if "\x00" in value or not value.strip():
        return False
    if flavor == FLAVOR_POSIX:
        return PurePosixPath(value).is_absolute()
    return PureWindowsPath(value).is_absolute()


def decide_ble_history_policy(
    settings: Any,
    *,
    override: Any = None,
    path_flavor: str = FLAVOR_POSIX,
) -> BleHistoryPolicy:
    """Decide the BLE-history policy from an already-loaded *settings* snapshot (pure).

    *override* is an optional caller-supplied directory value, interpreted only for an eligible
    persistent request. *path_flavor* (``"posix"`` / ``"windows"``) selects path-syntax rules and
    is validated only when an override is actually interpreted. Returns a frozen
    :class:`BleHistoryPolicy`; malformed or refused input yields ``selected=None`` with a finite
    ``reason`` and never a persist selection.
    """
    if not isinstance(settings, Mapping):
        return BleHistoryPolicy(requested=None, selected=None, reason=REASON_MALFORMED_SETTINGS)

    # Distinguish a MISSING key (disabled default) from a PRESENT null/malformed value (a finite
    # configuration error): membership, not ``.get() is None``, which would conflate the two.
    if "ble_history" not in settings:
        return BleHistoryPolicy(requested=MODE_DISABLED, selected=MODE_DISABLED)
    section = settings["ble_history"]
    if not isinstance(section, Mapping):
        return BleHistoryPolicy(requested=None, selected=None, reason=REASON_MALFORMED_SECTION)

    if "mode" not in section:
        requested = MODE_DISABLED
    else:
        raw_mode = section["mode"]
        if type(raw_mode) is not str:
            return BleHistoryPolicy(requested=None, selected=None, reason=REASON_MALFORMED_MODE)
        if raw_mode not in _KNOWN_MODES:
            # Never echo the arbitrary supplied mode into the decision.
            return BleHistoryPolicy(requested=None, selected=None, reason=REASON_UNKNOWN_MODE)
        requested = raw_mode

    # A present plaintext_ack must be an exact bool whenever present (structural validation), before
    # the mode branch — a malformed ack is a bounded configuration error regardless of mode.
    if "plaintext_ack" in section and not _is_exact_bool(section.get("plaintext_ack")):
        return BleHistoryPolicy(requested=requested, selected=None, reason=REASON_MALFORMED_ACK)

    if requested in (MODE_DISABLED, MODE_MEMORY):
        # No directory/path interpretation; an unused override is ignored; memory is non-durable.
        return BleHistoryPolicy(requested=requested, selected=requested)

    # requested == persistent
    if section.get("plaintext_ack") is not True:
        return BleHistoryPolicy(
            requested=requested, selected=None, reason=REASON_PERSISTENT_REQUIRES_ACK)

    # Secure-container conflict: nested security.secure_container from the SAME snapshot. Exact bool
    # identity only — no truthiness, no unlocked/available lookup, no second load, no secure_store.
    security = settings.get("security", {})
    if not isinstance(security, Mapping):
        return BleHistoryPolicy(
            requested=requested, selected=None, reason=REASON_MALFORMED_SECURITY)
    secure = security.get("secure_container", False)   # missing nested field uses the False default
    if secure is True:
        return BleHistoryPolicy(
            requested=requested, selected=None, reason=REASON_SECURE_CONTAINER_CONFLICT)
    if secure is not False:
        return BleHistoryPolicy(
            requested=requested, selected=None, reason=REASON_MALFORMED_SECURITY)

    # Eligible persistent: interpret the override (absolute-path syntax only) when one was supplied.
    if override is None:
        return BleHistoryPolicy(requested=requested, selected=MODE_PERSISTENT, override_path=None)
    # Require an exact str before any membership/equality, so a str subclass or an equality-impostor
    # can neither be accepted as a flavor nor leak its own exception out of this boundary.
    if type(path_flavor) is not str or path_flavor not in _KNOWN_FLAVORS:
        return BleHistoryPolicy(requested=requested, selected=None, reason=REASON_MALFORMED_FLAVOR)
    if type(override) is not str or not _is_absolute_path_syntax(override, path_flavor):
        return BleHistoryPolicy(
            requested=requested, selected=None, reason=REASON_MALFORMED_OVERRIDE)
    return BleHistoryPolicy(requested=requested, selected=MODE_PERSISTENT, override_path=override)
