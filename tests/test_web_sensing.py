"""WS1 /api/sensing read route + the CrossCommHub observer that feeds it. The endpoint exposes the
SensingModel rollup (per-node presence/motion + room-occupied summary) for a future Sense view;
passive/read-only. Also guards that a `sensing_verdict` parsed event folds into hub.sensing (while
nothing else does), so a connected csi-sensor node's output actually reaches the endpoint."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.sensing_model import SensingModel
from src.protocols.base import ParsedEvent
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(model=None, authed=True):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                           sensing_model=model)
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["cred_gen"] = c.application.extensions["cc_web_credentials"].generation
            sess["csrf"] = "tok"
    return c


def test_sensing_requires_auth():
    assert _client(authed=False).get("/api/sensing").status_code == 401


def test_sensing_no_model_is_empty_but_supported():
    body = _client(SensingModel() if False else None).get("/api/sensing").get_json()
    assert body["supported"] is True
    assert body["nodes"] == []
    assert body["summary"]["any_occupied"] is False


def test_sensing_reports_nodes_and_occupancy():
    m = SensingModel()
    m.observe({"presence": True, "motion": 0.42, "confidence": 0.8, "node_id": "n1"}, now=1.0)
    m.observe({"presence": False, "motion": 0.0, "confidence": 0.1, "node_id": "n2"}, now=1.0)
    body = _client(m).get("/api/sensing").get_json()
    assert body["supported"] is True
    ids = {n["node_id"] for n in body["nodes"]}
    assert ids == {"n1", "n2"}
    n1 = next(n for n in body["nodes"] if n["node_id"] == "n1")
    assert n1["presence"] is True and abs(n1["motion"] - 0.42) < 1e-6
    # the recovered fields never include raw CSI or a MAC — it's a verdict rollup, not a target
    assert "mac" not in n1 and "bssid" not in n1


def test_sensing_endpoint_never_leaks_a_target_shape():
    m = SensingModel()
    m.observe({"presence": True, "motion": 0.5, "confidence": 0.6, "node_id": "n1"}, now=1.0)
    text = _client(m).get("/api/sensing").get_data(as_text=True)
    assert "sensing_verdict" not in text   # the event name is internal; the API exposes fields only


# ── the hub observer: a sensing_verdict parsed event folds into hub.sensing, nothing else does ──
def test_hub_folds_sensing_verdict_into_the_model():
    hub = CrossCommHub(DeviceManager(), EventBus(), TargetPool())
    assert hub.sensing.count == 0
    ev = ParsedEvent("sensing_verdict",
                     {"presence": True, "motion": 0.3, "confidence": 0.7, "node_id": "n1"}, "raw")
    hub._on_parsed_event(ev, "COM9")
    assert hub.sensing.count == 1
    assert hub.sensing.get("n1").presence is True


def test_hub_ignores_non_sensing_events():
    hub = CrossCommHub(DeviceManager(), EventBus(), TargetPool())
    hub._on_parsed_event(ParsedEvent("ap_found", {"ssid": "Net", "bssid": "AA:BB"}, "raw"), "COM9")
    hub._on_parsed_event(ParsedEvent("info", {"message": "boot ok"}, "raw"), "COM9")
    assert hub.sensing.count == 0   # only sensing_verdict feeds the sensing model
