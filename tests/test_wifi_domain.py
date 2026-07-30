"""Wi-Fi DOMAIN DETAIL three-panel view (src/ui/qt/wifi_domain.py).

Asserts the CLAIM, offscreen: the right detail collapses on a cramped canvas and reveals on
selection, and selecting an AP populates the detail with its real, OUI-resolved, graded fields.
(The old passive/active posture panel was authorization theater — deleted in Spade v2 P2c.)
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from src.core.oui import lookup_vendor  # noqa: E402
from src.core.wifi_analyzer import AccessPoint  # noqa: E402
from src.ui.qt.wifi_domain import APDetailPanel, WifiDomainView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _ap(bssid="00:00:01:11:22:33", ssid="Lab", enc="WPA2", rssi=-55, channel=6):
    return AccessPoint(bssid=bssid, ssid=ssid, encryption=enc, rssi=rssi, channel=channel)


def _view(qapp):
    return WifiDomainView(center=QWidget())


def test_detail_collapses_when_cramped_and_reveals_on_selection(qapp):
    v = _view(qapp)
    v.resize(1600, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.columns() == 3 and v.detail_visible() is True     # expanded: all three panels
    v.resize(420, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.columns() == 1 and v.detail_visible() is False  # cramped + nothing picked: collapsed
    v.select_object(_ap())
    assert v.detail_visible() is True                           # a selection reveals the detail


def test_panels_stack_vertically_on_compact(qapp):
    from PyQt5.QtWidgets import QBoxLayout
    v = _view(qapp)
    v.resize(1600, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.is_stacked() is False
    assert v._row.direction() == QBoxLayout.LeftToRight   # expanded: three panels side by side
    v.resize(420, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.is_stacked() is True
    assert v._row.direction() == QBoxLayout.TopToBottom   # compact: panels stacked vertically


def test_selection_populates_the_detail(qapp):
    v = _view(qapp)
    got = []
    v.object_selected.connect(got.append)
    ap = _ap(ssid="HomeNet")
    v.select_object(ap)
    assert got == [ap]
    assert v.detail.ap is ap
    assert v.detail._title.text() == "HomeNet"


def test_ap_detail_panel_shows_resolved_and_graded_fields(qapp):
    panel = APDetailPanel()
    ap = _ap(bssid="00:00:01:aa:bb:cc", ssid="Net", enc="WEP")
    panel.set_ap(ap)
    # Vendor is the real OUI resolution (00:00:01 is a registered prefix), not a stub.
    assert panel._rows["Vendor"].text() == lookup_vendor("00:00:01:aa:bb:cc")
    assert panel._rows["Vendor"].text()  # non-empty: really resolved
    assert panel._rows["BSSID"].text() == "00:00:01:aa:bb:cc"
    # WEP grades "weak" — the security line reflects the honest grade.
    assert "Weak" in panel._rows["Security"].text()
    panel.clear()
    assert panel.ap is None and panel._rows["Vendor"].text() == "—"


def test_ap_detail_shows_the_persistent_identicon(qapp):
    # Card identity (GUI rebuild): a selected AP shows a non-null identicon keyed on its BSSID; a
    # BSSID-less AP shows none (no stable identity, no fabricated face). The face is deterministic.
    from src.ui.qt.identicon_pixmap import identicon_pixmap
    panel = APDetailPanel()
    ap = _ap(bssid="00:00:01:aa:bb:cc")
    panel.set_object(ap)
    pm = panel._identicon.pixmap()
    assert pm is not None and not pm.isNull()
    # Deterministic: the panel's face matches the pure render for the same BSSID.
    assert pm.toImage() == identicon_pixmap("00:00:01:aa:bb:cc").toImage()

    panel.set_object(_ap(bssid=""))
    assert panel._identicon.pixmap() is None or panel._identicon.pixmap().isNull()

    panel.set_object(ap)
    panel.clear()
    assert panel._identicon.pixmap() is None or panel._identicon.pixmap().isNull()
