"""Flash progress percent parsing.

Regression for a bug found by watching a real GhostESP flash: esptool prints "X.Y%" progress and the
engine's `(\\d+)%` regex captured the digit right before "%" — the TENTHS ("27.6%" -> 6) — so the flash
progress bar jittered 0-9 forever instead of climbing 0->100. The percent must be the integer part.
"""

from __future__ import annotations

from src.core.flash_engine import _percent_adapter


def _run(lines):
    seen = []
    on_line = _percent_adapter(lambda pct, msg: seen.append(pct))
    for ln in lines:
        on_line(ln)
    return seen


def test_progress_parses_integer_percent_not_tenths():
    # Real esptool write_flash progress lines.
    lines = [
        "Writing at 0x00000000 [                    ]   0.0% 0/1721440 bytes...",
        "Writing at 0x000104b3 [                    ]   1.9% 32768/1721440 bytes...",
        "Writing at 0x001095fd [=======>            ]  27.6% 475136/1721440 bytes...",
        "Writing at 0x0019f5a0 [====================] 100.0% 1721440/1721440 bytes...",
    ]
    # Integer percents, monotonically climbing — NOT the tenths digit (which was 0, 9, 6, 0).
    assert _run(lines) == [0, 1, 27, 100]


def test_progress_is_monotonic_over_a_full_sweep():
    # Every integer 0..100 with a .5 tenths — must come back as 0..100 ascending, never the tenths (5).
    lines = [f"Writing [ ] {i}.5% {i}/100 bytes..." for i in range(0, 101)]
    seen = _run(lines)
    assert seen == list(range(0, 101))
    assert seen == sorted(seen)  # strictly non-decreasing, no 0-9 jitter
