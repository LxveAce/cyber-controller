"""Device-true regression: Marauder identified by its command set when the version banner is missed.

Grounded in a real HIL capture (2026-07-24, machine ``extra``): three ESP32 Marauder boards on
COM34/35/36 emitted the identical Marauder command list, but only the one that happened to reprint
its ``Marauder vX.Y`` banner inside the read window was identified — the other two came back
``None`` despite the unmistakable command output. ``match_firmware`` now falls back to the command
set. The fixtures below are VERBATIM excerpts of what those real boards emitted (the oracle), not a
hand-written echo of the matcher.
"""
from __future__ import annotations

from src.core.device_detect import match_firmware

# ── verbatim capture: a Marauder board whose `version` banner was NOT in the read window ──
# (COM34, 2026-07-24 — returned fw=None before the command-set fallback existed.)
MARAUDER_NO_BANNER = """\
#version
> #?
> #help
============ Commands ============
channel [-s <channel>]
settings [-s <setting> enable/disable>]/[-r]
reboot
evilportal [-c start [-w html.html]/sethtml <html.html>]
wardrive
wardrivepoi [label] - Tag a GPS POI during wardrive
sniffbeacon
sniffprobe
sniffdeauth
sniffpmkid [-c <channel>][-d][-l]
stopscan [-f]
attack -t <quiet/csa/sae/beacon [-l/-r/-a]/deauth [-c]/probe/rickroll/badmsg [-c]/sleep [-c]>
"""

# ── verbatim capture: a real LxveOS board (COM37) — must NOT be mistaken for Marauder ──
LXVEOS_HELP = """\
lxveos> help
info
  Show firmware version, board id, chip and UI profile
scan
  Passive Wi-Fi AP scan (listen only, no frames sent)
sniff
  Passive Wi-Fi packet monitor: sniff [seconds] [channel] (listen only)
stations
  Passive client-station scan: stations [seconds] [channel] (listen only)
"""


def test_marauder_identified_by_command_set_without_version_banner():
    fw, ver = match_firmware(MARAUDER_NO_BANNER)
    assert fw == "marauder", "Marauder command list must identify even with no version banner"
    assert ver is None  # no version captured this way — name only


def test_version_banner_path_still_wins_with_version():
    fw, ver = match_firmware("ESP32 Marauder v4.0.1\n" + MARAUDER_NO_BANNER)
    assert fw == "marauder"
    assert ver == "4.0.1", "the version-banner signature still wins and captures the version"


def test_lxveos_not_false_matched_as_marauder():
    fw, _ = match_firmware(LXVEOS_HELP)
    assert fw == "lxveos", "real LxveOS help must resolve to lxveos, not a Marauder false positive"


def test_unrelated_text_stays_unidentified():
    fw, ver = match_firmware("boot ok\nsome random device banner\n> ")
    assert fw is None and ver is None
