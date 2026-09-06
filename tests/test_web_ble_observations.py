"""Exercise scan output through the real parser, hub and authenticated endpoint."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.protocols.marauder import MarauderProtocol
from src.ui.web.app import create_app


class ScanInput:
    port = "SYNTHETIC_BLE"

    def on_line(self, callback):
        self.feed = callback


@pytest.fixture
def scan_app(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    hub = CrossCommHub(DeviceManager())
    app, _ = create_app(hub.dm, FlashEngine(), hub.bus, hub.pool)
    app.config["cc_hub"] = hub
    conn = ScanInput()
    hub.ingestor.attach(conn, MarauderProtocol())
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["cred_gen"] = app.extensions["cc_web_credentials"].generation
    return app, client, hub, conn


def test_reports_require_current_authenticated_session(scan_app):
    app, client, _, conn = scan_app
    conn.feed("-60 Device: private label")
    assert app.test_client().get("/api/ble-observations").status_code == 401
    with client.session_transaction() as session:
        session["cred_gen"] = "expired"
    response = client.get("/api/ble-observations")
    assert response.status_code == 401
    assert "private label" not in response.get_data(as_text=True)


def test_official_scan_and_list_records_reach_api_without_creating_targets(scan_app):
    _, client, hub, conn = scan_app
    conn.feed("-60 Device: Watch")
    conn.feed("[0][RSSI:-61] Watch")
    conn.feed("-62 Device: 02:00:00:00:00:01")
    response = client.get("/api/ble-observations")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.get_json()
    assert data["available"] is True
    reports = data["observations"]
    assert [r["label"] for r in reports] == ["Watch", "Watch", "02:00:00:00:00:01"]
    assert [r["format"] for r in reports] == ["live", "list", "live"]
    assert reports[1]["reported_index"] == 0
    assert len({r["observation_id"] for r in reports}) == 3
    assert all(r["addressable"] is False and "mac" not in r for r in reports)
    assert hub.pool.count == 0
    assert client.get("/api/targets").get_json() == []


def test_existing_address_bearing_target_stays_separate(scan_app):
    _, client, _, conn = scan_app
    conn.feed("BLE: 02:00:00:00:00:01 Name: Watch RSSI: -60")
    conn.feed("-61 Device: Watch")
    targets = client.get("/api/targets").get_json()
    reports = client.get("/api/ble-observations").get_json()["observations"]
    assert len(targets) == len(reports) == 1
    assert targets[0]["mac"] == "02:00:00:00:00:01"
    assert reports[0]["label"] == "Watch"


def test_empty_collection_and_unavailable_collection_are_distinct(scan_app):
    app, client, _, _ = scan_app
    assert client.get("/api/ble-observations").get_json() == {"available": True, "observations": []}
    del app.config["cc_hub"]
    assert client.get("/api/ble-observations").get_json() == {"available": False, "observations": []}


def test_report_history_response_is_bounded_and_keeps_firmware_text_literal(scan_app):
    _, client, _, conn = scan_app
    for index in range(205):
        conn.feed(f"-60 Device: label {index}")
    conn.feed('-61 Device: <img src=x onerror="alert(1)">')
    reports = client.get("/api/ble-observations").get_json()["observations"]
    assert len(reports) == 200
    assert reports[0]["label"] == "label 6"
    assert reports[-1]["label"] == '<img src=x onerror="alert(1)">'


def test_report_endpoint_does_not_accept_commands(scan_app):
    _, client, _, _ = scan_app
    assert client.post("/api/ble-observations", json={"command": "scan"}).status_code == 405
