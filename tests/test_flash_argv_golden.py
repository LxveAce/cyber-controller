"""Golden regression net for the flash-command surface.

Stage 0 of the flasher-consolidation (see command-center/projects/
flasher-consolidation-PLAN.md). This LOCKS the per-profile, flash-critical
decisions so a future generic / JSON-driven profile rewrite (Stage 1) can be
proven *argv-identical* to today's hardware-validated behavior before anything
is shipped.

What it captures, per profile in ``flash_core.PROFILES`` × chip:
    * ``image_model`` (merged-single-bin vs multi-file-offsets) + ``supports_suicide``;
    * per-chip ``app_offset`` and the **app-mode** esptool argv;
    * the **full-mode** support-file offsets (bootloader / partitions / boot_app0 —
      incl. the hardware-critical ESP32-C5 0x2000 bootloader) and the full-mode argv.

Deterministic by construction: NO network, NO esptool, NO device. We monkeypatch
``flash_core._run_stream`` (captures argv instead of spawning esptool) and the
download helpers ``download_to`` / ``download_and_extract`` / ``_github_latest``
(so ``support_files`` returns its offset map without fetching anything — the
offset KEYS are computed locally; only the file PATHS would be downloaded).
``sys.executable`` (argv[0]) is normalized to ``"<py>"`` and downloaded paths to
``"<file>"`` so the golden is portable across machines.

NOTE: a few profiles flash via a non-esptool backend at runtime (rtl8720/adb/sd),
but they still inherit the shared ``flash_assets``; capturing it locks that shared
plumbing too. Merged-image profiles legitimately have no support files (recorded
as ``{"support": null}``).

Regenerate INTENTIONALLY (only after a reviewed behavior change):
    UPDATE_FLASH_GOLDEN=1 py -m pytest tests/test_flash_argv_golden.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

GOLDEN = Path(__file__).parent / "golden" / "flash_argv_golden.json"
CHIPS = ["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32c5"]
_PORT = "PORTX"
_APP = "APP.bin"
_BAUD = 921600


def _noop(*_a, **_k) -> None:
    return None


def _normalize(argv: list[str]) -> list[str]:
    out = []
    for tok in argv:
        if tok == sys.executable:
            out.append("<py>")
        else:
            out.append(tok)
    return out


def _capture_argv(prof, chip: str, mode: str, support) -> dict:
    """Capture the esptool argv ``prof.flash_assets`` builds, with ``_run_stream``
    stubbed so nothing actually runs."""
    captured: dict = {}
    real = flash_core._run_stream

    def _fake(argv, on_line):  # noqa: ANN001
        captured["argv"] = list(argv)
        return 0

    flash_core._run_stream = _fake  # type: ignore[assignment]
    try:
        prof.flash_assets(_PORT, chip, _APP, _noop, mode=mode, baud=_BAUD, support=support)
    except Exception as exc:  # record, never crash the snapshot
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        flash_core._run_stream = real  # type: ignore[assignment]
    return {"argv": _normalize(captured.get("argv", []))}


def _build_snapshot() -> dict:
    snap: dict = {}
    # Stub the network so support_files() returns its offset map offline; the offset
    # KEYS are computed locally, only the PATHS would be fetched.
    saved = {n: getattr(flash_core, n) for n in ("download_to", "download_and_extract", "_github_latest")}
    flash_core.download_to = lambda url, cache_dir, name, on_line: "<file>"  # type: ignore[assignment]
    flash_core.download_and_extract = lambda url, cache_dir, asset_name, member, on_line: "<file>"  # type: ignore[assignment]
    flash_core._github_latest = lambda api_url: ("v0.0.0", [])  # type: ignore[assignment]
    try:
        for pid in sorted(flash_core.PROFILES):
            prof = flash_core.PROFILES[pid]
            entry = {
                "image_model": prof.image_model,
                "supports_suicide": bool(prof.supports_suicide),
                "chips": {},
            }
            for chip in CHIPS:
                try:
                    app_off = prof.app_offset(chip)
                except Exception as exc:  # noqa: BLE001
                    app_off = f"ERR:{type(exc).__name__}"
                cell = {"app_offset": app_off, "app": _capture_argv(prof, chip, "app", None)}

                # full mode: support-file offsets (bootloader/partitions/boot_app0) + full argv
                try:
                    support = prof.support_files(chip, "CACHE", _noop)
                except Exception as exc:  # noqa: BLE001
                    cell["full"] = {"support_error": f"{type(exc).__name__}: {exc}"}
                else:
                    if support:
                        cell["support_offsets"] = sorted(support.keys())
                        cell["full"] = _capture_argv(prof, chip, "full", support)
                    else:
                        cell["full"] = {"support": None}  # merged single image — no support files
                entry["chips"][chip] = cell
            snap[pid] = entry
    finally:
        for name, val in saved.items():
            setattr(flash_core, name, val)
    return snap


def test_flash_argv_golden() -> None:
    current = _build_snapshot()

    if os.environ.get("UPDATE_FLASH_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"golden regenerated at {GOLDEN}")

    assert GOLDEN.exists(), (
        f"golden missing; regenerate with UPDATE_FLASH_GOLDEN=1 ({GOLDEN})"
    )
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert set(current) == set(expected), (
        f"profile set changed: added={sorted(set(current) - set(expected))} "
        f"removed={sorted(set(expected) - set(current))}"
    )
    for pid in sorted(expected):
        assert current[pid] == expected[pid], (
            f"flash surface changed for profile '{pid}':\n"
            f"  expected={json.dumps(expected[pid], sort_keys=True)}\n"
            f"  current ={json.dumps(current[pid], sort_keys=True)}"
        )
