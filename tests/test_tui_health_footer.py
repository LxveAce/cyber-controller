"""TUI HealthFooter — the colour-coded health class must react to RAM pressure, not only CPU.

The footer shows both 'CPU: x% | RAM: y%'. Before the fix only watch_cpu_pct existed, so a machine sitting at
low CPU but near-exhausted RAM stayed styled health-ok, defeating the indicator for the RAM half it displays.
Both halves must be able to raise the footer to health-warn/health-crit.
"""
from __future__ import annotations

import pytest

pytest.importorskip("textual")

from src.ui.tui.app import HealthFooter, _health_class


def test_high_ram_low_cpu_raises_crit():
    # CPU healthy (5%), RAM critical (95%) — the footer must go health-crit off the RAM half.
    footer = HealthFooter()
    footer.cpu_pct = 5.0
    footer.ram_pct = 95.0
    assert footer.has_class("health-crit")
    assert not footer.has_class("health-ok")


def test_high_ram_low_cpu_raises_warn():
    # CPU healthy (5%), RAM in the warn band (70%) — footer must be health-warn, not health-ok.
    footer = HealthFooter()
    footer.cpu_pct = 5.0
    footer.ram_pct = 70.0
    assert footer.has_class("health-warn")
    assert not footer.has_class("health-ok")


def test_high_cpu_still_raises_crit():
    # Regression guard for the original behaviour: CPU pressure alone still drives the class.
    footer = HealthFooter()
    footer.ram_pct = 5.0
    footer.cpu_pct = 95.0
    assert footer.has_class("health-crit")


def test_both_low_is_ok():
    footer = HealthFooter()
    footer.cpu_pct = 10.0
    footer.ram_pct = 10.0
    assert footer.has_class("health-ok")
    assert not footer.has_class("health-crit")


def test_class_follows_worse_of_the_two():
    # The class tracks whichever half is worse.
    footer = HealthFooter()
    footer.cpu_pct = 90.0  # crit
    footer.ram_pct = 10.0  # ok
    assert footer.has_class("health-crit")
    assert _health_class(max(90.0, 10.0)) == "health-crit"
