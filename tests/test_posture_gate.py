"""Master posture gate (A2): the shell's Recon/Offense posture is a REAL global offensive gate.

The posture toggle used to gate nothing. Now a Recon posture (the default) hard-blocks every
offensive verb at every send surface — BEFORE the per-command arm/confirm gate — while Offense lets
it proceed to that normal gate. This ADDS a layer over ``src.core.safety`` and never weakens it: the
gate can only ever REFUSE a command. Offscreen Qt; the posture is a process global, reset per test.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core import posture as P  # noqa: E402
from src.core import safety  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_posture():
    P.set_posture(P.POSTURE_RECON)
    yield
    P.set_posture(P.POSTURE_RECON)


# ── the pure gate ────────────────────────────────────────────────────
def test_recon_blocks_offense_offense_allows():
    assert P.offensive_blocked("lab-only", "recon") is True
    assert P.offensive_blocked("illegal-tx", "recon") is True
    assert P.offensive_blocked("lab-only", "offense") is False


def test_safe_verb_is_never_blocked():
    assert P.offensive_blocked("", "recon") is False
    assert P.offensive_blocked("", "offense") is False


def test_default_is_the_safe_posture_and_bogus_is_ignored():
    assert P.get_posture() == P.POSTURE_RECON
    P.set_posture("nonsense")
    assert P.get_posture() == P.POSTURE_RECON     # unchanged, fail-safe


def test_page_layout_constants_do_not_drift_from_core():
    from src.ui.qt.page_layout import POSTURE_OFFENSE, POSTURE_RECON
    assert (POSTURE_RECON, POSTURE_OFFENSE) == (P.POSTURE_RECON, P.POSTURE_OFFENSE)


# ── the shell binder mirrors the visible posture into the gate ────────
def test_binder_mirrors_visible_posture_into_the_gate(qapp):
    from src.ui.qt.page_layout import PageLayout
    from src.ui.qt.page_layout_binder import PageLayoutBinder
    layout = PageLayout()
    PageLayoutBinder(layout, hub=None, authorize_offense=lambda: True)
    assert P.get_posture() == P.POSTURE_RECON          # initial sync
    layout.set_posture(P.POSTURE_OFFENSE)               # emits posture_changed -> gate follows
    assert P.get_posture() == P.POSTURE_OFFENSE
    layout.set_posture(P.POSTURE_RECON)
    assert P.get_posture() == P.POSTURE_RECON


# ── the real send surface (Operate console) honors the gate ───────────
class _Conn:
    def __init__(self):
        self.writes = []

    def write(self, cmd):
        self.writes.append(cmd)


class _FakeDM:
    def __init__(self, dev, conn):
        self._dev, self._conn = dev, conn

    def list_devices(self):
        return [self._dev]

    def get_device(self, port):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port):
        return self._conn


def _armed_operate_tab():
    from src.models.device import Device
    from src.ui.qt.operate_tab import OperateTab
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "armed"                              # per-verb gate already satisfied
    conn = _Conn()
    tab = OperateTab(_FakeDM(dev, conn), dms_seen=set())
    tab._active_port = "COM23"
    tab._grid_fw = ""
    tab._refresh()
    return tab, conn


def _an_offensive_verb():
    from src.protocols import get_protocol
    for ci in get_protocol("lxveos").cached_commands():
        if safety.classify(ci.name, ci):
            return ci
    raise AssertionError("lxveos must expose at least one offensive verb")


def test_operate_send_blocks_offense_under_recon_even_when_armed(qapp):
    tab, conn = _armed_operate_tab()
    ci = _an_offensive_verb()
    P.set_posture(P.POSTURE_RECON)
    tab._send(ci.name, ci)
    assert conn.writes == []                             # blocked despite the device being ARMED


def test_operate_send_allows_offense_under_offense_posture(qapp, monkeypatch):
    # Under Offense the offensive verb clears the posture gate and reaches its per-verb gate — the
    # confirm dialog. Auto-accept it (headless can't show a modal) to prove the send proceeds.
    from PyQt5.QtWidgets import QMessageBox

    import src.ui.qt.operate_tab as OT
    monkeypatch.setattr(OT.QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    tab, conn = _armed_operate_tab()
    ci = _an_offensive_verb()
    P.set_posture(P.POSTURE_OFFENSE)
    tab._send(ci.name, ci)
    assert conn.writes == [ci.name]                      # posture cleared -> the armed verb sends


def test_operate_send_allows_safe_verb_under_recon(qapp):
    # A SAFE verb is never touched by the posture gate — recon must not block a passive command.
    from src.protocols import get_protocol
    tab, conn = _armed_operate_tab()
    cmds = get_protocol("lxveos").cached_commands()
    safe = next(ci for ci in cmds if not safety.classify(ci.name, ci))
    P.set_posture(P.POSTURE_RECON)
    tab._send(safe.name, safe)
    assert conn.writes == [safe.name]
