"""Golden regression net for the flash-command surface.

Stage 0 of the flasher-consolidation (see command-center/projects/
flasher-consolidation-PLAN.md). This LOCKS the per-profile, flash-critical
decisions so a future generic / JSON-driven profile rewrite (Stage 1) can be
proven *argv-identical* to today's hardware-validated behavior before anything
is shipped.

What it captures, per profile in ``flash_core.PROFILES``:
    * ``image_model`` (merged-single-bin vs multi-file-offsets) and
      ``supports_suicide``;
    * per-chip ``app_offset`` (where the app/merged image is written);
    * the exact esptool ``argv`` the shared ``flash_assets()`` builds in *app*
      mode, for a fixed chip set.

Deterministic by construction: NO network, NO esptool, NO device. We monkeypatch
``flash_core._run_stream`` to capture the argv instead of spawning esptool, and
normalize ``sys.executable`` (argv[0]) to ``"<py>"`` so the golden is portable
across machines. (App mode never downloads — full-mode support-file offsets,
which require the network, are a Stage-0 follow-up; the per-chip *bootloader*
offset contract is already locked by ``test_flash_core.test_bootloader_offset``.)

NOTE: a few profiles flash via a non-esptool backend at runtime (rtl8720/adb/sd),
but they still inherit the shared ``flash_assets``; capturing it here locks that
shared plumbing too. The golden records whatever each profile produces.

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


def _normalize(argv: list[str]) -> list[str]:
    argv = list(argv)
    if argv and argv[0] == sys.executable:
        argv[0] = "<py>"
    return argv


def _capture_app_argv(prof, chip: str) -> dict:
    """Capture the esptool argv ``prof.flash_assets`` builds in app mode, with
    ``_run_stream`` stubbed so nothing actually runs."""
    captured: dict = {}
    real = flash_core._run_stream

    def _fake(argv, on_line):  # noqa: ANN001
        captured["argv"] = list(argv)
        return 0

    flash_core._run_stream = _fake  # type: ignore[assignment]
    try:
        prof.flash_assets(_PORT, chip, _APP, lambda *_a, **_k: None, mode="app", baud=_BAUD)
    except Exception as exc:  # record, never crash the snapshot
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        flash_core._run_stream = real  # type: ignore[assignment]
    return {"argv": _normalize(captured.get("argv", []))}


def _build_snapshot() -> dict:
    snap: dict = {}
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
            entry["chips"][chip] = {"app_offset": app_off, **_capture_app_argv(prof, chip)}
        snap[pid] = entry
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
