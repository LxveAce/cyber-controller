"""RF/Sub-GHz DOMAIN DETAIL (src/ui/qt/subghz_domain.py) — the 4th domain on the shared frame.

Asserts the frame reuse (posture boundary + reflow) and the SubGHz detail fields (protocol/code/
address/data/bits + optional rssi/frequency), offscreen. RX/awareness-only.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from src.core.subghz_ook import SubGhzSignal  # noqa: E402
from src.ui.qt.domain_view import DomainDetailView  # noqa: E402
from src.ui.qt.subghz_domain import SubGhzDetailPanel, SubGhzDomainView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _sig(code=0xA5C3E1, rssi=-72, freq=433.92):
    return SubGhzSignal(protocol="ev1527", code=code, address=code >> 4, data=code & 0xF, bits=24,
                        rssi=rssi, frequency_mhz=freq, first_seen="t")


def test_subghz_domain_reuses_the_shared_frame(qapp):
    v = SubGhzDomainView(center=QWidget())
    assert isinstance(v, DomainDetailView)   # same frame, not a one-off
    asked = []
    v.active_authorization_requested.connect(lambda: asked.append(True))
    v.request_active()
    assert v.posture() == DomainDetailView.POSTURE_PASSIVE and asked == [True]
    v.confirm_active()
    assert v.posture() == DomainDetailView.POSTURE_ACTIVE
    v.resize(1600, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.detail_visible() is True and v.is_stacked() is False
    v.resize(420, 900)
    qapp.processEvents()
    v._relayout_for_size()
    assert v.is_stacked() is True


def test_subghz_detail_panel_shows_decoded_fields(qapp):
    panel = SubGhzDetailPanel()
    panel.set_object(_sig())
    assert panel._title.text() == "EV1527"
    assert panel._rows["Protocol"].text() == "ev1527"
    assert panel._rows["Code"].text() == "0xA5C3E1"
    assert panel._rows["Address"].text() == "0xA5C3E"
    assert panel._rows["Data"].text() == "0x1"
    assert panel._rows["Bits"].text() == "24"
    assert panel._rows["Signal"].text() == "-72 dBm"
    assert panel._rows["Frequency"].text() == "433.92 MHz"
    panel.clear()
    assert panel.signal is None and panel._rows["Code"].text() == "—"


def test_subghz_detail_optional_fields_show_dash_when_absent(qapp):
    panel = SubGhzDetailPanel()
    panel.set_object(_sig(rssi=None, freq=None))
    assert panel._rows["Signal"].text() == "—" and panel._rows["Frequency"].text() == "—"


def test_selecting_a_signal_populates_the_detail(qapp):
    v = SubGhzDomainView(center=QWidget())
    got = []
    v.object_selected.connect(got.append)
    sig = _sig()
    v.select_object(sig)
    assert got == [sig]
    assert v.detail.signal is sig
