"""Wi-Fi DOMAIN DETAIL three-panel view (src/ui/qt/wifi_domain.py).

Asserts the CLAIM, offscreen: the passive/active split is a real boundary (Active is never one tap —
it requires authorization), the right detail collapses on a cramped canvas and reveals on selection,
and selecting an AP populates the detail with its real, OUI-resolved, honestly-graded fields.
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


def test_default_posture_is_passive(qapp):
    assert _view(qapp).posture() == WifiDomainView.POSTURE_PASSIVE


def test_active_is_a_boundary_not_one_tap(qapp):
    v = _view(qapp)
    asked = []
    v.active_authorization_requested.connect(lambda: asked.append(True))
    v.request_active()
    # The tap did NOT enter Active — it asked for authorization (the real boundary).
    assert v.posture() == WifiDomainView.POSTURE_PASSIVE
    assert asked == [True]
    # Only an explicit host confirm (after the safety gate) enters Active.
    v.confirm_active()
    assert v.posture() == WifiDomainView.POSTURE_ACTIVE
    v.set_passive()
    assert v.posture() == WifiDomainView.POSTURE_PASSIVE


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
