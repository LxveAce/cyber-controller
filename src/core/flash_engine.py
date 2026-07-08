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
import os
import re
import shutil
import string
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from src.core import flash_core, profile_loader

log = logging.getLogger(__name__)


class _PortBusy(Exception):
    """Raised internally when a serial port is already mid flash/backup/erase (see the busy-guard)."""

ProgressCallback = Callable[[int, str], None]  # (percent, message)

# esptool prints flash progress as "X.Y%" (e.g. "27.6%"). A naive (\d+)% captures the digit right
# BEFORE the "%" — i.e. the TENTHS ("27.6%" -> 6), because the "." breaks the digit run — so the flash
# progress bar jittered 0-9 over and over instead of climbing 0->100. Match the optional ".frac" so
# group(1) is the whole-percent integer.
_RE_PROGRESS = re.compile(r"(\d+)(?:\.\d+)?\s*%")


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
    offline_fallback_path: str = ""  # vaulted binary to flash if the live download can't run (offline
                                     # flashing). Set by the UI from FirmwareVault.get_cached(); the engine
                                     # consults it ONLY after a release/download failure, so a board-aware
                                     # variant pick still wins whenever the network is up.
    variant: str = ""  # explicit release-asset variant (name or label). Empty = per-chip default.
                       # CRITICAL for boards a chip-ID can't distinguish (CYD/M5/etc. are all 'esp32');
                       # picking the wrong one flashes the wrong display driver -> white screen.
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
            variant=data.get("variant", ""),
            boards=profile_loader.list_boards(data),
            firmware_urls=data.get("firmware_urls", {}) if isinstance(data.get("firmware_urls"), dict) else {},
            default_baud=profile_loader.default_baud(data),
            core_id=profile_loader.core_id_for(json_id),
            raw=data,
        )


def supported_boards_text(profile: FirmwareProfile) -> str:
    """Human-readable list of the boards a firmware profile supports, for a UI tooltip.

    Reads the profile's rich ``boards`` list (each entry carries a ``name`` and the
    esptool ``chip`` id). Returns an empty string when the profile lists no boards — a
    bare ``custom`` profile or a non-ESP OS image — so the caller can skip the tooltip
    instead of showing a blank one. Pure / UI-free so it can be unit-tested directly.
    """
    seen: list[str] = []
    for b in profile.boards:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name") or "").strip()
        chip = str(b.get("chip") or "").strip()
        if name and chip:
            label = f"{name} ({chip})"
        elif name:
            label = name
        elif chip:
            label = chip
        else:
            continue
        if label not in seen:
            seen.append(label)
    if not seen:
        return ""
    return "Supported boards:\n" + "\n".join(f"• {s}" for s in seen)


def _norm_chip(chip: str) -> str:
    """Normalize an esptool chip id for comparison: lowercase, alphanumerics only, so
    ``esp32-s3`` / ``ESP32_S3`` / ``esp32s3`` all compare equal."""
    return "".join(c for c in str(chip).lower() if c.isalnum())


def chip_match(connected_chip: str | None, profile: FirmwareProfile) -> str:
    """Advisory compatibility of a firmware profile with the connected board's chip.

    Returns ``'match'`` (the profile supports this chip), ``'mismatch'`` (the profile
    lists boards but none use this chip), or ``'neutral'`` (can't say — the chip is
    unknown, or the profile lists no chips / a non-ESP target). Purely advisory: the UI
    colours a hint from this and must NEVER block on it, because chip detection over a
    shared USB-serial adapter is often ambiguous.
    """
    norm = _norm_chip(connected_chip or "")
    if not norm or norm == "unknown":
        return "neutral"
    chips = {_norm_chip(b.get("chip")) for b in profile.boards
             if isinstance(b, dict) and b.get("chip")}
    chips.discard("")
    if not chips:
        return "neutral"
    return "match" if norm in chips else "mismatch"


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


def _sd_line_progress_adapters(
    progress: ProgressCallback | None,
) -> tuple[Callable[[str], None], Callable[[float], None]]:
    """Bridge sd_backend's split callbacks — ``on_line(str)`` for log lines and
    ``on_progress(fraction 0..1)`` for byte progress — onto the engine's single
    ``(percent, message)`` :data:`ProgressCallback`. The last line and the last fraction
    are held so either callback can emit a complete ``(percent, message)`` pair."""
    state = {"pct": 0, "msg": ""}

    def on_line(msg: str) -> None:
        state["msg"] = msg
        if progress is not None:
            progress(state["pct"], msg)

    def on_progress(frac: float) -> None:
        state["pct"] = max(0, min(100, int(frac * 100)))
        if progress is not None:
            progress(state["pct"], state["msg"])

    return on_line, on_progress


class FlashEngine:
    """High-level flash orchestrator. Routes by backend to the proven flash core."""

    def __init__(self) -> None:
        self._status = FlashStatus.IDLE
        self._lock = threading.Lock()
        self._current_thread: threading.Thread | None = None
        # Ports currently mid flash/backup/erase. A SECOND esptool on the same UART while one is writing
        # is a known way to brick a board, and nothing serialized the ops before: the Qt buttons weren't
        # cross-disabled, and every UI (+ the scriptable web API) shares one engine. Guard at THIS layer so
        # all surfaces are protected at once; different ports still run in parallel (multi-board flashing).
        self._busy_ports: set[str] = set()
        # Backend registry: profile.backend -> flash handler. Adding a new flash tool / hardware is now
        # "write a _flash_<x>(port, profile, progress) method + register it here" (Stage 2 of the
        # flasher-consolidation — ease-of-growth for backends, mirroring the data-driven profile model).
        self._backends: dict[str, Callable[[str, "FirmwareProfile", ProgressCallback | None], bool]] = {
            "esptool": self._flash_esptool,
            "qflipper": self._flash_qflipper,
            "adb": self._flash_adb,
            "sd": self._flash_sd,
            "sd-image": self._flash_sd,
            "rtl8720": self._flash_rtl8720,
            # Phase-3 scaffolds (HW-validation pending — argv/flow unit-tested, no board yet):
            "dfu": self._flash_dfu,   # dfu-util → RP2040/Pi Pico in DFU + generic USB-DFU
            "uf2": self._flash_uf2,   # UF2 mass-storage → RP2040-family / UF2 bootloaders
            "nrf_dfu": self._flash_nrf_dfu,  # Nordic nRF52 DFU .zip → ChameleonUltra / Nordic dongle DFU
        }

    @property
    def status(self) -> FlashStatus:
        return self._status

    def load_profile(self, path: str | Path) -> FirmwareProfile:
        """Load and return a firmware profile from a JSON file."""
        return FirmwareProfile.from_file(path)

    # ── Per-port concurrency guard ───────────────────────────────────
    def is_port_busy(self, port: str) -> bool:
        """True if *port* is currently mid flash/backup/erase on any surface. Lets a UI pre-disable or a
        web endpoint return 409 rather than launch a second, board-bricking esptool on the same port."""
        with self._lock:
            return bool(port) and port in self._busy_ports

    @contextmanager
    def _port_guard(self, port: str):
        """Reserve *port* for a single serial operation. Raises :class:`_PortBusy` if it's already in use.
        A falsy port (SD/UF2/DFU paths, blank) is not reserved — only real serial ports need the guard."""
        if not port:
            yield
            return
        with self._lock:
            if port in self._busy_ports:
                raise _PortBusy(port)
            self._busy_ports.add(port)
        try:
            yield
        finally:
            with self._lock:
                self._busy_ports.discard(port)

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
        try:
            with self._port_guard(port):
                with self._lock:
                    self._status = FlashStatus.FLASHING
                try:
                    backend = (profile.backend or "esptool").lower()
                    handler = self._backends.get(backend)
                    if handler is None:
                        if progress:
                            progress(0, f"Unknown backend: {profile.backend}")
                        ok = False
                    else:
                        ok = handler(port, profile, progress)
                except Exception as exc:  # never let a backend exception leak unlabelled
                    log.exception("flash failed")
                    if progress:
                        progress(0, f"Error: {exc}")
                    ok = False
                with self._lock:
                    self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
                return ok
        except _PortBusy:
            if progress:
                progress(0, f"Port {port} is busy with another flash/backup/erase — aborted so a second "
                            f"esptool can't fight the first over the same port (a known way to brick it).")
            return False

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

        # Honor an explicitly-requested full wipe BEFORE writing. erase_first exists to clear
        # residual NVS/SPIFFS/user data that a merged or app reflash does NOT overwrite; a wipe
        # that FAILS must abort the flash rather than leave stale data behind under a
        # "Flash complete" (mirrors BatchFlasher._flash_one — derive success from the erase rc).
        if profile.erase_first:
            on_line(f"[erase] erasing flash on {port} before flashing...")
            erase_rc = flash_core.erase(port, chip, on_line)
            if erase_rc != 0:
                on_line(f"[error] erase failed (exit {erase_rc}) — refusing to reflash over stale flash")
                if progress:
                    progress(0, "Flash failed")
                return False

        # Local-file flash (explicit .bin) — merged image at 0x0 by default.
        if profile.local_path:
            custom = flash_core.get_profile("custom")
            rc = custom.flash_local(port, chip, profile.local_path, on_line, baud=profile.baud,
                                    extra_args=profile.extra_args or None)
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
            fb = self._flash_offline_fallback(port, chip, profile, on_line, progress,
                                              f"could not fetch release ({exc})")
            if fb is not None:
                return fb
            on_line(f"[error] could not fetch release: {exc}")
            return False
        variant = self._resolve_variant(core, assets, chip, profile.variant, on_line)
        if not variant:
            on_line(f"[error] no firmware asset for chip {chip} in the {core_id} release")
            return False

        cache = flash_core.cache_dir()
        try:
            # Firmwares that ship a per-board ZIP bundle (e.g. GhostESP) carry a
            # "zip_member" — download the zip and extract the flashable merged image.
            if variant.get("zip_member"):
                # zip_name (when present) is the actual archive filename — shared across boards
                # in a chip-wide bundle so the big download is cached/reused (Meshtastic).
                app_path = flash_core.download_and_extract(
                    variant["url"], cache, variant.get("zip_name") or variant["name"],
                    variant["zip_member"], on_line)
            else:
                app_path = flash_core.download_to(variant["url"], cache, variant["name"], on_line)
            # Pinned-firmware integrity gate (closed-source profiles like BlueJammer-V2 carry a
            # "sha256"): reject a tampered/changed app image BEFORE esptool writes it.
            if variant.get("sha256"):
                flash_core.verify_sha256(app_path, variant["sha256"], on_line)
        except Exception as exc:
            fb = self._flash_offline_fallback(port, chip, profile, on_line, progress,
                                              f"download/verify failed ({exc})")
            if fb is not None:
                return fb
            on_line(f"[error] download/verify failed: {exc}")
            return False

        support = None
        mode = profile.flash_mode if profile.flash_mode in ("app", "full") else "full"
        if mode == "full":
            try:
                # Merged-image profiles legitimately return None here (no fetch, no raise). An EXCEPTION
                # is a real failure to obtain the bootloader/partitions/boot_app0 a multi-file profile
                # needs — and writing only the app to a blank board produces a non-booting device.
                support = core.support_files(chip, cache, on_line)
            except Exception as exc:
                # Do NOT silently downgrade to app-only and report success (the old behavior wrote a
                # dead board yet showed "Flash complete"). Abort loudly so the failure is visible.
                if "no auto support-file mapping" in str(exc).lower():
                    # Permanent gap: this firmware ships no bootloader/partition files for this chip
                    # upstream (e.g. Marauder on ESP32-C5). Retrying won't help — say so plainly instead
                    # of implying a connection problem.
                    on_line(f"[error] {chip}: this firmware has no full-flash support for this chip yet — "
                            f"no bootloader/partition files are published for it upstream. Not a connection "
                            f"error; retrying won't help. Choose a supported board/chip. ({exc})")
                else:
                    on_line(f"[error] support files unavailable ({exc}); aborting full flash — writing the "
                            f"app alone would leave a non-booting board. Fix the download/connection and retry.")
                if progress:
                    progress(0, "Flash failed")
                return False

        app_offset = variant.get("offset") or core.app_offset(chip)
        rc = core.flash_assets(
            port, chip, app_path, on_line, mode=mode, baud=profile.baud,
            support=support, app_offset=app_offset, extra_args=profile.extra_args or None,
        )
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    def _flash_offline_fallback(
        self, port: str, chip: str, profile: "FirmwareProfile",
        on_line: Callable[[str], None], progress: ProgressCallback | None, reason: str,
    ) -> bool | None:
        """Flash the vaulted binary when the live download can't run (offline flashing).

        This is the READ side of the FirmwareVault contract: the UI hands us the cached path via
        ``profile.offline_fallback_path`` (from :meth:`FirmwareVault.get_cached`) so a network-less
        flash can still succeed instead of failing despite the firmware sitting in the "offline
        cache". Returns ``True``/``False`` when a cached binary was flashed, or ``None`` when no
        usable cached binary is available (the caller then reports the original download error).

        The online path is untouched — this runs ONLY after a release/download failure, so a
        board-aware variant pick still wins whenever the network is up. The cached image is treated
        as a merged blob @0x0 (same as an explicit ``local_path``).
        """
        path = getattr(profile, "offline_fallback_path", "") or ""
        if not path or not Path(path).exists():
            return None
        on_line(f"[vault] {reason}; flashing cached firmware from the offline vault: {path}")
        custom = flash_core.get_profile("custom")
        rc = custom.flash_local(port, chip, path, on_line, baud=profile.baud)
        if progress:
            progress(100 if rc == 0 else 0,
                     "Flash complete (offline vault)" if rc == 0 else "Flash failed")
        return rc == 0

    def _resolve_variant(self, core, assets, chip, requested, on_line):
        """Pick the release asset to flash.

        Honors an explicit ``requested`` variant (the UI's board selection) by exact name,
        then name/label substring; otherwise falls back to the per-chip default. ALWAYS logs
        which variant was chosen so a wrong board pick (e.g. old_hardware on a CYD -> white
        screen) is visible instead of silent.
        """
        cands = core.variants_for_chip(assets, chip)
        if requested:
            req = requested.strip().lower()
            for a in cands:  # exact asset name
                if a.get("name", "").lower() == req:
                    on_line(f"[variant] {a['name']} (selected)")
                    return a
            # Token-boundary pass BEFORE the loose substring match: a detection fragment like
            # "cyd_2432S028" must NOT match the superset asset "..._cyd_2432S028_2usb.bin" (a DIFFERENT
            # display driver — flashing it yields a white/garbled screen). Require the fragment to be the
            # final token before the extension (immediately followed by '.'), which is unambiguous
            # regardless of the order GitHub lists the assets in.
            for a in cands:
                if (req + ".") in a.get("name", "").lower():
                    on_line(f"[variant] {a['name']} — {a.get('label', '')} (matched '{requested}')")
                    return a
            for a in cands:  # substring of asset name or friendly label
                if req in a.get("name", "").lower() or req in a.get("label", "").lower():
                    on_line(f"[variant] {a['name']} — {a.get('label', '')} (matched '{requested}')")
                    return a
            on_line(f"[warn] requested variant '{requested}' not found for {chip}; using default")
        v = core.default_variant(assets, chip)
        if v:
            on_line(f"[variant] {v['name']} — {v.get('label', '')} (default for {chip}; "
                    "set a variant if your board's display stays blank)")
        return v

    def _resolve_binary(
        self, profile: "FirmwareProfile", on_line: Callable[[str], None], label: str
    ) -> str | None:
        """Download-or-local resolution shared by the qFlipper/DFU/UF2 backends.

        Returns a filesystem path to the artifact to flash, or ``None`` on any failure
        (always with a clear ``on_line`` message — this NEVER returns a path that wasn't
        actually resolved, so a backend can't fake success on nothing).

        Order of resolution (mirrors the rewritten ``_flash_qflipper``):
            1. ``profile.local_path`` — an explicit local file the user pointed at.
            2. otherwise resolve ``profile.core_id`` in :data:`flash_core.PROFILES`,
               fetch ``core.latest_release()``, pick the asset via ``_resolve_variant``,
               ``flash_core.download_to`` it, and ``verify_sha256`` when the variant pins one.

        ``label`` is a short backend tag ("dfu"/"uf2"/…) used only in log lines.
        """
        if profile.local_path:
            on_line(f"[{label}] using local file: {profile.local_path}")
            return profile.local_path
        core_id = profile.core_id if profile.core_id in flash_core.PROFILES else "custom"
        if core_id == "custom":
            on_line(f"[{label}] no flash-core profile for this firmware and no local file "
                    "provided — nothing to flash")
            return None
        core = flash_core.get_profile(core_id)
        try:
            on_line(f"[release] fetching latest {core_id} release...")
            _tag, assets = core.latest_release()
        except Exception as exc:  # offline / API error — never fake success
            on_line(f"[{label}] could not fetch release: {exc}")
            return None
        # Prefer an explicit chip; fall back to a chip declared in the raw profile, else 'auto'
        # (the variant resolver logs whichever asset it picks, so a wrong pick stays visible).
        chip = profile.chip if profile.chip and profile.chip != "auto" else (
            profile.raw.get("chip") or "auto")
        variant = self._resolve_variant(core, assets, chip, profile.variant, on_line)
        if not variant:
            on_line(f"[{label}] no firmware asset in the {core_id} release")
            return None
        try:
            app_path = flash_core.download_to(
                variant["url"], flash_core.cache_dir(), variant["name"], on_line)
            # Pinned-firmware integrity gate: reject a tampered/changed image BEFORE flashing it.
            if variant.get("sha256"):
                flash_core.verify_sha256(app_path, variant["sha256"], on_line)
        except Exception as exc:
            on_line(f"[{label}] download/verify failed: {exc}")
            return None
        return app_path

    def list_variants(self, profile: "FirmwareProfile", chip: str | None = None) -> list[dict]:
        """Return the selectable firmware variants (``{name, label, chip, url}``) for *profile*'s
        firmware on *chip*, so a UI can offer a board picker. Empty list if not a download profile
        or the release can't be fetched (offline). Never raises."""
        core_id = profile.core_id if profile.core_id in flash_core.PROFILES else "custom"
        if core_id == "custom":
            return []
        core = flash_core.get_profile(core_id)
        try:
            _tag, assets = core.latest_release()
        except Exception as exc:  # noqa: BLE001 — offline / API error is non-fatal for a picker
            log.debug("list_variants(%s) release fetch failed: %s", core_id, exc)
            return []
        # A pinned chip (explicit arg or single-chip profile) lists that chip's builds. A multi-chip /
        # auto profile lists EVERY board's builds (union) so the user can pick, say, the M5Stick-S3
        # image even though the profile's first board is an esp32 — the old "else esp32" fallback hid
        # all the S3/C5 variants from the picker.
        if chip:
            chips = [chip]
        elif profile.chip and profile.chip not in ("", "auto"):
            chips = [profile.chip]
        else:
            chips = sorted({str(b.get("chip")).strip() for b in (profile.boards or [])
                            if isinstance(b, dict) and b.get("chip")}) or ["esp32"]
        out: list[dict] = []
        seen: set = set()
        for c in chips:
            try:
                for v in core.variants_for_chip(assets, c):
                    key = v.get("name")
                    if key not in seen:
                        seen.add(key)
                        out.append(v)
            except Exception as exc:  # noqa: BLE001
                log.debug("list_variants(%s,%s) failed: %s", core_id, c, exc)
        return out

    # ── qFlipper (Flipper Zero firmwares) ────────────────────────────

    def _flash_qflipper(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        """Flipper Zero (Momentum/Unleashed) update via qFlipper.

        Flipper firmware ships as web-update ``.tgz`` packages, not esptool images. We resolve +
        download the real release package, then delegate to the proven flash_core Momentum/Unleashed
        ``flash_assets`` which shells ``qFlipper --install <package>`` through ``_run_stream`` (stdin
        + kill/reap handling for free). We NEVER launch a bare qFlipper with no package and report
        success — the previous version did exactly that (``local_path`` is empty for the shipped
        download profiles), so closing an idle qFlipper returned rc 0 and the UI logged a flash that
        never actually happened, with nothing downloaded or installed.
        """
        on_line = _percent_adapter(progress)
        if profile.local_path:
            app_path = profile.local_path
        else:
            core_id = profile.core_id if profile.core_id in flash_core.PROFILES else "custom"
            if core_id == "custom":
                on_line("[error] no flash-core profile for this Flipper firmware and no local "
                        "package provided — nothing to flash")
                return False
            core = flash_core.get_profile(core_id)
            try:
                on_line(f"[release] fetching latest {core_id} release...")
                _tag, assets = core.latest_release()
            except Exception as exc:
                on_line(f"[error] could not fetch release: {exc}")
                return False
            variant = self._resolve_variant(core, assets, "flipper", profile.variant, on_line)
            if not variant:
                on_line(f"[error] no firmware package in the {core_id} release")
                return False
            try:
                app_path = flash_core.download_to(
                    variant["url"], flash_core.cache_dir(), variant["name"], on_line)
                if variant.get("sha256"):
                    flash_core.verify_sha256(app_path, variant["sha256"], on_line)
            except Exception as exc:
                on_line(f"[error] download/verify failed: {exc}")
                return False
        core = flash_core.get_profile(
            profile.core_id if profile.core_id in flash_core.PROFILES else "momentum")
        rc = core.flash_assets(port, "flipper", app_path, on_line,
                               mode=profile.flash_mode, baud=profile.baud)
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    # ── dfu-util (RP2040/Pi Pico in DFU + generic USB-DFU) ───────────

    def _flash_dfu(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        """USB-DFU flashing via ``dfu-util``.

        HW-validation pending — argv/flow unit-tested only. Targets: RP2040/Pi Pico in
        DFU + generic USB-DFU.

        We resolve the firmware image (local or downloaded+verified, via ``_resolve_binary``),
        then shell ``dfu-util`` through the proven ``flash_core._run_stream`` (stdin/kill/reap
        for free). Backend-specific options come from ``profile.raw``: ``dfu_alt`` (the DFU alt
        setting, default 0) and ``dfu_id`` ("VID:PID" passed to ``-d`` to disambiguate the
        device). ``-R`` resets the target after a successful download. We NEVER report success
        when ``dfu-util`` is missing or nothing was flashed.
        """
        on_line = _percent_adapter(progress)
        if not shutil.which("dfu-util"):
            on_line("[dfu] dfu-util not found. Install it "
                    "(Windows: 'winget install dfu-util' or the dfu-util.sourceforge.io binaries; "
                    "Debian/Ubuntu: 'sudo apt install dfu-util'; macOS: 'brew install dfu-util') "
                    "and re-run.")
            return False
        app_path = self._resolve_binary(profile, on_line, "dfu")
        if not app_path:
            return False
        alt = profile.raw.get("dfu_alt", 0)
        dfu_id = profile.raw.get("dfu_id")  # "VID:PID"
        argv = ["dfu-util", "-a", str(alt)]
        if dfu_id:
            argv += ["-d", dfu_id]
        argv += ["-D", app_path, "-R"]
        rc = flash_core._run_stream(argv, on_line)
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    def _flash_nrf_dfu(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        """Nordic nRF52 DFU flashing via ``adafruit-nrfutil`` (or legacy ``nrfutil`` 6.x).

        HW-validation pending — argv/flow unit-tested only. Targets: nRF52840 Nordic-DFU ``.zip``
        packages (ChameleonUltra, stock Nordic dongle DFU). This is NOT ``dfu-util`` (standard
        USB-DFU, the ``dfu`` backend) — Nordic DFU is its own protocol over a serial (or BLE)
        transport.

        We resolve the ``.zip`` package (local or downloaded+verified, via ``_resolve_binary``), then
        shell the Nordic tool through the proven ``flash_core._run_stream``. Options come from
        ``profile.raw``: ``nrf_dfu_tool`` (``adafruit-nrfutil``|``nrfutil``; auto-detected if unset),
        ``nrf_dfu_transport`` (``serial``|``ble`` — only serial is wired), ``nrf_dfu_baud`` (default
        115200) and ``nrf_dfu_flow_control`` (0/1, adafruit-nrfutil only). We NEVER report success
        when the tool is missing or nothing was flashed. Entering DFU mode + the actual write are
        BENCH-GATED (need a real nRF52840); BLE-DFU and nrfutil v7's different CLI are out of scope.
        """
        on_line = _percent_adapter(progress)
        # Prefer an explicit tool if it's actually on PATH; else auto-detect (adafruit-nrfutil first).
        tool = profile.raw.get("nrf_dfu_tool")
        tool = (shutil.which(tool) if tool else None) or shutil.which("adafruit-nrfutil") or shutil.which("nrfutil")
        if not tool:
            on_line("[nrf_dfu] no Nordic DFU tool found. Install one "
                    "('pip install adafruit-nrfutil', or nRF Util from Nordic) and re-run.")
            return False
        transport = profile.raw.get("nrf_dfu_transport", "serial")
        if transport != "serial":
            on_line(f"[nrf_dfu] transport {transport!r} not wired — only 'serial' DFU is supported "
                    "(BLE-DFU needs a real nRF52840 + a BLE link to validate).")
            return False
        pkg_path = self._resolve_binary(profile, on_line, "nrf_dfu")
        if not pkg_path:
            return False
        baud = profile.raw.get("nrf_dfu_baud", 115200)
        if "adafruit" in os.path.basename(tool).lower():
            argv = [tool, "dfu", "serial", "-pkg", pkg_path, "-p", port, "-b", str(baud)]
            fc = profile.raw.get("nrf_dfu_flow_control")
            if fc is not None:
                argv += ["-fc", str(fc)]
        else:
            # legacy Nordic nrfutil 6.x uses the "usb-serial" subcommand shape.
            argv = [tool, "dfu", "usb-serial", "-pkg", pkg_path, "-p", port]
        rc = flash_core._run_stream(argv, on_line)
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    # ── UF2 (mass-storage drag-drop bootloader) ──────────────────────

    def _flash_uf2(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        """UF2 mass-storage flashing (drag-drop a ``.uf2`` onto the bootloader volume).

        HW-validation pending — detection/copy logic unit-tested. Targets: RP2040-family
        and other UF2 bootloaders (which mount as a removable FAT volume, e.g. ``RPI-RP2``).

        We resolve the ``.uf2`` (local or downloaded+verified, via ``_resolve_binary``), find the
        target volume (``profile.raw['uf2_target']`` if set, else auto-detect a removable volume
        containing ``INFO_UF2.TXT``), copy the file onto it, then best-effort flush the OS write
        cache so the bytes land before the board reboots and unmounts. We NEVER report success
        when nothing was resolved or no bootloader volume is present.
        """
        on_line = _percent_adapter(progress)
        app_path = self._resolve_binary(profile, on_line, "uf2")
        if not app_path:
            return False
        target = profile.raw.get("uf2_target") or self._find_uf2_volume(on_line)
        if not target:
            on_line("[uf2] no UF2 bootloader volume found. Put the board into bootloader mode "
                    "(RP2040/Pi Pico: hold BOOTSEL while plugging it in — it mounts as 'RPI-RP2') "
                    "and retry, or set 'uf2_target' in the profile.")
            return False
        try:
            dest = shutil.copy2(app_path, target)
            on_line(f"[uf2] copied {app_path} -> {dest}")
        except Exception as exc:
            on_line(f"[uf2] copy to {target} failed: {exc}")
            return False
        # Best-effort flush: push the OS write cache to disk so the .uf2 is fully committed
        # before the board reboots (many UF2 bootloaders reset the instant the file lands).
        try:
            if hasattr(os, "sync"):
                os.sync()
        except Exception:
            pass
        if progress:
            progress(100, "Flash complete")
        return True

    def _uf2_candidate_volumes(self) -> list[str]:
        """Return candidate removable-volume mount points to probe for a UF2 bootloader.

        Windows: every ``<LETTER>:\\`` drive root. POSIX: the usual removable mount roots
        (``/media``, ``/run/media``, ``/Volumes``) plus one nested level so per-user layouts
        like ``/run/media/<user>/<label>`` and ``/media/<user>/<label>`` are covered.
        Split out from :meth:`_find_uf2_volume` so the scan is easy to stub in tests.
        """
        candidates: list[str] = []
        if os.name == "nt":
            candidates.extend(f"{letter}:\\" for letter in string.ascii_uppercase)
        else:
            for root in ("/media", "/run/media", "/Volumes"):
                try:
                    children = [os.path.join(root, e) for e in os.listdir(root)]
                except OSError:
                    continue
                candidates.extend(children)
                for child in children:  # /media/<user>/<label>, /run/media/<user>/<label>
                    try:
                        candidates.extend(os.path.join(child, e) for e in os.listdir(child))
                    except OSError:
                        continue
        return candidates

    def _find_uf2_volume(self, on_line: Callable[[str], None]) -> str | None:
        """Auto-detect a mounted UF2 bootloader volume (one containing ``INFO_UF2.TXT``).

        HW-validation pending — detection logic unit-tested. Returns the mount path of the
        first matching removable volume, or ``None`` if none is present.
        """
        for vol in self._uf2_candidate_volumes():
            try:
                if os.path.isfile(os.path.join(vol, "INFO_UF2.TXT")):
                    on_line(f"[uf2] found UF2 bootloader volume: {vol}")
                    return vol
            except OSError:
                continue
        return None

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

    # ── RTL8720DN / BW16 (AmebaD ImageTool, not esptool) ─────────────

    def _flash_rtl8720(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        import os

        from src.core.backends import rtl8720_backend as rtl

        on_line = _percent_adapter(progress)
        if not rtl.ambd_tool_available():
            on_line("[rtl8720] " + rtl.ambd_install_guidance())
            return False

        # Resolve the firmware bundle: a local dir the user pointed at, else download the
        # Vampire Deauther AmebaD bundle (km0/km4/app + SRAM loader) to a dedicated workdir.
        bundle_dir = profile.local_path if profile.local_path else None
        if bundle_dir and os.path.isdir(bundle_dir):
            on_line(f"[rtl8720] using local bundle dir: {bundle_dir}")
        else:
            # Resolve the bundle from the SELECTED profile (not hardcoded) so any rtl8720-backend
            # firmware works — the Vampire Deauther OR BlueJammer-V2's BW16 controller, each with
            # its own pinned km0/km4/image2 bundle. Falls back to the Vampire profile.
            core_id = profile.core_id if profile.core_id in flash_core.PROFILES else "rtl8720"
            core = flash_core.get_profile(core_id)
            try:
                on_line(f"[rtl8720] resolving {core_id} AmebaD bundle...")
                _tag, assets = core.latest_release()
            except Exception as exc:
                on_line(f"[rtl8720] could not resolve firmware bundle: {exc}")
                return False
            bundle_dir = os.path.join(flash_core.cache_dir(), f"{core_id}_bundle")
            os.makedirs(bundle_dir, exist_ok=True)
            try:
                for a in assets:
                    p = flash_core.download_to(a["url"], bundle_dir, a["name"], on_line)
                    # Pinned-firmware integrity gate: reject a tampered/changed bundle BEFORE
                    # it reaches the AmebaD ImageTool (which would flash it regardless).
                    if a.get("sha256"):
                        flash_core.verify_sha256(p, a["sha256"], on_line)
            except Exception as exc:
                on_line(f"[rtl8720] firmware download/verify failed: {exc}")
                return False

        try:
            # auto=True: the BW16 auto-enters download mode via DTR/RTS (validated on HW).
            rc = rtl.flash_ambd(port, bundle_dir, auto=True, on_line=on_line)
        except Exception as exc:
            on_line(f"[rtl8720] flash failed: {exc}")
            rc = 1
        if progress:
            progress(100 if rc == 0 else 0, "Flash complete" if rc == 0 else "Flash failed")
        return rc == 0

    # ── SD image (Raspberry Pi firmwares) ────────────────────────────

    def _flash_sd(
        self, port: str, profile: FirmwareProfile, progress: ProgressCallback | None
    ) -> bool:
        # SD images (Pwnagotchi / RaspyJack / Kali ARM) are NOT serial-flashable: the serial Flash
        # path only carries a `port`, while SD imaging needs an explicit removable target drive +
        # confirmation (the whole drive is erased). The real, device-driven flow is
        # :meth:`flash_sd_image` (fed by :meth:`discover_sd_images`). This handler only produces an
        # accurate message if such a profile ever reaches the serial dispatcher; it never writes.
        on_line = _percent_adapter(progress)
        label = profile.name or profile.id or "SD image"
        on_line(f"[sd] '{label}' is a Raspberry-Pi SD image, not a serial-flashable firmware — the "
                "serial Flash path only has a port. SD imaging needs an explicit removable target "
                "drive + confirmation. Pick an asset with FlashEngine.discover_sd_images(profile_id), "
                "then write it with FlashEngine.flash_sd_image(profile_id, asset, device, "
                "confirmed=True) — the same removable-only, verified writer the OS/USB imaging flow uses.")
        return False

    def discover_sd_images(
        self, profile_id: str, progress_callback: ProgressCallback | None = None
    ) -> list[dict]:
        """Return the downloadable image asset(s) for a Pi SD profile (``pwnagotchi`` /
        ``raspyjack`` / ``kali-arm``) so a caller can pick one for :meth:`flash_sd_image`.

        Thin, network-touching delegation to :func:`sd_backend.discover_images` — the entry point
        the device-driven SD-imaging flow uses (the serial Flash path cannot; see :meth:`_flash_sd`)."""
        from src.core.backends import sd_backend

        on_line, _ = _sd_line_progress_adapters(progress_callback)
        return sd_backend.discover_images(profile_id, on_line)

    def flash_sd_image(
        self,
        profile_id: str,
        asset: dict,
        device: str,
        progress_callback: ProgressCallback | None = None,
        *,
        confirmed: bool = False,
        verify: bool = True,
    ) -> bool:
        """Image a Raspberry-Pi SD firmware to a removable *device*: download -> decompress ->
        block-write -> read-back verify, via the hardened :mod:`sd_backend` pipeline.

        This is the real "SD flow" the serial Flash path can't drive (it only has a port). *device*
        must be a removable target from ``sd_backend.detect_sd_cards`` — the writer re-validates that
        and refuses fixed/system disks. Returns True on success.

        Raises :class:`ValueError` unless ``confirmed=True``: the ENTIRE target drive is erased, so
        nothing is written without an explicit confirmation (mirrors ``sd_backend.flash_sd`` and
        ``os_catalog.flash_os_image``)."""
        if not confirmed:
            raise ValueError("flash_sd_image requires confirmed=True — the entire target drive "
                             "will be erased")
        from src.core.backends import sd_backend

        on_line, on_progress = _sd_line_progress_adapters(progress_callback)
        try:
            # Reserve the device the way serial ops reserve a port, so two writes can't race onto
            # (and corrupt) the same card. A different device still images in parallel.
            with self._port_guard(device):
                with self._lock:
                    self._status = FlashStatus.FLASHING
                try:
                    rc = sd_backend.flash_sd(profile_id, asset, device, on_line, on_progress,
                                             confirmed=True, verify=verify)
                    ok = rc == 0
                except Exception as exc:  # never let a backend exception leak unlabelled
                    log.exception("SD image flash failed")
                    on_line(f"[sd] flash failed: {exc}")
                    ok = False
                with self._lock:
                    self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
                return ok
        except _PortBusy:
            on_line(f"[sd] target {device} is busy with another flash/backup/erase — aborted.")
            return False

    # ── Backup ───────────────────────────────────────────────────────

    def backup(
        self,
        port: str,
        output_path: str | Path,
        progress_callback: ProgressCallback | None = None,
        *,
        chip: str = "auto",
        size: str = "detect",
    ) -> bool:
        """Read the entire flash to *output_path* (exact file) via the proven esptool
        plumbing, auto-detecting the chip when ``chip='auto'`` and the real flash SIZE when
        ``size='detect'`` (the default).

        A hardcoded 4 MB read silently truncated the backup of any >4 MB board (S3 DevKit 8 MB,
        T-Deck 16 MB, …): esptool reads only the first 4 MB with no error, so a later restore can
        never recover anything above 0x400000 — defeating the safety net the backup exists for.

        (For a richer backup-with-restore + .meta sidecar + listing, see
        :mod:`src.core.backup`, surfaced through a dedicated backup flow.)
        """
        try:
            with self._port_guard(port):
                with self._lock:
                    self._status = FlashStatus.BACKING_UP
                on_line = _percent_adapter(progress_callback)
                if not chip or chip == "auto":
                    chip = flash_core.detect_chip(port, on_line) or "esp32"
                # An explicit size is trusted; a detection MISS is a guessed 4 MB that must be flagged so
                # the completion status can't read as a clean full backup when it may be truncated.
                size_detected = True
                if not size or size in ("detect", "auto"):
                    size, size_detected = self._detect_flash_size(port, chip, on_line)
                argv = flash_core.esptool_argv(
                    "--chip", chip, "--port", port, "--baud", "921600",
                    "read_flash", "0x0", size, str(output_path),
                )
                rc = flash_core._run_stream(argv, on_line)
                ok = rc == 0
                if progress_callback:
                    if ok and not size_detected:
                        progress_callback(100, "Backup complete — ⚠ flash size not detected (assumed 4 MB, "
                                               "may be truncated on a larger board)")
                    else:
                        progress_callback(100 if ok else 0, "Backup complete" if ok else "Backup failed")
                with self._lock:
                    self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
                return ok
        except _PortBusy:
            if progress_callback:
                progress_callback(0, f"Port {port} is busy with another flash/backup/erase — backup aborted.")
            return False

    @staticmethod
    def _detect_flash_size(port: str, chip: str, on_line: Callable[[str], None]) -> tuple[str, bool]:
        """Detect the chip's real flash size via ``esptool flash_id`` and return ``(hex_byte_count,
        detected)``. Falls back to 0x400000 (4 MB) if detection yields nothing — so an undetectable
        board still backs up its first 4 MB rather than failing, while >4 MB boards get a FULL image.

        ``detected=False`` means the 4 MB is a GUESS, not a read value: the caller MUST surface that
        loudly, because a silent 4 MB read of a larger board produces a truncated backup that can never
        fully restore it — the exact data-loss hazard this backup path exists to prevent."""
        lines: list[str] = []

        def cap(s: str) -> None:
            lines.append(s)
            on_line(s)

        try:
            flash_core._run_stream(
                flash_core.esptool_argv("--chip", chip, "--port", port, "flash_id"), cap)
        except Exception as exc:  # noqa: BLE001 — detection is best-effort; fall back to 4 MB
            on_line(f"[backup] flash-size detection failed ({exc}); assuming 4 MB")
        size_map = {"1MB": "0x100000", "2MB": "0x200000", "4MB": "0x400000",
                    "8MB": "0x800000", "16MB": "0x1000000", "32MB": "0x2000000"}
        for line in lines:
            if "Detected flash size:" in line:
                key = line.split(":")[-1].strip()
                if key in size_map:
                    return size_map[key], True
                on_line(f"[backup] WARNING: unrecognized flash size {key!r} from esptool — assuming 4 MB. "
                        "A board larger than 4 MB will have a TRUNCATED backup that cannot fully restore it.")
                return "0x400000", False
        on_line("[backup] WARNING: could NOT detect flash size — assuming a 4 MB read. If this board is "
                "LARGER than 4 MB, this backup will be TRUNCATED and cannot fully restore it. Re-run once "
                "the board answers flash_id, or pass an explicit size.")
        return "0x400000", False

    def erase(
        self,
        port: str,
        progress_callback: ProgressCallback | None = None,
        *,
        chip: str = "auto",
    ) -> bool:
        """Erase the entire flash via the proven esptool plumbing, auto-detecting the chip when
        ``chip='auto'``. Destructive — callers should confirm with the user first."""
        try:
            with self._port_guard(port):
                with self._lock:
                    self._status = FlashStatus.FLASHING
                on_line = _percent_adapter(progress_callback)
                if not chip or chip == "auto":
                    chip = flash_core.detect_chip(port, on_line) or "esp32"
                rc = flash_core.erase(port, chip, on_line)
                ok = rc == 0
                if progress_callback:
                    progress_callback(100 if ok else 0, "Erase complete" if ok else "Erase failed")
                with self._lock:
                    self._status = FlashStatus.DONE if ok else FlashStatus.ERROR
                return ok
        except _PortBusy:
            if progress_callback:
                progress_callback(0, f"Port {port} is busy with another flash/backup/erase — erase aborted.")
            return False
