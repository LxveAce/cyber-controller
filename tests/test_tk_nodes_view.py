"""tk Nodes view (W1.1) — the Tkinter mirror of the Qt nodes_tab. Runs headless on a hidden Tk root.

Same guarantees asserted as test_nodes_tab.py: gate-locked shows the notice (not the table) and fails CLOSED;
unlocked populates a KEY-FREE table (the key hex appears nowhere); actions delegate to the controller.
"""
from __future__ import annotations

import copy

import pytest

from src.core import node_provision
from src.core.device_manager import DeviceManager
from src.core.nodes_controller import NodesController
from src.core.serial_handler import ConnectionState
from src.models.device import Device

FAKE_KEY = bytes(range(32))


@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - only on a truly headless CI with no Tk
        pytest.skip("no display for tkinter")
    root.withdraw()
    yield root
    root.destroy()


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


def _view(tk_root, vault=None, *, locked=False, provision=None):
    from src.ui.tk.nodes_view import NodesView

    v = vault if vault is not None else FakeVault()
    if provision:
        for nid, kw in provision.items():
            node_provision.provision_node(v, nid, **kw)
    getter = _locked if locked else (lambda: v)
    ctrl = NodesController(DeviceManager(), vault_getter=getter)
    return NodesView(tk_root, ctrl), ctrl, v


def _all_cell_text(view) -> str:
    out = []
    for rid in view._tree.get_children():
        out.extend(str(x) for x in view._tree.item(rid, "values"))
    return "\n".join(out)


def _row_for(view, node_id):
    for rid in view._tree.get_children():
        if view._tree.item(rid, "values")[0] == str(node_id):
            return rid
    return None


def test_locked_gate_shows_notice_not_table(tk_root):
    view, _c, _v = _view(tk_root, locked=True)
    assert view._locked_label.winfo_manager() == "pack"   # notice shown
    assert view._tree.winfo_manager() == ""               # table hidden (not managed)
    assert view._tree.get_children() == ()
    assert all(b.instate(["disabled"]) for b in view._buttons)


def test_unlocked_populates_key_free_table(tk_root):
    view, _c, _v = _view(tk_root, provision={
        2: {"key": FAKE_KEY, "role": "node", "label": "scanner"},
        1: {"key": FAKE_KEY, "role": "host", "label": "pager"},
    })
    assert view._tree.winfo_manager() == "pack"
    assert view._locked_label.winfo_manager() == ""
    rows = view._tree.get_children()
    assert len(rows) == 2
    first = view._tree.item(rows[0], "values")            # sorted by node id
    assert first[0] == "1" and first[1] == "pager" and first[2] == "host"
    assert first[5] == "no" and first[6] == "no"          # connected / attached
    assert FAKE_KEY.hex() not in _all_cell_text(view)     # THE key-free guarantee


def test_do_provision_rotate_deprovision(tk_root):
    view, _c, v = _view(tk_root)
    view._do_provision(5, "host", "relay")
    assert len(view._tree.get_children()) == 1
    assert view._tree.item(view._tree.get_children()[0], "values")[0] == "5"
    k1 = v.get("node_keys")["5"]["key"]
    view._do_rotate(5)
    assert v.get("node_keys")["5"]["key"] != k1
    view._do_deprovision(5)
    assert view._tree.get_children() == ()
    assert FAKE_KEY.hex() not in _all_cell_text(view)


def test_do_attach_detach_via_gateway(tk_root):
    from src.ui.tk.nodes_view import NodesView

    v = FakeVault()
    node_provision.provision_node(v, 3, key=FAKE_KEY, role="host")
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    view = NodesView(tk_root, ctrl)
    gw = MockGateway("COM7")
    gw.connect()
    ctrl._dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)

    view._do_attach(3, "COM7")
    rid = _row_for(view, 3)
    assert rid is not None and view._tree.item(rid, "values")[6] == "yes"   # attached
    assert FAKE_KEY.hex() not in _all_cell_text(view)

    view._do_detach(3)
    rid = _row_for(view, 3)
    assert view._tree.item(rid, "values")[6] == "no"


def test_attach_via_bad_gateway_raises_key_free(tk_root):
    view, _c, _v = _view(tk_root, provision={5: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    with pytest.raises(ValueError):
        view._do_attach(5, "node:5")       # a node link can't be a gateway
    assert FAKE_KEY.hex() not in _all_cell_text(view)


def test_refresh_fails_closed_on_read_race(tk_root):
    """Gate passes is_unlocked() then relocks before list_rows(): must fail closed AND disable buttons."""
    from src.ui.tk.nodes_view import NodesView

    v = FakeVault()
    node_provision.provision_node(v, 4, key=FAKE_KEY, role="host")
    calls = {"n": 0}

    def racy():
        calls["n"] += 1
        if calls["n"] == 1:
            return v
        raise node_provision.VaultLockedError("relocked")

    view = NodesView(tk_root, NodesController(DeviceManager(), vault_getter=racy))
    assert view._tree.winfo_manager() == ""
    assert view._locked_label.winfo_manager() == "pack"
    assert view._tree.get_children() == ()
    assert all(b.instate(["disabled"]) for b in view._buttons)


def test_periodic_tick_recovers_locked_to_unlocked(tk_root):
    """FINDING A regression: a view BUILT while the gate is locked hides/disables its own Refresh button, so
    the ONLY thing that can rescue it is the periodic timer (_tick). Prove it recovers both ways on its own."""
    from src.ui.tk.nodes_view import NodesView

    v = FakeVault()
    node_provision.provision_node(v, 7, key=FAKE_KEY, role="host", label="relay")
    state = {"locked": True}

    def getter():
        if state["locked"]:
            raise node_provision.VaultLockedError("locked")
        return v

    view = NodesView(tk_root, NodesController(DeviceManager(), vault_getter=getter))
    assert view._tree.winfo_manager() == "" and view._after_id is not None   # built locked, timer armed
    assert all(b.instate(["disabled"]) for b in view._buttons)

    state["locked"] = False           # gate opens; the timer fires -> view recovers with NO manual action
    view._tick()
    assert view._tree.winfo_manager() == "pack"
    assert _row_for(view, 7) is not None
    assert not any(b.instate(["disabled"]) for b in view._buttons)
    assert FAKE_KEY.hex() not in _all_cell_text(view)

    state["locked"] = True            # relock; the next tick fails closed again
    view._tick()
    assert view._tree.winfo_manager() == "" and view._tree.get_children() == ()
    assert all(b.instate(["disabled"]) for b in view._buttons)


def test_selected_node_id_reads_first_column(tk_root):
    view, _c, _v = _view(tk_root, provision={9: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    assert view._selected_node_id() is None                       # nothing selected -> None
    view._tree.selection_set(view._tree.get_children()[0])
    assert view._selected_node_id() == 9


def test_provision_error_is_key_free(tk_root):
    view, _c, _v = _view(tk_root, provision={5: {"key": FAKE_KEY, "role": "host", "label": "x"}})
    with pytest.raises(Exception) as ei:                          # duplicate id -> controller raises
        view._do_provision(5, "host", "dup")
    assert FAKE_KEY.hex() not in str(ei.value)                    # the message shown via messagebox is key-free
