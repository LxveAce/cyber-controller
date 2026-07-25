"""Sub-GHz OOK protocol decoders — RX-ONLY (decode a received pulse stream, never transmit).

``decode_ev1527`` decodes the EV1527 / PT2262-style OOK remote protocol (garage/gate remotes, cheap
433 MHz sensors) from a captured pulse-width stream into its 24-bit code (a 20-bit chip-unique
address + a 4-bit data nibble). It is a pure function over a list of pulse durations — no radio, no
I/O — so it unit-tests headless against golden vectors, and never builds or transmits an OOK frame.

EV1527 timing (public spec), in units of a base period Te (~250–500 µs):
  * a data bit is TWO pulses (high then low): ``0`` = 1 Te high + 3 Te low; ``1`` = 3 Te high + 1 Te
    low
  * the sync/preamble is 1 Te high + 31 Te low (a long gap that precedes the 24-bit data word)

Input ``pulses_us`` is a flat list of alternating high, low, high, low… durations in µs, with the
sync gap preceding the data. Real captures vary in framing/repeat layout; that reconciliation is a
firmware/SDR-integration concern (flagged), not this pure decoder's job.
"""
from __future__ import annotations

from dataclasses import dataclass

_DATA_BITS = 24
_DATA_PULSES = _DATA_BITS * 2  # each bit is a high+low pulse pair
_SYNC_MIN_TE = 8               # sync low is ~31 Te; require clearly-long to count as a real sync


def _classify(pulse: float, te: float, tol: float) -> "str | None":
    """A pulse is 'short' (~1 Te) or 'long' (~3 Te) within tolerance, else None (ambiguous)."""
    if abs(pulse - te) <= tol * te:
        return "short"
    if abs(pulse - 3 * te) <= tol * 3 * te:
        return "long"
    return None


def decode_ev1527(pulses_us, te: "float | None" = None, tolerance: float = 0.35) -> "dict | None":
    """Decode an EV1527 frame from a captured OOK pulse stream. Locates the sync gap, estimates the
    base period Te from the short-pulse cluster (unless given), and decodes the 24 data bits that
    follow. Returns ``{"protocol": "ev1527", "bits": 24, "code", "address", "data"}`` or ``None`` on
    no sync, too few pulses, or an out-of-tolerance pulse — never a fabricated code.
    """
    if not isinstance(pulses_us, (list, tuple)):
        return None
    pulses = list(pulses_us)
    if len(pulses) < _DATA_PULSES + 2:  # need at least a sync pair + the 24 data-bit pairs
        return None
    if any(not isinstance(p, (int, float)) or isinstance(p, bool) or p <= 0 for p in pulses):
        return None

    sync_idx = max(range(len(pulses)), key=lambda i: pulses[i])  # the sync low is the dominant gap
    data = pulses[sync_idx + 1: sync_idx + 1 + _DATA_PULSES]
    if len(data) < _DATA_PULSES:
        return None

    if te is None:
        te = sum(sorted(data)[:_DATA_BITS]) / _DATA_BITS  # mean of the 24 shortest = the 1 Te band
    if te <= 0 or pulses[sync_idx] < _SYNC_MIN_TE * te:
        return None

    bits = []
    for i in range(0, _DATA_PULSES, 2):
        high = _classify(data[i], te, tolerance)
        low = _classify(data[i + 1], te, tolerance)
        if high == "short" and low == "long":
            bits.append(0)
        elif high == "long" and low == "short":
            bits.append(1)
        else:
            return None  # not a valid EV1527 bit (ambiguous or out of tolerance)

    code = 0
    for b in bits:
        code = (code << 1) | b
    return {"protocol": "ev1527", "bits": _DATA_BITS, "code": code,
            "address": code >> 4, "data": code & 0xF}


@dataclass(frozen=True)
class SubGhzSignal:
    """A decoded Sub-GHz OOK signal (received, not transmitted) — the persistent object a SubGHz
    domain / detail shows. Built from :func:`decode_ev1527`'s output plus optional capture metadata
    (rssi / frequency / first-seen). RX/awareness-only: never authors or transmits an OOK frame."""

    protocol: str
    code: int
    address: int
    data: int
    bits: int
    rssi: "int | None" = None
    frequency_mhz: "float | None" = None
    first_seen: str = ""

    def code_hex(self) -> str:
        """The full code as zero-padded hex (e.g. a 24-bit code -> ``0xA5C3E1``)."""
        return f"0x{self.code:0{max(1, (self.bits + 3) // 4)}X}"

    def address_hex(self) -> str:
        return f"0x{self.address:X}"

    def data_hex(self) -> str:
        return f"0x{self.data:X}"


def signal_from_ev1527(decoded, *, rssi: "int | None" = None,
                       frequency_mhz: "float | None" = None,
                       first_seen: str = "") -> "SubGhzSignal | None":
    """Build a :class:`SubGhzSignal` from :func:`decode_ev1527`'s dict + optional capture metadata.
    Returns ``None`` when ``decoded`` is falsy — never a fabricated signal."""
    if not decoded:
        return None
    return SubGhzSignal(
        protocol=decoded["protocol"],
        code=decoded["code"],
        address=decoded["address"],
        data=decoded["data"],
        bits=decoded["bits"],
        rssi=rssi,
        frequency_mhz=frequency_mhz,
        first_seen=first_seen,
    )
