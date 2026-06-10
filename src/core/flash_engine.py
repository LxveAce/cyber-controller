"""Flash engine — firmware flashing orchestrator.

This is a thin, UI-facing orchestrator over the hardware-validated
:mod:`src.core.flash_core` (esptool plumbing, 15 firmware profiles, SSRF + path-
traversal hardening, TOCTOU-safe suicide-bundle flashing) and the real backend
modules under :mod:`src.core.backends` (ADB, SD-image). It keeps the stable public
surface the UIs call — ``FlashEngine.flash/backup/status`` and
``FirmwareProfile.from_file`` — but routes the actual work to the proven code.

Key reliability properties inherited from flash_core:
    * esptool ``write_flash -z --flash_size detect --before default_reset
      --after hard_reset`` (the ``--flash_size detect`` patch prevents a 4MB board
      boot-looping on a 16MB-header image — the single most important reliability flag);
    * chip auto-detection via ``esptool chip_id`` (never hardcoded);
    * correct per-chip bootloader offsets (0x1000 / 0x0 / **0x2000 for C5**);
    * child-process kill+reap on error so the serial port is released.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from src.core import flash_core, profile_loader

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]  # (percent, message)

_RE_PROGRESS = re.compile(r"(\d+)\s*%")


class FlashStatus(Enum):
    IDLE = "idle"
    FLASHING = "flashing"
    BACKING_UP = "backing_up"
    DONE = "done"
    ERROR = "error"


@dataclass
class FirmwareProfile:
    """A firmware flash profile loaded from JSON.

    Carries BOTH the flat fields the engine needs and the rich fields the shipped
    profiles use (``id``/``boards``/``firmware_urls``/``protocol``/``default_baud``)
    so nothing is silently dropped. ``core_id`` is the resolved
    :mod:`src.core.flash_core` profile key.
    """

    name: str = ""
    id: str = ""
    board: str = ""
    backend: str = "esptool"
    protocol: str = ""
    files: dict[str, str] = field(default_factory=dict)
    baud: int = 921600
    chip: str = "auto"
    erase_first: bool = False
    extra_args: list[str] = field(default_factory=list)
    flash_mode: str = "full"  # 'full' (blank board) or 'app' (update only)
    local_path: str = ""  # explicit local .bin to flash, if any
    boards: list = field(default_factory=list)
    firmware_urls: dict = field(default_factory=dict)
    default_baud: int = 921600
    core_id: str = "custom"
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "FirmwareProfile":
        """Load a profile from a JSON file (rich schema preserved)."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        json_id = data.get("id", path.stem)
        # Back-compat: a legacy flat profile may already carry files/chip/baud.
        files = data.get("files", {}) if isinstance(data.get("files"), dict) else {}
        return cls(
            name=data.get("name", path.stem),
            id=json_id,
            board=data.get("board", ""),
            backend=data.get("backend", "esptool"),
            protocol=data.get("protocol") or "",
            files=files,
            # FLASH baud (fast, proven 921600) — distinct from default_baud, which is
            # the device's serial-MONITOR baud (e.g. 115200 for Marauder).
            baud=int(data.get("flash_baud") or data.get("baud") or 921600),
            chip=data.get("chip") or profile_loader.select_chip(data),
            erase_first=bool(data.get("erase_first", False)),
            extra_args=data.get("extra_args", []) if isinstance(data.get("extra_args"), list) else [],
            flash_mode=data.get("flash_mode", "full"),
            local_path=data.get("local_path", ""),
            boards=profile_loader.list_boards(data),
            firmware_urls=data.get("firmware_urls", {}) if isinstance(data.get("firmware_urls"), dict) else {},
            default_baud=profile_loader.default_baud(data),
            core_id=profile_loader.core_id_for(json_id),
            raw=data,
        )


def _percent_adapter(progress: ProgressCallback | None) -> Callable[[str], None]:
    """Wrap a (percent, message) callback as flash_core's on_line(str) callback,
    parsing esptool progress percentages out of the streamed lines."""
    last = {"pct": -1}

    def on_line(line: str) -> None:
        if progress is None:
            return
        m = _RE_PROGRESS.search(line)
        if m:
            pct = int(m.group(1))
            if pct != last["pct"]:
                last["pct"] = pct
                progress(pct, line)
                return
        progress(max(last["pct"], 0), line)

    return on_line


class FlashEngine:
    """High-level flash orchestrator. Routes by backend to the proven flash core."""

    def __init__(self) -> None:
        self._status = FlashStatus.IDLE
        self._lock = threading.Lock()
        self._current_thread: threading.Thread | None = None

    @property
    def status(self) -> FlashStatus:
        return self._status

    def load_profile(self, path: str | Path) -> FirmwareProfile:
        """Load and return a firmware profile from a JSON file."""
        return FirmwareProfile.from_file(path)

    # ── Flash ────────────────────────────────────────────────────────

    def flash(
        self,
        port: str,
        profile: FirmwareProfile,
        progress_callback: ProgressCallback | None = None,
        *,
        async_: bool = False,
    ) -> bool | None:
        """Flash firmware to *port* using *profile*.

        Returns True/False synchronously, or None when ``async_=True``.
        """
        if async_:
            t = threading.Thread(
                target=self._do_flash, args=(port, profile, progress_callback), daemon=True
            )
            t.start()
            self._current_thread = t
            return None
        return self._do_flash(port, profile, progress_callback)

    def _do_flash(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        with self._lock:
            self._status = FlashStatus.FLASHING
        try:
            backend = (profile.backend or "esptool").lower()
            if backend == "esptool":
                ok = self._flash_esptool(port, profile, progress)
            elif backend == "qflipper":
                ok = self._flash_qflipper(port, profile, progress)
            elif backend == "adb":
                ok = self._flash_adb(port, profile, progress)
            elif backend in ("sd", "sd-image"):
                ok = self._flash_sd(port, profile, progress)
            else:
                if progress:
                    progress(0, f"Unknown backend: {profile.backend}")
                ok = False
        except Exception as exc:  # never let a backend exception leak unlabelled
            log.exception("flash failed")
            if progress:
                progress(0, f"Error: {exc}")
            ok = False
        with self._lock:
            self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
        return ok

    # ── esptool (the bulk of firmwares) ──────────────────────────────

    def _flash_esptool(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        on_line = _percent_adapter(progress)

        # Resolve chip: explicit > board chip > auto-detect via 'esptool chip_id'.
        chip = profile.chip
        if not chip or chip == "auto":
            on_line("[chip] detecting...")
            chip = flash_core.detect_chip(port, on_line) or "esp32"
            on_line(f"[chip] using {chip}")

        # Local-file flash (explicit .bin) — merged image at 0x0 by default.
        if profile.local_path:
            custom = flash_core.get_profile("custom")
            rc = custom.flash_local(port, chip, profile.local_path, on_line, baud=profile.baud)
            if progress:
                progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
            return rc == 0

        # Download-and-flash via the proven per-profile logic in flash_core.
        core_id = profile.core_id if profile.core_id in flash_core.PROFILES else "custom"
        if core_id == "custom":
            on_line("[error] no flash-core profile for this firmware and no local .bin provided")
            return False
        core = flash_core.get_profile(core_id)

        try:
            on_line(f"[release] fetching latest {core_id} release...")
            _tag, assets = core.latest_release()
        except Exception as exc:
            on_line(f"[error] could not fetch release: {exc}")
            return False
        variant = core.default_variant(assets, chip)
        if not variant:
            on_line(f"[error] no firmware asset for chip {chip} in the {core_id} release")
            return False

        cache = flash_core.cache_dir()
        try:
            app_path = flash_core.download_to(variant["url"], cache, variant["name"], on_line)
        except Exception as exc:
            on_line(f"[error] download failed: {exc}")
            return False

        support = None
        mode = profile.flash_mode if profile.flash_mode in ("app", "full") else "full"
        if mode == "full":
            try:
                support = core.support_files(chip, cache, on_line)
            except Exception as exc:
                # Merged-image profiles legitimately return None; a real failure on a
                # multi-file profile means we can't safely do a blank-board flash.
                on_line(f"[warn] no support files ({exc}); falling back to app-only flash")
                mode = "app"

        app_offset = variant.get("offset") or core.app_offset(chip)
        rc = core.flash_assets(
            port, chip, app_path, on_line, mode=mode, baud=profile.baud,
            support=support, app_offset=app_offset,
        )
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    # ── qFlipper (Flipper Zero firmwares) ────────────────────────────

    def _flash_qflipper(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        """Best-effort Flipper Zero update via the qFlipper CLI.

        Flipper firmware (Momentum/Unleashed) ships as web-update .tgz/.dfu packages,
        not esptool images. We download the release asset and hand it to a locally
        installed ``qFlipper`` if present; otherwise we report clearly that the user
        needs qFlipper or the web updater (we never pretend a flash happened).
        """
        import shutil
        import subprocess

        on_line = _percent_adapter(progress)
        qflipper = shutil.which("qFlipper") or shutil.which("qflipper")
        if not qflipper:
            on_line("[qflipper] qFlipper not found on PATH. Install qFlipper "
                    "(https://flipperzero.one/update) or use the web updater; "
                    "Flipper firmware cannot be flashed with esptool.")
            return False
        on_line(f"[qflipper] using {qflipper} — launching firmware update")
        try:
            proc = subprocess.Popen(
                [qflipper, "--update", profile.local_path] if profile.local_path else [qflipper],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line.rstrip())
            rc = proc.wait()
        except Exception as exc:
            on_line(f"[qflipper] error: {exc}")
            return False
        return rc == 0

    # ── ADB (RayHunter / Orbic RC400L) ───────────────────────────────

    def _flash_adb(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        from src.core.backends import adb_backend

        on_line = _percent_adapter(progress)
        if not adb_backend.find_adb():
            on_line("[adb] adb not found. Install Android platform-tools.")
            return False
        try:
            # full_install(on_line, serial=None auto-picks) returns an esptool-style rc.
            rc = adb_backend.full_install(on_line)
            return rc == 0
        except Exception as exc:
            on_line(f"[adb] install failed: {exc}")
            return False

    # ── SD image (Raspberry Pi firmwares) ────────────────────────────

    def _flash_sd(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        from src.core.backends import sd_backend

        on_line = _percent_adapter(progress)
        on_line("[sd] SD-card imaging requires choosing a removable target device and "
                "explicit confirmation (and Administrator/root). Use the SD flow which "
                "calls sd_backend.flash_sd(profile_id, device, confirmed=True).")
        # The SD backend intentionally refuses to write without an explicit device +
        # confirmed=True (a safety invariant); the UI drives that flow, not a 'port'.
        return False

    # ── Backup ───────────────────────────────────────────────────────

    def backup(
        self,
        port: str,
        output_path: str | Path,
        progress_callback: ProgressCallback | None = None,
        *,
        chip: str = "auto",
        size: str = "0x400000",
    ) -> bool:
        """Read the entire flash to *output_path* (exact file) via the proven esptool
        plumbing, auto-detecting the chip when ``chip='auto'``.

        (For a richer backup-with-restore + .meta sidecar + listing, see
        :mod:`src.core.backup`, surfaced through a dedicated backup flow.)
        """
        with self._lock:
            self._status = FlashStatus.BACKING_UP
        on_line = _percent_adapter(progress_callback)
        if not chip or chip == "auto":
            chip = flash_core.detect_chip(port, on_line) or "esp32"
        argv = flash_core.esptool_argv(
            "--chip", chip, "--port", port, "--baud", "921600",
            "read_flash", "0x0", size, str(output_path),
        )
        rc = flash_core._run_stream(argv, on_line)
        ok = rc == 0
        if progress_callback:
            progress_callback(100 if ok else 0, "Backup complete" if ok else "Backup failed")
        with self._lock:
            self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
        return ok
