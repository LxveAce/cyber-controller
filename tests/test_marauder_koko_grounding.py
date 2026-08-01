"""Marauder support grounded against JustCallMeKoko's real ESP32 Marauder firmware (v1.14.1 source).

From the `koko-marauder-grounding` research (workflow wf_44cb9afa, 2026-08-01): three fixes verified against
his actual firmware — the CAPTURE_HANDSHAKES broadcast verb was routing to the wrong sniff primitive, the
`gpstracker` GPS verb was missing, and our WiGLE writer threw away the HDOP it already parses.
"""
from __future__ import annotations

from src.core import wardrive as wd
from src.core.broadcast import BroadcastVerb
from src.protocols.marauder import BROADCAST_CAPABILITIES, MarauderProtocol


def test_capture_handshakes_routes_to_sniffpmkid_not_sniffpwn():
    # Ground truth: Marauder's handshake-material capture is `sniffpmkid` (PMKID from the EAPOL exchange).
    # `sniffpwn` is its Pwnagotchi-BEACON monitor — a different op — so routing "capture handshakes" there
    # captured no handshakes on a real board. Our own catalog already uses sniffpmkid for that intent.
    assert BROADCAST_CAPABILITIES[BroadcastVerb.CAPTURE_HANDSHAKES][1] == "sniffpmkid"


def test_gpstracker_is_in_the_gps_catalog():
    # Ground truth: `gpstracker -c <start|stop>` logs the device's OWN path to GPX (distinct from the
    # gpspoi/wardrivepoi POI markers). It's a real Marauder GPS verb and was missing from our surface.
    names = {c.name for c in MarauderProtocol().get_commands()}
    assert "gpstracker -c <start|stop>" in names


def test_to_wigle_row_writes_accuracy_from_hdop():
    # Ground truth: Marauder writes AccuracyMeters = 2.5 × HDOP (the WiGLE accuracy convention). We parse
    # HDOP into GpsFix already, so use it instead of a hardcoded 0. (Our HDOP is the raw GGA field — NOT
    # MicroNMEA's tenths — so it's 2.5×hdop, no /10.) Column 10 of the 14-col WigleWifi-1.6 row.
    obs = wd.ApObservation(bssid="aa:bb:cc:dd:ee:ff", ssid="Net", channel=6, rssi=-40, auth="[WPA2][ESS]")
    fix = wd.GpsFix(lat=48.1173, lon=11.5167, alt=545.4, has_fix=True, hdop=1.5)
    accuracy = wd.to_wigle_row(obs, fix, "2026-06-27 12:00:00").split(",")[10]
    assert accuracy == "3.8"                                             # 2.5 × 1.5 = 3.75 -> "3.8"

    no_hdop = wd.GpsFix(lat=48.1173, lon=11.5167, alt=545.4, has_fix=True)   # hdop defaults 0.0
    assert wd.to_wigle_row(obs, no_hdop, "2026-06-27 12:00:00").split(",")[10] == "0"
