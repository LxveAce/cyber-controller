"""Batch flash — flash multiple connected devices simultaneously or sequentially.

Useful for building a new cyberdeck: plug in all ESP32 boards, assign firmware to
each port, and flash them all in one operation.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

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

        for i, job in enumerate(jobs, 1):
            if self._cancelled:
                self._on_line(f"[batch] Cancelled after {i-1}/{len(jobs)} devices")
                break

            self._on_line(f"[batch] Flashing {i}/{len(jobs)}: {job.profile_id} → {job.port}")
            result = self._flash_one(job)
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

        threads: List[threading.Thread] = []
        for job in jobs:
            t = threading.Thread(target=self._flash_worker, args=(job,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self._running = False
        self._on_line(f"[batch] Complete: {sum(1 for r in self._results if r.success)}/{len(self._results)} succeeded")
        return self._results

    def _flash_worker(self, job: FlashJob):
        result = self._flash_one(job)
        with self._lock:
            self._results.append(result)
        if self._on_complete:
            self._on_complete(result)

    def _flash_one(self, job: FlashJob) -> FlashResult:
        """Flash one device by delegating to the SINGLE flash path — FlashEngine._flash_esptool.

        BatchFlasher used to hand-maintain a parallel copy of that flow, and the copy drifted (it
        once ignored a variant's offset, and never applied a profile's ``extra_args`` —
        brick-adjacent, since extra_args strips a ``--flash_size`` injection that would re-open a
        wrong-size bootloop). Routing through FlashEngine means erase + variant + extra_args + the
        offset + the tails (offline-vault fallback, size warning, support_members, source-only
        messages) come from the ONE proven path — a batch flash is byte-identical to a single one.
        """
        from src.core.flash_engine import FirmwareProfile, FlashEngine
        from src.core.resources import resource_path

        log: List[str] = []
        start = time.monotonic()

        def progress(_pct: int, msg: str) -> None:
            log.append(msg)
            self._on_line(f"[{job.port}] {msg}")

        def done(success: bool, error: str = "") -> FlashResult:
            return FlashResult(
                port=job.port, profile_id=job.profile_id, success=success,
                exit_code=0 if success else 1, error=error, log=log,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            profile_path = resource_path("src", "config", "profiles") / f"{job.profile_id}.json"
            profile = FirmwareProfile.from_file(profile_path)
            # Per-job options ride on the engine profile FlashEngine consumes, as a single flash
            # sets them from the UI toggles: erase/mode/variant. The profile's own baud, extra_args,
            # core_id and tails govern the rest (so a stray FlashJob.baud can't diverge from a
            # single flash — the profile's flash baud is authoritative on the one path).
            profile.erase_first = job.erase_first
            profile.flash_mode = job.mode
            if job.variant_name:
                profile.variant = job.variant_name
            ok = FlashEngine().flash(job.port, profile, progress)
            return done(bool(ok), "" if ok else "flash failed")
        except Exception as e:  # noqa: BLE001 — a per-device failure must fail that job, not the batch
            return done(False, str(e))


def create_deck_flash_plan() -> List[FlashJob]:
    """Return the default flash plan for a full cyberdeck build (9 devices)."""
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
