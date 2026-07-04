"""Web Nodes view (W1.1) — Flask /nodes route + POST mutations, the web mirror of the Qt/tk nodes view.

Same guarantees, asserted server-side via a headless test_client: gate-locked renders the notice (never the
table) and the key hex is in NO response body; unlocked renders KEY-FREE rows; POST mutations are auth+CSRF
gated and delegate to the controller. Lockout state is isolated per-test via CC_GATE_CONFIG.
"""
from __future__ import annotations

import copy

import pytest

pytest.importorskip("flask")

from src.core import node_provision
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.nodes_controller import NodesController
from src.core.serial_handler import ConnectionState
from src.models.device import Device
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app

FAKE_KEY = bytes(range(32))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Isolate the persistent lockout counter to a temp file so the no-auth test can't pollute real state,
    # and pin known web creds. (Mirrors tests/test_web_persistent_lockout.py.)
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


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


def _authed_client(controller):
    """A test_client with an authenticated session + a valid CSRF token (returned for POST headers)."""
    app, _sio = create_app(controller._dm, FlashEngine(), EventBus(), TargetPool(),
                           nodes_controller=controller)
    client = app.test_client()
    token = new_csrf_token()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = token
    return client, token


def test_nodes_page_locked_shows_notice_no_key():
    ctrl = NodesController(DeviceManager(), vault_getter=_locked)   # gate locked
    client, _tok = _authed_client(ctrl)
    resp = client.get("/nodes")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Vault locked" in body            # notice rendered
    assert "nodes-tbody" not in body         # the table is NOT rendered
    assert FAKE_KEY.hex() not in body        # key-free even on the locked path


def test_nodes_page_unlocked_key_free_rows():
    v = FakeVault()
    node_provision.provision_node(v, 2, key=FAKE_KEY, role="node", label="scanner")
    node_provision.provision_node(v, 1, key=FAKE_KEY, role="host", label="pager")
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, _tok = _authed_client(ctrl)
    resp = client.get("/nodes")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "nodes-tbody" in body             # table rendered
    assert "pager" in body and "scanner" in body
    assert FAKE_KEY.hex() not in body        # THE key-free guarantee, server-side


def test_provision_delegates_with_csrf():
    v = FakeVault()
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    resp = client.post("/api/nodes/provision", json={"node_id": 5, "role": "host", "label": "relay"},
                       headers={"X-CSRF-Token": tok})
    assert resp.status_code == 200 and resp.get_json()["status"] == "ok"
    assert "5" in v.get("node_keys")                         # controller actually provisioned it
    assert FAKE_KEY.hex() not in resp.get_data(as_text=True)  # response carries no key


def test_rotate_and_deprovision_delegate():
    v = FakeVault()
    node_provision.provision_node(v, 6, key=FAKE_KEY, role="host")
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    k1 = v.get("node_keys")["6"]["key"]
    r = client.post("/api/nodes/rotate", json={"node_id": 6}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert v.get("node_keys")["6"]["key"] != k1              # key rotated in the vault
    r2 = client.post("/api/nodes/deprovision", json={"node_id": 6}, headers={"X-CSRF-Token": tok})
    assert r2.status_code == 200 and r2.get_json()["removed"] is True
    assert "6" not in v.get("node_keys")


def test_attach_detach_delegate_key_free():
    v = FakeVault()
    node_provision.provision_node(v, 3, key=FAKE_KEY, role="host")
    dm = DeviceManager()
    ctrl = NodesController(dm, vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    gw = MockGateway("COM7")
    gw.connect()
    dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)

    r = client.post("/api/nodes/attach", json={"node_id": 3, "gateway_port": "COM7"},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert 3 in ctrl.attached_ids()
    assert FAKE_KEY.hex() not in client.get("/nodes").get_data(as_text=True)

    r2 = client.post("/api/nodes/detach", json={"node_id": 3}, headers={"X-CSRF-Token": tok})
    assert r2.status_code == 200 and r2.get_json()["detached"] is True
    assert 3 not in ctrl.attached_ids()


def test_mutation_fails_closed_when_locked():
    ctrl = NodesController(DeviceManager(), vault_getter=_locked)   # locked
    client, tok = _authed_client(ctrl)
    r = client.post("/api/nodes/provision", json={"node_id": 5, "role": "host"},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 403 and r.get_json()["error"] == "vault is locked"   # controller-enforced


def test_attach_bad_gateway_rejected_key_free():
    v = FakeVault()
    node_provision.provision_node(v, 5, key=FAKE_KEY, role="host")
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    r = client.post("/api/nodes/attach", json={"node_id": 5, "gateway_port": "node:5"},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 400                              # a node link is not a gateway
    assert FAKE_KEY.hex() not in r.get_data(as_text=True)


def test_invalid_node_id_rejected():
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: FakeVault())
    client, tok = _authed_client(ctrl)
    for bad in ({"node_id": "abc"}, {"node_id": 70000}, {}, {"node_id": True}):
        r = client.post("/api/nodes/rotate", json=bad, headers={"X-CSRF-Token": tok})
        assert r.status_code == 400, bad


def test_post_without_csrf_is_rejected():
    v = FakeVault()
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, _tok = _authed_client(ctrl)
    r = client.post("/api/nodes/provision", json={"node_id": 5})   # no X-CSRF-Token header
    assert r.status_code == 403
    assert v.get("node_keys") in (None, {})                        # nothing provisioned


def test_post_without_auth_is_rejected():
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: FakeVault())
    app, _sio = create_app(ctrl._dm, FlashEngine(), EventBus(), TargetPool(), nodes_controller=ctrl)
    client = app.test_client()                                     # NO authenticated session
    r = client.post("/api/nodes/provision", json={"node_id": 5}, headers={"X-CSRF-Token": "x"})
    assert r.status_code == 401                                    # basic-auth challenge (gate isolated)


def test_non_object_json_body_is_clean_400_not_500():
    """A truthy non-object JSON body (a bare scalar/array) must 400 cleanly, never AttributeError -> 500."""
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: FakeVault())
    client, tok = _authed_client(ctrl)
    for body in ("5", "\"x\"", "[1,2]", "true"):
        r = client.post("/api/nodes/provision", data=body,
                        headers={"X-CSRF-Token": tok, "Content-Type": "application/json"})
        assert r.status_code == 400, body        # not 500


def test_oversize_label_rejected():
    v = FakeVault()
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    r = client.post("/api/nodes/provision", json={"node_id": 8, "label": "x" * 65},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 400
    assert v.get("node_keys") in (None, {})       # not persisted


def test_label_with_html_is_escaped_no_xss():
    v = FakeVault()
    node_provision.provision_node(v, 9, key=FAKE_KEY, role="host", label='<script>"x"')
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, _tok = _authed_client(ctrl)
    body = client.get("/nodes").get_data(as_text=True)
    assert "<script>\"x\"" not in body            # raw injection absent
    assert "&lt;script&gt;" in body               # autoescaped
    assert FAKE_KEY.hex() not in body


def test_api_nodes_response_is_no_store():
    v = FakeVault()
    ctrl = NodesController(DeviceManager(), vault_getter=lambda: v)
    client, tok = _authed_client(ctrl)
    r = client.post("/api/nodes/provision", json={"node_id": 5}, headers={"X-CSRF-Token": tok})
    assert r.headers.get("Cache-Control") == "no-store"   # /api/ responses aren't cached


def test_all_mutations_fail_closed_when_locked():
    dm = DeviceManager()
    ctrl = NodesController(dm, vault_getter=_locked)
    client, tok = _authed_client(ctrl)
    # a real connected gateway so attach_via_port clears the gateway check and REACHES the vault (which,
    # locked, is what must 403 — otherwise attach would 400 on the gateway lookup before the vault).
    gw = MockGateway("COM7")
    gw.connect()
    dm.attach_connection(Device(port="COM7", name="Marauder", connected=True), gw)
    for path, body in (
        ("/api/nodes/provision", {"node_id": 5}),
        ("/api/nodes/rotate", {"node_id": 5}),
        ("/api/nodes/deprovision", {"node_id": 5}),
        ("/api/nodes/attach", {"node_id": 5, "gateway_port": "COM7"}),
    ):
        r = client.post(path, json=body, headers={"X-CSRF-Token": tok})
        assert r.status_code == 403, path         # controller-enforced fail-closed (not just UI)
