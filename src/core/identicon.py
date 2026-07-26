"""Deterministic identicon — a stable little avatar for a MAC / node id (card-identity core).

The design brief calls for a Meshtastic-style avatar/identicon per MAC/node, reused across the
object's table row, detail panel, map marker, and archive so the same discovered thing has ONE
visual identity everywhere it appears. This module is the pure, Qt-free core of that: a key (a MAC,
BSSID, node id, or basic-id) maps deterministically to a small symmetric on/off cell grid + an
accent colour. The Qt render (a QPixmap) is a thin layer over this in the UI package, so the
pattern/colour logic unit-tests headless with exact, reproducible output.

Pure: no Qt, no I/O, no randomness — the same key always yields the same identicon, which is the
point (a device keeps its face across sessions and views).
"""
from __future__ import annotations

import colorsys
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Identicon:
    """A resolved identicon: a symmetric ``grid``x``grid`` cell mask + an RGB accent colour.

    ``cells[row][col]`` is True where the accent colour paints; the grid is left/right mirrored so
    the face is symmetric (the GitHub-identicon convention). ``color`` is an ``(r, g, b)`` 0-255
    tuple chosen for contrast on a dark UI (fixed saturation + lightness, hue from the key).
    """

    key: str
    grid: int
    cells: tuple[tuple[bool, ...], ...]
    color: tuple[int, int, int]


def _digest(key: str) -> bytes:
    """A stable digest of the normalised key. Case/separator-insensitive so ``AA:BB:..`` and
    ``aabb..`` (the same MAC in different notations) get the SAME face."""
    norm = key.strip().lower().replace(":", "").replace("-", "").replace(" ", "")
    return hashlib.sha256(norm.encode("utf-8")).digest()


def _color_from(digest: bytes) -> tuple[int, int, int]:
    """Pick an accent colour from the digest: hue spans the wheel, but saturation + lightness are
    pinned to a legible band so every identicon reads clearly on a dark ground (no near-black, no
    washed-out pastel). Deterministic."""
    hue = digest[0] / 255.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.62, 0.68)  # L=0.62, S=0.68 — bright, not neon
    return (round(r * 255), round(g * 255), round(b * 255))


def identicon(key: str, grid: int = 5) -> Identicon:
    """Resolve *key* to a deterministic symmetric :class:`Identicon`.

    A ``grid`` of 5 (default) gives the classic 5x5 GitHub look: each of the left-hand columns (the
    middle one is the mirror axis) has ``grid`` cells decided by successive digest bits; the right
    columns mirror the left. An empty/whitespace key still resolves (to the digest of ""), so a
    nameless object never crashes the view — it just shares the "unknown" face.
    """
    if grid < 1:
        raise ValueError("grid must be >= 1")
    digest = _digest(key)
    half = (grid + 1) // 2  # columns we actually decide; the rest mirror
    bits: list[bool] = []
    # One bit per decided cell from the digest bytes (2 bytes reserved above for the colour).
    for i in range(grid * half):
        byte = digest[2 + (i % (len(digest) - 2))]
        bits.append(bool((byte >> (i % 8)) & 1))
    rows: list[tuple[bool, ...]] = []
    for r in range(grid):
        left = [bits[r * half + c] for c in range(half)]
        full = [left[c] if c < half else left[grid - 1 - c] for c in range(grid)]
        rows.append(tuple(full))
    return Identicon(key=key, grid=grid, cells=tuple(rows), color=_color_from(digest))
