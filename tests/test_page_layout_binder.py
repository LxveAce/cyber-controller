"""PageLayoutBinder (src/ui/qt/page_layout_binder.py) — wires the shell frame to live hub data.

Offscreen. Feeds a fake hub (real EventBus + stub pool/captures/dm) and asserts: badges update on
bus events, ARMED status reflects a connected device, and the posture-escalation boundary is gated —
denied by default, applied only when the host authorizes.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus  # noqa: E402
from src.ui.qt.page_layout import POSTURE_OFFENSE, POSTURE_RECON, PageLayout  # noqa: E402
from src.ui.qt.page_layout_binder import PageLayoutBinder  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _fake_hub(devices=None):
    bus = EventBus()
    return SimpleNamespace(
        bus=bus,
        pool=SimpleNamespace(count=0),
        captures=SimpleNamespace(count=0),
        dm=SimpleNamespace(list_connected=lambda: list(devices or [])),
    )


def _framed():
    p = PageLayout()
    p.add_destination("targets", "Targets")
    p.add_destination("captures", "Captures")
    return p


def test_badges_update_on_bus_events(qapp):
    hub = _fake_hub()
    p = _framed()
    PageLayoutBinder(p, hub)
    assert "(0)" not in p._destinations["targets"].text()   # 0 hides the badge
    hub.pool.count = 3
    hub.bus.publish("target.added", {})
    assert "(3)" in p._destinations["targets"].text()
    hub.captures.count = 2
    hub.bus.publish("capture.added", {})
    assert "(2)" in p._destinations["captures"].text()
    hub.pool.count = 0
    hub.bus.publish("target.cleared", {})
    assert "(0)" not in p._destinations["targets"].text()   # back to hidden


def test_armed_status_reflects_a_connected_device(qapp):
    p = _framed()
    dev = SimpleNamespace(arm_state="armed")
    hub = _fake_hub(devices=[dev])
    PageLayoutBinder(p, hub)
    assert p._status["armed"].text() == "ARMED"
    assert p._status["link"].text() == "1 device"


def test_arming_pending_status(qapp):
    p = _framed()
    hub = _fake_hub(devices=[SimpleNamespace(arm_state="pending")])
    PageLayoutBinder(p, hub)
    assert p._status["armed"].text() == "ARMING"


def test_no_devices_clears_armed_and_link(qapp):
    p = _framed()
    hub = _fake_hub(devices=[])
    PageLayoutBinder(p, hub)
    assert p._status["armed"].isHidden()   # empty -> hidden
    assert p._status["link"].isHidden()


def test_posture_escalation_denied_by_default(qapp):
    # No authorizer -> a click's escalation request is DENIED; posture never reaches Offense.
    p = _framed()
    PageLayoutBinder(p, _fake_hub())
    p._on_posture_clicked()                # emits posture_escalation_requested
    assert p.posture == POSTURE_RECON      # denied -> stays Recon


def test_posture_escalation_applied_when_host_authorizes(qapp):
    p = _framed()
    PageLayoutBinder(p, _fake_hub(), authorize_offense=lambda: True)
    p._on_posture_clicked()
    assert p.posture == POSTURE_OFFENSE     # host granted -> applied
    # a refused authorizer keeps it safe
    p2 = _framed()
    PageLayoutBinder(p2, _fake_hub(), authorize_offense=lambda: False)
    p2._on_posture_clicked()
    assert p2.posture == POSTURE_RECON


def test_binder_without_a_bus_degrades(qapp):
    # A hub missing a bus must not crash the binder (it just can't live-update).
    p = _framed()
    hub = SimpleNamespace(pool=SimpleNamespace(count=5), captures=SimpleNamespace(count=1),
                          dm=SimpleNamespace(list_connected=lambda: []))
    PageLayoutBinder(p, hub)               # no .bus -> no subscribe, refresh still runs
    assert "(5)" in p._destinations["targets"].text()
