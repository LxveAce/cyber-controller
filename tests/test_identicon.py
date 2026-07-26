"""Pure identicon core (src/core/identicon.py) — deterministic, symmetric, notation-insensitive.

No Qt: this exercises the pattern/colour logic that the GUI's card-identity avatar renders. The
whole point of an identicon is that a device keeps the SAME face everywhere and across sessions, so
determinism + symmetry are the load-bearing properties asserted here.
"""
from __future__ import annotations

import pytest

from src.core.identicon import Identicon, identicon


def test_is_deterministic():
    a = identicon("aa:bb:cc:dd:ee:ff")
    b = identicon("aa:bb:cc:dd:ee:ff")
    assert a == b                      # same key -> byte-identical identicon (frozen dataclass eq)
    assert isinstance(a, Identicon)


def test_notation_insensitive_same_face():
    # The same MAC in different notations is the SAME device -> the SAME FACE (cells + colour), even
    # though each identicon keeps its own original key string for provenance. A device keeps its
    # identity whether shown as AA:BB:.. or aabb.. or with dashes.
    def face(k):
        ic = identicon(k)
        return (ic.cells, ic.color)
    base = face("AA:BB:CC:DD:EE:FF")
    assert face("aabbccddeeff") == base
    assert face("aa-bb-cc-dd-ee-ff") == base
    assert face("  AA:BB:CC:DD:EE:FF  ") == base


def test_grid_is_left_right_symmetric():
    for key in ("aa:bb:cc:dd:ee:ff", "node-1234", "", "11:22:33:44:55:66"):
        ic = identicon(key, grid=5)
        for row in ic.cells:
            assert len(row) == 5
            for c in range(5):
                assert row[c] == row[5 - 1 - c], f"row not mirrored for {key!r}: {row}"
        assert len(ic.cells) == 5


def test_color_is_legible_band_not_near_black_or_white():
    # Colour is pinned to a bright-but-not-neon band so it reads on a dark UI: never near-black,
    # never washed-out. Check a spread of keys stay inside a sane brightness range.
    for key in ("aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01", "meshnode", "1581F5XYZ", "x"):
        r, g, b = identicon(key).color
        assert all(0 <= v <= 255 for v in (r, g, b))
        brightness = (r + g + b) / 3
        assert 60 < brightness < 235, f"{key!r} colour {r,g,b} out of legible band"


def test_different_keys_generally_differ():
    faces = {(ic.cells, ic.color) for ic in
             (identicon(f"aa:bb:cc:dd:ee:{i:02x}") for i in range(32))}
    assert len(faces) >= 30   # allow a rare collision, but they must not collapse to a handful


def test_empty_key_resolves_not_raises():
    ic = identicon("")            # a nameless object must still get a (shared "unknown") face
    assert isinstance(ic, Identicon) and len(ic.cells) == 5


def test_grid_size_respected_and_validated():
    assert len(identicon("x", grid=7).cells) == 7
    assert all(len(r) == 7 for r in identicon("x", grid=7).cells)
    with pytest.raises(ValueError):
        identicon("x", grid=0)
