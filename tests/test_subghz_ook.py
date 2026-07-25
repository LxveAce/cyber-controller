"""Golden-vector tests for the EV1527 Sub-GHz OOK decoder (src/core/subghz_ook.py) — RX/decode only.

We SYNTHESIZE a pulse stream from a known 24-bit code + a chosen Te (the sync gap + 24 bit-pairs per
the public EV1527 timing), then assert the decoder recovers the exact code / address / data. Plus
jitter tolerance and rejections (no sync, too few pulses, ambiguous pulse, empty, non-numeric).
"""
from __future__ import annotations

from src.core.subghz_ook import decode_ev1527, signal_from_ev1527

_CODE = 0xA5C3E1  # a 24-bit code with mixed bits (exercises both the 0 and 1 encodings)


def _synth(code: int, te: float, jitter=None) -> list:
    """Build the EV1527 pulse stream: sync (1 Te high + 31 Te low) then 24 (high, low) bit pairs.
    ``0`` = 1 Te high + 3 Te low; ``1`` = 3 Te high + 1 Te low. Optional per-index jitter factor."""
    pulses = [te, 31 * te]
    for i in range(23, -1, -1):          # MSB first
        if (code >> i) & 1:
            pulses += [3 * te, te]       # '1'
        else:
            pulses += [te, 3 * te]       # '0'
    if jitter is not None:
        pulses = [p * jitter(i) for i, p in enumerate(pulses)]
    return pulses


def test_decode_ev1527_golden_vector():
    got = decode_ev1527(_synth(_CODE, te=350))
    assert got == {"protocol": "ev1527", "bits": 24, "code": _CODE,
                   "address": _CODE >> 4, "data": _CODE & 0xF}
    # address/data computed independently from the spec (20-bit address + 4-bit data).
    assert got["address"] == 0xA5C3E and got["data"] == 0x1


def test_decode_ev1527_across_te_values():
    for te in (250, 400, 500):
        got = decode_ev1527(_synth(_CODE, te=te))
        assert got is not None and got["code"] == _CODE


def test_decode_ev1527_tolerates_jitter_within_tolerance():
    # deterministic +/-20% jitter (0.8 / 1.0 / 1.2 cycling) — inside the 0.35 tolerance window
    def jitter(i):
        return 1.0 + 0.2 * ((i % 3) - 1)
    got = decode_ev1527(_synth(_CODE, te=350, jitter=jitter))
    assert got is not None and got["code"] == _CODE


def test_decode_ev1527_honors_explicit_te():
    got = decode_ev1527(_synth(_CODE, te=320), te=320)
    assert got is not None and got["code"] == _CODE


def test_rejects_no_sync_and_short_and_empty():
    te = 350
    assert decode_ev1527([te] * 60) is None                 # all equal -> no long sync gap
    assert decode_ev1527([te, 31 * te] + [te] * 10) is None  # too few data pulses
    assert decode_ev1527([]) is None
    assert decode_ev1527(None) is None                       # type: ignore[arg-type]
    assert decode_ev1527("pulses") is None                   # type: ignore[arg-type]


def test_rejects_ambiguous_pulse():
    te = 350
    p = _synth(_CODE, te=te)
    p[2] = 1.6 * te   # a data pulse in the dead zone between short (~1 Te) and long (~3 Te)
    assert decode_ev1527(p) is None


def test_rejects_non_numeric_and_bool_pulses():
    te = 350
    p = _synth(_CODE, te=te)
    assert decode_ev1527(p[:2] + [True] + p[3:]) is None     # bool is not a real duration
    assert decode_ev1527(p[:2] + ["x"] + p[3:]) is None


# ── SubGhzSignal object model (built from the decoder output + capture metadata) ──
def test_signal_from_ev1527_carries_decoder_fields_and_metadata():
    sig = signal_from_ev1527(decode_ev1527(_synth(_CODE, te=350)),
                             rssi=-72, frequency_mhz=433.92, first_seen="t")
    assert sig.protocol == "ev1527" and sig.bits == 24
    assert sig.code == _CODE and sig.address == _CODE >> 4 and sig.data == _CODE & 0xF
    assert sig.rssi == -72 and sig.frequency_mhz == 433.92 and sig.first_seen == "t"


def test_subghz_signal_hex_formatting():
    sig = signal_from_ev1527({"protocol": "ev1527", "bits": 24,
                              "code": 0xA5C3E1, "address": 0xA5C3E, "data": 0x1})
    assert sig.code_hex() == "0xA5C3E1"   # zero-padded to the bit width (24 bits -> 6 hex digits)
    assert sig.address_hex() == "0xA5C3E" and sig.data_hex() == "0x1"


def test_signal_from_ev1527_is_none_when_undecodable():
    assert signal_from_ev1527(None) is None   # a non-EV1527 / undecodable capture -> no signal
    assert signal_from_ev1527({}) is None
