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
import multiprocessing
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
        default=None,
        help="UI backend to launch. If omitted, a graphical launcher dialog "
             "is shown to select the interface.",
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
    parser.add_argument(
        "--deadman-setup",
        action="store_true",
        help="Run the Dead Man's Switch password & duress setup (host-side provisioning) and exit. "
             "Collects a boot password (hashed host-side, never stored) + arm/wipe config and bakes "
             "the guardcfg bundle. Owner-only defensive use.",
    )
    parser.add_argument(
        "--create-physical-key",
        action="store_true",
        help="Provision a USB stick as a physical unlock key, then exit. Owner-only defensive use.",
    )
    parser.add_argument(
        "--key-drive",
        default=None,
        help="USB target path for --create-physical-key (skip the interactive drive picker).",
    )
    parser.add_argument(
        "--set-admin-password",
        action="store_true",
        help="Set the access-gate admin password (stored as a salted scrypt verifier), then exit.",
    )
    parser.add_argument(
        "--gate-policy",
        choices=("both", "either", "password", "key"),
        default=None,
        help="Set the access-gate policy (both = password AND key; either = OR), then exit.",
    )
    parser.add_argument(
        "--gate-status",
        action="store_true",
        help="Print the access-gate configuration and exit.",
    )
    parser.add_argument(
        "--clear-gate",
        action="store_true",
        help="Remove the access gate (admin password + physical key), then exit.",
    )
    parser.add_argument(
        "--flash-tails",
        action="store_true",
        help="Flash the Tails OS (amnesiac live OS) image to a removable USB, then exit. Destructive.",
    )
    parser.add_argument("--tails-image", default=None, help="Path to a local Tails .img to flash (recommended: download + verify from tails.net first).")
    parser.add_argument("--tails-sha256", default=None, help="Expected SHA-256 of the Tails image (from the official checksum).")
    parser.add_argument("--tails-sig", default=None, help="Path to the detached OpenPGP .sig to verify against the Tails signing key (needs gpg).")
    parser.add_argument("--target", default=None, help="Target removable device for --flash-tails / --flash-os (e.g. \\\\.\\PhysicalDrive2 or /dev/sdX); skips the picker.")
    parser.add_argument("--yes", action="store_true", help="Skip the destructive-write confirmation prompt (use with care).")
    # Software-OS catalog (Kali / Tails / Arch / ... to USB)
    parser.add_argument("--list-os", action="store_true", help="List the flashable PC/USB operating systems in the catalog, then exit.")
    parser.add_argument("--flash-os", default=None, metavar="ID", help="Flash a catalog OS (e.g. kali, tails, arch) to a removable USB, then exit. Destructive.")
    parser.add_argument("--os-image", default=None, help="Path to a local OS image (.iso/.img) for --flash-os (skips download).")
    parser.add_argument("--os-sig", default=None, help="Path to a detached OpenPGP .sig for --flash-os (image_sig OSes).")
    parser.add_argument("--offline", action="store_true", help="For --flash-os: use the bundled (pinned) version instead of resolving the latest online.")
    return parser.parse_args(argv)


# ── Logging ──────────────────────────────────────────────────────────

def _setup_logging(level: str, log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
    root.addHandler(console)

    # In-memory ring buffer so Help ▸ Report a Bug can attach recent logs (redacted). Session-only.
    try:
        from src.core.diagnostics import install_ring_handler

        install_ring_handler()
    except Exception:  # noqa: BLE001 — diagnostics capture must never block startup
        pass

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
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.firmware_vault import FirmwareVault
    from src.core.flash_engine import FlashEngine
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
    # Ship with starter macros: seed the bundled cc_*.json builtins on first run so the list is not
    # empty. Idempotent + never clobbers a user macro; a builtin the user deletes is not re-seeded.
    try:
        seeded = macro.seed_default_macros()
        if seeded:
            log.info("Seeded %d starter macro(s) into the macros dir", len(seeded))
    except Exception:
        log.debug("Starter-macro seeding skipped", exc_info=True)
    # L-2: durable, owner-only hash-chained audit trail (loads + verifies any prior chain).
    from pathlib import Path
    audit = AuditTrail(persist_path=Path.home() / ".cyber-controller" / "audit-trail.jsonl")
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
        # The web UI has no window of its own — it serves a browser. In a packaged --windowed build
        # there's no console to show the URL, so a user who picked "Web Remote" from the launcher would
        # see nothing. Open the default browser at the server URL once it's had a moment to start.
        _open_browser_when_ready(host, port)
        return launch_web(dm, fe, bus, pool, host=host, port=port, audit=audit)
    except ImportError:
        log.error("Flask is not installed.  pip install cyber-controller[web]")
        return 1


def _open_browser_when_ready(host: str, port: int) -> None:
    """Open the default browser at the web-UI URL after a short delay (server warm-up), in a daemon
    thread so it never blocks the server. Localhost is used for display when bound to 0.0.0.0."""
    import threading
    import webbrowser

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}"

    def _go() -> None:
        import time
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            log.info("Web remote available at %s", url)

    log.info("Web remote starting at %s", url)
    threading.Thread(target=_go, name="open-web-browser", daemon=True).start()


_LAUNCHERS = {
    "qt": _launch_qt,
    "tk": _launch_tk,
    "tui": _launch_tui,
    "web": _launch_web,
}


# ── Main ─────────────────────────────────────────────────────────────

def _acquire_instance_lock():
    """Prevent multiple instances on Windows via named mutex."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "CyberController_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    # Frozen-build esptool dispatcher. In a PyInstaller build sys.executable is CyberController.exe, so
    # flash_core routes every esptool op back to this binary as `--_run-esptool <args>`. Run the BUNDLED
    # esptool in-process and exit. This MUST precede the single-instance lock — the esptool "subprocess"
    # is a child of the running GUI, and the lock would otherwise abort it as a duplicate instance.
    _argv = sys.argv[1:] if argv is None else argv
    if _argv and _argv[0] == "--_run-esptool":
        import esptool
        return esptool.main(_argv[1:])

    args = _parse_args(argv)
    _setup_logging(args.log_level, args.log_file)

    # Smart install: reconcile the persistent config (~/.cyber-controller) against this version —
    # migrate-on-upgrade (silent), record the version, and flag a downgrade so the GUI can prompt to
    # keep or back-up-and-start-fresh. Safe + silent for headless/CLI flows.
    try:
        from src.core import install
        install.reconcile()
    except Exception:
        log.debug("install reconcile skipped", exc_info=True)

    # Dead Man's Switch password & duress setup is a standalone host-side flow — no UI bootstrap.
    if args.deadman_setup:
        from src.core.suicide_setup import run_cli
        return run_cli()

    # Access-gate management (standalone) — run the requested action and exit.
    from src.security import access_gate as _ag
    from src.security import physical_key as _pk
    # Boot-attack resistance: mutating an ALREADY-configured gate (add a key, change the password,
    # change policy, or clear it) must first PASS the existing gate. Otherwise `--clear-gate` /
    # `--set-admin-password` would be a pre-auth startup bypass — an attacker could reset/disable the
    # gate without knowing the current factor(s). First-time setup (no gate yet) needs no auth; the
    # read-only `--gate-status` never does. (The vault itself stays fail-closed regardless, but this
    # also protects the gate as an access control and prevents skipping the brute-force/duress logic.)
    # A "gate mutation" is any subcommand that changes the gate. Split out the non-clear mutations so the
    # corrupt-config block below derives from the SAME set (never a hand-copied duplicate that could drift):
    # add a new mutation flag here and it flows into both the "any mutation?" gate and the corrupt block.
    _non_clear_gate_mutation = (
        args.create_physical_key or args.set_admin_password or args.gate_policy
    )
    _gate_mutation = _non_clear_gate_mutation or args.clear_gate
    if _gate_mutation:
        if _pk.config_is_corrupt():
            # CC-GATE: a present-but-unreadable gate config is CONFIGURED-AND-LOCKED, never "absent".
            # is_configured() reports False for a corrupt config (password/key can't be read), so the old
            # `and is_configured()` guard skipped enforce() and let a mutation run PRE-AUTH — a fail-open
            # that contradicts the fail-closed invariant the runtime path holds (enforce() and
            # check_access() both refuse a corrupt config). enforce() can't authenticate a config it can't
            # parse, so the ONLY thing allowed here is a PURE --clear-gate: the recovery path that resets the
            # unreadable gate so the owner can reprovision. Clearing leaves the encrypted vault in place, and
            # enforce() still fails closed on vault-without-gate afterward, so no protected data is exposed.
            # NB: the action dispatch below runs create/set/policy BEFORE clear_gate, so a co-passed mutation
            # (e.g. `--set-admin-password --clear-gate`) must ALSO be blocked — otherwise --clear-gate would
            # wave a real mutation through pre-auth on a config we can't authenticate against.
            if _non_clear_gate_mutation:
                print("Locked: the access-gate configuration is unreadable/corrupt. Only --clear-gate "
                      "(on its own) may proceed — it resets the gate so you can reprovision.", file=sys.stderr)
                return 1  # nonzero: the mutation was BLOCKED — a script checking $? must not read it as done
            # else: only --clear-gate was requested — fall through to the recovery path (no enforce()).
        elif _pk.is_configured():
            if not _ag.enforce("console"):
                print("Access denied — authenticate to modify the access gate.", file=sys.stderr)
                return 1  # nonzero: the mutation was BLOCKED — a script checking $? must not read it as done
    if args.gate_status:
        return _ag.status_cli()
    if args.create_physical_key:
        return _ag.create_key_cli(args.key_drive)
    if args.set_admin_password:
        return _ag.set_password_cli()
    if args.gate_policy:
        return _ag.set_policy_cli(args.gate_policy)
    if args.clear_gate:
        return _ag.clear_cli()

    # Flash Tails OS to a USB (standalone, destructive) — run and exit.
    if args.flash_tails:
        from src.core.tails import run_flash_cli as _flash_tails
        return _flash_tails(target=args.target, image=args.tails_image,
                            sha256=args.tails_sha256, sig=args.tails_sig, assume_yes=args.yes)

    # Software-OS catalog (Kali / Tails / Arch / ...) — list or flash, then exit.
    if args.list_os:
        from src.core.os_catalog import list_catalog_cli
        return list_catalog_cli()
    if args.flash_os:
        from src.core.os_catalog import run_os_flash_cli
        return run_os_flash_cli(args.flash_os, target=args.target, image=args.os_image,
                                sig=args.os_sig, assume_yes=args.yes, offline=args.offline)

    # Single-instance lock guards ONLY the interactive launch (GUI/TUI/web) — never the headless one-shot
    # CLI subcommands handled above. Those (--deadman-setup, --flash-os, --flash-tails, gate mutations, ...)
    # are routinely run from a terminal WHILE the GUI is open — the DMS fail-safe even directs the user to
    # run `--deadman-setup` right after clicking Flash — so acquiring the lock before them made them a
    # silent no-op that returned 0 (success). A genuine second GUI launch is refused with a nonzero code.
    if not _acquire_instance_lock():
        print("Cyber Controller is already running.", file=sys.stderr)
        return 1

    # If no --ui flag was given, show the launcher dialog to let the user pick.
    if args.ui is None:
        try:
            from src.ui.launcher import select_ui
            args.ui = select_ui()
        except Exception:
            log.warning("Launcher dialog unavailable, defaulting to qt")
            args.ui = "qt"

    log.info("Cyber Controller starting — ui=%s", args.ui)

    # Access gate (physical key / admin password). Fail-closed: a denied/cancelled gate exits
    # before any device bootstrap. A no-op when no gate is configured.
    from src.security.access_gate import enforce as _enforce_gate
    if not _enforce_gate(args.ui):
        log.warning("Access denied — exiting.")
        print("Access denied.", file=sys.stderr)
        return 1  # nonzero: the gate DENIED startup — never report success on a fail-closed exit

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
    multiprocessing.freeze_support()
    sys.exit(main())
