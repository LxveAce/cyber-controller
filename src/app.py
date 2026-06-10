"""Cyber Controller — main entry point.

Usage:
    cyber-controller [--ui qt|tk|tui|web] [--log-level DEBUG|INFO|WARNING|ERROR]

Parses CLI arguments, initialises logging, and launches the selected UI.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import sys
from pathlib import Path

log = logging.getLogger("cyber-controller")

_UI_CHOICES = ("qt", "tk", "tui", "web")
_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_LOG_DATE = "%H:%M:%S"


# ── CLI ──────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cyber-controller",
        description="Cyberdeck-oriented all-in-one security hardware controller.",
    )
    parser.add_argument(
        "--ui",
        choices=_UI_CHOICES,
        default="qt",
        help="UI backend to launch (default: qt).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to a log file.",
    )
    return parser.parse_args(argv)


# ── Logging ──────────────────────────────────────────────────────────

def _setup_logging(level: str, log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
    root.addHandler(console)

    # Optional file handler
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
        root.addHandler(fh)


# ── Core bootstrapping ──────────────────────────────────────────────

def _bootstrap():
    """Create shared core objects used by every UI."""
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.firmware_vault import FirmwareVault
    from src.core.health_monitor import HealthMonitor
    from src.core.macro_recorder import MacroRecorder

    dm = DeviceManager()
    fe = FlashEngine()
    bus = EventBus()
    pool = TargetPool(bus)
    vault = FirmwareVault()
    health = HealthMonitor()
    macro = MacroRecorder()

    dm.start_hotplug()
    atexit.register(dm.shutdown)

    return dm, fe, bus, pool, vault, health, macro


# ── UI launchers ─────────────────────────────────────────────────────

def _launch_qt(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    from src.ui.qt.main_window import launch_qt
    return launch_qt(dm, fe, bus, pool, vault, health, macro)


def _launch_tk(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    log.info("Tkinter UI — placeholder.  Use --ui qt for the full interface.")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("Cyber Controller (Tk — placeholder)")
        root.geometry("600x400")
        label = tk.Label(
            root,
            text="Cyber Controller\n\nTkinter UI is a placeholder.\nUse --ui qt for the full interface.",
            font=("Segoe UI", 14),
        )
        label.pack(expand=True)
        root.mainloop()
    except ImportError:
        log.error("Tkinter is not available on this system.")
        return 1
    return 0


def _launch_tui(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    log.info("TUI — placeholder.  Use --ui qt for the full interface.")
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Header, Footer, Static

        class CyberTUI(App):
            TITLE = "Cyber Controller"
            CSS = "Screen { align: center middle; } Static { content-align: center middle; }"

            def compose(self) -> ComposeResult:
                yield Header()
                yield Static("Cyber Controller — TUI placeholder.\nPress Q to quit.")
                yield Footer()

        CyberTUI().run()
    except ImportError:
        log.error("textual is not installed.  pip install textual")
        return 1
    return 0


def _launch_web(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    log.info("Web UI — placeholder.  Use --ui qt for the full interface.")
    try:
        from flask import Flask
        app = Flask(__name__)

        @app.route("/")
        def index():
            return (
                "<h1>Cyber Controller — Web UI placeholder</h1>"
                "<p>Use <code>--ui qt</code> for the full interface.</p>"
            )

        log.info("Starting Flask on http://127.0.0.1:5000")
        app.run(host="127.0.0.1", port=5000, debug=False)
    except ImportError:
        log.error("Flask is not installed.  pip install flask")
        return 1
    return 0


_LAUNCHERS = {
    "qt": _launch_qt,
    "tk": _launch_tk,
    "tui": _launch_tui,
    "web": _launch_web,
}


# ── Main ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.log_level, args.log_file)

    log.info("Cyber Controller starting — ui=%s", args.ui)

    dm, fe, bus, pool, vault, health, macro = _bootstrap()

    launcher = _LAUNCHERS.get(args.ui)
    if launcher is None:
        log.error("Unknown UI backend: %s", args.ui)
        return 1

    try:
        code = launcher(dm, fe, bus, pool, vault, health, macro)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
        code = 0
    except Exception:
        log.exception("Fatal error in UI")
        code = 1
    finally:
        dm.shutdown()

    log.info("Cyber Controller exited (code=%d)", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
