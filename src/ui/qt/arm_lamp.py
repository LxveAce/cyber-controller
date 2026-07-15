"""Shared ARM/SAFE lamp rendering for the offensive-TX arm gate.

A firmware's arm state (LxveOS ``arm``/``disarm``: safe / pending / armed / tx_disabled) maps to a
prominent one-line lamp — green safe, amber mid-handshake, red hot, grey compiled-out. Both the
Devices tab (inline, under the telemetry line) and the Operate console read this, so the table lives
here rather than inside the 79 KB ``device_tab`` module — importing that pulls the whole
BlueJammer / serial / safety stack just to colour a label. ``DeviceTab._arm_lamp_render`` delegates.
"""
from __future__ import annotations

# state -> (label text, color). Colors read as a traffic light. Keep in sync with the firmware's
# arm-gate states (LxveOS lxveos.py arm parsing); an unrecognized-but-nonblank token still renders
# verbatim (muted) so a future arm state is never silently lost.
_ARM_LAMP_TABLE: dict[str, tuple[str, str]] = {
    "safe":        ("● SAFE — offensive TX locked",          "#3fb950"),
    "pending":     ("● ARM PENDING — awaiting token",        "#d29922"),
    "armed":       ("● ARMED — offensive TX permitted",      "#f85149"),
    "tx_disabled": ("● TX DISABLED — offensive TX not built", "#6e7681"),
}


def arm_lamp_render(state: str) -> tuple[str, str]:
    """(label text, color) for an arm state. Blank/unknown -> blank (no lamp until the fw speaks).
    A recognized-but-unlisted token still renders verbatim (muted) so a future state isn't lost."""
    if state in _ARM_LAMP_TABLE:
        return _ARM_LAMP_TABLE[state]
    if state:
        return (f"● {state}", "#8b949e")
    return ("", "#8b949e")
