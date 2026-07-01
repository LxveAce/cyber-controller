"""Connect-time device handshake / health probe (comms rework, S3-c).

`Device.connected` says the serial link is *open*; it does NOT say the firmware on the other end is alive or
even speaks a text CLI. This module closes that gap: given an open connection, it sends a probe command
(`help` — which both proves liveness AND lists the live command vocabulary), watches the reply for a short
window, and sets `device.health` + `device.fw_banner` from what came back. It also learns which of the
firmware's known commands the device actually advertises, so command drift (e.g. Marauder `scanall` vs the
older `scanap`) is read from the device, not guessed.

Design:
- The pure pieces — :func:`classify_reply` (lines -> health + banner) and :func:`learn_vocabulary` (lines ->
  which known commands appear) — are unit-testable against canned help dumps with no hardware.
- :func:`probe_device` is the thin orchestration (register a temp line callback, send, wait, classify).
- Non-text-CLI nodes (stream/control-map) are marked ``"no-cli"`` WITHOUT any write — honest, not a failure.
- Best-effort: a probe must never crash a connect, so I/O errors are swallowed and leave health unchanged.

Live-hardware confirmation of the probe against every firmware is the S5 bench gate; the logic + parsing land
here now, test-gated.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# `help` is the most universal probe: it answers on every text-CLI firmware we support and its output IS the
# live command list, which is exactly what we want to learn. Kept as a tuple so it's easy to extend per-firmware.
DEFAULT_PROBE_COMMANDS = ("help",)


@dataclass
class HandshakeResult:
    """Outcome of a probe. ``health`` is the same vocabulary as ``Device.health``."""

    health: str
    banner: str = ""
    live_commands: "frozenset[str]" = frozenset()
    lines: "tuple[str, ...]" = ()


def _protocol_for(device):
    fw = getattr(device, "firmware", "") or ""
    if not fw:
        return None
    try:
        from src.protocols import get_protocol
        return get_protocol(fw)
    except Exception:  # noqa: BLE001
        return None


def probe_commands_for(device) -> list[str]:
    """The probe command(s) to send. Empty for a non-text-CLI node (stream/control-map) — there is no text
    command channel to probe, so nothing is written."""
    if getattr(device, "driver_type", "text-cli") != "text-cli":
        return []
    return list(DEFAULT_PROBE_COMMANDS)


def classify_reply(lines, device) -> "tuple[str, str]":
    """Pure. Given the collected reply *lines*, return ``(health, banner)``: ``"alive"`` if any non-empty line
    came back, else ``"no-reply"``. The banner is the first line the firmware's ``protocol.identify()`` claims
    (a real firmware fingerprint), or the first non-empty line as a fallback, capped for display."""
    nonempty = [ln.strip() for ln in lines if ln and ln.strip()]
    if not nonempty:
        return ("no-reply", "")
    proto = _protocol_for(device)
    banner = ""
    if proto is not None:
        for ln in nonempty:
            try:
                if proto.identify(ln):
                    banner = ln
                    break
            except Exception:  # noqa: BLE001
                pass
    return ("alive", (banner or nonempty[0])[:120])


def learn_vocabulary(lines, device) -> "frozenset[str]":
    """Pure. Which of the firmware's known command names actually appear in the reply text (whole-token match,
    so ``scan`` doesn't match inside ``scanap``). Confirms the device's live vocabulary from what it advertises
    — the fix for command drift. Empty for an unknown firmware or no match."""
    proto = _protocol_for(device)
    if proto is None:
        return frozenset()
    try:
        names = {ci.name.split()[0] for ci in proto.cached_commands() if getattr(ci, "name", "")}
    except Exception:  # noqa: BLE001
        return frozenset()
    if not names:
        return frozenset()
    blob = "\n".join(lines)
    present = {
        name for name in names
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", blob)
    }
    return frozenset(present)


def probe_device(conn, device, *, timeout: float = 0.8, settle: float = 0.15) -> HandshakeResult:
    """Probe an OPEN connection and set ``device.health`` / ``device.fw_banner``.

    Non-text-CLI nodes are marked ``"no-cli"`` with no write. Otherwise: attach a temporary line callback,
    send the probe command(s), wait up to *timeout* for the first reply then a brief *settle* for the rest,
    classify, and detach. Best-effort — never raises on I/O trouble."""
    if not probe_commands_for(device):
        device.health = "no-cli"
        return HandshakeResult(health="no-cli")

    lines: list[str] = []
    cb = lines.append  # bind once so remove_line_callback gets the exact same object we registered
    try:
        conn.on_line(cb)
    except Exception:  # noqa: BLE001 — can't observe the stream, so we can't probe; leave health as-is
        return HandshakeResult(health=getattr(device, "health", "unknown"))

    try:
        for cmd in probe_commands_for(device):
            try:
                conn.write(cmd)
            except Exception:  # noqa: BLE001
                log.debug("probe write %r failed", cmd, exc_info=True)
        _wait_for_reply(lines, timeout=timeout, settle=settle)
    finally:
        try:
            conn.remove_line_callback(cb)
        except Exception:  # noqa: BLE001
            pass

    health, banner = classify_reply(lines, device)
    vocab = learn_vocabulary(lines, device)
    device.health = health
    device.fw_banner = banner
    log.info("handshake %s: %s (banner=%r, %d live commands)",
             getattr(device, "port", "?"), health, banner, len(vocab))
    return HandshakeResult(health=health, banner=banner, live_commands=vocab, lines=tuple(lines))


def _wait_for_reply(lines, *, timeout: float, settle: float) -> None:
    """Wait up to *timeout* for the first line, then a short *settle* window for the rest of a multi-line
    reply (help output arrives as many lines). Returns as soon as the reply stops growing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not lines:
        time.sleep(0.01)
    if not lines:
        return
    settle_deadline = time.monotonic() + settle
    while time.monotonic() < settle_deadline:
        n = len(lines)
        time.sleep(0.02)
        if len(lines) == n:  # a quiet window -> the reply has stopped
            break
