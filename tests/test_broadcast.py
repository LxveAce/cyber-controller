"""Unified Action Broadcast engine — pure logic, no hardware. Fakes a DeviceManager + EventBus."""
from __future__ import annotations

from src.core.broadcast import BroadcastEngine, BroadcastVerb


class _Dev:
    def __init__(self, port: str, firmware: str):
        self.port = port
        self.firmware = firmware
        self.name = firmware


class _Conn:
    def __init__(self, fail: bool = False):
        self.writes: list[str] = []
        self.fail = fail

    def write(self, s: str) -> None:
        if self.fail:
            raise OSError("port closed")
        self.writes.append(s)


class _DM:
    def __init__(self, devices, conns=None):
        self._devices = devices
        self._conns = conns or {d.port: _Conn() for d in devices}

    def list_connected(self):
        return self._devices

    def get_connection(self, port):
        return self._conns.get(port)


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


def test_plan_find_aps_translates_and_skips_unsupported():
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "bw16"),
              _Dev("COM3", "ghost-esp"), _Dev("COM4", "flipper")])
    plan = BroadcastEngine(dm, _Bus()).plan(BroadcastVerb.FIND_APS)
    assert {c.port: c.command for c in plan.concrete} == {
        "COM1": "scanap", "COM2": "AT+SCAN", "COM3": "scanap"}
    assert plan.skipped == [("COM4", "flipper", "unsupported by this firmware")]


def test_plan_deauth_all_is_lab_only():
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "bw16")])
    plan = BroadcastEngine(dm, _Bus()).plan(BroadcastVerb.DEAUTH_ALL)
    assert plan.worst_danger == "lab-only"
    # every concrete command on a dangerous verb classifies dangerous (no silent safe)
    assert all(c.danger for c in plan.concrete)


def test_dispatch_unconfirmed_dangerous_sends_nothing():
    dm = _DM([_Dev("COM1", "marauder")])
    eng = BroadcastEngine(dm, _Bus())
    plan = eng.plan(BroadcastVerb.DEAUTH_ALL)
    res = eng.dispatch(plan, confirmed=False)
    assert len(res) == 1 and res[0].status == "needs-confirm"
    assert dm.get_connection("COM1").writes == []


def test_dispatch_sends_pre_then_command_in_order():
    dm = _DM([_Dev("COM1", "marauder")])
    eng = BroadcastEngine(dm, _Bus())
    eng.dispatch(eng.plan(BroadcastVerb.DEAUTH_ALL), confirmed=True)
    assert dm.get_connection("COM1").writes == ["select -a all", "attack -t deauth"]


def test_dispatch_isolates_failed_device():
    conns = {"COM1": _Conn(fail=True), "COM2": _Conn()}
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "ghost-esp")], conns)
    eng = BroadcastEngine(dm, _Bus())
    res = {r.port: r.status for r in eng.dispatch(eng.plan(BroadcastVerb.FIND_APS), confirmed=True)}
    assert res["COM1"] == "failed" and res["COM2"] == "sent"
    assert conns["COM2"].writes == ["scanap"]


def test_stop_all_is_safe_and_dispatches_without_confirm():
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "bw16")])
    eng = BroadcastEngine(dm, _Bus())
    plan = eng.plan(BroadcastVerb.STOP_ALL)
    assert plan.worst_danger == ""
    res = {r.port: r.command for r in eng.dispatch(plan, confirmed=False)}
    assert res == {"COM1": "stopscan", "COM2": "AT+STOP"}


def test_unknown_firmware_is_skipped():
    dm = _DM([_Dev("COM1", "totally-unknown-fw")])
    plan = BroadcastEngine(dm, _Bus()).plan(BroadcastVerb.FIND_APS)
    assert plan.concrete == []
    assert plan.skipped and plan.skipped[0][2] == "firmware unknown"


def test_available_verbs_counts_supporting_devices():
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "bw16")])
    av = BroadcastEngine(dm, _Bus()).available_verbs()
    assert av[BroadcastVerb.FIND_APS] == 2     # both
    assert av[BroadcastVerb.SUBGHZ_SCAN] == 0  # neither


def test_dispatch_emits_bus_trail():
    dm = _DM([_Dev("COM1", "marauder"), _Dev("COM2", "ghost-esp")])
    bus = _Bus()
    eng = BroadcastEngine(dm, bus)
    eng.dispatch(eng.plan(BroadcastVerb.FIND_APS), confirmed=True)
    topics = [t for t, _ in bus.events]
    assert "broadcast.started" in topics and "broadcast.completed" in topics
    assert topics.count("action.executed") == 2  # one per device
