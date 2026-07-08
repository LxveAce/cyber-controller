"""Tab/window icon helper (src/ui/qt/icons.py) — the currentColor-aware SVG->QIcon loader + the tab map.

Keeps the label->icon map honest against the shipped assets/icons/*.svg so a rename can't silently blank a
tab, and proves the loader degrades to an empty (never crashing) icon on a missing/bad file.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtSvg")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.resources import resource_path  # noqa: E402
from src.ui.qt.icons import TAB_ICONS, label_icon, tab_icon  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_every_mapped_icon_svg_exists():
    # the tab->icon map must not drift from the shipped SVG set
    missing = [f"{label} -> {name}.svg"
               for label, name in TAB_ICONS.items()
               if not resource_path("assets", "icons", f"{name}.svg").exists()]
    assert not missing, f"mapped icons with no SVG on disk: {missing}"


def test_tab_icon_renders_non_null(qapp):
    for label, name in TAB_ICONS.items():
        assert not tab_icon(name).isNull(), f"{label} ({name}) rendered a null icon"


def test_label_icon_maps_known_and_falls_back(qapp):
    assert not label_icon("Connect").isNull()          # a mapped label renders
    assert label_icon("Not A Real Tab").isNull()       # an unmapped label -> empty QIcon, no crash


def test_tab_icon_missing_file_degrades_gracefully(qapp):
    assert tab_icon("definitely-not-an-icon-name").isNull()   # missing file -> empty QIcon, never raises
