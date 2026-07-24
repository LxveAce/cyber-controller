"""Regression: an LxveOS board must not be misdetected as a Flipper (HW-found on a real board, 2026-07-23).

A locked LxveOS answers info/caps/status with only "type 'agree' ... authorized-use terms (see
RESPONSIBLE-USE.md)" + the ``lxveos>`` prompt, and its help lists a "Passive Flipper Zero detector" command.
The old Flipper identify() matched the bare "Flipper" substring in that detector line and won (Flipper is
checked before LxveOS), so CC labeled our own firmware "flipper".
"""

from __future__ import annotations

from src.core.handshake import detect_firmware
from src.protocols.flipper import FlipperProtocol
from src.protocols.lxveos import LxveOSProtocol

_LXVEOS_DETECTOR_LINE = "Passive Flipper Zero detector (BLE service-UUID match): flipper [seconds]"
_LXVEOS_LOCK_LINE = "locked — type 'agree' to accept the authorized-use terms (see RESPONSIBLE-USE.md) first."


def test_flipper_identify_rejects_flipper_detector_feature():
    fp = FlipperProtocol()
    assert fp.identify(_LXVEOS_DETECTOR_LINE) is False
    # other firmwares' flipper-hunter lines must also not read as a Flipper
    assert fp.identify("flipper  Detect nearby Flipper Zeros") is False
    assert fp.identify("Scanning for Flipper Zero devices...") is False


def test_flipper_identify_still_matches_real_flipper():
    fp = FlipperProtocol()
    assert fp.identify(">: ") is True                       # the Flipper CLI prompt
    assert fp.identify("SubGhz: ready") is True
    assert fp.identify("Flipper Zero fw 1.0.0") is True     # a genuine banner, no detector context


def test_lxveos_identify_matches_lock_and_prompt():
    lp = LxveOSProtocol()
    assert lp.identify(_LXVEOS_LOCK_LINE) is True
    assert lp.identify("lxveos> ") is True
    assert lp.identify("LXVEOS/1 status ok") is True


def test_detect_firmware_lxveos_not_flipper():
    # The exact probe output shape from a locked LxveOS board -> must resolve to lxveos, not flipper.
    lines = [
        "#info",
        _LXVEOS_LOCK_LINE,
        "lxveos> ",
        "flipper [seconds]",  # the detector command name, also present in help
    ]
    assert detect_firmware(lines) == "lxveos"


def test_detect_firmware_real_flipper_still_detects():
    lines = ["Flipper Zero fw 1.0.0", "SubGhz: ready", ">: "]
    assert detect_firmware(lines) == "flipper"
