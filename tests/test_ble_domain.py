"""BLE DOMAIN DETAIL (src/ui/qt/ble_domain.py) — proves the shared three-panel frame REUSES.

BLE gets the exact same DomainDetailView frame as WiFi (posture boundary + responsive reflow),
differing only in the right detail panel. Asserts the frame reuse + BLE detail fields, offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from src.core.ble_analyzer import BleDevice  # noqa: E402
from src.ui.qt.ble_domain import BleDetailPanel, BleDomainView  # noqa: E402
from src.ui.qt.domain_view import DomainDetailView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _dev(addr="aa:bb:cc:dd:ee:ff", name="Fitbit"):
    return BleDevice(addr=addr, name=name, company_name="Fitbit, Inc.", addr_type="random",
                     appearance_name="Heart Rate Sensor", rssi=-60, tracker=True)


def test_ble_domain_reuses_the_shared_frame(qapp):
    v = BleDomainView(center=QWidget())
    assert isinstance(v, DomainDetailView)   # same frame, not a WiFi one-off
    # responsive reflow (inherited): side-by-side expanded, stacked + detail-collapsed compact.
    v.resize(1600, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.detail_visible() is True and v.is_stacked() is False
    v.resize(420, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.is_stacked() is True and v.detail_visible() is False


def test_ble_detail_panel_shows_device_fields(qapp):
    panel = BleDetailPanel()
    panel.set_object(_dev())
    assert panel._title.text() == "Fitbit"
    assert panel._rows["Address"].text() == "aa:bb:cc:dd:ee:ff"
    assert panel._rows["Vendor"].text() == "Fitbit, Inc."
    assert panel._rows["Appearance"].text() == "Heart Rate Sensor"
    assert "Yes" in panel._rows["Tracker"].text()   # a tracker is honestly flagged
    panel.clear()
    assert panel.device is None and panel._rows["Address"].text() == "—"


def test_selecting_a_device_populates_the_detail(qapp):
    v = BleDomainView(center=QWidget())
    got = []
    v.object_selected.connect(got.append)
    dev = _dev(name="Tile")
    v.select_object(dev)
    assert got == [dev]
    assert v.detail.device is dev
    assert v.detail._title.text() == "Tile"
