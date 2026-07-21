#!/usr/bin/env python3
"""Generate the LxveLabs / Cyber Controller monochrome icon set.

One coherent line-art system (24-grid, 1.75 stroke, round caps) drawn with
``stroke="currentColor"`` so a single SVG tints to whatever context uses it:
- purple (#A371F7) inside the CC Qt/Tk UI,
- neon green (#39FF14) in LxveLabs brand / web contexts,
- ``var(--accent)`` on each site.

Run:  python assets/gen_icons.py           # writes assets/icons/*.svg
Feature icons map 1:1 to CC's tabs (Connect/Operate/Network/Settings/Devices/
Device View/Firmware/Flash/Software OS/Nodes/Cross-Comm/Broadcast/Health/Macros/
Targets/Wardrive/Multi-Wardrive/Flock Map/Graph/Remote/Crack Lab) plus brand marks.
"""
from __future__ import annotations

import math
import os

OUT = os.path.join(os.path.dirname(__file__), "icons")

_HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" '
    'fill="none" stroke="currentColor" stroke-width="1.75" '
    'stroke-linecap="round" stroke-linejoin="round">'
)
_DOT = '<circle cx="{x}" cy="{y}" r="{r}" fill="currentColor" stroke="none"/>'


def dot(x, y, r=1.15):
    return _DOT.format(x=x, y=y, r=r)


def radial(cx, cy, r0, r1, n, start=0.0):
    """n evenly spaced radial line segments from r0..r1 (for gear teeth / pins)."""
    out = []
    for i in range(n):
        a = math.radians(start + i * 360.0 / n)
        x0, y0 = cx + r0 * math.cos(a), cy - r0 * math.sin(a)
        x1, y1 = cx + r1 * math.cos(a), cy - r1 * math.sin(a)
        out.append(f'<line x1="{x0:.2f}" y1="{y0:.2f}" x2="{x1:.2f}" y2="{y1:.2f}"/>')
    return "".join(out)


def chip_pins(cx, cy, half, stub):
    """3 short pin stubs on each of the 4 sides of a square die (clean IC look)."""
    out = []
    for o in (-half * 0.5, 0.0, half * 0.5):
        out.append(f'<line x1="{cx+o:.2f}" y1="{cy-half:.2f}" x2="{cx+o:.2f}" y2="{cy-half-stub:.2f}"/>')
        out.append(f'<line x1="{cx+o:.2f}" y1="{cy+half:.2f}" x2="{cx+o:.2f}" y2="{cy+half+stub:.2f}"/>')
        out.append(f'<line x1="{cx-half:.2f}" y1="{cy+o:.2f}" x2="{cx-half-stub:.2f}" y2="{cy+o:.2f}"/>')
        out.append(f'<line x1="{cx+half:.2f}" y1="{cy+o:.2f}" x2="{cx+half+stub:.2f}" y2="{cy+o:.2f}"/>')
    return "".join(out)


# ---- icon geometry (inner markup only) -------------------------------------
ICONS: dict[str, str] = {
    # --- CC feature / tab icons ---
    "connect": '<path d="M10 14a3.2 3.2 0 0 1 0-4.5l2-2a3.2 3.2 0 0 1 4.5 4.5l-1 1"/>'
               '<path d="M14 10a3.2 3.2 0 0 1 0 4.5l-2 2a3.2 3.2 0 0 1-4.5-4.5l1-1"/>',
    "operate": '<line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/>'
               '<circle cx="10" cy="8" r="2.3" fill="none"/><circle cx="15" cy="16" r="2.3" fill="none"/>',
    "network": '<circle cx="12" cy="12" r="2.1" fill="none"/><line x1="12" y1="12" x2="5.5" y2="6"/>'
               '<line x1="12" y1="12" x2="18.5" y2="7"/><line x1="12" y1="12" x2="17" y2="18"/>'
               + dot(5.5, 6) + dot(18.5, 7) + dot(17, 18),
    "settings": '<circle cx="12" cy="12" r="3.2" fill="none"/>' + radial(12, 12, 5, 7.2, 8),
    "devices": '<rect x="3.5" y="4.5" width="11" height="8.5" rx="1.4" fill="none"/>'
               '<rect x="9.5" y="10.5" width="11" height="8.5" rx="1.4" fill="none"/>'
               + dot(12, 8.7, 0.9) + dot(18, 14.7, 0.9),
    "device-view": '<rect x="3.5" y="4.5" width="12" height="9.5" rx="1.4" fill="none"/>'
                   + radial(9.5, 9.2, 6.4, 7.6, 4) +
                   '<circle cx="16" cy="15" r="3.4" fill="none"/><line x1="18.4" y1="17.4" x2="21" y2="20"/>',
    "firmware": '<rect x="8" y="8" width="8" height="8" rx="1" fill="none"/>' + chip_pins(12, 12, 4, 2.2) +
                '<path d="M12 10.2v3.2"/><path d="M10.5 11.9 12 13.4l1.5-1.5"/>',
    "flash": '<path d="M13 2.5 5.5 13H11l-1 8.5L18.5 10H13z" fill="none"/>',
    "software-os": '<rect x="3.5" y="5" width="17" height="14" rx="1.6" fill="none"/>'
                   '<line x1="3.5" y1="9" x2="20.5" y2="9"/>' + dot(6, 7, 0.7) + dot(8.4, 7, 0.7),
    "nodes": '<circle cx="12" cy="6" r="1.7" fill="none"/><circle cx="6" cy="16" r="1.7" fill="none"/>'
             '<circle cx="18" cy="16" r="1.7" fill="none"/>'
             '<line x1="10.7" y1="7.2" x2="7.2" y2="14.6"/><line x1="13.3" y1="7.2" x2="16.8" y2="14.6"/>'
             '<line x1="7.7" y1="16" x2="16.3" y2="16"/>',
    "cross-comm": '<path d="M6 9h11l-3-3"/><path d="M18 15H7l3 3"/>',
    "broadcast": '<line x1="12" y1="7.5" x2="12" y2="20"/>' + dot(12, 6, 1.15) +
                 '<path d="M9.2 8.8a4 4 0 0 0 0 5.6"/><path d="M6.8 6.8a8 8 0 0 0 0 9.6"/>'
                 '<path d="M14.8 8.8a4 4 0 0 1 0 5.6"/><path d="M17.2 6.8a8 8 0 0 1 0 9.6"/>',
    "health": '<path d="M3 12.5h4l2-5 3.5 9 2-4h6.5"/>',
    "macros": '<path d="M8.5 8 4.5 12l4 4"/><path d="M15.5 8l4 4-4 4"/><line x1="13.5" y1="6" x2="10.5" y2="18"/>',
    "targets": '<circle cx="12" cy="12" r="7" fill="none"/>' + dot(12, 12, 1.6) +
               '<line x1="12" y1="2.5" x2="12" y2="5.5"/><line x1="12" y1="18.5" x2="12" y2="21.5"/>'
               '<line x1="2.5" y1="12" x2="5.5" y2="12"/><line x1="18.5" y1="12" x2="21.5" y2="12"/>',
    "wardrive": '<path d="M12 21s5.5-5 5.5-9.5a5.5 5.5 0 0 0-11 0C6.5 16 12 21 12 21z" fill="none"/>'
                '<circle cx="12" cy="11.2" r="2" fill="none"/>',
    "multi-wardrive": '<path d="M8.5 15.5s3.5-3.2 3.5-6a3.5 3.5 0 0 0-7 0c0 2.8 3.5 6 3.5 6z" fill="none"/>'
                      '<path d="M16 20s3.5-3.2 3.5-6a3.5 3.5 0 0 0-7 0c0 2.8 3.5 6 3.5 6z" fill="none"/>'
                      + dot(8.5, 9.5, 0.9) + dot(16, 14, 0.9),
    "flock-map": '<path d="M3.5 7.5 17 4l1.2 4.2-13.5 3.5z" fill="none"/>'
                 '<line x1="9" y1="8.7" x2="11" y2="13"/><path d="M4.7 11.2v3.8h4"/>'
                 '<circle cx="18.5" cy="16.5" r="2.4" fill="none"/>',
    "graph": '<polyline points="4,4 4,20 20,20"/><polyline points="6.5,16 10.5,11 13.5,13.5 19,6.5"/>'
             + dot(10.5, 11, 0.9) + dot(19, 6.5, 0.9),
    "remote": '<rect x="8" y="3" width="8" height="18" rx="2.4" fill="none"/>'
              '<path d="M10.2 6.2a3 3 0 0 1 3.6 0"/>' + dot(12, 6, 0.7) +
              '<line x1="10.5" y1="11" x2="13.5" y2="11"/><line x1="10.5" y1="14" x2="13.5" y2="14"/>',
    # Crack Lab (offline WPA key-recovery): an open padlock — the shackle sprung on the right.
    "crack-lab": '<rect x="5.5" y="11" width="13" height="9" rx="2" fill="none"/>'
                 '<path d="M8.5 11 V7.5 a3.5 3.5 0 0 1 6.9 -0.8"/>' + dot(12, 15, 1.3) +
                 '<line x1="12" y1="15" x2="12" y2="17.6"/>',
    # Survey surface (GPS field survey — Wardrive/Multi-Wardrive/Flock Map): a compass, needle on the NE bearing.
    "survey": '<circle cx="12" cy="12" r="8.5" fill="none"/>'
              '<path d="M16 8 12.8 11.2 8 16 11.2 12.8Z" fill="none"/>' + dot(12, 12, 0.7),
    # Analyze surface (situational awareness + offline post-processing): a magnifier over a small rising chart.
    "analyze": '<circle cx="10.5" cy="10.5" r="6" fill="none"/><line x1="14.8" y1="14.8" x2="19.5" y2="19.5"/>'
               '<polyline points="7.7,11.5 9.5,9 11,10.5 13.3,7.7"/>',
    # Console (single-device command console): a terminal window with a prompt and a command line.
    "console": '<rect x="3.5" y="5" width="17" height="14" rx="2" fill="none"/>'
               '<path d="M7 10l2.6 2-2.6 2"/><line x1="11.8" y1="15" x2="16.5" y2="15"/>',

    # --- brand / product marks ---
    "cybercontroller": '<rect x="3" y="5" width="18" height="14" rx="2.2" fill="none"/>'
                       '<path d="M7 10l3 2.5-3 2.5"/><line x1="12.5" y1="15" x2="16.5" y2="15"/>',
    "lxvelabs": '<path d="M12 3l7.5 4.3v9.4L12 21l-7.5-4.3V7.3z" fill="none"/>'
                '<line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/>',
    "lxveace": '<path d="M12 3c2.6 3.4 6 5.4 6 9a4 4 0 0 1-4.4 4l.9 3h-5l.9-3A4 4 0 0 1 6 12c0-3.6 3.4-5.6 6-9z" fill="none"/>'
               + dot(12, 12, 1.1),
    "wifi": '<path d="M4.5 11.5a11 11 0 0 1 15 0"/><path d="M7.5 14.5a7 7 0 0 1 9 0"/>' + dot(12, 18.5, 1.3),
    "ble": '<path d="M8.5 7.5 15 12l-4.5 3.5V4l4.5 3.5L8.5 16.5"/>',
    "chip": '<rect x="8" y="8" width="8" height="8" rx="1" fill="none"/>'
            '<rect x="10.5" y="10.5" width="3" height="3" rx="0.4" fill="none"/>' + chip_pins(12, 12, 4, 2.2),
    "shield": '<path d="M12 3l7 2.5v5c0 5-3.4 8-7 9.5-3.6-1.5-7-4.5-7-9.5v-5z" fill="none"/>'
              '<path d="M9 12l2 2 4-4.5"/>',
    "sensing": '<path d="M4 15a8 8 0 0 1 16 0" fill="none"/><line x1="12" y1="15" x2="17" y2="10.5"/>'
               + dot(12, 15, 1.2) + dot(8, 8.5, 0.9),
}


def build(name: str, inner: str) -> str:
    return _HEAD + inner + "</svg>"


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for name, inner in ICONS.items():
        with open(os.path.join(OUT, f"{name}.svg"), "w", encoding="utf-8") as fh:
            fh.write(build(name, inner))
    print(f"wrote {len(ICONS)} icons to {OUT}")


if __name__ == "__main__":
    main()
