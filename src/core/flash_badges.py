"""Board-first flash data layer — the board->firmware index + an HONEST per-pair badge.

FlashTab today is firmware-first: a flat list of ~50 firmwares that assumes you already know which
one fits the board you plugged in. Board-first inverts that — pick your hardware, see only the
firmware that runs on it, each row badged with the truth about whether CC can flash it right now.
This module is the DATA half (Atlas owns it); FlashTab (Spade) is the chrome that renders it.

Two pieces:

- :func:`badge_for` — a PURE function ``(profile, board) -> BadgeResult`` deriving one honest badge
  from facts already in the profile + a few small, source-grounded exception constants. It never
  over-promises: ✅ ``PROVEN`` is reserved for owner-HIL-confirmed pairs (the allowlist is EMPTY
  today and fills in the W5 bench audit); a backend merely existing is at most ⚠️ ``EXPERIMENTAL``.
- :func:`build_board_index` — the inverse join over every profile's ``boards[]``: a
  ``(board_name, chip) -> [variant records]`` map, each variant carrying its badge.

The badge is a property of the (board, firmware) **pair**, not the firmware: one profile can flash
one board over esptool and another over uf2 (CatSniffer, WHAD ButteRFly), so :func:`badge_for`
resolves the board's effective backend before badging.

Honesty is the whole point. This is a static UI hint; the flash engine remains the authoritative gate
(it resolves releases at flash time and hard-aborts a staged/source-only profile regardless of what
badge the UI showed).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.core import profile_loader


class Badge(Enum):
    """One honest flash-readiness badge for a (board, firmware) pair."""

    PROVEN = "proven"              # ✅ owner-HIL-confirmed on this exact board
    EXPERIMENTAL = "experimental"  # ⚠️ bundled backend + a real binary, not yet HIL-confirmed
    NEEDS_TOOL = "needs_tool"      # 🔒 flashing needs an external tool CC does not bundle
    SOURCE_ONLY = "source_only"    # ⛔ no prebuilt binary (source-first / staged / no image)


BADGE_EMOJI: dict[Badge, str] = {
    Badge.PROVEN: "✅",
    Badge.EXPERIMENTAL: "⚠️",
    Badge.NEEDS_TOOL: "🔒",
    Badge.SOURCE_ONLY: "⛔",
}

# ── source-grounded exception constants (see the reform spec for each provenance) ────────────────

#: ✅ owner-HIL-confirmed (profile_id, board_name) pairs. EMPTY today — the W5 per-board bench audit
#: fills it one confirmed pair at a time. A backend existing or a "HARDWARE-VALIDATED" profile tag
#: does NOT earn ✅; the owner physically flashing the pair does.
HIL_PROVEN: frozenset[tuple[str, str]] = frozenset()

#: on_error=source_only_empty profiles whose NOTES confirm real shipped release binaries (assets
#: verified live) — the exception that keeps them ⚠️ instead of ⛔.
CONFIRMED_BINARY_FALLBACK: frozenset[str] = frozenset({"m5gotchi", "porkchop"})

#: Unpublished / placeholder-pinned profiles the flash core hard-aborts as STAGED (an unresolved
#: "<commit>"/"<pinned-sha>" ref → the URL 404s). flash_core.py's staged guard names bluestress.
STAGED_UNPUBLISHED: frozenset[str] = frozenset({"bluestress"})

#: Profiles that resolve to NO flashable image: a download directory INDEX (kali_arm) or a
#: git-clone/install-script overlay with zero image assets (raspyjack). Per their notes.
NO_FLASHABLE_IMAGE: frozenset[str] = frozenset({"kali_arm", "raspyjack"})

#: Flash backend -> the external tool the user must install. CC does NOT bundle these. esptool
#: (frozen into the exe), uf2 (drag-drop) and sd (built-in imaging) need no third-party tool.
BACKEND_TOOL: dict[str, str] = {
    "qflipper": "the qFlipper app",
    "adb": "Android platform-tools (adb)",
    "rtl8720": "the Realtek AmebaD ImageTool",
    "cc2538_bsl": "cc2538-bsl",
    "nrf_dfu": "adafruit-nrfutil",
    "hackrf_spiflash": "the hackrf tools",
    "dfu": "dfu-util",
}


@dataclass
class BadgeResult:
    """A badge plus a plain-language reason (and the tool name when ``NEEDS_TOOL``)."""

    badge: Badge
    reason: str
    external_tool: str = ""

    @property
    def emoji(self) -> str:
        return BADGE_EMOJI[self.badge]


def _effective_backend(profile: dict[str, Any], board: dict[str, Any] | None) -> str:
    """The backend that flashes THIS board: a per-board override wins over the profile default
    (esptool), because one profile can flash different boards over different backends."""
    be = (board or {}).get("backend") or profile.get("backend") or "esptool"
    return str(be).strip().lower()


def _source_only_reason(pid: str, profile: dict[str, Any]) -> str | None:
    """A plain reason string if this profile has no prebuilt binary to flash, else None."""
    if pid in STAGED_UNPUBLISHED:
        return "staged/unpublished — the pinned firmware ref is not finalized yet"
    if pid in NO_FLASHABLE_IMAGE:
        return "no flashable image — the source is a directory index / install-script overlay"
    on_error = (profile.get("resolver_params") or {}).get("on_error")
    if on_error == "source_only_empty" and pid not in CONFIRMED_BINARY_FALLBACK:
        return "source-first build — releases publish no prebuilt binary"
    return None


def badge_for(profile: dict[str, Any], board: dict[str, Any] | None = None) -> BadgeResult:
    """Derive the honest flash badge for a (profile, board) pair.

    Precedence, first match wins: ✅ owner-HIL → ⛔ source-only (no binary at all) → 🔒 needs an
    external tool → ⚠️ experimental (bundled backend + a real binary, unverified). *board* is a
    single entry from ``profile['boards']``; pass None to badge the profile's first board's context.
    """
    pid = str(profile.get("id") or "")
    pid_n = pid.replace("-", "_").lower()   # ids vary hyphen/underscore (kali-arm vs kali_arm)
    board_name = str((board or {}).get("name") or "")

    if (pid_n, board_name) in HIL_PROVEN:
        return BadgeResult(Badge.PROVEN, "owner HIL-confirmed this firmware on this board")

    reason = _source_only_reason(pid_n, profile)
    if reason is not None:
        return BadgeResult(Badge.SOURCE_ONLY, reason)

    be = _effective_backend(profile, board)
    if be in BACKEND_TOOL:
        tool = BACKEND_TOOL[be]
        return BadgeResult(Badge.NEEDS_TOOL, f"needs {tool} (backend {be}, not bundled)",
                           external_tool=tool)

    return BadgeResult(Badge.EXPERIMENTAL,
                       f"bundled backend ({be}) + a release binary, not yet HIL-confirmed")


def _profiles_dir() -> Path:
    """``src/config/profiles`` relative to this module."""
    return Path(__file__).resolve().parent.parent / "config" / "profiles"


def build_board_index(profiles_dir: str | Path | None = None) -> dict[tuple[str, str], list[dict]]:
    """Inverse-join every profile's ``boards[]`` into a board-keyed index.

    Returns ``{(board_name, chip): [variant, ...]}`` where each variant is
    ``{firmware, profile_id, backend, badge, badge_emoji, reason, external_tool}``. The same
    board named across several profiles collapses to one key with one variant per profile.
    """
    root = Path(profiles_dir) if profiles_dir else _profiles_dir()
    index: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(root.glob("*.json")):
        profile = profile_loader.load_rich(path)
        pid = str(profile.get("id") or path.stem)
        firmware = str(profile.get("name") or pid)
        for board in profile_loader.list_boards(profile):
            key = (str(board.get("name") or ""), str(board.get("chip") or ""))
            res = badge_for(profile, board)
            index.setdefault(key, []).append({
                "firmware": firmware,
                "profile_id": pid,
                "backend": _effective_backend(profile, board),
                "badge": res.badge,
                "badge_emoji": res.emoji,
                "reason": res.reason,
                "external_tool": res.external_tool,
            })
    return index
