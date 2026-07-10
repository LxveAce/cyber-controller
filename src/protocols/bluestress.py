"""BlueStress protocol — gated serial CLI for LxveLabs' in-house RF-disruption firmware.

BlueStress (LxveLabs) is an ESP32-WROOM-32 driving 1-2 nRF24L01+PA/LNA radios as a 2.4 GHz /
BLE constant-carrier & sweep RF-disruption device. It is built by SUBTRACTING the on-boot
transmit from two source-available upstream nRF24 primitives (wirebits/nrfBlueNullifier,
GPL-3.0; smoochiee's "Noisy-boy" as a behaviour/pin reference only) and ADDING a boot-idle
control envelope. Operating it on the air is **illegal** (FCC 47 U.S.C. 333); it is integrated
here ONLY for authorized, RF-shielded lab study on hardware you own.

Unlike the fire-on-boot upstream jammers CC treats as no-ops (``nrf_bluenullifier2`` /
``bluejammer``: ``driver_type="controlmap"``, empty ``get_commands``), BlueStress **boots IDLE
and exposes a real line-based serial CLI**, so this is a genuine ``text-cli`` protocol with a
*gated* operate surface:

* ``driver_type = "text-cli"`` — an honest statement that it HAS a sendable command channel. NOT
  ``controlmap``, which ``network_tab.py`` renders as a "web-UI / no serial commands" badge —
  true for BlueJammer/nRF but a lie for BlueStress, which genuinely has a serial CLI.
* :meth:`get_commands` returns four real commands: two safe read-only helpers (``Status`` /
  ``Bands``) and two operate controls — ``Flood`` (``danger="illegal-tx"`` so
  :func:`src.core.safety.classify` gates it behind the illegal-tx consent dialog) and ``Off``
  (a SAFE cease action, always reachable ungated per the cease-actions-never-gated directive).
* :meth:`parse_line` mirrors ``bluejammer`` — bracketed tags become structured events (``[TX]``
  -> a live-carrier warning, ``[IDLE]``/``[OFF]`` -> stopped, ``[ERR]``/``[FAIL]`` -> failed
  status; else read-only info).
* :meth:`identify` claims the firmware on its boot banner / idle state line.

Defence-in-depth: CC's consent gate is only the SOFTWARE gate. The firmware itself boots idle
and transmits ``flood <band>`` ONLY while physically armed (button hold / a recent ``arm``
within timeout) — the HARDWARE gate — and ``off`` always disarms + stops + returns to idle. The
band argument NARROWS channels; it can never widen power beyond the built-in ``RF24_PA_MAX``. CC
authors no transmit payload and adds no RF power. The flash profile lives in
``src/config/profiles/bluestress.json``.
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# Bracketed status tags BlueStress emits, e.g. "[SYS] idle", "[BAND] ble-adv 37/38/39",
# "[TX] carrier live", "[OFF] disarmed", "[ERR] not armed". Tolerant like bluejammer's parser.
_RE_TAG = re.compile(r"^\[(?P<tag>[A-Za-z0-9_]+)\]\s*(?P<msg>.*)$")


class BlueStressProtocol(BaseProtocol):
    """Gated text-CLI parser for BlueStress (boots idle; Flood=illegal-tx, Off=safe stop)."""

    # BlueStress has a REAL line-based serial CLI (unlike the controlmap no-op jammers).
    driver_type = "text-cli"
    # 2.4 GHz / BLE disruption over nRF24 — surfaced as node capability chips.
    capabilities = frozenset({"ble", "nrf24", "jammer"})
    # \n-terminated CLI (Serial.begin(115200), newline-submitted commands).
    line_ending = "\n"

    @property
    def protocol_name(self) -> str:
        return "bluestress"

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None
        m = _RE_TAG.match(line)
        if not m:
            # Non-bracketed chatter (banner text, HELP output) — surfaced read-only as info.
            return ParsedEvent(event_type="info", data={"message": line}, raw=line)
        tag = m.group("tag").upper()
        msg = m.group("msg").strip()
        data: dict[str, object] = {"tag": tag}
        if msg:
            data["message"] = msg
        if tag == "TX":
            # Carrier is LIVE — the loud, honest "RF is transmitting" event.
            data["live"] = True
            return ParsedEvent(event_type="warning", data=data, raw=line)
        if tag in ("IDLE", "OFF"):
            # Disarmed / carrier stopped — back to the safe idle state.
            data["live"] = False
            return ParsedEvent(event_type="stopped", data=data, raw=line)
        if tag in ("ERR", "FAIL", "FAULT"):
            data["ok"] = False
            return ParsedEvent(event_type="status", data=data, raw=line)
        # [SYS], [BAND], and any other bracketed tag are read-only info telemetry.
        return ParsedEvent(event_type="info", data=data, raw=line)

    def get_commands(self) -> list[CommandInfo]:
        return [
            CommandInfo(
                name="Status",
                category="status",
                description="Show state, engine, band/channel, arm flags, watchdog (read-only)",
            ),
            CommandInfo(
                name="Bands",
                category="status",
                description="List the selectable band-narrowing presets (read-only)",
            ),
            CommandInfo(
                name="Flood",
                category="operate",
                description=(
                    "Begin 2.4 GHz/BLE RF disruption on the given band (ARMED-ONLY). Illegal to "
                    "operate on air (FCC 47 U.S.C. 333); authorized RF-shielded lab only."
                ),
                args="<band-id, e.g. ble-adv-37-38-39>",
                danger="illegal-tx",
            ),
            CommandInfo(
                name="Off",
                category="control",
                description="Disarm, stop the carrier, return to idle (always reachable)",
            ),
        ]

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        name = (cmd or "").strip()
        low = name.lower()
        # Flood is the one operate command that carries a band-id argument.
        if low == "flood":
            band = ""
            if args:
                band = str(args.get("band") or next(iter(args.values()), "")).strip()
            return f"flood {band}".rstrip()
        # The other three known commands map to their bare lowercase wire tokens.
        if low in ("status", "bands", "off"):
            return low
        # Anything else (free-typed serial) passes through verbatim; append any args.
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{name} {arg_str}"
        return name

    def identify(self, line: str) -> bool:
        """Recognise BlueStress on its boot banner / idle state line."""
        markers = ("BlueStress", "BLUESTRESS", "[SYS] idle")
        return any(m in line for m in markers)
