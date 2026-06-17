"""Batch flash — flash multiple connected devices simultaneously or sequentially.

Useful for building a new cyberdeck: plug in all ESP32 boards, assign firmware to
each port, and flash them all in one operation.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

Line = Callable[[str], None]


@dataclass
class FlashJob:
    port: str
    profile_id: str
    variant_name: Optional[str] = None
    mode: str = "app"
    baud: int = 921600
    erase_first: bool = False


@dataclass
class FlashResult:
    port: str
    profile_id: str
    success: bool
    exit_code: int = 0
    duration_ms: int = 0
    error: str = ""
    log: List[str] = field(default_factory=list)


class BatchFlasher:
    """Flash multiple ESP32 devices, each on its own port, concurrently."""

    def __init__(self, on_line: Line, on_complete: Optional[Callable[["FlashResult"], None]] = None):
        self._on_line = on_line
        self._on_complete = on_complete
        self._results: List[FlashResult] = []
        self._lock = threading.Lock()
        self._running = False
        self._cancelled = False

    @property
    def results(self) -> List[FlashResult]:
        with self._lock:
            return list(self._results)

    def cancel(self):
        self._cancelled = True

    def flash_sequential(self, jobs: List[FlashJob]) -> List[FlashResult]:
        self._running = True
        self._cancelled = False
        with self._lock:
            self._results.clear()

        from src.core import flash_core as flasher

        for i, job in enumerate(jobs, 1):
            if self._cancelled:
                self._on_line(f"[batch] Cancelled after {i-1}/{len(jobs)} devices")
                break

            self._on_line(f"[batch] Flashing {i}/{len(jobs)}: {job.profile_id} → {job.port}")
            result = self._flash_one(job, flasher)
            with self._lock:
                self._results.append(result)
            if self._on_complete:
                self._on_complete(result)

        self._running = False
        self._on_line(f"[batch] Complete: {sum(1 for r in self._results if r.success)}/{len(self._results)} succeeded")
        return self._results

    def flash_parallel(self, jobs: List[FlashJob]) -> List[FlashResult]:
        self._running = True
        self._cancelled = False
        with self._lock:
            self._results.clear()

        from src.core import flash_core as flasher

        threads: List[threading.Thread] = []
        for job in jobs:
            t = threading.Thread(target=self._flash_worker, args=(job, flasher), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self._running = False
        self._on_line(f"[batch] Complete: {sum(1 for r in self._results if r.success)}/{len(self._results)} succeeded")
        return self._results

    def _flash_worker(self, job: FlashJob, flasher_module):
        result = self._flash_one(job, flasher_module)
        with self._lock:
            self._results.append(result)
        if self._on_complete:
            self._on_complete(result)

    def _flash_one(self, job: FlashJob, flasher_module) -> FlashResult:
        log: List[str] = []
        start = time.monotonic()

        def capture(line: str):
            log.append(line)
            self._on_line(f"[{job.port}] {line}")

        try:
            profile = flasher_module.get_profile(job.profile_id)

            if job.erase_first:
                capture(f"[erase] Erasing flash on {job.port}...")
                chip = flasher_module._detect_chip(job.port, capture)
                if chip:
                    flasher_module.erase(job.port, chip, capture)

            tag, assets = profile.latest_release()
            chip = flasher_module._detect_chip(job.port, capture)
            if not chip:
                return FlashResult(
                    port=job.port, profile_id=job.profile_id, success=False,
                    error="Could not detect chip", log=log,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            if job.variant_name:
                variant = next((a for a in assets if a["name"] == job.variant_name), None)
            else:
                variant = profile.default_variant(assets, chip)

            if not variant:
                return FlashResult(
                    port=job.port, profile_id=job.profile_id, success=False,
                    error=f"No variant found for chip {chip}", log=log,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            cache = flasher_module.cache_dir()
            app_path = flasher_module.download_to(
                variant["url"], cache, variant["name"], capture
            )

            support = None
            if job.mode == "full":
                support = profile.support_files(chip, cache, capture)

            rc = profile.flash_assets(
                job.port, chip, app_path, capture,
                mode=job.mode, baud=job.baud, support=support,
            )

            elapsed = int((time.monotonic() - start) * 1000)
            return FlashResult(
                port=job.port, profile_id=job.profile_id,
                success=(rc == 0), exit_code=rc,
                duration_ms=elapsed, log=log,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            return FlashResult(
                port=job.port, profile_id=job.profile_id, success=False,
                error=str(e), log=log, duration_ms=elapsed,
            )


def create_deck_flash_plan() -> List[FlashJob]:
    """Return the default flash plan for a full cyberdeck build (14 devices)."""
    return [
        FlashJob(port="", profile_id="marauder", mode="full", erase_first=True),
        FlashJob(port="", profile_id="marauder", mode="full", erase_first=True),
        FlashJob(port="", profile_id="flock-you", mode="full", erase_first=True),
        FlashJob(port="", profile_id="airtag-scanner", mode="full", erase_first=True),
        FlashJob(port="", profile_id="marauder", mode="full", erase_first=True),
        FlashJob(port="", profile_id="ghostesp", mode="full", erase_first=True),
        FlashJob(port="", profile_id="meshtastic", mode="full", erase_first=True),
        FlashJob(port="", profile_id="halehound", mode="full", erase_first=True),
        FlashJob(port="", profile_id="sky-spy", mode="full", erase_first=True),
    ]
