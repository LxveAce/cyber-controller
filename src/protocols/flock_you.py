"""Flock-You protocol — serial parser for the Flock-You ALPR-camera detector firmware.

Flock-You (``colonelpanichacks/flock-you``, Promiscuous WiFi Edition) is a PASSIVE, receive-only
2.4 GHz sniffer that flags Flock Safety ALPR (automated license-plate reader) surveillance
cameras by their WiFi OUI plus a probe-request IE fingerprint. It transmits nothing. This parser
turns its detections into ALPR targets so they surface in the Targets pool and the network graph
for lawful counter-surveillance awareness — you are not attacking anything, you are noticing it.

Serial output (read from upstream ``main.cpp`` on the ``promiscious-dev`` branch, 2026-07-03):

  * a machine JSON line per detection::

      {"event":"detection","detection_method":"wifi_wildcard_probe_ie_sig","protocol":"wifi_2_4ghz",
       "mac_address":"AA:BB:CC:11:22:33","oui":"AABBCC","device_name":"","rssi":-61,"channel":6,
       "frequency":2437,"ssid":""}

  * and a human mirror line::

      [flockyou] DETECT-SSID type=<frame> mac=<MAC> ssid="<ssid>" rssi=<int> ch=<n> count=<n>
      [flockyou] DETECT-OUI  mac=<MAC> oui=<oui> rssi=<int> ch=<n> addr=<addr2> count=<n>

  * plus ``[flockyou]`` status / heartbeat / boot lines.

The exact schema can still drift (upstream is an active dev branch, and GPS lat/lon is added
host-side by the Flask API — it is NOT present on the wire). So this parser is deliberately
TOLERANT: it prefers the JSON line, falls back to the human line, never raises on a malformed or
partial line, and emits ``alpr_found`` from whatever identifier fields it can recover.
``verify:`` field-exactness against ONE real device capture before relying on it in the field
(Stage-5 hardware gate).
"""
from __future__ import annotations

import json
import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# Human-mirror line marker: "[flockyou] DETECT-SSID ..." / "[flockyou] DETECT-OUI ..."
_RE_HUMAN = re.compile(r"\[flockyou\]\s+DETECT-(?:SSID|OUI)\b", re.IGNORECASE)
# Field pulls from a human line (order-independent, each optional).
_RE_MAC = re.compile(r"\bmac=([0-9a-fA-F:]{17})")
_RE_SSID = re.compile(r'\bssid="((?:[^"\\]|\\.)*)"')
_RE_RSSI = re.compile(r"\brssi=(-?\d+)")
_RE_CH = re.compile(r"\bch=(\d+)")
_RE_OUI = re.compile(r"\boui=([0-9a-fA-F]{6})")
# Any other "[flockyou] ..." line (heartbeat / boot / status) -> surfaced as info, not a target.
_RE_STATUS = re.compile(r"\[flockyou\]\s*(.*)", re.IGNORECASE)


def _to_int(value: object, default: int = 0) -> int:
    """int() that never raises — tolerant of missing / non-numeric / float / inf fields."""
    text = str(value).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        # Tolerate a drifted float like "-61.5" -> -61. OverflowError guards "inf"/"nan".
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return default


class FlockYouProtocol(BaseProtocol):
    """Parser for the Flock-You passive ALPR-camera detector.

    Passive / receive-only: there is no command channel to the firmware, so ``get_commands()``
    is empty (a connected Flock-You device is a *sensor*, not a controllable target surface).
    ``parse_line`` recognises the JSON detection line, the human mirror line, and plain
    ``[flockyou]`` status lines; everything else is treated as noise.
    """

    # Passive receive-only sensor: no text CLI, so mark it non-"text-cli" (like halehound) — the
    # connect probe then reports "no-cli" without writing an unsolicited `help` it can't answer.
    driver_type = "controlmap"

    # The firmware is a passive WiFi sniffer; the wardriving rig carries GPS host-side.
    capabilities = frozenset({"wifi", "gps"})

    @property
    def protocol_name(self) -> str:
        return "flock-you"

    # ── Parsing ──────────────────────────────────────────────────────
    def parse_line(self, line: str) -> ParsedEvent | None:
        line = (line or "").strip()
        if not line:
            return None

        # 1) Preferred: the machine JSON detection line. Accept a drifted line that dropped the
        #    "detection" method but still carries an identifier, so a schema change doesn't blind us.
        if line.startswith("{") and ("detection" in line or "mac_address" in line):
            ev = self._parse_json(line)
            if ev is not None:
                return ev
            # JSON was malformed/partial -> fall through to the other shapes rather than drop.

        # 2) Human mirror line: [flockyou] DETECT-SSID / DETECT-OUI ...
        if _RE_HUMAN.search(line):
            return self._parse_human(line)

        # 3) Any other [flockyou] line (heartbeat / boot / status) -> info event, not a target.
        status = _RE_STATUS.search(line)
        if status:
            return ParsedEvent(event_type="info", data={"message": status.group(1).strip()}, raw=line)

        # 4) Unknown line -> noise.
        return None

    @staticmethod
    def _parse_json(line: str) -> ParsedEvent | None:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        # Only treat it as a detection if it looks like one; other JSON stays out of the pool.
        if str(obj.get("event", "")).lower() not in ("detection", "") and "mac_address" not in obj:
            return None
        # Only a STRING identifier is a real MAC. A drifted line with a nested/typed mac_address
        # (e.g. {"mac_address": {...}}) must degrade to "" so ingest drops it — not manufacture a
        # phantom target keyed on str(dict).
        mac_raw = obj.get("mac_address") or obj.get("mac") or obj.get("bssid") or ""
        mac = mac_raw.strip() if isinstance(mac_raw, str) else ""
        data = {
            "mac": mac,
            "ssid": str(obj.get("ssid") or obj.get("device_name") or ""),
            "rssi": _to_int(obj.get("rssi")),
            "channel": _to_int(obj.get("channel")),
            "oui": str(obj.get("oui") or "").strip(),
            "detection_method": str(obj.get("detection_method") or "").strip(),
            "frequency": _to_int(obj.get("frequency")),
        }
        return ParsedEvent(event_type="alpr_found", data=data, raw=line)

    @staticmethod
    def _parse_human(line: str) -> ParsedEvent:
        def grp(rx: "re.Pattern[str]", default: str = "") -> str:
            m = rx.search(line)
            return m.group(1) if m else default

        data = {
            "mac": grp(_RE_MAC),
            "ssid": grp(_RE_SSID),
            "rssi": _to_int(grp(_RE_RSSI, "0")),
            "channel": _to_int(grp(_RE_CH, "0")),
            "oui": grp(_RE_OUI),
            "detection_method": "",
        }
        return ParsedEvent(event_type="alpr_found", data=data, raw=line)

    # ── Commands (none — passive detector) ───────────────────────────
    def get_commands(self) -> list[CommandInfo]:
        # Flock-You takes no serial commands: it just listens and reports. Nothing to send.
        return []

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            return f"{cmd} " + " ".join(str(v) for v in args.values())
        return cmd

    def identify(self, line: str) -> bool:
        low = (line or "").lower()
        if "[flockyou]" in low:
            return True
        return '"detection_method"' in low and '"mac_address"' in low
