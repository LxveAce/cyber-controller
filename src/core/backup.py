"""Firmware backup/dump — read current firmware from an ESP32 before flashing.

Wraps esptool read_flash to dump the entire flash contents to a local file so the
user can restore if something goes wrong. Also supports restoring from a backup.
"""

import os
import time
from typing import Callable, Optional

from src.core.flash_core import (
    _detect_chip,
    _run_stream,
    _sha256_file,
    esptool_argv,
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

    size_detected = True
    if flash_size == "detect":
        on_line("[backup] Detecting flash size...")
        size_argv = esptool_argv("--chip", chip, "--port", port, "flash_id")
        size_lines = []

        def cap(s: str):
            size_lines.append(s)
            on_line(s)

        _run_stream(size_argv, cap)
        detected_size = "0x400000"
        size_detected = False
        size_map = {
            "1MB": "0x100000", "2MB": "0x200000", "4MB": "0x400000",
            "8MB": "0x800000", "16MB": "0x1000000", "32MB": "0x2000000",
        }
        for line in size_lines:
            if "Detected flash size:" in line:
                size_str = line.split(":")[-1].strip()
                if size_str in size_map:
                    detected_size = size_map[size_str]
                    size_detected = True
                else:
                    on_line(f"[backup] WARNING: unrecognized flash size {size_str!r} — assuming 4 MB.")
                break
        if not size_detected:
            on_line("[backup] WARNING: could NOT detect flash size — assuming a 4 MB read. If this board "
                    "is LARGER than 4 MB, this backup will be TRUNCATED and cannot fully restore it.")
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
        if not size_detected:
            on_line("[backup] ⚠ NOTE: flash size was NOT detected — this backup assumed 4 MB and may be "
                    "TRUNCATED. Do not rely on it to fully restore a board larger than 4 MB.")
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

    # A restore must reproduce the dump VERBATIM, so write with --flash_size keep (NOT detect).
    # With `detect`, esptool re-patches the flash-size nibble in the image header to the chip's
    # physically-detected size; on a chip whose bootloader lives at 0x0 (S3 / C-series / H2) the
    # write address 0x0 IS that header, so the on-flash byte then differs from backup_path. The
    # verify_flash below reads the reference straight from backup_path (esptool's default
    # --flash_size keep), so a `detect` write would trip a spurious 1-byte "flash may be corrupt"
    # mismatch AND leave a non-byte-exact restore. `keep` preserves the exact bytes and stays
    # symmetric with the verify.
    argv = esptool_argv(
        "--chip", chip, "--port", port, "--baud", "921600",
        "write_flash", "-z", "--flash_size", "keep",
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
        meta.update(_read_meta(path))
        backups.append(meta)
    return backups


def _read_meta(backup_path: str) -> dict:
    """Parse a backup's sidecar ``<backup>.meta`` (``key=value`` lines) into a dict; empty if it's
    absent or unreadable. Shared by :func:`list_backups` and :func:`verify_backup_integrity`."""
    meta: dict = {}
    meta_path = backup_path + ".meta"
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as mf:
                for line in mf:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        meta[k] = v
        except OSError:
            pass
    return meta


def verify_backup_integrity(backup_path: str) -> dict:
    """Re-hash a backup ``.bin`` and compare it to the SHA-256 recorded in its ``.meta`` sidecar, so on-disk
    corruption or truncation is caught BEFORE the backup is relied on to restore a board.

    Returns a dict whose ``status`` is one of:
      * ``ok`` — the file hashes to the recorded sha256 (safe to restore);
      * ``mismatch`` — the file has CHANGED since backup (corrupt/truncated/tampered — do NOT restore);
      * ``no_meta`` — no sidecar, so there is nothing to check against;
      * ``no_sha`` — the sidecar carries no ``sha256`` line;
      * ``unreadable`` — the ``.bin`` could not be read;
      * ``missing`` — no such backup file.
    Never raises. Echoes ``size`` and the recorded ``size_detected`` so a truncated-backup caveat carries through.
    """
    result: dict = {"path": backup_path, "status": "missing", "recorded": None, "actual": None}
    if not os.path.isfile(backup_path):
        return result
    result["size"] = os.path.getsize(backup_path)
    meta = _read_meta(backup_path)
    if not meta:
        result["status"] = "no_meta"
        return result
    result["size_detected"] = meta.get("size_detected")
    recorded = meta.get("sha256")
    if not recorded:
        result["status"] = "no_sha"
        return result
    result["recorded"] = recorded
    try:
        actual = _sha256_file(backup_path)
    except OSError:
        result["status"] = "unreadable"
        return result
    result["actual"] = actual
    result["status"] = "ok" if actual == recorded else "mismatch"
    return result


_INTEGRITY_MESSAGES = {
    "ok": "intact - the file matches its recorded SHA-256; safe to restore.",
    "mismatch": "CORRUPT - the file no longer matches its recorded SHA-256. Do NOT restore from it; re-take the backup.",
    "no_meta": "no .meta sidecar - cannot check integrity (an older backup, or the sidecar was removed).",
    "no_sha": "the .meta sidecar records no SHA-256 - cannot check integrity.",
    "unreadable": "the backup file could not be read.",
    "missing": "no such backup file.",
}


def verify_backup_cli(backup_path: str) -> int:
    """CLI for ``--verify-backup``: print a backup's integrity against its ``.meta`` SHA-256. Returns 0 when
    the file is intact, 1 otherwise (mismatch / missing / uncheckable) so it's usable in a script."""
    r = verify_backup_integrity(backup_path)
    print(f"[verify-backup] {backup_path}")
    if r.get("size") is not None:
        caveat = ("  (flash size was ASSUMED, not detected — may be truncated)"
                  if r.get("size_detected") == "False" else "")
        print(f"  size: {r['size']} bytes{caveat}")
    if r.get("recorded"):
        print(f"  recorded sha256: {r['recorded']}")
    if r.get("actual"):
        print(f"  actual   sha256: {r['actual']}")
    print(f"  => {_INTEGRITY_MESSAGES.get(r['status'], r['status'])}")
    return 0 if r["status"] == "ok" else 1
