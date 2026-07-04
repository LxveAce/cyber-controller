"""Tests for NodesController (W1.1) — the UI-agnostic surface the Nodes tab binds to.

Covers: gate-gating (fail closed when locked), key-free rows, provision/rotate/deprovision delegation,
attach presenting a node as a managed DeviceManager device (with live connected/attached state), the
crash-safe epoch reservation flowing through, refuse-double-attach, and detach persisting the replay head
+ tearing down cleanly. Uses the REAL DeviceManager so attach_connection is genuinely exercised.
All keys are OBVIOUSLY FAKE.
"""
from __future__ import annotations

import copy

import pytest

from src.core import node_provision
from src.core.device_manager import DeviceManager
from src.core.node_link import NodeLink
from src.core.nodes_controller import DEFAULT_MASKS, AlreadyAttachedError, NodesController
from src.core.serial_handler import ConnectionState

FAKE_KEY = bytes(range(32))


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
        for cb in list(self._state_cbs):
            cb(self._state)

    def write(self, data):
        self.sent.append(data)

    def deliver(self, line):
        for cb in list(self._line_cbs):
            cb(line)


def _locked():
    raise node_provision.VaultLockedError("gate locked")


def _ctrl(vault=None):
    v = vault if vault is not None else FakeVault()
    dm = DeviceManager()
    return NodesController(dm, vault_getter=lambda: v), v, dm


# ── gate gating ──────────────────────────────────────────────────────
def test_is_unlocked_reflects_gate():
    ok, _, dm = _ctrl()
    assert ok.is_unlocked() is True
    locked = NodesController(dm, vault_getter=_locked)
    assert locked.is_unlocked() is False


def test_every_op_fails_closed_when_locked():
    dm = DeviceManager()
    c = NodesController(dm, vault_getter=_locked)
    with pytest.raises(node_provision.VaultLockedError):
        c.list_rows()
    with pytest.raises(node_provision.VaultLockedError):
        c.provision(1)
    with pytest.raises(node_provision.VaultLockedError):
        c.rotate(1)
    with pytest.raises(node_provision.VaultLockedError):
        c.attach(1, MockGateway())


# ── read: key-free rows ──────────────────────────────────────────────
def test_list_rows_is_key_free_and_reflects_state():
    c, v, dm = _ctrl()
    c.provision(2, role="node", label="scanner")
    c.provision(1, role="host", label="pager")
    rows = c.list_rows()
    assert [r["node_id"] for r in rows] == [1, 2]           # sorted
    assert all("key" not in r for r in rows)                # NEVER a key
    r1 = rows[0]
    assert r1["label"] == "pager" and r1["role"] == "host"
    assert r1["port"] == "node:1" and r1["connected"] is False and r1["attached"] is False


def test_masks_catalogue():
    c, _, _ = _ctrl()
    assert c.masks() == list(DEFAULT_MASKS)


# ── provisioning delegation ──────────────────────────────────────────
def test_provision_rotate_deprovision():
    c, v, dm = _ctrl()
    summ = c.provision(5, label="relay")
    assert "key" not in summ and summ["node_id"] == 5
    k1 = v.get("node_keys")["5"]["key"]
    c.rotate(5)
    assert v.get("node_keys")["5"]["key"] != k1              # rotated
    assert c.deprovision(5) is True
    assert c.list_rows() == []


# ── attach presents a node as a managed device ───────────────────────
def test_attach_registers_device_and_tracks_state():
    c, v, dm = _ctrl()
    c.provision(7, role="host", label="pager")
    gw = MockGateway()
    link = c.attach(7, gw)
    assert isinstance(link, NodeLink) and link.port == "node:7"
    assert dm.get_device("node:7") is not None               # registered as a managed device
    assert c.attached_ids() == [7]
    assert c.list_rows()[0]["attached"] is True

    gw.connect()                                             # connection state flows through
    assert dm.get_device("node:7").connected is True
    assert c.list_rows()[0]["connected"] is True


def test_attach_reserves_epoch_crash_safe():
    c, v, dm = _ctrl()
    c.provision(3, role="host")
    assert v.get("node_keys")["3"]["tx_epoch"] == 0
    c.attach(3, MockGateway())
    assert v.get("node_keys")["3"]["tx_epoch"] == 1          # reserved before the link was returned


def test_refuse_double_attach():
    c, v, dm = _ctrl()
    c.provision(4, role="host")
    c.attach(4, MockGateway())
    with pytest.raises(AlreadyAttachedError):
        c.attach(4, MockGateway())


# ── detach persists replay head + tears down ─────────────────────────
def test_detach_persists_rx_and_unregisters():
    c, v, dm = _ctrl()
    # provision with a known key so a peer (same key, opposite role) can talk to it
    node_provision.provision_node(v, 8, key=FAKE_KEY, role="host")
    gw = MockGateway()
    host = c.attach(8, gw)
    host.connect()
    got = []
    host.on_line(got.append)

    # a peer node (same key, opposite role) sends a frame so the receiver window advances
    peer_gw = MockGateway("peer")
    peer = NodeLink(peer_gw, FAKE_KEY, 8, role="node")
    peer.connect()
    peer.write("hello")
    for line in peer_gw.sent:
        gw.deliver(line)
    assert got == ["hello"]                                  # attached node behaves like a serial device

    rx_epoch, rx_highest = host.rx_epoch, host.rx_highest
    assert c.detach(8) is True
    rec = v.get("node_keys")["8"]
    assert rec["rx_epoch"] == rx_epoch and rec["rx_highest"] == rx_highest  # replay head persisted
    assert dm.get_device("node:8") is None                   # unregistered
    assert c.attached_ids() == []
    assert c.detach(8) is False                              # idempotent


def test_available_gateways_excludes_nodes_and_disconnected():
    from src.models.device import Device

    c, v, dm = _ctrl()
    gw = MockGateway("COM7")
    gw.connect()
    dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)   # real dongle, connected
    dm.attach_connection(Device(port="COM8", name="Off"), MockGateway("COM8"))         # gateway not connected
    c.provision(1, role="host")
    c.attach(1, MockGateway("gwX"))                                                    # registers a node:1 device
    ports = {g["port"] for g in c.available_gateways()}
    assert "COM7" in ports          # connected non-node with a live connection
    assert "COM8" not in ports      # disconnected
    assert "node:1" not in ports    # a node link can't be a gateway (no self-attach loop)
    assert all("key" not in g for g in c.available_gateways())


def test_attach_via_port_guards_and_delegates():
    from src.models.device import Device

    c, v, dm = _ctrl()
    gw = MockGateway("COM7")
    gw.connect()
    dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)
    node_provision.provision_node(v, 5, key=FAKE_KEY, role="host")
    link = c.attach_via_port(5, "COM7")
    assert link.port == "node:5" and 5 in c.attached_ids()
    with pytest.raises(ValueError):
        c.attach_via_port(6, "node:5")     # a node link can't be a gateway
    c.provision(7, role="host")
    with pytest.raises(ValueError):
        c.attach_via_port(7, "COM404")     # no live connection on that port


def test_custom_port_node_link_cannot_be_a_gateway():
    """Identity guard, not naming: a NodeLink attached under a non-'node:' custom port must NOT be offered
    as a gateway and must be refused when passed as one — closing the self-attach-loop bypass."""
    c, v, dm = _ctrl()
    node_provision.provision_node(v, 1, key=FAKE_KEY, role="host")
    gwA = MockGateway("gwA")
    gwA.connect()
    c.attach(1, gwA, port="custom:1")                          # node link registered under a non-node: port
    assert all(g["port"] != "custom:1" for g in c.available_gateways())   # excluded by the isinstance check
    node_provision.provision_node(v, 2, key=FAKE_KEY, role="host")
    with pytest.raises(ValueError):
        c.attach_via_port(2, "custom:1")                       # resolves to a NodeLink -> refused
    with pytest.raises(ValueError):
        c.attach(2, c._links[1])                               # passing the NodeLink object is refused at the root


def test_attach_via_port_fails_closed_when_locked():
    from src.models.device import Device

    dm = DeviceManager()
    gw = MockGateway("COM7")
    gw.connect()
    dm.attach_connection(Device(port="COM7", name="dongle", connected=True), gw)
    c = NodesController(dm, vault_getter=_locked)
    with pytest.raises(node_provision.VaultLockedError):
        c.attach_via_port(5, "COM7")


def test_deprovision_detaches_first():
    c, v, dm = _ctrl()
    c.provision(9, role="host")
    c.attach(9, MockGateway())
    assert c.deprovision(9) is True
    assert dm.get_device("node:9") is None
    assert node_provision.is_provisioned(v, 9) is False


# ── exception-safe teardown (DEBUG finding 1) ────────────────────────
def test_detach_completes_teardown_when_persist_fails():
    """If persist_rx_state raises a non-VaultLockedError (e.g. another process deprovisioned the node
    mid-session), detach must STILL close the link + unregister the device — never strand a live link."""
    c, v, dm = _ctrl()
    node_provision.provision_node(v, 7, key=FAKE_KEY, role="host")
    gw = MockGateway()
    c.attach(7, gw)
    assert len(gw._line_cbs) == 1                       # link is decoding inbound frames
    node_provision.deprovision_node(v, 7)              # another process removes the record
    assert c.detach(7) is True                          # persist raises internally -> swallowed
    assert dm.get_device("node:7") is None              # device unregistered anyway
    assert gw._line_cbs == []                           # link detached from the gateway (stops decrypting)
    assert c.attached_ids() == []


def test_detach_while_locked_still_tears_down():
    c, v, dm = _ctrl()
    node_provision.provision_node(v, 6, key=FAKE_KEY, role="host")
    gw = MockGateway()
    c.attach(6, gw)
    c._vault_getter = _locked                           # gate locks mid-session
    assert c.detach(6) is True                          # persist skipped, teardown still runs
    assert dm.get_device("node:6") is None
    assert gw._line_cbs == []


def test_deprovision_while_attached_survives_persist_failure():
    c, v, dm = _ctrl()
    node_provision.provision_node(v, 9, key=FAKE_KEY, role="host")
    c.attach(9, MockGateway())
    node_provision.deprovision_node(v, 9)              # record vanishes (another process)
    assert c.deprovision(9) is False                    # already gone from vault, but no exception
    assert dm.get_device("node:9") is None              # no phantom device left behind
    assert c.attached_ids() == []


def test_reattach_after_detach_advances_epoch():
    c, v, dm = _ctrl()
    c.provision(3, role="host")
    l1 = c.attach(3, MockGateway())
    e1 = l1.tx_epoch
    c.detach(3)
    l2 = c.attach(3, MockGateway())
    assert l2.tx_epoch == e1 + 1                         # fresh epoch — no reuse across attach cycles


def test_list_rows_reads_live_link_port(monkeypatch):
    c, v, dm = _ctrl()
    c.provision(5, role="host")
    gw = MockGateway()
    c.attach(5, gw, port="custom:5")                    # a caller overrides the port
    gw.connect()
    row = c.list_rows()[0]
    assert row["port"] == "custom:5" and row["attached"] is True and row["connected"] is True
