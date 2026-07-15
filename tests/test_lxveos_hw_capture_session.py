"""LxveOS ↔ Cyber Controller integration, pinned to REAL SILICON output (B17).

Every line below is a VERBATIM capture from a physical ESP32-D0WD-V3 running the ci-latest LxveOS
build (app 5b1ced9, board ``bare_esp32_headless``), flashed and probed over COM23 during the
2026-07-15 hardware-validation. The synthetic session test (``test_lxveos_bridge_session.py``) hand-
shapes lines from the protocol spec; THIS one pins the CC side to what the firmware actually emits,
so a firmware output drift the spec-shaped fixtures would miss shows up here as a failure.

Covers the end-to-end lifecycle the FINISH-MISSION B17 calls for — identify → status → info/caps →
agree (onboarding) → features → arm — through the real ``LxveOSProtocol`` + ``TargetIngestor`` +
``Device``, so the whole CC seam is asserted against ground truth rather than a model.
"""
from __future__ import annotations

from src.protocols.lxveos import LxveOSProtocol

# ── verbatim COM23 capture (LxveOS 0.1.0-m0, app 5b1ced9, bare_esp32_headless) ──

# Boot banner lines the firmware logs before the CLI is ready.
_BOOT_BANNER = "I (653) lxveos: LxveOS 0.1.0-m0 — by LxveLabs, built by LxveAce"
_BOOT_BOARD = "I (666) lxveos_board: board 'bare_esp32_headless' (chip esp32, ui 'headless')"
_BOOT_READY = "I (685) lxveos_cli: LxveOS ready on 'bare_esp32_headless' (ui: headless, boot #2)."
# Generic ESP-IDF 2nd-stage bootloader noise — must NOT be mistaken for LxveOS.
_BOOT_IDF = "I (27) boot: ESP-IDF v6.0.2 2nd stage bootloader"

# `agree` unlock reply (onboarding gate) — human prose, not an event line. Verbatim capture.
_AGREE_REPLY = "Acknowledged. Authorized, lawful security research & education only. Commands unlocked."  # noqa: E501

# The real machine-readable status line (note ops=17/7/12, heap=157244 — the live values, not the
# synthetic test's 12/3/6 / 184988).
_STATUS = (
    "LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 "
    "panel=none caps=0x007 ops=17/7/12 heap=157244 arm=safe tx=1"
)
_INFO = [
    "fw    : LxveOS 0.1.0-m0", "board : bare_esp32_headless", "chip  : esp32", "ui    : headless",
]
_PROMPT = "lxveos>"

# A representative slice of the real `features` catalog reply (the firmware prints one line per op).
_FEATURES = [
    "LxveOS operation catalog (CI-green, not yet hardware-validated; attack ops are arm-gated, lab-only)",  # noqa: E501
    "  [ready      ] recon    std        wifi_ap_scan   Wi-Fi AP scan          (wifi, ~Marauder)",
    "  [ready      ] recon    std        airspace_summary Airspace occupancy summary (wifi, ~custom)",  # noqa: E501
    "  [ready      ] attack   offensive  evil_portal    Evil-portal captive portal (wifi, ~Marauder)",  # noqa: E501
    "  [planned    ] attack   restricted deauth_burst   Deauth/disassoc burst  (wifi, ~Marauder)",
]


# ── identify: auto-detection on real boot/CLI output ─────────────────

def test_identify_matches_real_lxveos_lines_and_rejects_idf_noise():
    p = LxveOSProtocol()
    # Auto-detect keys on the branded banner ("LxveOS"), the LXVEOS/ status framing, the `info` fw
    # line, and the linenoise prompt — the surfaces a connect-time probe actually reads.
    for line in (_BOOT_BANNER, _BOOT_READY, _STATUS, _INFO[0], _PROMPT):
        assert p.identify(line) is True, f"should identify as LxveOS: {line!r}"
    # Generic ESP-IDF bootloader output must NOT be claimed as LxveOS (it precedes any firmware).
    assert p.identify(_BOOT_IDF) is False
    # A lowercase IDF component-log tag (`lxveos_board:`, no branded "LxveOS") is deliberately NOT a
    # detection surface — auto-detect uses the banner/status/prompt above, not every log line.
    assert p.identify(_BOOT_BOARD) is False


# ── status: the real telemetry values type correctly ─────────────────

def test_real_status_line_types_live_values():
    ev = LxveOSProtocol().parse_line(_STATUS)
    assert ev is not None and ev.event_type == "device_info"
    d = ev.data
    assert d["board"] == "bare_esp32_headless" and d["chip"] == "esp32" and d["ui"] == "headless"
    assert d["fw"] == "0.1.0-m0"
    assert d["caps"] == 0x007 and d["caps_tokens"] == ["wifi", "ble", "bt_classic"]
    # the LIVE op tally + heap (differ from the synthetic fixture — this pins the real values)
    assert d["ops"] == {"ready": 17, "planned": 7, "unavailable": 12}
    assert d["heap"] == 157244
    # arm gate + TX capability: the board is TX-capable but currently SAFE
    assert d["arm"] == "safe" and d["tx"] is True


# ── info block: the 4-line identity accumulates to one device_info ───

def test_real_info_block_accumulates_to_identity():
    p = LxveOSProtocol()
    for line in _INFO[:-1]:
        assert p.parse_line(line) is None  # accumulating
    ev = p.parse_line(_INFO[-1])           # closing ui line emits
    assert ev is not None and ev.event_type == "device_info"
    assert ev.data["board"] == "bare_esp32_headless" and ev.data["fw"] == "0.1.0-m0"


# ── agree + features: prose/catalog never crash or misclassify ───────

def test_agree_and_features_lines_are_benign_info_not_events():
    # The onboarding `agree` reply and the `features` catalog are human prose, not LXVEOS/1 event
    # lines. They must parse without raising and must NOT be misread as a typed event (ap/arm/etc.).
    p = LxveOSProtocol()
    for line in [_AGREE_REPLY, *_FEATURES]:
        ev = p.parse_line(line)
        # either dropped (None) or surfaced as generic info/status — never a structured event
        if ev is not None:
            assert ev.event_type in ("info", "status"), f"{line!r} -> {ev.event_type}"


# ── full lifecycle through the real pipeline ─────────────────────────

class _FakeConn:
    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs: list = []

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


class _FakeDM:
    def __init__(self, devices: dict) -> None:
        self._devices = devices

    def get_device(self, port: str):
        return self._devices.get(port)


class _NullPool:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, target) -> None:
        self.added.append(target)


def test_real_boot_to_arm_session_drives_device_state():
    """Feed the real boot → agree → status → info → features → arm sequence through a live
    TargetIngestor and assert the Device ends in the state the board actually reported."""
    from src.core.target_ingest import TargetIngestor
    from src.models.device import Device

    session = [
        _BOOT_IDF, _BOOT_BANNER, _BOOT_BOARD, _BOOT_READY,   # boot
        _AGREE_REPLY, _PROMPT,                               # onboarding unlock
        _STATUS,                                             # identity/telemetry poll
        *_INFO,                                              # info block
        *_FEATURES,                                          # catalog dump
        "LXVEOS/1 arm state=pending token=428913 window=30",  # two-factor arm...
        "LXVEOS/1 arm state=armed",
        "LXVEOS/1 arm state=safe",                           # ...then disarm
    ]
    dev = Device(port="COM23", firmware="lxveos")
    ingest = TargetIngestor(pool=_NullPool(), devices=_FakeDM({"COM23": dev}))
    conn = _FakeConn("COM23")
    ingest.attach(conn, LxveOSProtocol())
    for ln in session:
        conn.feed(ln)

    # identity/telemetry from the real status + info: runtime caps + live heap/ops landed
    assert dev.runtime_capabilities == frozenset({"wifi", "ble", "bt_classic"})
    assert dev.telemetry["board"] == "bare_esp32_headless"
    assert dev.telemetry["heap"] == 157244
    assert dev.telemetry["ops"] == {"ready": 17, "planned": 7, "unavailable": 12}
    # arm gate progressed pending -> armed -> safe; the lamp reads the final SAFE
    assert dev.arm_state == "safe"
