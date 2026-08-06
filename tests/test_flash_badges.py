"""Board-first flash badges (``src/core/flash_badges.py``) — the honest per-pair badge + index.

Every assertion runs against the REAL shipped profiles in ``src/config/profiles`` (via conftest's
``PROFILES_DIR``), so the badges are checked against the actual backend/resolver facts, not a mock.
The overriding rule under test is honesty: nothing badges higher than the facts justify — ✅ is
owner-HIL only (empty today), and source-only / needs-tool are correctly de-rated.
"""
from __future__ import annotations

from conftest import PROFILES_DIR

from src.core import profile_loader
from src.core.flash_badges import (
    BADGE_EMOJI,
    Badge,
    badge_for,
    build_board_index,
)


def _prof(pid: str) -> dict:
    return profile_loader.load_rich(PROFILES_DIR / f"{pid}.json")


def _badge(pid: str, board_idx: int = 0) -> Badge:
    p = _prof(pid)
    boards = profile_loader.list_boards(p)
    board = boards[board_idx] if boards else None
    return badge_for(p, board).badge


# ── ⛔ source-only ───────────────────────────────────────────────────────────────────────────────

def test_flock_you_is_source_only():
    # on_error=source_only_empty, note admits "formal releases may 404" — no prebuilt binary.
    assert _badge("flock_you") is Badge.SOURCE_ONLY


def test_all_source_only_empty_profiles_are_source_only_except_the_confirmed_two():
    # The 9 on_error=source_only_empty profiles: 7 ship no binary (⛔); m5gotchi + porkchop have
    # notes confirming real release binaries, so they stay ⚠️.
    source_only = ("airtag_scanner", "cyt_ng", "flock_you", "halehound", "minigotchi",
                   "oui_spy", "sky_spy")
    for pid in source_only:
        assert _badge(pid) is Badge.SOURCE_ONLY, f"{pid} should be source-only"


def test_staged_unpublished_is_source_only():
    # bluestress: LxveAce/BlueStress unpublished, pinned ref is a placeholder — flash core aborts.
    assert _badge("bluestress") is Badge.SOURCE_ONLY


def test_index_only_and_overlay_are_source_only():
    # kali_arm points at a directory index; raspyjack ships zero image assets (script overlay).
    assert _badge("kali_arm") is Badge.SOURCE_ONLY
    assert _badge("raspyjack") is Badge.SOURCE_ONLY


# ── ⚠️ experimental (the confirmed-binary exception + the esptool/sd baseline) ────────────────────

def test_confirmed_binary_fallback_stays_experimental():
    # Same on_error=source_only_empty as flock_you, but their notes verify shipped binaries.
    assert _badge("m5gotchi") is Badge.EXPERIMENTAL
    assert _badge("porkchop") is Badge.EXPERIMENTAL


def test_esptool_release_profile_is_experimental():
    # Marauder: esptool + a real GitHub release, not yet HIL-confirmed → ⚠️ (not ✅).
    assert _badge("marauder") is Badge.EXPERIMENTAL


def test_sd_real_image_is_experimental():
    # pwnagotchi: sd backend (bundled imaging) + a real Pi .img release → ⚠️.
    assert _badge("pwnagotchi") is Badge.EXPERIMENTAL


# ── 🔒 needs an external tool ─────────────────────────────────────────────────────────────────────

def test_qflipper_backend_needs_tool():
    p = _prof("flipper_momentum")
    res = badge_for(p, profile_loader.list_boards(p)[0])
    assert res.badge is Badge.NEEDS_TOOL and "qFlipper" in res.external_tool


def test_adb_and_cc2538_backends_need_tool():
    assert _badge("rayhunter") is Badge.NEEDS_TOOL       # adb → platform-tools
    assert _badge("catsniffer") is Badge.NEEDS_TOOL      # cc2538_bsl → cc2538-bsl


# ── ✅ proven is owner-HIL only (empty today) ─────────────────────────────────────────────────────

def test_no_pair_is_proven_because_the_hil_allowlist_is_empty():
    proven = [
        f"{v['profile_id']}/{k[0]}"
        for k, variants in build_board_index().items()
        for v in variants
        if v["badge"] is Badge.PROVEN
    ]
    assert proven == [], f"nothing is HIL-confirmed yet, so nothing may be ✅: {proven}"


# ── the (board, firmware) PAIR principle ──────────────────────────────────────────────────────────

def test_per_board_backend_override_beats_profile_backend():
    # CatSniffer's profile backend is cc2538_bsl (🔒), but a board that overrides to a bundled
    # backend must badge ⚠️ — the badge follows the PAIR, not the firmware.
    p = _prof("catsniffer")
    board = dict(profile_loader.list_boards(p)[0])
    board["backend"] = "uf2"
    assert badge_for(p, board).badge is Badge.EXPERIMENTAL


# ── the board index ───────────────────────────────────────────────────────────────────────────────

def test_build_board_index_shape_and_multi_firmware_board():
    index = build_board_index()
    assert index, "board index must not be empty"
    for key, variants in index.items():
        assert isinstance(key, tuple) and len(key) == 2   # (board_name, chip)
        for v in variants:
            assert v["firmware"] and v["profile_id"] and v["backend"]
            assert isinstance(v["badge"], Badge) and v["badge_emoji"] in BADGE_EMOJI.values()
            assert v["reason"]
    # An M5Cardputer key carries several firmwares (Bruce/GhostESP/Marauder/M5Gotchi/...).
    cardputer = [v for k, variants in index.items() if "Cardputer" in k[0] for v in variants]
    assert len({v["profile_id"] for v in cardputer}) >= 3


def test_badge_emoji_are_distinct():
    assert len(set(BADGE_EMOJI.values())) == len(Badge)
