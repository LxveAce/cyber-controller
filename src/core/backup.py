"""Firmware backup/dump — read current firmware from an ESP32 before flashing.

Wraps esptool read_flash to dump the entire flash contents to a local file so the
user can restore if something goes wrong. Also supports restoring from a backup.
"""

import os
import time
from typing import Callable, Optional

from src.core.flash_core import (
    esptool_argv,
    _run_stream,
    _detect_chip,
    _BOOTLOADER_0,
    _sha256_file,
)

Line = Callable[[str], None]


def _data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        d = os.path.join(base, "universal-flasher", "backups")
    else:
        d = os.path.expanduser("~/.universal-flasher/backups")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


def backup_flash(port: str, on_line: Line, chip: Optional[str] = None,
                 output_dir: Optional[str] = None, flash_size: str = "detect",
                 label: str = "") -> Optional[str]:
    """Dump the entire flash contents from an ESP32 to a local file."""
    if not chip:
        on_line("[backup] Detecting chip...")
        chip = _detect_chip(port, on_line)
        if not chip:
            on_line(f"[error] Could not detect chip on {port}")
            return None

    dest_dir = output_dir or _data_dir()
    os.makedirs(dest_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    port_safe = _safe_filename(port.replace("/dev/", "").replace("\\", "_").replace(".", "_"))
    name_parts = [chip, port_safe, timestamp]
    if label:
        name_parts.insert(0, _safe_filename(label))
    filename = "_".join(name_parts) + ".bin"
    dest = os.path.join(dest_dir, filename)

    if flash_size == "detect":
        on_line("[backup] Detecting flash size...")
        size_argv = esptool_argv("--chip", chip, "--port", port, "flash_id")
        size_lines = []

        def cap(s: str):
            size_lines.append(s)
            on_line(s)

        _run_stream(size_argv, cap)
        detected_size = "0x400000"
        for line in size_lines:
            if "Detected flash size:" in line:
                size_str = line.split(":")[-1].strip()
                size_map = {
                    "1MB": "0x100000", "2MB": "0x200000", "4MB": "0x400000",
                    "8MB": "0x800000", "16MB": "0x1000000", "32MB": "0x2000000",
                }
                detected_size = size_map.get(size_str, "0x400000")
                break
        flash_size = detected_size

    on_line(f"[backup] Reading {flash_size} bytes from {port} ({chip})...")
    on_line(f"[backup] Saving to: {dest}")

    argv = esptool_argv(
        "--chip", chip, "--port", port, "--baud", "921600",
        "read_flash", "0x0", flash_size, dest,
    )
    rc = _run_stream(argv, on_line)

    if rc != 0:
        on_line(f"[error] Backup failed (exit code {rc})")
        return None

    if os.path.isfile(dest):
        size = os.path.getsize(dest)
        sha = _sha256_file(dest)
        on_line(f"[backup] Success: {size} bytes, SHA256: {sha[:16]}...")
        on_line(f"[backup] Saved: {dest}")

        meta_path = dest + ".meta"
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"chip={chip}\n")
            f.write(f"port={port}\n")
            f.write(f"flash_size={flash_size}\n")
            f.write(f"sha256={sha}\n")
            f.write(f"timestamp={timestamp}\n")
            if label:
                f.write(f"label={label}\n")
        return dest

    on_line("[error] Backup file not created")
    return None


def restore_flash(port: str, backup_path: str, on_line: Line,
                  chip: Optional[str] = None, verify: bool = True) -> int:
    """Restore a flash backup to an ESP32 device."""
    if not os.path.isfile(backup_path):
        on_line(f"[error] Backup file not found: {backup_path}")
        return 1

    if not chip:
        meta_path = backup_path + ".meta"
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("chip="):
                        chip = line.split("=", 1)[1].strip()
                        break
        if not chip:
            on_line("[backup] Detecting chip...")
            chip = _detect_chip(port, on_line)
            if not chip:
                on_line("[error] Could not detect chip")
                return 1

    size = os.path.getsize(backup_path)
    on_line(f"[restore] Writing {size} bytes to {port} ({chip})...")

    argv = esptool_argv(
        "--chip", chip, "--port", port, "--baud", "921600",
        "write_flash", "-z", "--flash_size", "detect",
        "0x0", backup_path,
    )
    rc = _run_stream(argv, on_line)

    if rc == 0 and verify:
        on_line("[restore] Verifying write...")
        argv = esptool_argv(
            "--chip", chip, "--port", port, "--baud", "921600",
            "verify_flash", "0x0", backup_path,
        )
        vrc = _run_stream(argv, on_line)
        if vrc != 0:
            on_line("[warning] Verification failed — flash may be corrupt")
            return vrc

    if rc == 0:
        on_line("[restore] Success")
    else:
        on_line(f"[error] Restore failed (exit code {rc})")
    return rc


def list_backups(backup_dir: Optional[str] = None):
    """Return list of available backup files with metadata."""
    d = backup_dir or _data_dir()
    if not os.path.isdir(d):
        return []

    backups = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".bin"):
            continue
        path = os.path.join(d, f)
        meta = {"file": f, "path": path, "size": os.path.getsize(path)}

        meta_path = path + ".meta"
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as mf:
                for line in mf:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        meta[k] = v
        backups.append(meta)
    return backups
