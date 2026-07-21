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
*gated* operate surface. The wire verbs are the UPPERCASE tokens the firmware's dispatcher
matches (``protocol.h`` / ``serial_cmd.cpp``): ``SET-ENGINE``, ``SET-BAND``, ``SET-POWER``,
``SET-SWEEP``, ``ATTEST``, ``ARM``, ``CONFIRM``, ``START``, ``CHAR``, ``STOP``, ``TLM``,
``LOG``, ``CAL-SET``, ``CAL-COMMIT``, ``CAL-CLEAR``. There is NO ``STATUS``/``BANDS``/``FLOOD``/
``OFF`` command — the earlier catalog sent those lowercase tokens and the firmware answered
every one with ``unknown-cmd`` (dead buttons), so they are removed here.

* ``driver_type = "text-cli"`` — an honest statement that it HAS a sendable command channel. NOT
  ``controlmap``, which ``network_tab.py`` renders as a "web-UI / no serial commands" badge —
  true for BlueJammer/nRF but a lie for BlueStress, which genuinely has a serial CLI.
* :meth:`get_commands` exposes the real dispatcher verbs. The config/safety/status/calibration
  verbs key no carrier (``danger=""``). The two emit verbs — ``START`` (keys the carrier) and
  ``CHAR`` (transmits a scripted stimulus) — carry ``danger="illegal-tx"`` so
  :func:`src.core.safety.classify` gates them behind the illegal-tx consent dialog. ``STOP`` is
  the SAFE always-accepted cease action, always reachable ungated.
* The offensive chain is the firmware's own two-factor arm: ``ATTEST`` -> ``ARM`` -> ``CONFIRM``
  -> ``START`` (``START`` is refused ``not-armed`` otherwise). CC surfaces each verb as its own
  button rather than a fictional single command, so an operator walks the real sequence; the
  arm state machine lives in the firmware, not here (``supports_arm`` stays False so the emit
  buttons are confirm-gated at send, never dead-ended waiting on an arm event CC can't observe).
* :meth:`parse_line` mirrors ``bluejammer`` — bracketed tags become structured events (``[TX]``
  -> a live-carrier warning, ``[IDLE]``/``[OFF]`` -> stopped, ``[ERR]``/``[FAIL]`` -> failed
  status; else read-only info).
* :meth:`identify` claims the firmware on its boot banner / idle state line.

Defence-in-depth: CC's consent gate is only the SOFTWARE gate. The firmware itself boots idle
and keys the carrier ONLY after the two-factor arm completes — the HARDWARE gate — and ``STOP``
always drops TX and returns to idle. ``SET-POWER`` is ceiling-clamped by the firmware's
calibrated caps; it can never widen power beyond the built-in limit. CC authors no transmit
payload and adds no RF power. The flash profile lives in
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
        # Every verb below is a REAL dispatcher token (protocol.h / serial_cmd.cpp). The bare
        # UPPERCASE name IS the wire command (the Operate console writes it verbatim, seeding the
        # `args` string for a prompt when one is present). No lowercase Status/Bands/Flood/Off —
        # the firmware has no such commands.
        return [
            # ── Config (no carrier keyed; danger="") ─────────────────────────
            CommandInfo(
                name="SET-ENGINE",
                category="config",
                description=(
                    "Select the active RF engine (e.g. base_nrf24 2.4 GHz vs coproc_proxy incl. "
                    "5 GHz via co-processor/SDR). Precondition for the whole config surface."
                ),
                args="<engine-id, e.g. base_nrf24 | coproc_proxy>",
            ),
            CommandInfo(
                name="SET-BAND",
                category="config",
                description="Set the operating band, single or dual (the real setter).",
                args="<2g4|5g|2g4+5g|both>",
            ),
            CommandInfo(
                name="SET-POWER",
                category="config",
                description="Set calibrated, ceiling-clamped output power by index or in dBm.",
                args="<index | <n>dbm>",
            ),
            CommandInfo(
                name="SET-SWEEP",
                category="config",
                description="Configure swept span/step/dwell for a band-occupancy survey.",
                args="<span_khz> <step_khz> <dwell_ms>",
            ),

            # ── Safety attestation (no carrier keyed; danger="") ─────────────
            CommandInfo(
                name="ATTEST",
                category="safety",
                description=(
                    "Present the lab-license attestation token (first precondition of the "
                    "two-factor arm). ARM is refused 'not-attested' until this passes."
                ),
                args="<lab-license token>",
            ),

            # ── Operate: two-factor arm then emit ────────────────────────────
            # ARM/CONFIRM key no carrier (danger=""); START/CHAR emit RF (danger="illegal-tx").
            CommandInfo(
                name="ARM",
                category="operate",
                description="Open the arm window (factor 1 of the two-factor arm). No carrier is keyed.",
            ),
            CommandInfo(
                name="CONFIRM",
                category="operate",
                description="Factor 2 of the two-factor arm; transitions to ARMED. No carrier is keyed.",
            ),
            CommandInfo(
                name="START",
                category="operate",
                description=(
                    "Key the carrier / begin emitting (ARMED-ONLY; refused 'not-armed' otherwise). "
                    "Illegal to operate on air (FCC 47 U.S.C. 333); authorized RF-shielded lab only."
                ),
                danger="illegal-tx",
            ),
            CommandInfo(
                name="CHAR",
                category="operate",
                description=(
                    "Characterize mode: run a scripted sweep that TRANSMITS a known stimulus to "
                    "characterize RX/detector resilience (ARMED-ONLY). Emits RF; authorized "
                    "RF-shielded lab only."
                ),
                args="<script>",
                danger="illegal-tx",
            ),

            # ── Control: always-accepted cease (danger="") ───────────────────
            CommandInfo(
                name="STOP",
                category="control",
                description=(
                    "Always-accepted cease: drop TX on the host and every co-proc/SDR chain, "
                    "return to idle (always reachable)."
                ),
            ),

            # ── Status / telemetry (read-only; danger="") ────────────────────
            CommandInfo(
                name="TLM",
                category="status",
                description=(
                    "Live telemetry sample: state, band, center_khz, fwd/refl dBm, temperature, "
                    "cal status (measured/estimated)."
                ),
            ),
            CommandInfo(
                name="LOG",
                category="status",
                description=(
                    "Query the append-only audit ring (records/dropped/newest); 'LOG PRIOR' "
                    "returns the persisted prior-session forensic snapshot that survives reboot."
                ),
                args="[PRIOR]",
            ),

            # ── Calibration (idle-only; no carrier keyed; danger="") ─────────
            CommandInfo(
                name="CAL-SET",
                category="calibration",
                description="Stage one measured power-calibration point in RAM (idle-only).",
                args="<index> <dbm>",
            ),
            CommandInfo(
                name="CAL-COMMIT",
                category="calibration",
                description="Persist the staged calibration set to NVS, keyed by engine id (idle-only).",
            ),
            CommandInfo(
                name="CAL-CLEAR",
                category="calibration",
                description="Wipe calibration (RAM + NVS) back to honest 'estimated' (idle-only).",
            ),
        ]

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        # The command names ARE the firmware's wire tokens (uppercase SET-ENGINE/START/STOP/…),
        # so there is no lowercase translation to do — pass the verb through and append any args.
        # (The Operate console prompts for the full argument string and sends it verbatim; this
        # path serves the macro/broadcast callers that supply an args dict.)
        name = (cmd or "").strip()
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{name} {arg_str}".rstrip()
        return name

    def identify(self, line: str) -> bool:
        """Recognise BlueStress on its boot banner / idle state line."""
        markers = ("BlueStress", "BLUESTRESS", "[SYS] idle")
        return any(m in line for m in markers)
