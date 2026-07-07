"""Detect a Cheap Yellow Display (CYD) board and its exact panel variant.

CYD boards share the plain-ESP32 chip, so esptool/the bootloader cannot tell a 2.8" ILI9341 from a
2.8" 2-USB ST7789 from a 3.5" ST7796 — only code running on the board and reading the display
controller can. This flashes a tiny bundled probe firmware (``src/config/probes/cyd_probe.bin``) that
reads the panel's controller ID + touch type + LDR over SPI/I2C and prints the matching Marauder
variant, then parses that report. The Flash tab's "Detect board" button uses it so users don't have to
guess which CYD they have (guessing wrong is what leaves the screen blank or mirrored/"oriented weird").

The probe verdict distinguishes a real CYD from a bare ESP32: a present display controller answers the
0x09 status register stably (a floating MISO reads 0x00/0xFF), and the LDR divider reads a plausible
mid-range value (a floating GPIO34 reads at the rail). So a display-less ESP32 reports CYD=no.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

from src.core.flash_core import esptool_argv
from src.core.resources import resource_path

PROBE_BIN = resource_path("src", "config", "probes", "cyd_probe.bin")

# Marauder variant key -> human label (controller + size + touch). Keep in sync with the keys in
# src/config/profiles/marauder.json (resolver_params label_map) and the probe's decision tree.
VARIANT_LABELS = {
    "cyd_2432S028": 'CYD 2.8" (ILI9341, resistive)',
    "cyd_2432S028_2usb": 'CYD 2.8" 2-USB (ST7789, resistive)',
    "cyd_2432S024_guition": 'CYD Guition (ST7789, capacitive touch)',
    "cyd_3_5_inch": 'CYD 3.5" (ST7796, 320x480)',
}


@dataclass
class CydResult:
    """Outcome of a CYD detection pass."""

    is_cyd: bool = False
    confidence: str = "none"     # high / medium / low / none
    controller: str = "none"     # ILI9341 / ST7796 / ST7789 / none
    touch: str = ""              # resistive / capacitive
    variant: str = ""            # marauder variant key ("" when not a CYD)
    label: str = ""              # human label for variant
    ldr: int = -1
    responded: bool = False      # True only when a parseable probe report block was actually seen
    raw: str = ""                # the parsed probe block, for the log / bug reports

    @property
    def summary(self) -> str:
        # A read that never produced a probe report block is NOT proof of a bare ESP32 — the board
        # may not be running the probe, or the reset/report window was missed (CH340 re-enumeration,
        # an adapter without control lines). Reporting that as a confident "bare ESP32" would defeat
        # the safeguard, so surface it as a distinct "no response" outcome instead of a false negative.
        if not self.responded:
            return (
                "No response from the detection probe — the board emitted no readable report. "
                "Re-flash the probe and retry (this is NOT a confirmed bare ESP32)."
            )
        if not self.is_cyd:
            return "No CYD display detected on this board (looks like a bare ESP32)."
        return f"Detected {self.label or self.variant}  —  confidence: {self.confidence}"


def _grab(text: str, pattern: str, default: str = "") -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else default


def parse_report(raw: str) -> CydResult:
    """Parse the probe's serial output into a CydResult, using the last complete report block."""
    blocks = [b for b in raw.split("=====CYD_PROBE=====") if "CYD=" in b]
    responded = bool(blocks)  # no CYD= block means the probe never reported — can't judge the panel
    block = blocks[-1] if blocks else raw
    is_cyd = _grab(block, r"CYD=(\w+)") == "yes"
    conf = _grab(block, r"CONF=(\w+)", "none")
    ctrl = _grab(block, r"CONTROLLER=(\S+)", "none")
    touch = _grab(block, r"TOUCH=(\S+)")
    variant = _grab(block, r"VARIANT=(\S+)")
    if variant == "none":
        variant = ""
    ldr_vals = re.findall(r"LDR=(-?\d+)", block)
    ldr = int(ldr_vals[-1]) if ldr_vals else -1
    return CydResult(
        is_cyd=is_cyd,
        confidence=conf,
        controller=ctrl,
        touch=touch,
        variant=variant,
        label=VARIANT_LABELS.get(variant, variant),
        ldr=ldr,
        responded=responded,
        raw=block.strip(),
    )


def _flash_probe(port: str, baud: int = 460800, timeout: float = 120.0) -> None:
    """Flash the bundled merged probe image at offset 0x0. Raises RuntimeError on failure."""
    if not PROBE_BIN.is_file():
        raise RuntimeError(f"probe firmware missing: {PROBE_BIN}")
    argv = esptool_argv(
        "--chip", "esp32", "--port", port, "--baud", str(baud),
        "--before", "default_reset", "--after", "hard_reset",
        "write_flash", "0x0", str(PROBE_BIN),
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"probe flash failed (exit {proc.returncode}): {tail}")


def _read_report(port: str, secs: float = 6.0) -> str:
    """Reset the board and read its serial output until two report blocks land or the timeout."""
    import serial

    sp = serial.Serial(port, 115200, timeout=0.3)
    try:
        sp.reset_input_buffer()
        try:  # pulse reset so setup() re-runs and reprints even if we connected late
            sp.setDTR(False)
            sp.setRTS(True)
            time.sleep(0.15)
            sp.setRTS(False)
        except Exception:  # noqa: BLE001 — some adapters don't expose the control lines
            pass
        buf = bytearray()
        start = time.time()
        while time.time() - start < secs:
            chunk = sp.read(4096)
            if chunk:
                buf.extend(chunk)
            if buf.count(b"=====END=====") >= 2:
                break
    finally:
        sp.close()
    return buf.decode("utf-8", "replace")


def detect_cyd(
    port: str,
    *,
    flash_probe: bool = True,
    read_secs: float = 6.0,
    progress=None,
) -> CydResult:
    """Flash the probe (unless already present) and return what panel is on ``port``.

    ``progress`` is an optional ``callable(str)`` for UI status lines. This OVERWRITES the board's
    firmware with the probe — callers should warn the user and re-flash real firmware afterward.
    """
    if flash_probe:
        if progress:
            progress("Flashing detection probe…")
        _flash_probe(port)
    if progress:
        progress("Reading panel identity…")
    raw = _read_report(port, read_secs)
    result = parse_report(raw)
    if progress:
        progress(result.summary)
    return result
