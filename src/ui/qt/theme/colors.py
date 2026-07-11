"""Design token constants for the Cyber Controller dark theme — LxveAce identity.

The interactive/brand accent is LxveAce violet (ace-of-spades), NOT the previous generic
acid-green that read as a Marauder clone. Functional green is retained only where it
carries meaning — "live / connected / go" (serial output, connect, online dots).
"""

# ── Backgrounds ─────────────────────────────────────────────────────
BG_DEEP = "#0d1117"
BG_SURFACE = "#161b22"
BG_CARD = "#1c2128"
BG_INPUT = "#2d333b"

# ── Borders ─────────────────────────────────────────────────────────
BORDER = "#30363d"

# ── Text ────────────────────────────────────────────────────────────
TEXT_PRIMARY = "#e6edf3"
TEXT_MUTED = "#8b949e"
TEXT_DISABLED = "#484f58"

# ── Brand accent — LxveAce violet (ace of spades) ───────────────────
ACCENT = "#a371f7"        # primary interactive/brand accent (tabs, focus, selection, titles)
ACCENT_BRIGHT = "#c9a3ff"  # emphasis / hover
ACCENT_DIM = "#6e40c9"     # pressed / dim

# ── Functional semantics ────────────────────────────────────────────
SUCCESS = "#3fb950"   # connected / go / online — green keeps its "live" meaning
WARNING = "#f0883e"
ERROR = "#f85149"
INFO = "#58a6ff"
TERMINAL = "#7ee787"  # live serial-output text (soft green on the deep background)

# ── Font stacks ─────────────────────────────────────────────────────
FONT_MONO = '"JetBrains Mono", "Cascadia Code", "Consolas", monospace'
FONT_SANS = '"Segoe UI", "Inter", sans-serif'

# ── Palette map ─────────────────────────────────────────────────────
#: Token name -> hex, the SINGLE SOURCE OF TRUTH for the app palette. ``apply_theme`` substitutes these
#: for ``${TOKEN}`` placeholders in cyber_dark.qss, so changing a colour here re-themes the whole app.
PALETTE = {
    "BG_DEEP": BG_DEEP, "BG_SURFACE": BG_SURFACE, "BG_CARD": BG_CARD, "BG_INPUT": BG_INPUT,
    "BORDER": BORDER, "TEXT_PRIMARY": TEXT_PRIMARY, "TEXT_MUTED": TEXT_MUTED,
    "TEXT_DISABLED": TEXT_DISABLED, "ACCENT": ACCENT, "ACCENT_BRIGHT": ACCENT_BRIGHT,
    "ACCENT_DIM": ACCENT_DIM, "SUCCESS": SUCCESS, "WARNING": WARNING, "ERROR": ERROR,
    "INFO": INFO, "TERMINAL": TERMINAL,
}
