"""End-to-end LxveOS ↔ Cyber Controller bridge-session simulation.

Per-line tests (``test_lxveos_protocol.py``) prove each event type decodes; this feeds a *whole realistic
operator session* through one ``LxveOSProtocol`` instance, in order, and asserts the resulting event stream.
It is the host-side stand-in for a live serial session (no hardware): identify → ``bridge on`` → recon
(scan/stations/blescan) → capture → a detector firing → the two-factor arm flow → disarm. It guards the
things per-line tests can't: ordering, the ``info``-accumulation / prompt / event interleave, and that a full
firmware event vocabulary round-trips without any line being dropped or misclassified.

The fixture is SYNTHETIC but shaped exactly like the firmware's emitted lines (see the firmware
``docs/EVENT-PROTOCOL.md`` + the emit sites in ``components/lxveos_cli/src/lxveos_cli.c``). When the firmware
event surface grows, extend this session so the contract stays covered end to end.
"""
from __future__ import annotations

from src.protocols.lxveos import LxveOSProtocol

# One realistic `bridge on` session, verbatim in the firmware's line shapes. Blank/prompt lines are included
# because a real capture interleaves them — the parser must handle them without corrupting event state.
_SESSION = [
    # 1. connect + identity poll (status line is always available, even before `bridge on`)
    "LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 "
    "panel=none caps=0x007 ops=12/3/6 heap=184988 arm=safe tx=1",
    "lxveos>",
    # 2. enable the machine event stream
    "LXVEOS/1 bridge state=on",
    # 3. `scan` — two APs (one hidden) then the batch marker
    "LXVEOS/1 ap bssid=de:ad:be:ef:00:01 ssid=4d794e6574 ch=6 rssi=-42 auth=wpa2",
    "LXVEOS/1 ap bssid=aa:bb:cc:dd:ee:ff ssid= ch=1 rssi=-70 auth=open",
    "LXVEOS/1 done of=scan n=2",
    # 4. `stations` — one inferred client
    "LXVEOS/1 sta mac=aa:bb:cc:00:11:22 ap=de:ad:be:ef:00:01 rssi=-58 frames=42 essid=4d794e6574",
    "LXVEOS/1 done of=stations n=1",
    # 5. `blescan` — one device that is also a known tracker
    "LXVEOS/1 ble addr=66:55:44:33:22:11 type=random rssi=-55 name=4d79 company=76 tracker=1",
    "LXVEOS/1 done of=blescan n=1",
    # 6. `capture` — a PMKID handshake, forwarded as a raw hashcat-22000 line
    "LXVEOS/1 hs kind=pmkid line=WPA*01*0102030405060708090a0b0c0d0e0f10"
    "*deadbeef0001*aabbcc001122*4d794e6574***",
    # 7. `defend` fires a deauth alert
    "LXVEOS/1 alert kind=deauth bssid=de:ad:be:ef:00:01 count=27 deauth=20 disassoc=7",
    # 8. `airspace` occupancy summary (custom) -> one snapshot event
    "LXVEOS/1 snapshot aps=2 open=1 wps=0 bles=1 trackers=1",
    # 9. two-factor arm flow, then disarm
    "LXVEOS/1 arm state=pending token=123456789 window=30",
    "LXVEOS/1 arm state=armed",
    "LXVEOS/1 arm state=safe",
]


def _run(lines):
    """Feed lines through one parser instance; return the list of non-None ParsedEvents."""
    p = LxveOSProtocol()
    return [ev for ln in lines if (ev := p.parse_line(ln)) is not None]


def test_full_bridge_session_event_stream():
    events = _run(_SESSION)
    # every non-blank line above yields exactly one event (nothing dropped, nothing doubled)
    types = [ev.event_type for ev in events]
    assert types == [
        "device_info",       # status
        "status",            # prompt (readiness signal)
        "bridge_state",      # bridge on
        "ap_found", "ap_found", "batch_done",         # scan
        "client_found", "batch_done",                 # stations
        "ble_found", "batch_done",                    # blescan
        "handshake_captured",                         # capture
        "alert",                                      # defend
        "snapshot",                                   # airspace
        "arm_state", "arm_state", "arm_state",        # arm -> armed -> safe
    ]


def test_session_identity_and_tx_capability():
    events = _run(_SESSION)
    ident = events[0]
    assert ident.event_type == "device_info"
    assert ident.data["board"] == "bare_esp32_headless"
    assert ident.data["caps_tokens"] == ["wifi", "ble", "bt_classic"]
    assert ident.data["arm"] == "safe" and ident.data["tx"] is True  # TX-capable but currently SAFE


def test_session_recon_payloads_decode():
    events = _run(_SESSION)
    by_type: dict[str, list] = {}
    for ev in events:
        by_type.setdefault(ev.event_type, []).append(ev)
    # scan: the visible AP decodes its SSID; the hidden AP is an empty (not missing) SSID
    aps = by_type["ap_found"]
    assert aps[0].data["ssid"] == "MyNet" and aps[0].data["ch"] == 6
    assert aps[1].data["ssid"] == "" and aps[1].data["auth"] == "open"
    # a client tied to the visible AP
    assert by_type["client_found"][0].data["ap"] == "de:ad:be:ef:00:01"
    # the BLE device is flagged as a tracker
    assert by_type["ble_found"][0].data["tracker"] == 1
    # the handshake keeps the crackable line verbatim AND surfaces the SSID for display
    hs = by_type["handshake_captured"][0]
    assert hs.data["line"].startswith("WPA*01*") and hs.data["essid"] == "MyNet"
    # the deauth alert names the busiest source and the split counts
    al = by_type["alert"][0]
    assert al.data["kind"] == "deauth" and al.data["count"] == 27 and al.data["deauth"] == 20


def test_session_arm_state_progression():
    # the arm flow must read out as SAFE-implied -> pending(token) -> armed -> safe, so the TX-lockout UI
    # can drive its lamp purely off the event stream.
    arms = [ev for ev in _run(_SESSION) if ev.event_type == "arm_state"]
    assert [a.data["state"] for a in arms] == ["pending", "armed", "safe"]
    assert arms[0].data["token"] == 123456789 and arms[0].data["window"] == 30


def test_events_before_bridge_on_are_still_parsed():
    # status + prompt arrive before `bridge on`; they must parse regardless (the dashboard poll is always on).
    events = _run(_SESSION[:2])
    assert [ev.event_type for ev in events] == ["device_info", "status"]
