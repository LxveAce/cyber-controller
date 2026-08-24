"""Antenna length calculator: wavelength and element lengths from a target frequency.

Pure math, no hardware. Given a frequency it computes the free-space wavelength (``lambda = c / f``)
and the common antenna element lengths (full-wave, 5/8, 1/2, 1/4, 1/8) in metric and imperial units,
with an optional velocity factor for real wire/coax. The hand method: ``c = 299,792,458 m/s``,
``lambda = c / f``, and quarter-wave ``= lambda / 4``.

Worked example (433 MHz ISM): lambda = 69.2 cm, quarter-wave = 17.3 cm (6.81 in).

Consumed by the CLI (``--antenna``) and the ``/api/antenna`` route; a GUI card comes later. The band
label is best-effort: an unrecognised frequency simply omits it; we never fabricate one. Text output
is ASCII so the CLI prints on any console (Windows cp1252 can't encode the lambda glyph).
"""
from __future__ import annotations

C = 299_792_458  # speed of light in vacuum, metres/second (exact, matches the video)

# unit token -> multiplier to hertz
_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}

# antenna element: key, human label (ASCII), fraction of a wavelength. Longest first.
_FRACTIONS = (
    ("full", "Full-wave (1/1)", 1.0),
    ("five_eighths", "5/8-wave", 0.625),
    ("half", "Half-wave (1/2)", 0.5),
    ("quarter", "Quarter-wave (1/4)", 0.25),
    ("eighth", "1/8-wave (1/8)", 0.125),
)

# best-effort band labels for frequencies our field touches: (low_hz, high_hz, label)
_BANDS = (
    (100_000, 150_000, "125 kHz (LF RFID / prox)"),
    (13_400_000, 13_700_000, "13.56 MHz (HF RFID / NFC)"),
    (26_900_000, 27_500_000, "27 MHz (CB / ISM)"),
    (40_000_000, 40_010_000, "40 MHz (ISM / RC)"),
    (300_000_000, 350_000_000, "315 MHz (ISM - US remotes / TPMS)"),
    (386_000_000, 400_000_000, "390 MHz (ISM - key fobs)"),
    (420_000_000, 450_000_000, "433 MHz (ISM - EU/worldwide remotes, sub-GHz)"),
    (863_000_000, 870_000_000, "868 MHz (ISM / LoRa EU)"),
    (900_000_000, 930_000_000, "915 MHz (ISM / LoRa US, sub-GHz)"),
    (1_087_000_000, 1_093_000_000, "1090 MHz (ADS-B)"),
    (1_573_000_000, 1_578_000_000, "1575 MHz (GPS L1)"),
    (2_400_000_000, 2_500_000_000, "2.4 GHz (Wi-Fi / BLE / Zigbee)"),
    (5_150_000_000, 5_895_000_000, "5 GHz (Wi-Fi)"),
)


def normalize_freq(value, unit: str = "mhz") -> float:
    """Frequency in hertz. ``value`` is a number (read with ``unit``) or a string carrying its own
    unit (``"433MHz"``, ``"2.4 GHz"``, ``"915"``). Raises ``ValueError`` on junk."""
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        found = None
        for tok in ("ghz", "mhz", "khz", "hz"):  # longest first so "hz" doesn't match inside "mhz"
            if s.endswith(tok):
                found = tok
                s = s[: -len(tok)]
                break
        try:
            num = float(s)
        except ValueError as exc:
            raise ValueError(f"could not parse frequency {value!r}") from exc
        if found:
            mult = _UNITS[found]
        else:  # bare number in a string: validate the fallback unit, same as the numeric branch
            mult = _UNITS.get(unit.lower())
            if mult is None:
                raise ValueError(f"unknown frequency unit {unit!r} (use hz/khz/mhz/ghz)")
    else:
        num = float(value)
        mult = _UNITS.get(unit.lower())
        if mult is None:
            raise ValueError(f"unknown frequency unit {unit!r} (use hz/khz/mhz/ghz)")
    hz = num * mult
    if hz <= 0:
        raise ValueError("frequency must be positive")
    return hz


def _units_of(metres: float) -> dict:
    """One length, expressed every way a builder measures: m, cm, mm, inches, feet."""
    return {
        "m": round(metres, 4),
        "cm": round(metres * 100, 2),
        "mm": round(metres * 1000, 1),
        "in": round(metres / 0.0254, 2),
        "ft": round(metres / 0.3048, 3),
    }


def band_label(freq_hz: float) -> str:
    """Best-effort band name, or ``""`` if outside the tables (never fabricated)."""
    for low, high, label in _BANDS:
        if low <= freq_hz <= high:
            return label
    return ""


def freq_label(freq_hz: float) -> str:
    """Human frequency string in a sensible unit (``433 MHz``, ``2.4 GHz``, ``125 kHz``)."""
    if freq_hz >= 1e9:
        return f"{freq_hz / 1e9:g} GHz"
    if freq_hz >= 1e6:
        return f"{freq_hz / 1e6:g} MHz"
    if freq_hz >= 1e3:
        return f"{freq_hz / 1e3:g} kHz"
    return f"{freq_hz:g} Hz"


def compute(value, unit: str = "mhz", velocity_factor: float = 1.0) -> dict:
    """Full antenna calculation for one frequency. Returns a JSON-friendly dict (freq, band,
    wavelength, and element lengths in metric + imperial) for the CLI, ``/api/antenna``, or a GUI.

    ``velocity_factor`` scales length: 1.0 = free-space/theoretical (the hand method), ~0.95 for a
    real bare-wire element (end effects), or a coax's rated VF (RG-58 ~0.66) for a stub.
    """
    if not 0 < velocity_factor <= 1.0:
        raise ValueError("velocity_factor must be in (0, 1]")
    freq_hz = normalize_freq(value, unit)
    lam = C * velocity_factor / freq_hz
    elements = {
        key: {"label": label, **_units_of(lam * frac)}
        for key, label, frac in _FRACTIONS
    }
    vf_note = f" x {velocity_factor} VF" if velocity_factor != 1.0 else ""
    notes = [
        f"Free-space wavelength (lambda) = c / f = {C:,} / {freq_hz:,.0f} Hz{vf_note}"
        f" = {lam * 100:.2f} cm.",
        "Quarter-wave (1/4) is the usual pick for a handheld/mobile whip; 5/8 gives more gain on a "
        "ground plane.",
    ]
    if velocity_factor == 1.0:
        notes.append(
            "These are theoretical lengths; a real bare-wire element runs ~95% of this (end "
            "effects). Set velocity factor 0.95, or the coax's own VF (RG-58 ~0.66) for a stub."
        )
    return {
        "freq_hz": freq_hz,
        "freq_label": freq_label(freq_hz),
        "band": band_label(freq_hz),
        "velocity_factor": velocity_factor,
        "wavelength": _units_of(lam),
        "elements": elements,
        "notes": notes,
    }


def format_report(result: dict) -> str:
    """Human-readable text block for the CLI. Mirrors what the GUI card will show."""
    lines = []
    band = f"  |  {result['band']}" if result["band"] else ""
    lines.append(f"Antenna calculator - {result['freq_label']}{band}")
    vf = result["velocity_factor"]
    lines.append(f"  Velocity factor: {vf}" + ("  (free-space / theoretical)" if vf == 1.0 else ""))
    wl = result["wavelength"]
    lines.append(f"  Wavelength (lambda): {wl['cm']} cm  /  {wl['in']} in  /  {wl['m']} m")
    lines.append("  Element lengths:")
    for e in result["elements"].values():
        lines.append(
            f"    {e['label']:<18} {e['cm']:>8.2f} cm   {e['in']:>7.2f} in   {e['mm']:>8.1f} mm"
        )
    lines.append("")
    for note in result["notes"]:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def antenna_cli(value, unit: str = "mhz", velocity_factor: float = 1.0) -> int:
    """CLI entry for ``--antenna``: print the report to stdout, return a process exit code."""
    try:
        result = compute(value, unit, velocity_factor)
    except ValueError as exc:
        print(f"antenna: {exc}")
        return 2
    print(format_report(result))
    return 0
