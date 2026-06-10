"""Cyber Controller — main entry point.

Usage:
    cyber-controller [--ui qt|tk|tui|web] [--log-level DEBUG|INFO|WARNING|ERROR]
    cyber-controller --ui web [--host 0.0.0.0] [--port 5000]

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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web UI bind address (default: 127.0.0.1 — local only). "
             "Use 0.0.0.0 for LAN ONLY with CC_WEB_ALLOW_LAN=1 (TLS recommended).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Web UI port (default: 5000).",
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
    from src.security.audit_trail import AuditTrail

    dm = DeviceManager()
    fe = FlashEngine()
    bus = EventBus()
    pool = TargetPool(bus)
    vault = FirmwareVault()
    health = HealthMonitor()
    macro = MacroRecorder()
    audit = AuditTrail()
    audit.record("app_start", {})

    dm.start_hotplug()
    atexit.register(dm.shutdown)

    return dm, fe, bus, pool, vault, health, macro, audit


# ── UI launchers ─────────────────────────────────────────────────────

def _launch_qt(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    from src.ui.qt.main_window import launch_qt
    return launch_qt(dm, fe, bus, pool, vault, health, macro)


def _launch_tk(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    log.info("Launching Tkinter lightweight UI")
    try:
        from src.ui.tk.app import launch_tk
        return launch_tk(dm, fe, bus, pool)
    except ImportError:
        log.error("Tkinter is not available on this system.")
        return 1


def _launch_tui(dm, fe, bus, pool, vault=None, health=None, macro=None) -> int:
    log.info("Launching Textual TUI")
    try:
        from src.ui.tui.app import launch_tui
        return launch_tui(dm, fe, bus, pool)
    except ImportError:
        log.error("textual is not installed.  pip install cyber-controller[tui]")
        return 1


def _launch_web(dm, fe, bus, pool, vault=None, health=None, macro=None,
                host="127.0.0.1", port=5000, audit=None) -> int:
    log.info("Launching Flask web remote UI")
    try:
        from src.ui.web.app import launch_web
        return launch_web(dm, fe, bus, pool, host=host, port=port, audit=audit)
    except ImportError:
        log.error("Flask is not installed.  pip install cyber-controller[web]")
        return 1


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

    dm, fe, bus, pool, vault, health, macro, audit = _bootstrap()

    launcher = _LAUNCHERS.get(args.ui)
    if launcher is None:
        log.error("Unknown UI backend: %s", args.ui)
        return 1

    try:
        if args.ui == "web":
            code = launcher(dm, fe, bus, pool, vault, health, macro,
                            host=args.host, port=args.port, audit=audit)
        else:
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
