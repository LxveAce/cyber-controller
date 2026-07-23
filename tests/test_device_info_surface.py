"""device_info surface — a firmware that reports its own identity over serial (LxveOS status/info)
lands on the connected Device as live identity + RUNTIME capabilities.

Before this, a parsed ``device_info`` event was dropped by the TargetIngestor (only ap_found/
client_found/captures were consumed), so LxveOS — which reports its radios at runtime via the
status line's ``caps=`` bitmask rather than a static per-firmware map — surfaced no capabilities
at all. These tests cover the model consumer (Device.apply_device_info, runtime-aware
capabilities) and the ingestor wire, end-to-end through the real hub over the verbatim COM23 output.
"""
from __future__ import annotations

from src.models.device import Device
from src.protocols.lxveos import LxveOSProtocol

# Verbatim COM23 captures (LxveOS 0.1.0-m0, bare_esp32_headless) — as in the protocol tests.
_STATUS = (
    "LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 "
    "panel=none caps=0x007 ops=12/3/6 ops_attach=2 heap=184988"
)
_INFO = [
    "fw    : LxveOS 0.1.0-m0", "board : bare_esp32_headless", "chip  : esp32", "ui    : headless",
]


def _status_data() -> dict:
    """The parsed device_info data for the real status line, straight from the real parser."""
    return LxveOSProtocol().parse_line(_STATUS).data


class _FakeConn:
    """Minimal SerialConnection stand-in: records on_line callbacks and feeds lines (matches
    test_target_ingest._FakeConn)."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs: list = []

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


class _FakeDM:
    """Minimal device registry: get_device(port) -> Device|None (all the ingestor needs)."""

    def __init__(self, devices: dict) -> None:
        self._devices = devices

    def get_device(self, port: str):
        return self._devices.get(port)


# ── Device model consumer ────────────────────────────────────────────

def test_apply_device_info_from_status_sets_runtime_caps_and_telemetry():
    dev = Device(port="COM23", firmware="lxveos")
    assert dev.runtime_capabilities == frozenset()
    assert dev.capabilities == frozenset()  # static lxveos map declares none (caps are runtime)

    changed = dev.apply_device_info(_status_data())
    assert changed is True
    # Runtime capabilities decoded from the real caps=0x007 bitmask.
    assert dev.runtime_capabilities == frozenset({"wifi", "ble", "bt_classic"})
    # capabilities now reflects what the board actually reported (was empty from the static map).
    assert dev.capabilities == frozenset({"wifi", "ble", "bt_classic"})
    # Live telemetry snapshot — identity + the ops/heap the status line carried.
    assert dev.telemetry["fw"] == "0.1.0-m0"
    assert dev.telemetry["board"] == "bare_esp32_headless"
    assert dev.telemetry["chip"] == "esp32"
    assert dev.telemetry["ui"] == "headless"
    assert dev.telemetry["panel"] == "none"
    assert dev.telemetry["ops"] == {"ready": 12, "planned": 3, "attachable_unavailable": 6}
    assert dev.telemetry["ops_attach"] == 2
    assert dev.telemetry["heap"] == 184988
    assert dev.telemetry["proto_version"] == 1
    # The raw bitmask + decoded tokens are NOT duplicated into telemetry (they drive runtime caps).
    assert "caps" not in dev.telemetry and "caps_tokens" not in dev.telemetry


def test_capabilities_prefers_runtime_over_static():
    # A firmware WITH a non-empty static map, then a device_info arrives: the runtime-reported
    # set must win, so a device is described by what it actually said over the wire.
    dev = Device(port="COM4", firmware="marauder")
    assert dev.capabilities  # marauder declares static capabilities
    dev.apply_device_info({"caps_tokens": ["only_runtime"]})
    assert dev.capabilities == frozenset({"only_runtime"})


def test_apply_device_info_from_info_block_fills_identity_without_caps():
    # The 4-line info block carries identity but no caps= bitmask -> telemetry fills, runtime caps
    # untouched (a capability-less report must not clear an already-known set).
    p = LxveOSProtocol()
    for line in _INFO[:-1]:
        p.parse_line(line)
    ev = p.parse_line(_INFO[-1])  # closing `ui :` emits the device_info
    dev = Device(port="COM23", firmware="lxveos")
    dev.runtime_capabilities = frozenset({"wifi"})  # pretend a prior status set this
    changed = dev.apply_device_info(ev.data)
    assert changed is True
    assert dev.telemetry["board"] == "bare_esp32_headless" and dev.telemetry["fw"] == "0.1.0-m0"
    assert dev.runtime_capabilities == frozenset({"wifi"})  # not cleared by a caps-less info block


def test_apply_device_info_is_idempotent_and_rejects_non_dict():
    dev = Device(port="COM23", firmware="lxveos")
    assert dev.apply_device_info(_status_data()) is True
    assert dev.apply_device_info(_status_data()) is False   # nothing changed the second time
    assert dev.apply_device_info(None) is False             # tolerant of a non-dict
    assert dev.apply_device_info("nope") is False


def test_status_arm_field_corrects_stale_arm_state():
    # HIGH (final 1.8.0 review): arm_state is set only by an explicit arm/disarm EVENT and has no
    # reset path, so a firmware that disarms/reboots WITHOUT a disarm event (watchdog, brown-out,
    # auto-timeout) would leave CC stale-"armed" — keeping the Operate console TX buttons + the
    # _send gate OPEN on a device the firmware reports SAFE. The authoritative arm= in the periodic
    # status line MUST correct it (dropped before the fix: apply_device_info kept only
    # _TELEMETRY_KEYS, which excludes arm).
    status = LxveOSProtocol()
    armed = status.parse_line(_STATUS + " arm=armed tx=1").data
    safe = status.parse_line(_STATUS + " arm=safe tx=1").data
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_arm_state({"state": "armed"})
    assert dev.arm_state == "armed"
    # A status reporting arm=safe (NO explicit disarm event) pulls CC back to safe.
    changed = dev.apply_device_info(safe)
    assert dev.arm_state == "safe", "status arm= must correct a stale armed state (TX-lockout)"
    assert changed is True
    # And a later status reporting arm=armed re-arms.
    dev.apply_device_info(armed)
    assert dev.arm_state == "armed"


def test_device_info_without_arm_field_never_clears_live_armed():
    # Fail-safe: the plain status/`info` block here carries NO arm= (see _STATUS), so it must NEVER
    # clear a live armed state — apply_arm_state ignores an absent value. Guards a spurious flip.
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_arm_state({"state": "armed"})
    assert dev.apply_device_info(_status_data()) is True   # caps/telemetry change, arm untouched
    assert dev.arm_state == "armed"


def test_device_info_round_trips_through_dict():
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_device_info(_status_data())
    restored = Device.from_dict(dev.to_dict())
    assert restored.runtime_capabilities == frozenset({"wifi", "ble", "bt_classic"})
    assert restored.telemetry["heap"] == 184988
    assert restored.capabilities == frozenset({"wifi", "ble", "bt_classic"})


# ── arm state (offensive-TX ARM/SAFE lamp) ───────────────────────────

def test_apply_arm_state_transitions_and_never_clears_on_a_blank():
    # A firmware's arm/disarm lifecycle (real LXVEOS/1 arm lines, from the parser) drives the stored
    # arm_state; a malformed line carrying no state must leave the live state intact.
    p = LxveOSProtocol()
    dev = Device(port="COM23", firmware="lxveos")
    assert dev.arm_state == ""
    pending = p.parse_line("LXVEOS/1 arm state=pending token=428913 window=30").data
    assert dev.apply_arm_state(pending) is True
    assert dev.arm_state == "pending"
    assert dev.apply_arm_state(p.parse_line("LXVEOS/1 arm state=armed").data) is True
    assert dev.arm_state == "armed"
    assert dev.apply_arm_state(p.parse_line("LXVEOS/1 arm state=armed").data) is False  # idempotent
    assert dev.apply_arm_state(p.parse_line("LXVEOS/1 arm state=safe").data) is True
    assert dev.arm_state == "safe"
    # a state-less dict (malformed line) or a non-dict must NOT wipe a known "safe"/"armed"
    assert dev.apply_arm_state({"proto_version": 1}) is False and dev.arm_state == "safe"
    assert dev.apply_arm_state(None) is False and dev.arm_state == "safe"


def test_arm_state_round_trips_through_dict():
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_arm_state({"state": "armed"})
    restored = Device.from_dict(dev.to_dict())
    assert restored.arm_state == "armed"


def test_ingestor_routes_arm_state_to_the_ports_device():
    from src.core.target_ingest import TargetIngestor

    dev = Device(port="COM23", firmware="lxveos")
    ingest = TargetIngestor(pool=_NullPool(), devices=_FakeDM({"COM23": dev}))
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    conn.feed("LXVEOS/1 arm state=armed")  # a real arm line over the wire
    assert dev.arm_state == "armed"
    conn.feed("LXVEOS/1 arm state=safe")   # disarm
    assert dev.arm_state == "safe"
    assert ingest is not None  # (an arm line is not a Target/capture; only the arm state changed)


# ── detector / watchlist alerts ──────────────────────────────────────

def test_apply_alert_stores_latest_and_counts():
    p = LxveOSProtocol()
    dev = Device(port="COM23", firmware="lxveos")
    assert dev.alert_count == 0 and dev.last_alert == {}
    deauth = p.parse_line("LXVEOS/1 alert kind=deauth bssid=de:ad:be:ef:00:01 count=27").data
    assert dev.apply_alert(deauth)
    assert dev.alert_count == 1
    assert dev.last_alert["kind"] == "deauth" and dev.last_alert["count"] == 27
    # a second, different alert replaces "latest" and bumps the count
    watch = p.parse_line("LXVEOS/1 alert kind=watch mac=11:22:33:44:55:66 band=ble rssi=-70").data
    assert dev.apply_alert(watch)
    assert dev.alert_count == 2
    assert dev.last_alert["kind"] == "watch" and dev.last_alert["band"] == "ble"
    # a non-dict is ignored (count unchanged)
    assert dev.apply_alert(None) is False and dev.alert_count == 2


def test_alert_round_trips_through_dict():
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_alert({"kind": "tracker", "vendor": "AirTag"})
    restored = Device.from_dict(dev.to_dict())
    assert restored.alert_count == 1 and restored.last_alert["vendor"] == "AirTag"


def test_ingestor_routes_alert_to_the_ports_device():
    from src.core.target_ingest import TargetIngestor

    dev = Device(port="COM23", firmware="lxveos")
    pool = _NullPool()
    ingest = TargetIngestor(pool=pool, devices=_FakeDM({"COM23": dev}))
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    conn.feed("LXVEOS/1 alert kind=tracker addr=11:22:33:44:55:66 vendor=AirTag rssi=-40")
    assert dev.alert_count == 1 and dev.last_alert["kind"] == "tracker"
    assert pool.added == []  # an alert is not a pool Target


# ── airspace occupancy snapshot ──────────────────────────────────────

def test_apply_snapshot_stores_latest_with_change_detect():
    # Unlike an alert (a counted stream), the `airspace` snapshot is latest-wins situational state:
    # store it, but return False when it's unchanged (no counter, no needless UI repaint).
    p = LxveOSProtocol()
    dev = Device(port="COM23", firmware="lxveos")
    assert dev.last_snapshot == {}
    first = p.parse_line("LXVEOS/1 snapshot aps=14 open=3 wps=2 bles=8 trackers=1").data
    assert dev.apply_snapshot(first) is True
    assert dev.last_snapshot["aps"] == 14 and dev.last_snapshot["open"] == 3
    # the same summary again is a no-op (change-detect returns False; there is NO counter to bump)
    same = p.parse_line("LXVEOS/1 snapshot aps=14 open=3 wps=2 bles=8 trackers=1").data
    assert dev.apply_snapshot(same) is False
    assert dev.last_snapshot["aps"] == 14
    # a changed count replaces the latest and reports the change
    moved = p.parse_line("LXVEOS/1 snapshot aps=15 open=3 wps=2 bles=8 trackers=1").data
    assert dev.apply_snapshot(moved) is True
    assert dev.last_snapshot["aps"] == 15
    # a non-dict is ignored and leaves the last snapshot intact
    assert dev.apply_snapshot(None) is False and dev.last_snapshot["aps"] == 15


def test_snapshot_round_trips_through_dict():
    dev = Device(port="COM23", firmware="lxveos")
    dev.apply_snapshot({"aps": 9, "open": 1, "bles": 4})
    restored = Device.from_dict(dev.to_dict())
    assert restored.last_snapshot == {"aps": 9, "open": 1, "bles": 4}


def test_forced_firmware_survives_a_dict_round_trip():
    # A manual firmware choice (firmware_forced=True) must persist through to_dict/from_dict, else a
    # later post-probe re-autodetect could silently overwrite the operator's choice.
    dev = Device(port="COM23", firmware="marauder", firmware_forced=True)
    restored = Device.from_dict(dev.to_dict())
    assert restored.firmware == "marauder" and restored.firmware_forced is True


def test_ingestor_routes_snapshot_to_the_ports_device():
    from src.core.target_ingest import TargetIngestor

    dev = Device(port="COM23", firmware="lxveos")
    pool = _NullPool()
    ingest = TargetIngestor(pool=pool, devices=_FakeDM({"COM23": dev}))
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    conn.feed("LXVEOS/1 snapshot aps=2 open=1 wps=0 bles=1 trackers=1")
    assert dev.last_snapshot["aps"] == 2 and dev.last_snapshot["trackers"] == 1
    assert pool.added == []  # a snapshot is not a pool Target


# ── TargetIngestor wire ──────────────────────────────────────────────

def test_ingestor_routes_device_info_to_the_ports_device():
    from src.core.target_ingest import TargetIngestor

    dev = Device(port="COM23", firmware="lxveos")
    ingest = TargetIngestor(pool=_NullPool(), devices=_FakeDM({"COM23": dev}))
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    conn.feed(_STATUS)  # a real status line over the wire
    assert dev.runtime_capabilities == frozenset({"wifi", "ble", "bt_classic"})
    assert dev.telemetry["heap"] == 184988


def test_ingestor_without_a_registry_drops_device_info_safely():
    # Backward compat: the Targets-only ingestor (no device registry) must ignore a device_info
    # line without error and without inventing a pool target.
    from src.core.target_ingest import TargetIngestor

    pool = _NullPool()
    ingest = TargetIngestor(pool=pool)  # devices defaults to None
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    conn.feed(_STATUS)  # must not raise
    assert pool.added == []  # a status line is not a Target


def test_cross_comm_hub_end_to_end_populates_device_runtime_caps():
    # The real path: DeviceManager opens a link -> CrossCommHub auto-attaches its ingestor (wired
    # with devices=self.dm) -> a real LxveOS status line refreshes the Device's runtime caps.
    from src.core.cross_comm_hub import CrossCommHub
    from src.core.device_manager import DeviceManager

    dm = DeviceManager()
    CrossCommHub(dm)  # subscribes to on_connection_opened, wires the ingestor with devices=dm
    dev = Device(port="COM23", name="LxveOS board", firmware="lxveos")
    conn = _FakeConn("COM23")
    dm.attach_connection(dev, conn)  # fires the hook -> hub attaches the lxveos-parsing ingestor
    conn.feed(_STATUS)
    assert dev.runtime_capabilities == frozenset({"wifi", "ble", "bt_classic"})
    assert dev.capabilities == frozenset({"wifi", "ble", "bt_classic"})


class _NullPool:
    """A pool that records adds; a device_info never produces one, which these tests assert."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, target) -> None:
        self.added.append(target)
