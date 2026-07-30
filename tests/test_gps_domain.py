"""GPS-Wardrive DOMAIN DETAIL (src/ui/qt/gps_domain.py) — the 3rd domain on the shared frame.

Asserts the frame reuse (posture boundary + reflow) and the GPS detail fields — the selected
observation's network AND its location — offscreen. Passive/awareness-only; the object model is
WardriveObservation.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from src.core.oui import lookup_vendor  # noqa: E402
from src.core.wardrive import ApObservation, GpsFix, WardriveObservation  # noqa: E402
from src.ui.qt.domain_view import DomainDetailView  # noqa: E402
from src.ui.qt.gps_domain import GpsDetailPanel, GpsDomainView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _obs(bssid="00:00:01:aa:bb:cc", ssid="LabNet", rssi=-55, ch=6, auth="[WPA2-PSK-CCMP][ESS]",
         lat=48.1173, lon=11.5167, alt=545.4, has_fix=True):
    ap = ApObservation(bssid=bssid, ssid=ssid, channel=ch, rssi=rssi, auth=auth)
    fix = GpsFix(lat=lat, lon=lon, alt=alt, has_fix=has_fix)
    return WardriveObservation(ap, fix, first_seen="2026-07-25 12:00:00")


def test_gps_domain_reuses_the_shared_frame(qapp):
    v = GpsDomainView(center=QWidget())
    assert isinstance(v, DomainDetailView)   # same frame, not a one-off
    v.resize(1600, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.detail_visible() is True and v.is_stacked() is False
    v.resize(420, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.is_stacked() is True


def test_gps_detail_panel_shows_network_and_location(qapp):
    panel = GpsDetailPanel()
    panel.set_object(_obs())
    assert panel._title.text() == "LabNet"
    assert panel._rows["BSSID"].text() == "00:00:01:aa:bb:cc"
    assert panel._rows["Vendor"].text() == lookup_vendor("00:00:01:aa:bb:cc")  # real OUI resolution
    assert "Strong" in panel._rows["Security"].text()   # WPA2 grades strong
    assert panel._rows["Signal"].text() == "-55 dBm"
    assert panel._rows["Channel"].text() == "6"
    assert panel._rows["Location"].text() == "48.117300, 11.516700"
    assert panel._rows["Altitude"].text() == "545.4 m"
    assert panel._rows["Fix"].text() == "GPS fix"
    panel.clear()
    assert panel.observation is None and panel._rows["Location"].text() == "—"


def test_no_fix_and_unknown_signal_are_honest(qapp):
    panel = GpsDetailPanel()
    panel.set_object(_obs(rssi=0, ch=0, has_fix=False))  # 0 = wardrive's unknown sentinel
    assert panel._rows["Signal"].text() == "—" and panel._rows["Channel"].text() == "—"
    assert panel._rows["Fix"].text() == "No fix"


def test_selecting_an_observation_populates_the_detail(qapp):
    v = GpsDomainView(center=QWidget())
    got = []
    v.object_selected.connect(got.append)
    obs = _obs(ssid="Home")
    v.select_object(obs)
    assert got == [obs]
    assert v.detail.observation is obs and v.detail._title.text() == "Home"
