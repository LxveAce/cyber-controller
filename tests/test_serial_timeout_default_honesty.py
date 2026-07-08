"""DEFAULTS must declare the serial timeout that's actually applied.

Regression for audit finding [13] (misleading/vestigial schema): DEFAULTS['serial']['timeout'] was 5, but
device_manager builds SerialConnection without a timeout arg, so the port always used SerialConnection's own default
(1.0s). Nothing reads the DEFAULTS value, so the '5' was a stale number that disagreed with real behavior — a reader
of settings.py would assume a 5s timeout that never happens. This ties the declared default to the constructor
default so they can't silently diverge again. (The remaining inert keys — flash.verify/auto_backup/mode,
cross_comm.* — are already documented as inert in settings_tab._gather_settings and kept only to keep the on-disk
schema stable; they carry no misleading concrete value.)
"""

from __future__ import annotations

import inspect

from src.config.settings import DEFAULTS
from src.core.serial_handler import SerialConnection


def test_defaults_serial_timeout_matches_the_applied_connection_default():
    applied = inspect.signature(SerialConnection.__init__).parameters["timeout"].default
    assert DEFAULTS["serial"]["timeout"] == applied, (
        f"DEFAULTS serial.timeout={DEFAULTS['serial']['timeout']!r} disagrees with the timeout actually used "
        f"({applied!r}) — device_manager builds SerialConnection without a timeout, so DEFAULTS must match its "
        f"constructor default (or be wired through to the port)."
    )
