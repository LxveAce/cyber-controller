"""Guard: the retired acid-green (#39ff14) must never reappear in the UI.

The brand/interactive accent is the LxveAce violet (theme.colors.ACCENT, #a371f7); functional green
lives in theme.colors.SUCCESS (#3fb950, connected/online/go) and TERMINAL (#7ee787, live serial
output). Locks the identity migration so a future edit can't quietly reintroduce the generic green."""

from __future__ import annotations

from pathlib import Path

_UI = Path(__file__).resolve().parent.parent / "src" / "ui"
_EXTS = {".py", ".qss", ".css", ".tcss"}


def test_no_acid_green_in_ui():
    offenders = []
    for p in _UI.rglob("*"):
        if p.suffix.lower() in _EXTS and "__pycache__" not in p.parts:
            if "#39ff14" in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(p.relative_to(_UI)))
    assert not offenders, f"retired acid-green #39ff14 must be a theme token, found in: {offenders}"
