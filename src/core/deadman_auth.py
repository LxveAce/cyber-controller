"""Dead Man's Switch authentication flow.

Detects when a connected device prompts for a DMS password and coordinates
the auth challenge across headed/headless modes.
"""

import logging
import re
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Patterns the device might send when it needs auth.
# The CANONICAL strings are the deadmans-switch boot-gate's actual serial output (GateInput_serial.cpp /
# SPEC §5-6): the prompt is `suicide-gate: enter 'unlock <password>' ...`, a wrong try is
# `suicide-gate: wrong. attempts left: N`, and a backoff is `suicide-gate: locked for Ns.`. The older
# BOOTGATE/SM_AUTH/DMS patterns are kept as defensive aliases but do NOT match real firmware output.
# NOTE: `SM>{...}` status JSON (SM_STATUS/SM_INFO) is deliberately NOT treated as an auth prompt — it is
# safe to poll and must never trigger a password send.
AUTH_PATTERNS = [
    re.compile(r"suicide-gate\s*:\s*enter", re.IGNORECASE),          # real prompt
    re.compile(r"BOOTGATE[:\s]*enter\s*password", re.IGNORECASE),
    re.compile(r"SM_AUTH_REQ", re.IGNORECASE),
    re.compile(r"DMS[:\s]*password\s*required", re.IGNORECASE),
    re.compile(r"\[LOCKED\]\s*Enter\s*password", re.IGNORECASE),
]

AUTH_SUCCESS_PATTERNS = [
    # The real gate prints no distinct "unlocked" line — on a correct password it simply boots (GATE_PASS).
    # These remain as defensive aliases for other firmwares.
    re.compile(r"BOOTGATE[:\s]*unlocked", re.IGNORECASE),
    re.compile(r"SM_AUTH_OK", re.IGNORECASE),
    re.compile(r"DMS[:\s]*authenticated", re.IGNORECASE),
]

AUTH_FAIL_PATTERNS = [
    re.compile(r"suicide-gate\s*:\s*wrong", re.IGNORECASE),          # real wrong-attempt line
    re.compile(r"suicide-gate\s*:\s*locked", re.IGNORECASE),         # real backoff/lockout line
    re.compile(r"attempts?\s*left[:\s]*(\d+)", re.IGNORECASE),       # real "attempts left: N"
    re.compile(r"BOOTGATE[:\s]*denied", re.IGNORECASE),
    re.compile(r"SM_AUTH_FAIL", re.IGNORECASE),
    re.compile(r"DMS[:\s]*wrong\s*password", re.IGNORECASE),
    re.compile(r"attempts?\s*remaining[:\s]*(\d+)", re.IGNORECASE),
]


class DeadManAuth:
    """Monitors serial lines for DMS auth challenges and coordinates password entry."""

    def __init__(self):
        self._on_auth_required: Optional[Callable[[], Optional[str]]] = None
        self._on_auth_result: Optional[Callable[[bool, str], None]] = None

    def set_auth_handler(self, handler: Callable[[], Optional[str]]):
        """Set callback that will be called when auth is needed.

        The handler should return the password string, or None to abort.
        In GUI mode: shows a password dialog and returns the result.
        In CLI mode: uses getpass and returns the result.
        """
        self._on_auth_required = handler

    def set_result_handler(self, handler: Callable[[bool, str], None]):
        """Set callback for auth result notifications (success/fail + message)."""
        self._on_auth_result = handler

    def check_line(self, line: str, send_fn: Callable[[str], None]) -> bool:
        """Check a serial line for auth prompts. Returns True if handled.

        Args:
            line: The serial line received from the device
            send_fn: Function to send a response back to the device
        """
        # Check for auth required
        for pattern in AUTH_PATTERNS:
            if pattern.search(line):
                log.info("Dead Man's Switch auth prompt detected")
                self._handle_auth(send_fn)
                return True

        # Check for auth success
        for pattern in AUTH_SUCCESS_PATTERNS:
            if pattern.search(line):
                log.info("Dead Man's Switch auth succeeded")
                if self._on_auth_result:
                    self._on_auth_result(True, line.strip())
                return True

        # Check for auth failure
        for pattern in AUTH_FAIL_PATTERNS:
            if pattern.search(line):
                log.warning("Dead Man's Switch auth failed: %s", line.strip())
                if self._on_auth_result:
                    self._on_auth_result(False, line.strip())
                return True

        return False

    def _handle_auth(self, send_fn: Callable[[str], None]):
        if not self._on_auth_required:
            log.warning("Auth required but no handler set")
            return
        password = self._on_auth_required()
        if password is None:
            log.info("Auth cancelled by user")
            return
        send_fn(password)
        # A Python str is immutable and cannot be wiped in place; rebinding it to a new string
        # does NOT scrub the original bytes from memory (the old claim here was a no-op). Drop the
        # reference promptly so GC can reclaim it. True in-memory zeroization would require carrying
        # the password as a bytearray end-to-end (as suicide_setup.build does for provisioning).
        del password
