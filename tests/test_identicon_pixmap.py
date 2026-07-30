"""Qt identicon renderer (src/ui/qt/identicon_pixmap.py) — a deterministic QPixmap for a key.

Thin view over the pure core (src/core/identicon.py, covered by test_identicon.py). This covers the
Qt paint layer DIRECTLY — the coverage that used to live on the retired domain-browser detail panel
(test_wifi_domain.py, deleted with the DomainDetailView scaffold in D6c-2). identicon_pixmap is kept
as a ready renderer for the card-identity re-wire (P4); this keeps it tested, not dead-and-untested.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtGui")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.identicon_pixmap import identicon_pixmap  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_identicon_pixmap_renders_a_sized_nonnull_face(qapp):
    pm = identicon_pixmap("00:00:01:aa:bb:cc", px=28)
    assert pm is not None and not pm.isNull()
    assert pm.width() == 28 and pm.height() == 28


def test_identicon_pixmap_is_deterministic(qapp):
    # Same key -> byte-identical face (so a MAC shows the SAME identity everywhere it appears).
    a = identicon_pixmap("aa:bb:cc:dd:ee:ff")
    b = identicon_pixmap("aa:bb:cc:dd:ee:ff")
    assert a.toImage() == b.toImage()


def test_identicon_pixmap_differs_by_key(qapp):
    # Different keys paint a different face (identity is key-derived, not a constant stamp).
    a = identicon_pixmap("00:00:01:aa:bb:cc")
    b = identicon_pixmap("de:ad:be:ef:00:11")
    assert a.toImage() != b.toImage()
