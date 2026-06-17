"""Live hardware integration test for cross-device routing (cross-comm / cross-resource).

The unit suite (test_cross_comm.py) covers the AutoRouter rule logic in isolation. THIS test exercises
the full chain on REAL hardware: a discovered-target event from "device A" -> AutoRouter rule match ->
`send_command` -> a REAL serial write to "device B" via the real DeviceManager/SerialConnection. That is
the "one device gets an AP, another executes on it" path.

Set CC_LIVE_PORT to a connected board (e.g. COM7) to run; skipped otherwise so CI stays green.

    CC_LIVE_PORT=COM7 python -m pytest tests/test_cross_comm_live.py -q
    python tests/test_cross_comm_live.py COM7      # direct run
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from src.core.device_manager import DeviceManager
from src.core.cross_comm import AutoRouter, EventBus, RoutingRule, TargetType


def _run(port: str) -> dict:
    """Wire the REAL DeviceManager + EventBus + AutoRouter, open `port`, inject an AP target event from
    'device A', and confirm the rule routes a command to the real serial port (device B). Returns a
    result dict. Raises on hard failure."""
    dm = DeviceManager()
    bus = EventBus()
    routed: list[tuple[str, str]] = []

    for dev in DeviceManager.scan_ports():  # register visible ports so open_connection(port) is known
        dm.add_device(dev)
    conn = dm.open_connection(port, 115200)
    assert conn is not None and conn.is_connected, f"could not open {port}"

    def send_command(p: str, cmd: str) -> None:
        # record the route, then deliver it to the REAL device over serial
        routed.append((p, cmd))
        c = dm.get_connection(p)
        assert c is not None and c.is_connected, f"no live connection for {p}"
        c.write(cmd)  # rejects embedded control chars (serial_handler hardening)

    router = AutoRouter(bus, send_command)
    # Rule: any AP whose SSID contains 'lab' -> tell device B to hop to that AP's channel (a benign,
    # representative cross-resource action: A discovered it, B acts on it).
    router.add_rule(RoutingRule(
        name="ap-to-deviceB",
        target_type=TargetType.AP,
        ssid_pattern="lab",
        min_rssi=-90,
        command_template="channel {channel}",
        device_port=port,
        cooldown=0.0,
        enabled=True,
    ))

    # "Device A" discovers an AP and publishes it onto the shared bus.
    bus.publish("target.added", {
        "target_type": "ap", "mac": "de:ad:be:ef:00:11", "ssid": "lab-ap", "rssi": -42, "channel": 6,
    })
    time.sleep(0.3)  # let the bus deliver + the router fire

    # capture any response the device emitted (best-effort; depends on the firmware on B)
    resp = b""
    try:
        end = time.time() + 1.0
        while time.time() < end:
            resp += conn.read() or b"" if hasattr(conn, "read") else b""
    except Exception:
        pass

    dm.shutdown()
    assert routed, "AutoRouter did not route the discovered AP to device B"
    assert routed[0][0] == port and routed[0][1] == "channel 6", f"unexpected route: {routed}"
    return {"routed": routed, "resp_bytes": len(resp)}


@pytest.mark.skipif(not os.environ.get("CC_LIVE_PORT"), reason="set CC_LIVE_PORT to a connected board")
def test_cross_device_route_delivers_to_real_serial():
    res = _run(os.environ["CC_LIVE_PORT"])
    assert res["routed"][0][1] == "channel 6"


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CC_LIVE_PORT", "COM7")
    print(f"[live cross-comm] port={p}")
    out = _run(p)
    print("routed:", out["routed"])
    print("device response bytes:", out["resp_bytes"])
    print("RESULT: cross-device route -> real serial delivery OK")
