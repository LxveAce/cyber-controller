"""LxveOS protocol — serial parser for the LxveOS headless control surface.

LxveOS (``LxveAce/lxveos``) is an ESP-IDF security-panel OS. Its esp_console CLI is the headless
control surface on every board. Two machine-readable surfaces feed the Cyber Controller bridge, both
using the ``LXVEOS/<v>`` framing (see the firmware ``docs/EVENT-PROTOCOL.md`` +
``LXVEOS-CC-CONTROL-SPEC.md``):

1. The ``status`` line — one versioned identity/telemetry line, always available, polled for the header:

       LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 panel=none \
           caps=0x007 ops=12/3/6 heap=184988 arm=safe

2. Event lines — emitted by the recon/defense/capture/arm ops when the operator (or CC) runs ``bridge on``:

       LXVEOS/1 ap bssid=de:ad:be:ef:00:01 ssid=4d794e6574 ch=6 rssi=-42 auth=wpa2
       LXVEOS/1 hs kind=pmkid bssid=de:ad:be:ef:00:01 essid=4e6574
       LXVEOS/1 arm state=pending token=123456789 window=30

Free-text / arbitrary-byte fields (SSIDs, BLE names) are HEX-encoded on the wire so a space in an SSID
can't break the space-split line; this parser decodes them back to text (keeping the raw hex as
``<field>_hex``). Every line keys on FIELD NAMES, not position, and unknown event types / unknown keys
are surfaced rather than dropped — so a newer firmware never breaks an older CC.

The ``info`` command still prints a four-line human summary, accumulated into one ``device_info``.
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# General CC bridge line: `LXVEOS/<v> <type> <space-separated key=value>`. `status` is one type; the
# recon/defense/capture/arm ops emit other types (ap/sta/probe/ble/hs/pcap/arm/alert/bridge/done/snapshot).
# Digit runs are bounded (\d{1,20}, not \d+): a hostile 4301-digit run would otherwise hit Python's
# ~4300-digit str->int limit and raise ValueError out of the parse. 20 digits fits any 64-bit value;
# an over-long run fails to match here (dropped clean) while a real version parses — and a legit
# up-to-10-digit arm token is never truncated.
_RE_EVENT = re.compile(r"^LXVEOS/(\d{1,20})\s+(\w+)\s*(.*)$")
# key=value pairs; value may be EMPTY (e.g. a hidden SSID emits `ssid=`), so `\S*` not `\S+`.
_RE_KV = re.compile(r"(\w+)=(\S*)")

# `info` command lines: `key<pad>: value` (the firmware left-pads the key to a fixed width).
_RE_INFO_FW = re.compile(r"^fw\s*:\s*LxveOS\s+(\S+)\s*$")
_RE_INFO_BOARD = re.compile(r"^board\s*:\s*(\S+)\s*$")
_RE_INFO_CHIP = re.compile(r"^chip\s*:\s*(\S+)\s*$")
_RE_INFO_UI = re.compile(r"^ui\s*:\s*(\S+)\s*$")

# The linenoise REPL prompt.
_RE_PROMPT = re.compile(r"^lxveos>\s*$")

# Arm gate (LXVEOS-CC-CONTROL-SPEC §4) — the human-prose replies of the `arm`/`disarm` commands, parsed to
# arm_state events for the ARM/SAFE UI. (The firmware ALSO emits a structured `LXVEOS/1 arm state=..` event
# when the bridge is on; that path is handled by the general dispatch — this is the fallback for the prose.)
_RE_ARM_REQUEST = re.compile(r"arm\s+requested.*?\barm\s+(\d{1,20})", re.IGNORECASE | re.DOTALL)
_RE_ARM_TXOFF = re.compile(r"compiled\s+OUT", re.IGNORECASE)
_RE_ARM_STATE = re.compile(r"^arm\s+state:\s*(\w+)", re.IGNORECASE)
_RE_ARM_ARMED = re.compile(r"\bARMED\b")  # the confirm reply; \b keeps it from matching DISARMED

# LxveOS capability bitmask -> slug, in the EXACT bit order of the firmware's `lxveos_cap_t` enum
# (components/lxveos_caps: WIFI=bit0, BLE=1, BT_CLASSIC=2, DISPLAY=3, STORAGE=4, GPS=5, IR_RX=6,
# SUBGHZ=7, NRF24=8, NFC=9, WIFI_5GHZ=10, IR_TX=11). IR splits into ir_rx (bit 6, the old `ir` slot)
# and ir_tx (bit 11, appended so no existing bit moves) -- a board can have an IR receiver but no
# emitter. Verified against source and a live board (COM23 caps=0x007 -> wifi/ble/bt_classic).
_CAP_SLUGS = (
    "wifi", "ble", "bt_classic", "display", "storage", "gps", "ir_rx", "subghz", "nrf24", "nfc",
    "wifi_5ghz", "ir_tx",
)

# Event type -> (CC event_type, int-typed fields, hex-encoded free-text fields). MAC-shaped fields
# (bssid/mac/ap/addr/sta) stay strings — the `aa:bb:..` form is already parse-safe. Any type not here is
# surfaced as a forward-compat `info` event rather than dropped.
_EVENT_MAP: dict[str, tuple[str, "frozenset[str]", "frozenset[str]"]] = {
    "ap":       ("ap_found",           frozenset({"ch", "rssi"}),                    frozenset({"ssid"})),
    "sta":      ("client_found",       frozenset({"rssi", "frames"}),                frozenset({"essid"})),
    "probe":    ("probe_request",      frozenset({"rssi", "seen"}),                  frozenset({"ssid"})),
    "ble":      ("ble_found",          frozenset({"rssi", "appr", "company", "fp", "tracker"}), frozenset({"name"})),
    "hs":       ("handshake_captured", frozenset(),                                  frozenset({"essid"})),
    "pcap":     ("pcap_saved",         frozenset({"bytes"}),                         frozenset()),
    "arm":      ("arm_state",          frozenset({"token", "window", "idle"}),       frozenset()),
    # `tracker/flock/meta/flipper/skimmer` are the kind=surveil sweep subcounts (one uint tally per
    # category) — typed here so they arrive as ints, not strings.
    "alert":    ("alert",              frozenset({"count", "bssids", "rate", "deauth", "disassoc",
                                                   "open", "enc", "grade", "wps", "uniq", "rssi",
                                                   "tracker", "flock", "meta", "flipper", "skimmer"}),
                                        frozenset({"ssid", "name"})),
    "bridge":   ("bridge_state",       frozenset(),                                  frozenset()),
    "done":     ("batch_done",         frozenset({"n"}),                             frozenset()),
    "snapshot": ("snapshot",           frozenset({"aps", "open", "wps", "bles", "trackers"}),
                                        frozenset()),
}


def _decode_caps(mask: int) -> list[str]:
    """Expand the ``caps`` bitmask into capability slugs (firmware bit order). A set bit beyond the
    known range is surfaced as ``capN`` rather than dropped, so a future capability the firmware
    reports isn't silently lost (same forward-compat posture as the unknown-key handling)."""
    out = []
    bit = 0
    while (1 << bit) <= mask:
        if mask & (1 << bit):
            out.append(_CAP_SLUGS[bit] if bit < len(_CAP_SLUGS) else f"cap{bit}")
        bit += 1
    return out


def _hex_field(val: str) -> tuple[str, str]:
    """Decode a hex-encoded event field to (text, raw_hex). SSIDs/names are hex on the wire; decode
    best-effort to UTF-8 text (undecodable bytes -> U+FFFD) and keep the raw hex alongside. A value that
    isn't valid hex is returned unchanged for both (forward-compat with a future non-hex field)."""
    try:
        raw = bytes.fromhex(val)
    except ValueError:
        return val, val
    return raw.decode("utf-8", "replace"), val


def _coerce_status_field(key: str, val: str):
    """Type the known ``status`` fields; leave any unknown (future) key as a raw string."""
    if key == "caps":  # hex capability bitmask, e.g. 0x007
        try:
            mask = int(val, 16)
        except ValueError:
            return val
        # A real caps bitmask is a few bits. A hostile/spoofed device can send a 64 KiB caps= hex string,
        # and _decode_caps is O(bits^2) big-int work on the serial reader thread (DoS). Cap at 64 bits
        # (ample for forward-compat capN); an over-large value stays a raw string.
        if mask < 0 or mask.bit_length() > 64:
            return val
        return mask
    if key == "heap":  # free-heap bytes (decimal)
        try:
            return int(val)
        except ValueError:
            return val
    if key == "ops":  # ready/planned/unavailable operation tally
        parts = val.split("/")
        # len<=20 bounds int() against a hostile 4301-digit run (isdigit() alone imposes no length
        # limit, so a huge run would pass the guard and raise ValueError on int()).
        if len(parts) == 3 and all(p.isdigit() and len(p) <= 20 for p in parts):
            return {"ready": int(parts[0]), "planned": int(parts[1]), "unavailable": int(parts[2])}
        return val
    if key == "arm":  # runtime arm-gate state. A build-time TX-lockout shows up as tx=0, so arm= is
        # one of the three runtime states. The allow-list documents the known set; an unknown/future
        # token still passes through raw so a newer firmware never breaks an older CC.
        if val not in ("safe", "pending", "armed"):
            return val  # forward-compat: surface an unrecognised state verbatim, don't drop it
        return val
    if key == "tx":  # offensive-TX build flag: 1 = compiled in (can arm), 0 = stripped (LXVEOS_TX_DISABLE)
        if val in ("0", "1"):
            return val == "1"  # bool: the TX-lockout UI needs "can this unit ever transmit" distinct from arm=
        return val
    return val


class LxveOSProtocol(BaseProtocol):
    """Parser + command formatter for LxveOS's headless esp_console surface."""

    # LxveOS reports its real capabilities at RUNTIME via the status line's `caps=` bitmask; no static
    # capability tokens are claimed here (declaring e.g. "wifi" would be a guess). Consumers read caps from
    # the parsed device_info.
    capabilities: "frozenset[str]" = frozenset()
    driver_type = "text-cli"
    supports_arm = True  # LxveOS implements the two-factor arm/token/disarm handshake (arm_state events).

    def __init__(self) -> None:
        # `info` prints four separate lines with no terminator line, so accumulate them across parse_line
        # calls and emit one device_info on the closing `ui :` line (the last field).
        self._info_record: dict = {}

    @property
    def protocol_name(self) -> str:
        return "lxveos"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # Any `LXVEOS/<v> <type> ...` line — status (identity) or a recon/defense/capture/arm event.
        m = _RE_EVENT.match(line)
        if m:
            return self._parse_event(int(m.group(1)), m.group(2), m.group(3), line)

        # Multi-line `info` output. Fields arrive on separate lines; accumulate and emit one device_info on
        # the closing `ui :` line. A stray board/chip/ui with no in-progress record is ignored (falls through
        # to benign info) rather than emitting a half-built identity.
        m = _RE_INFO_FW.match(line)
        if m:
            self._info_record = {"fw": m.group(1), "source": "info_cmd"}
            return None
        m = _RE_INFO_BOARD.match(line)
        if m and self._info_record:
            self._info_record["board"] = m.group(1)
            return None
        m = _RE_INFO_CHIP.match(line)
        if m and self._info_record:
            self._info_record["chip"] = m.group(1)
            return None
        m = _RE_INFO_UI.match(line)
        if m and self._info_record:
            rec, self._info_record = self._info_record, {}
            rec["ui"] = m.group(1)
            return ParsedEvent(event_type="device_info", data=rec, raw=line)

        # The REPL prompt — a readiness signal, not noise.
        if _RE_PROMPT.match(line):
            return ParsedEvent(event_type="status", data={"prompt": True}, raw=line)

        # Arm-gate prose (spec §4) -> arm_state, so the ARM/SAFE UI tracks state even without `bridge on`.
        m = _RE_ARM_REQUEST.search(line)
        if m:
            return ParsedEvent("arm_state", {"state": "pending", "token": int(m.group(1)), "window": 30}, line)
        if _RE_ARM_TXOFF.search(line):
            return ParsedEvent("arm_state", {"state": "tx_disabled"}, line)
        m = _RE_ARM_STATE.match(line)
        if m:
            return ParsedEvent("arm_state", {"state": m.group(1).lower()}, line)
        if _RE_ARM_ARMED.search(line):
            return ParsedEvent("arm_state", {"state": "armed"}, line)

        # Anything else (boot log, help text, ack-gate messages) — surfaced as benign info.
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    def _parse_event(self, version: int, etype: str, rest: str, raw: str) -> ParsedEvent:
        """Dispatch one `LXVEOS/<version> <etype> <rest>` line to a typed event."""
        kv = dict(_RE_KV.findall(rest))

        if etype == "status":
            data: dict = {"proto_version": version, "source": "status_line"}
            for key, val in kv.items():
                data[key] = _coerce_status_field(key, val)
            if isinstance(data.get("caps"), int):
                data["caps_tokens"] = _decode_caps(data["caps"])
            return ParsedEvent(event_type="device_info", data=data, raw=raw)

        mapping = _EVENT_MAP.get(etype)
        if mapping is None:
            # Forward-compat: unknown event type — keep it (name + fields) rather than dropping.
            return ParsedEvent(
                event_type="info",
                data={"lxveos_event": etype, "fields": kv, "proto_version": version},
                raw=raw,
            )

        cc_type, int_fields, hex_fields = mapping
        data = {"proto_version": version}
        for key, val in kv.items():
            if key in int_fields:
                try:
                    data[key] = int(val)
                except ValueError:
                    data[key] = val
            elif key in hex_fields:
                text, raw_hex = _hex_field(val)
                data[key] = text
                data[key + "_hex"] = raw_hex
            else:
                data[key] = val
        # `hs` carries the raw hashcat-22000 `line` (kept verbatim for Crack Lab); surface its ESSID for a
        # human display name. Format: WPA*<01|02>*<pmkid|mic>*<ap>*<sta>*<essid_hex>*... — field 5 is the
        # SSID, hex-encoded. Best-effort: a malformed line just leaves essid unset.
        if etype == "hs" and isinstance(data.get("line"), str):
            parts = data["line"].split("*")
            if len(parts) > 5 and parts[5]:
                data["essid"], data["essid_hex"] = _hex_field(parts[5])
        return ParsedEvent(event_type=cc_type, data=data, raw=raw)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """The full LxveOS command surface (LXVEOS-CC-CONTROL-SPEC §5). Offensive ops carry
        ``danger="lab-only"``; LxveOS ships NO interference emitter, so nothing is ``illegal-tx``. The
        multi-purpose radio commands (subghz/nrf24/nfc/ir) stay danger-free at the command level — their
        offensive SUBcommands (replay/mousejack/clone) are caught by safety.classify's keyword scan on the
        actual line sent."""
        C = CommandInfo
        return [
            # System / housekeeping
            C("help", "System", "List all registered commands"),
            C("agree", "System", "Accept the authorized-use terms (unlocks the command set this session)"),
            C("info", "System", "Human-readable fw/board/chip/ui summary"),
            C("status", "System", "One machine-readable status line (CC bridge format)"),
            C("bridge", "System", "Toggle LXVEOS/1 event emission for the CC bridge", args="on|off|status"),
            C("caps", "System", "List the active capability registry"),
            C("features", "System", "Operation catalog (ready/planned/unavailable per op)"),
            C("sysinfo", "System", "Chip / reset-reason / heap system details"),
            C("loglevel", "System", "Set ESP-IDF log verbosity", args="<tag|*> <level>"),
            C("nvs", "System", "Operator key/value store", args="get|set <key> [value]"),
            C("reboot", "System", "Reboot the device"),
            # Recon — Wi-Fi (passive, listen-only)
            C("scan", "Recon-WiFi", "Passive Wi-Fi AP scan"),
            C("sniff", "Recon-WiFi", "Passive packet monitor (frame-type tally)", args="[seconds] [channel]"),
            C("stations", "Recon-WiFi", "Passive client-station scan", args="[seconds] [channel]"),
            C("probes", "Recon-WiFi", "Probe-request SSID logger", args="[seconds] [channel]"),
            C("capture", "Recon-WiFi", "EAPOL/PMKID capture -> hashcat 22000", args="[seconds] [channel]"),
            C("wardrive", "Recon-WiFi", "Wardrive CSV export (bssid,ssid,ch,rssi,auth)"),
            # Recon — BLE / add-on radios
            C("blescan", "Recon-BLE", "BLE device scan (+vendor/appearance/service-UUIDs)", args="[seconds]"),
            C("blewardrive", "Recon-BLE", "BLE wardrive CSV (addr,name,rssi,vendor,tracker)"),
            C("subghz", "Recon-Radio", "CC1101 sub-GHz (add-on module)",
              args="begin <sclk> <miso> <mosi> <cs> | rssi <mhz> | capture <gdo0> <mhz> [s] | replay <gdo0> | end"),
            C("nrf24", "Recon-Radio", "nRF24 2.4GHz (add-on module)",
              args="begin <sck> <miso> <mosi> <csn> <ce> | scan | sniff | mousejack <text> | end"),
            C("nfc", "Recon-Radio", "PN532 NFC (add-on module)",
              args="begin <sda> <scl> | read [seconds] | clone <8hexUID> | end"),
            C("ir", "Recon-Radio", "IR capture + replay (universal remote)",
              args="recv <rx_gpio> [s] | send <tx_gpio> | show"),
            # Defense (passive detectors)
            C("defend", "Defense", "Deauth/disassoc attack detector", args="[seconds] [channel]"),
            C("pwnwatch", "Defense", "Pwnagotchi-presence detector", args="[seconds] [channel]"),
            C("eviltwin", "Defense", "Evil-twin / rogue-AP detector"),
            C("apaudit", "Defense", "AP security audit (open/WEP/legacy-WPA/WPS)"),
            C("bleflood", "Defense", "BLE advert-flood/spam detector", args="[seconds]"),
            C("btracker", "Defense", "BLE item-tracker/stalking detector", args="[seconds]"),
            C("flipper", "Defense", "Flipper Zero detector (BLE service-UUID)", args="[seconds]"),
            C("meta", "Defense", "Meta / Ray-Ban + Oculus detector (BLE)", args="[seconds]"),
            C("skimmer", "Defense", "Card-skimmer heuristic (HC-0x BT-serial)", args="[seconds]"),
            C("flock", "Defense", "Flock camera heuristic (BLE, experimental)", args="[seconds]"),
            C("surveil", "Defense", "Counter-surveillance BLE sweep", args="[seconds]"),
            C("blehid", "Defense", "Flag nearby BLE HID devices (rogue keyboards/injectors)", args="[seconds]"),
            C("airspace", "Defense", "Airspace occupancy summary (APs, open/WPS, BLE, trackers)"),
            C("watch", "Defense", "Watchlist — flag when a watched BSSID/BLE-addr is present",
              args="add <mac> [label] | del <mac> | list | clear | scan [seconds]"),
            # Arm gate (the safety mechanism itself transmits nothing)
            C("arm", "Offensive", "Two-factor enable for offensive-TX ops", args="[token|status]"),
            C("disarm", "Offensive", "Hard-disarm: return to SAFE"),
            # Offensive (need arm; lab-only — never illegal-tx, LxveOS ships no interference emitter)
            C("evilportal", "Offensive", "Rogue AP + captive portal (needs arm)",
              args="[ssid|karma|template <id>|templates|creds|stop]", danger="lab-only"),
            C("badble", "Offensive", "BLE HID keystroke injection (needs arm)",
              args='"<duckyscript>" | stop | status', danger="lab-only"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like LxveOS output."""
        return (
            line.startswith("LXVEOS/")
            or "LxveOS" in line
            or bool(_RE_PROMPT.match(line.strip()))
        )
