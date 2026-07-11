"""Unit tests for the Windows Defender helper (src/core/defender.py).

Only the pure / read-only pieces are exercised — the elevated exclusion-add and the exe launch-probe
shell out (and are interactive on Windows), so they're covered by the owner's real-machine test, not here.
"""
from __future__ import annotations

from src.core import defender


def test_exclusion_command_names_the_path():
    cmd = defender.exclusion_command(r"C:\Users\x\.cyber-controller\tools")
    assert cmd.startswith("Add-MpPreference -ExclusionPath")
    assert r"C:\Users\x\.cyber-controller\tools" in cmd


def test_exclusion_command_escapes_single_quotes():
    # A path with a ' (a valid Windows username char, e.g. O'Brien) must be escaped by doubling the
    # quote — otherwise it breaks out of the single-quoted literal, and this text runs ELEVATED.
    cmd = defender.exclusion_command(r"C:\Users\O'Brien\tools")
    assert "O''Brien" in cmd                      # the ' was doubled
    assert cmd.count("'") % 2 == 0                # quotes are balanced (no early break-out)
    # no lone unescaped quote that could terminate the string then inject
    assert "O'Brien" not in cmd.replace("O''Brien", "")


def test_is_windows_is_bool():
    assert isinstance(defender.is_windows(), bool)


def test_status_queries_never_crash():
    # Off Windows -> None; on Windows -> True/False. Either way, never raises.
    assert defender.pua_protection_on() in (True, False, None)
    assert defender.realtime_on() in (True, False, None)
