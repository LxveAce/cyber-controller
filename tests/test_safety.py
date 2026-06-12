"""Tests for ``src.core.safety`` — the pure danger-classification + gate engine.

These tests need NO hardware and NO heavy optional deps (no PyQt5, pyserial, or
esptool): the module under test is pure Python and reads only plain dicts, so we
can exercise every branch directly.  ``src.protocols.base`` (for ``CommandInfo``)
is stdlib-only, so the ``importorskip`` is belt-and-suspenders parity with the
rest of the suite.
"""

from __future__ import annotations

import pytest

safety = pytest.importorskip("src.core.safety")

from src.core.safety import (  # noqa: E402  (after importorskip)
    ILLEGAL_TX,
    LAB_ONLY,
    SAFE,
    classify,
    lab_only_warning_text,
    legal_disclaimer_text,
    needs_first_run_disclaimer,
    should_confirm,
    worst_of,
)
from src.protocols.base import CommandInfo  # noqa: E402


# ── Constants sanity ─────────────────────────────────────────────────

def test_level_constants() -> None:
    assert SAFE == ""
    assert LAB_ONLY == "lab-only"
    assert ILLEGAL_TX == "illegal-tx"


# ── Keyword classification: lab-only set ─────────────────────────────

# Every keyword the spec requires to map to AT LEAST lab-only.  Some of these
# (jammer/protokill) are also in the illegal set; those are asserted separately
# below — here we only require they are not downgraded to safe.
_LAB_ONLY_KEYWORDS = [
    "deauth",
    "jam",
    "beacon",
    "spam",
    "brute",
    "sourapple",
    "sour_apple",
    "attack",
]


@pytest.mark.parametrize("kw", _LAB_ONLY_KEYWORDS)
def test_keyword_classifies_at_least_lab_only(kw: str) -> None:
    level = classify(kw)
    assert level in (LAB_ONLY, ILLEGAL_TX)
    assert level != SAFE


@pytest.mark.parametrize(
    "kw, expected",
    [
        ("deauth", LAB_ONLY),
        ("beacon", LAB_ONLY),
        ("spam", LAB_ONLY),
        ("brute", LAB_ONLY),
        ("sourapple", LAB_ONLY),
        ("sour_apple", LAB_ONLY),
        ("attack", LAB_ONLY),
    ],
)
def test_lab_only_keywords_exact_level(kw: str, expected: str) -> None:
    assert classify(kw) == expected


# ── Keyword classification: illegal-tx set ───────────────────────────

@pytest.mark.parametrize(
    "kw",
    [
        "broadband", "jammer", "jam_reader", "tag_disrupt", "protokill",
        # Regression: ANY jam-family command must reach illegal-tx, not the
        # milder lab-only — jamming is the operation the disclaimer cites as
        # illegal under 47 U.S.C. 333 / FCC.
        "jam", "jamming", "signal_jam", "rf_jam", "ble_jam",
    ],
)
def test_illegal_tx_keywords(kw: str) -> None:
    assert classify(kw) == ILLEGAL_TX


def test_illegal_keyword_is_not_downgraded_when_also_lab_keyword() -> None:
    # 'jammer' contains 'jam' (lab-only) but must resolve to the worse level.
    assert classify("jammer") == ILLEGAL_TX
    # 'protokill' is in both tables; illegal-tx must win.
    assert classify("protokill") == ILLEGAL_TX


# ── Keyword classification: realistic command surfaces ───────────────

@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("attack -t deauth", LAB_ONLY),
        ("attack -t beacon -r", LAB_ONLY),
        ("wifi_deauth", LAB_ONLY),
        ("blespam -t apple", LAB_ONLY),
        ("subghz_brute", LAB_ONLY),
        ("subghz_replay", LAB_ONLY),
        ("broadband_jam", ILLEGAL_TX),
        ("tag_disrupt", ILLEGAL_TX),
    ],
)
def test_classify_realistic_commands(cmd: str, expected: str) -> None:
    assert classify(cmd) == expected


def test_classification_is_case_insensitive() -> None:
    assert classify("DEAUTH") == LAB_ONLY
    assert classify("Attack -T Deauth") == LAB_ONLY
    assert classify("BROADBAND") == ILLEGAL_TX


# ── Benign / unknown input classifies as safe ────────────────────────

@pytest.mark.parametrize(
    "cmd",
    [
        "",
        "   ",
        "scanap",
        "list -a",
        "status",
        "info",
        "version",
        "help",
        "channel 6",
        "reboot",
        "nfc_scan",
        "wifi_scan",
        "guardian",
    ],
)
def test_benign_commands_classify_safe(cmd: str) -> None:
    assert classify(cmd) == SAFE


# ── CommandInfo.danger takes precedence over keywords ────────────────

def test_classify_prefers_commandinfo_danger() -> None:
    # The string alone would scan as safe, but the annotation says lab-only.
    info = CommandInfo("scanap", "Scanning", "scan", danger=LAB_ONLY)
    assert classify("scanap", info) == LAB_ONLY


def test_classify_commandinfo_illegal_overrides_safe_string() -> None:
    info = CommandInfo("mystery_cmd", "X", "y", danger=ILLEGAL_TX)
    assert classify("mystery_cmd", info) == ILLEGAL_TX


def test_classify_empty_commandinfo_danger_falls_back_to_keywords() -> None:
    # danger == "" -> not authoritative -> keyword scan of the string wins.
    info = CommandInfo("attack -t deauth", "Attack", "deauth", danger="")
    assert classify("attack -t deauth", info) == LAB_ONLY


def test_classify_commandinfo_danger_is_stripped() -> None:
    info = CommandInfo("x", danger="  lab-only  ")
    assert classify("benign", info) == LAB_ONLY


def test_classify_none_info_uses_keywords() -> None:
    assert classify("attack", None) == LAB_ONLY


# ── worst_of helper ──────────────────────────────────────────────────

def test_worst_of_picks_highest_severity() -> None:
    assert worst_of(SAFE, LAB_ONLY) == LAB_ONLY
    assert worst_of(LAB_ONLY, ILLEGAL_TX) == ILLEGAL_TX
    assert worst_of(SAFE, SAFE) == SAFE
    assert worst_of() == SAFE


def test_worst_of_ignores_unknown_levels() -> None:
    # An unrecognised level must never lower a real danger.
    assert worst_of("bogus", LAB_ONLY) == LAB_ONLY
    assert worst_of("bogus") == SAFE


# ── should_confirm truth table ───────────────────────────────────────

def _settings(confirm: bool, suppress: bool, ack: bool = True) -> dict:
    """Build a settings dict in the integrator's DEFAULTS shape."""
    return {
        "safety": {
            "confirm_dangerous": confirm,
            "suppress_all_warnings": suppress,
        },
        "_disclaimer_ack": ack,
    }


def test_should_confirm_safe_command_never_confirms() -> None:
    # No matter the settings, a safe (empty-danger) command is never gated.
    for confirm in (True, False):
        for suppress in (True, False):
            assert should_confirm(SAFE, _settings(confirm, suppress)) is False


@pytest.mark.parametrize("danger", [LAB_ONLY, ILLEGAL_TX])
@pytest.mark.parametrize(
    "confirm, suppress, expected",
    [
        (True, False, True),    # default: confirm dangerous, not suppressed
        (True, True, False),    # suppress overrides confirm
        (False, False, False),  # confirmation disabled
        (False, True, False),   # both off
    ],
)
def test_should_confirm_truth_table(
    danger: str, confirm: bool, suppress: bool, expected: bool
) -> None:
    assert should_confirm(danger, _settings(confirm, suppress)) is expected


def test_should_confirm_missing_section_fails_toward_warning() -> None:
    # No 'safety' section at all -> defaults (confirm=True, suppress=False) ->
    # a dangerous command is still gated.
    assert should_confirm(LAB_ONLY, {}) is True
    assert should_confirm(ILLEGAL_TX, {"safety": {}}) is True
    assert should_confirm(SAFE, {}) is False


def test_should_confirm_tolerates_none_and_garbage() -> None:
    assert should_confirm(LAB_ONLY, None) is True
    assert should_confirm(LAB_ONLY, {"safety": "not-a-dict"}) is True
    assert should_confirm(SAFE, None) is False


# ── needs_first_run_disclaimer ───────────────────────────────────────

def test_needs_first_run_disclaimer_true_when_unacked() -> None:
    assert needs_first_run_disclaimer({"_disclaimer_ack": False}) is True
    assert needs_first_run_disclaimer({}) is True
    assert needs_first_run_disclaimer(None) is True


def test_needs_first_run_disclaimer_false_when_acked() -> None:
    assert needs_first_run_disclaimer({"_disclaimer_ack": True}) is False


def test_disclaimer_is_independent_of_suppress_all_warnings() -> None:
    # The crux invariant: suppressing per-command warnings must NOT skip the
    # one-time disclaimer.  With warnings fully suppressed but no ack yet, the
    # disclaimer is still required.
    suppressed_unacked = {
        "safety": {"confirm_dangerous": False, "suppress_all_warnings": True},
        "_disclaimer_ack": False,
    }
    assert needs_first_run_disclaimer(suppressed_unacked) is True
    # And once acknowledged, suppression state is irrelevant.
    suppressed_acked = dict(suppressed_unacked)
    suppressed_acked["_disclaimer_ack"] = True
    assert needs_first_run_disclaimer(suppressed_acked) is False


def test_disclaimer_and_confirm_are_orthogonal() -> None:
    # Suppressing warnings turns OFF per-command confirmation but does NOT
    # affect whether the first-run disclaimer must show.
    s = {
        "safety": {"confirm_dangerous": True, "suppress_all_warnings": True},
        "_disclaimer_ack": False,
    }
    assert should_confirm(LAB_ONLY, s) is False          # warning suppressed
    assert needs_first_run_disclaimer(s) is True          # disclaimer still due


# ── Text builders ────────────────────────────────────────────────────

def test_legal_disclaimer_text_mentions_key_terms() -> None:
    text = legal_disclaimer_text()
    assert isinstance(text, str)
    assert text.strip()
    lower = text.lower()
    assert "47 u.s.c. 333" in lower
    assert "fcc" in lower
    assert "authorized" in lower
    # Mentions controlled-lab use and operator responsibility.
    assert "lab" in lower
    assert "responsible" in lower


@pytest.mark.parametrize("danger", [LAB_ONLY, ILLEGAL_TX])
def test_lab_only_warning_text_non_empty_and_names_command(danger: str) -> None:
    cmd = "attack -t deauth"
    text = lab_only_warning_text(cmd, danger)
    assert isinstance(text, str)
    assert text.strip()
    assert cmd in text  # the exact command is echoed for confirmation


def test_lab_only_warning_text_lab_mentions_authorization() -> None:
    text = lab_only_warning_text("attack -t deauth", LAB_ONLY).lower()
    assert "lab" in text
    assert "authoriz" in text  # authorized / authorization


def test_lab_only_warning_text_illegal_mentions_illegality() -> None:
    text = lab_only_warning_text("broadband_jam", ILLEGAL_TX).lower()
    assert "illegal" in text
    assert "47 u.s.c. 333" in text or "fcc" in text


def test_lab_only_warning_text_handles_empty_command() -> None:
    # Should not raise and should still produce a usable, non-empty body.
    text = lab_only_warning_text("", LAB_ONLY)
    assert text.strip()
