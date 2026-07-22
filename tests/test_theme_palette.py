"""Theme palette is the single source of truth (A4 deslop).

Guards that every ``${TOKEN}`` placeholder in cyber_dark.qss resolves against ``colors.PALETTE`` (so
``apply_theme`` never leaves an unsubstituted token), and that the A4-added tokens exist. Qt-free.
"""
from __future__ import annotations

import pathlib
import re

from src.ui.qt.theme import colors


def test_palette_covers_every_qss_placeholder():
    qss = (pathlib.Path(colors.__file__).parent / "cyber_dark.qss").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{(\w+)\}", qss))
    missing = used - set(colors.PALETTE)
    assert not missing, f"cyber_dark.qss uses tokens not in colors.PALETTE: {sorted(missing)}"


def test_a4_added_tokens_present_and_registered():
    # A4 tokenised device_tab's inline hexes; these three colours needed new palette entries.
    assert colors.TEXT_DIM == "#6e7681"
    assert colors.ALERT == "#d29922"
    assert colors.ERROR_BRIGHT == "#ff6a60"
    for name in ("TEXT_DIM", "ALERT", "ERROR_BRIGHT"):
        assert name in colors.PALETTE, f"{name} missing from PALETTE (QSS ${{{name}}} would not resolve)"


def test_palette_values_match_their_constants():
    # every PALETTE entry equals its module constant (no drift between the two views of a token)
    for name, value in colors.PALETTE.items():
        assert getattr(colors, name) == value, f"{name}: constant != PALETTE value"
