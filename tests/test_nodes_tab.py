"""Nodes tab (W1.1b) — gate-locked empty state, key-free table population, and delegated actions. Offscreen."""
from __future__ import annotations

import copy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core import node_provision  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.nodes_controller import NodesController  # noqa: E402
from src.core.serial_handler import ConnectionState  # noqa: E402
from src.models.device import Device  # noqa: E402

FAKE_KEY = bytes(range(32))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeVault:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return copy.deepcopy(self._d.get(key, default))

    def set(self, key, value):
        self._d[key] = copy.deepcopy(value)


class MockGateway:
    def __init__(self, port="gw"):
        self.port = port
        self._state = ConnectionState.DISCONNECTED
        self._line_cbs, self._state_cbs = [], []
        self.sent = []

    @property
    def is_connected(self):
        return self._state == ConnectionState.CONNECTED

    @property
    def state(self):
        return self._state

    def on_line(self, cb):
        self._line_cbs.append(cb)

    def remove_line_callback(self, cb):
        try:
            self._line_cbs.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb):
        self._state_cbs.append(cb)

    def connect(self):
        self._state = ConnectionState.CONNECTED
        for cb in list(self._state_cbs):
            cb(self._state)

    def disconnect(self):
        self._state = ConnectionState.DISCONNECTED

    def write(self, data):
        self.sent.append(data)


def _locked():
    raise node_provision.VaultLockedError("locked")


def _row_for(tab, node_id):
    for r in range(tab._table.rowCount()):
        it = tab._table.item(r, 0)
        if it is not None and it.text() == str(node_id):
            return r
    return None


def _tab(vault=None, *, locked=False, provision=None):
    from src.ui.qt.nodes_tab import NodesTab

    v = vault if vault is not None else FakeVault()
    if provision:
        for nid, kw in provision.items():
            node_provision.provision_node(v, nid, **kw)
    getter = _locked if locked else (lambda: v)
    ctrl = NodesController(DeviceManager(), vault_getter=getter)
    return NodesTab(controller=ctrl), ctrl, v


def _all_cell_text(tab) -> str:
    out = []
    for r in range(tab._table.rowCount()):
        for c in range(tab._table.columnCount()):
            it = tab._table.item(r, c)
            if it is not None:
                out.append(it.text())
    return "\n".join(out)


def test_locked_gate_shows_notice_not_table(qapp):
    tab, _c, _v = _tab(locked=True)
    # isHidden() reflects the explicit setVisible flag (isVisible() needs a shown top-level window).
    assert tab._locked_label.isHidden() is False
    assert tab._table.isHidden() is True
    assert tab._table.rowCount() == 0
    assert all(not b.isEnabled() for b in tab._buttons)


def test_unlocked_populates_key_free_table(qapp):
    tab, _c, _v = _tab(provision={
        2: {"key": FAKE_KEY, "role": "node", "label": "scanner"},
        1: {"key": FAKE_KEY, "role": "host", "label": "pager"},
    })
    assert tab._table.isHidden() is False
    assert tab._locked_label.isHidden() is True
    assert tab._table.rowCount() == 2
    # sorted by node id; columns are Node/Label/Role/TX/RX/Connected/Attached
    assert tab._table.item(0, 0).text() == "1"
    assert tab._table.item(0, 1).text() == "pager"
    assert tab._table.item(0, 2).text() == "host"
    assert tab._table.item(0, 5).text() == "no"   # not connected
    assert tab._table.item(0, 6).text() == "no"   # not attached
    # THE key-free guarantee: the key hex must appear nowhere in the table
    assert FAKE_KEY.hex() not in _all_cell_text(tab)


def test_do_provision_rotate_deprovision(qapp):
    tab, _c, v = _tab()
    tab._do_provision(5, "host", "relay")
    assert tab._table.rowCount() == 1 and tab._table.item(0, 0).text() == "5"
    k1 = v.get("node_keys")["5"]["key"]
    tab._do_rotate(5)
    assert v.get("node_keys")["5"]["key"] != k1
    tab._do_deprovision(5)
    assert tab._table.rowCount() == 0
    assert FAKE_KEY.hex() not in _all_cell_text(tab)


def test_selected_node_id_reads_first_column(qapp):
    tab, _c, _v = _tab(provision={7: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    tab._table.setCurrentCell(0, 0)
    assert tab._selected_node_id() == 7


def test_refresh_reflects_gate_relock(qapp):
    v = FakeVault()
    node_provision.provision_node(v, 3, key=FAKE_KEY, role="host")
    unlocked = {"v": True}
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v if unlocked["v"] else _locked())
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab(controller=ctrl)
    assert not tab._table.isHidden() and tab._table.rowCount() == 1
    unlocked["v"] = False          # gate re-locks
    tab._refresh()
    assert tab._table.isHidden() is True and tab._locked_label.isHidden() is False


def test_refresh_fails_closed_on_read_race(qapp):
    """Gate passes is_unlocked() then relocks before list_rows(): must fail closed AND disable buttons."""
    from src.ui.qt.nodes_tab import NodesTab

    v = FakeVault()
    node_provision.provision_node(v, 4, key=FAKE_KEY, role="host")
    calls = {"n": 0}

    def racy():
        calls["n"] += 1
        if calls["n"] == 1:                # is_unlocked() sees it open...
            return v
        raise node_provision.VaultLockedError("relocked")  # ...list_rows() then finds it locked

    tab = NodesTab(controller=NodesController(DeviceManager(), vault_getter=racy))
    assert tab._table.isHidden() is True
    assert tab._locked_label.isHidden() is False
    assert tab._table.rowCount() == 0
    assert all(not b.isEnabled() for b in tab._buttons)   # buttons disabled on the race path too


def test_do_attach_detach_via_gateway(qapp):
    from src.ui.qt.nodes_tab import NodesTab

    v = FakeVault()
    node_provision.provision_node(v, 3, key=FAKE_KEY, role="host")
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    tab = NodesTab(controller=ctrl)
    # a connected gateway dongle registered in DeviceManager
    gw = MockGateway("COM7")
    gw.connect()
    ctrl._dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)

    tab._do_attach(3, "COM7")
    r = _row_for(tab, 3)
    assert r is not None and tab._table.item(r, 6).text() == "yes"   # Attached column
    assert FAKE_KEY.hex() not in _all_cell_text(tab)

    tab._do_detach(3)
    r = _row_for(tab, 3)
    assert tab._table.item(r, 6).text() == "no"


def test_attach_via_bad_gateway_raises_key_free(qapp):
    tab, _c, v = _tab(provision={5: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    with pytest.raises(ValueError):
        tab._do_attach(5, "node:5")       # a node link can't be a gateway
    assert FAKE_KEY.hex() not in _all_cell_text(tab)


def test_duplicate_provision_error_is_key_free(qapp):
    tab, _c, _v = _tab(provision={5: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    with pytest.raises(node_provision.NodeExistsError) as ei:
        tab._do_provision(5, "host", "dup")
    assert FAKE_KEY.hex() not in str(ei.value)   # the error surfaced to a dialog carries no key material
