"""Flash engine — firmware flashing with esptool and extensible backends."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]  # (percent, message)


class FlashStatus(Enum):
    IDLE = "idle"
    FLASHING = "flashing"
    BACKING_UP = "backing_up"
    DONE = "done"
    ERROR = "error"


@dataclass
class FirmwareProfile:
    """A firmware flash profile loaded from JSON.

    Attributes:
        name: Human-readable firmware name.
        board: Target board type string.
        backend: Flash backend to use ('esptool', 'adb', 'qflipper', 'sd').
        files: Dict mapping flash address (hex string) to binary path.
        baud: Flash baud rate.
        chip: Chip type for esptool (e.g. 'esp32', 'esp32s3').
        erase_first: Whether to erase flash before writing.
        extra_args: Additional backend-specific arguments.
    """

    name: str = ""
    board: str = ""
    backend: str = "esptool"
    files: dict[str, str] = field(default_factory=dict)
    baud: int = 921600
    chip: str = "esp32"
    erase_first: bool = True
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> FirmwareProfile:
        """Load a profile from a JSON file."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── Regex for esptool progress ───────────────────────────────────────

_RE_PROGRESS = re.compile(r"(\d+)\s*%")
_RE_HASH = re.compile(r"Hash of data verified")
_RE_WRITE = re.compile(r"Writing at 0x[\da-fA-F]+")
_RE_ERASE = re.compile(r"Erasing")
_RE_CONNECT = re.compile(r"Connecting")
_RE_CHIP = re.compile(r"Chip is (.+)")
_RE_READ_PROGRESS = re.compile(r"(\d+)\s*%")


# ── Flash backends ───────────────────────────────────────────────────

class EsptoolBackend:
    """Flash backend that drives esptool as a subprocess."""

    def flash(
        self,
        port: str,
        profile: FirmwareProfile,
        progress: ProgressCallback | None = None,
    ) -> bool:
        """Flash firmware to *port* using *profile*.

        Returns:
            True on success, False on failure.
        """
        cmd = self._build_flash_cmd(port, profile)
        log.info("esptool flash: %s", " ".join(cmd))
        return self._run(cmd, progress)

    def backup(
        self,
        port: str,
        output_path: str | Path,
        chip: str = "esp32",
        size: str = "0x400000",
        baud: int = 921600,
        progress: ProgressCallback | None = None,
    ) -> bool:
        """Read entire flash contents to *output_path*.

        Returns:
            True on success.
        """
        cmd = [
            sys.executable, "-m", "esptool",
            "--chip", chip,
            "--port", port,
            "--baud", str(baud),
            "read_flash",
            "0x0", size,
            str(output_path),
        ]
        log.info("esptool backup: %s", " ".join(cmd))
        return self._run(cmd, progress)

    def erase(
        self,
        port: str,
        chip: str = "esp32",
        progress: ProgressCallback | None = None,
    ) -> bool:
        """Erase entire flash."""
        cmd = [
            sys.executable, "-m", "esptool",
            "--chip", chip,
            "--port", port,
            "erase_flash",
        ]
        log.info("esptool erase: %s", " ".join(cmd))
        return self._run(cmd, progress)

    # ── Internal ─────────────────────────────────────────────────────

    def _build_flash_cmd(self, port: str, profile: FirmwareProfile) -> list[str]:
        cmd = [
            sys.executable, "-m", "esptool",
            "--chip", profile.chip,
            "--port", port,
            "--baud", str(profile.baud),
        ]
        if profile.extra_args:
            cmd.extend(profile.extra_args)

        cmd.append("write_flash")

        if profile.erase_first:
            cmd.append("--erase-all")

        for addr, filepath in profile.files.items():
            cmd.extend([addr, str(filepath)])

        return cmd

    def _run(self, cmd: list[str], progress: ProgressCallback | None) -> bool:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            log.error("esptool not found — is it installed?")
            if progress:
                progress(0, "Error: esptool not found")
            return False

        last_pct = -1
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            log.debug("esptool: %s", line)

            m = _RE_PROGRESS.search(line)
            if m and progress:
                pct = int(m.group(1))
                if pct != last_pct:
                    progress(pct, line)
                    last_pct = pct
            elif progress:
                if _RE_CONNECT.search(line):
                    progress(0, "Connecting...")
                elif _RE_ERASE.search(line):
                    progress(0, "Erasing flash...")
                elif _RE_HASH.search(line):
                    progress(100, "Verified!")

        rc = proc.wait()
        if rc != 0:
            log.error("esptool exited with code %d", rc)
            if progress:
                progress(0, f"Error: esptool exited with code {rc}")
            return False

        if progress:
            progress(100, "Flash complete")
        return True


class ADBBackend:
    """Placeholder backend for Android ADB flashing."""

    def flash(self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None = None) -> bool:
        log.warning("ADB backend not yet implemented")
        if progress:
            progress(0, "ADB backend not yet implemented")
        return False

    def backup(self, port: str, output_path: str | Path, progress: ProgressCallback | None = None) -> bool:
        log.warning("ADB backup not yet implemented")
        return False


class QFlipperBackend:
    """Placeholder backend for Flipper Zero flashing via qFlipper."""

    def flash(self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None = None) -> bool:
        log.warning("qFlipper backend not yet implemented")
        if progress:
            progress(0, "qFlipper backend not yet implemented")
        return False

    def backup(self, port: str, output_path: str | Path, progress: ProgressCallback | None = None) -> bool:
        log.warning("qFlipper backup not yet implemented")
        return False


class SDBackend:
    """Placeholder backend for SD card image writing."""

    def flash(self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None = None) -> bool:
        log.warning("SD backend not yet implemented")
        if progress:
            progress(0, "SD backend not yet implemented")
        return False

    def backup(self, port: str, output_path: str | Path, progress: ProgressCallback | None = None) -> bool:
        log.warning("SD backup not yet implemented")
        return False


# ── Engine ───────────────────────────────────────────────────────────

_BACKENDS: dict[str, type] = {
    "esptool": EsptoolBackend,
    "adb": ADBBackend,
    "qflipper": QFlipperBackend,
    "sd": SDBackend,
}


class FlashEngine:
    """High-level flash orchestrator.

    Selects the correct backend based on the firmware profile and
    exposes :meth:`flash` / :meth:`backup` with progress callbacks.
    """

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

    def flash(
        self,
        port: str,
        profile: FirmwareProfile,
        progress_callback: ProgressCallback | None = None,
        *,
        async_: bool = False,
    ) -> bool | None:
        """Flash firmware to *port*.

        Args:
            port: Serial port.
            profile: Firmware profile.
            progress_callback: (percent, message) callback.
            async_: If True, run in a background thread and return None.

        Returns:
            True/False on synchronous success/failure, None if async.
        """
        if async_:
            t = threading.Thread(
                target=self._do_flash,
                args=(port, profile, progress_callback),
                daemon=True,
            )
            t.start()
            self._current_thread = t
            return None
        return self._do_flash(port, profile, progress_callback)

    def backup(
        self,
        port: str,
        output_path: str | Path,
        progress_callback: ProgressCallback | None = None,
        *,
        chip: str = "esp32",
        size: str = "0x400000",
    ) -> bool:
        """Read flash contents to a file."""
        with self._lock:
            self._status = FlashStatus.BACKING_UP

        backend = self._get_backend("esptool")
        if isinstance(backend, EsptoolBackend):
            ok = backend.backup(port, output_path, chip=chip, size=size, progress=progress_callback)
        else:
            ok = backend.backup(port, output_path, progress=progress_callback)

        with self._lock:
            self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
        return ok

    # ── Internal ─────────────────────────────────────────────────────

    def _do_flash(
        self,
        port: str,
        profile: FirmwareProfile,
        progress: ProgressCallback | None,
    ) -> bool:
        with self._lock:
            self._status = FlashStatus.FLASHING

        backend = self._get_backend(profile.backend)
        ok = backend.flash(port, profile, progress)

        with self._lock:
            self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
        return ok

    @staticmethod
    def _get_backend(name: str) -> Any:
        cls = _BACKENDS.get(name)
        if cls is None:
            raise ValueError(f"Unknown flash backend: {name!r}")
        return cls()
