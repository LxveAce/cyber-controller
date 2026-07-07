"""Dead Man's Switch — password & duress setup (host-side provisioning wrapper).

Owner-only DEFENSIVE anti-forensic layer for hardware you own. A disarmed or unprovisioned board can
NEVER wipe (fail-safe). This module drives the Suicide-Marauder host provisioner
(`provision.build_bundle`) to bake a per-device ``guardcfg`` NVS image — the **PBKDF2-HMAC-SHA256
hashed boot password** plus the arm/wipe config — and a flash bundle manifest.

Security: the plaintext password is hashed **host-side** and the buffer is **zeroized**; it is never
stored, logged, or sent to the device (only {salt, pwhash, params} reach the board). This is
"Approach A" — set up the password in the UI/CLI BEFORE flashing the Suicide build. The complete
flash bundle additionally needs the Suicide-Marauder firmware ``.bin``s in ``build_dir`` (build them
first); the password/config (``guardcfg.bin``) is provisioned here regardless.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.core.resources import resource_path

_SUBMODULE = resource_path("deadmans-switch")
_HOST = _SUBMODULE / "host"
_PARTS = _SUBMODULE / "firmware" / "partitions"
# Cyber-Controller-bundled partition tables, checked BEFORE the submodule's — lets us ship a layout the
# pinned submodule doesn't carry (e.g. the 8 MB guardian table) without editing the public submodule.
_LOCAL_PARTS = resource_path("src", "config", "dms_partitions")

# (flash_size, variant) -> partition CSV. guardcfg/otadata offsets are READ from the CSV by the
# provisioner, never hardcoded here.
_CSV_BY_SIZE = {
    ("4MB", "fork"): "suicide_4MB.csv",
    ("8MB", "fork"): "suicide_8MB.csv",
    ("16MB", "fork"): "suicide_16MB.csv",
    ("8MB", "guardian"): "suicide_guardian_8MB.csv",
    ("16MB", "guardian"): "suicide_guardian_16MB.csv",
}


@dataclass
class SuicideConfig:
    """The gate config baked into ``guardcfg`` NVS (SPEC §4). Defaults are SAFE (disarmed, T1)."""

    chip: str = "esp32"            # esp32 | esp32s2 | esp32s3 | esp32c3 | esp32c6 | esp32h2
    variant: str = "fork"          # fork | guardian
    flash_size: str = "4MB"        # 4MB | 8MB | 16MB
    arm_pin: int = 27              # dead-man GPIO (never a strapping pin)
    arm_level: int = 1             # 1=HIGH means ARMED
    arm_pull: int = 2              # 0=none 1=pullup 2=pulldown (fail-safe)
    max_att: int = 2               # wrong-password attempts before wipe
    deadman: int = 1               # 1=cut/disarmed line wipes when armed
    armed: int = 0                 # MASTER ARM (0=DISARMED safe default)
    wipe_ota: int = 1
    wipe_nvs: int = 1
    wipe_spiffs: int = 1
    wipe_sd: int = 1
    brick: int = 0                 # 0=T1 reflashable, 1=T2 brick boot chain
    sd_passes: int = 1
    flash_passes: int = 1          # internal-flash overwrite passes (defense-in-depth)
    fast_wipe: int = 0
    kdf_iter: int = 10000
    build_dir: str = ""            # dir with bootloader/partitions/app/boot_app0 bins (when built)


_FLASH_ALIASES = {"4mb": "4MB", "8mb": "8MB", "16mb": "16MB"}


def _canon_flash_size(v: str) -> str:
    """Canonicalize free-form flash-size text ('16mb', '16 MB') to the exact key ('16MB')."""
    return _FLASH_ALIASES.get(v.strip().lower().replace(" ", ""), v.strip())


# esptool --chip names (mirrors provision.CHIPS). The bootloader offset the provisioner derives is an
# EXACT-membership test (S3/C3/C6/H2 -> 0x0, else 0x1000), so any non-exact spelling silently yields
# the classic 0x1000 offset. We must hand the provisioner a canonical name — never free-form text.
_CHIPS = ("esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2")


def _canon_chip(v: str) -> str:
    """Canonicalize free-form chip text to the exact esptool key, or RAISE on an unknown chip.

    Lowercases, strips, and drops hyphens/underscores/spaces so Espressif's own branding
    ('ESP32-S3'), run-together forms ('ESP32S3'), and bare-suffix shorthand ('s3' -> 'esp32s3') all
    map to the canonical 'esp32s3'. RAISES ``ValueError`` on anything not in :data:`_CHIPS`.

    This is fail-loud by design (mirrors :func:`_canon_flash_size` / :func:`partitions_csv`): the
    provisioner's ``bootloader_offset`` uses an EXACT membership test, so an unrecognized spelling of
    an S3/C3/C6/H2 part would silently default to the classic-ESP32 2nd-stage bootloader offset
    (0x1000). Flashing that bundle writes the bootloader to 0x1000 while the ROM loader reads it from
    0x0 -> the board is unbootable/soft-bricked, yet the tool reports success. Reject it here instead.
    """
    s = v.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    # bare-suffix shorthand ('s3','c3','h2',...) -> prepend the family prefix
    if not s.startswith("esp32") and ("esp32" + s) in _CHIPS:
        s = "esp32" + s
    if s not in _CHIPS:
        raise ValueError(
            f"unknown chip {v!r}; known chips: {list(_CHIPS)}. An unrecognized spelling would "
            f"default to the classic-ESP32 bootloader offset 0x1000, which soft-bricks an "
            f"S3/C3/C6/H2 board (its 2nd-stage bootloader must live at 0x0)."
        )
    return s


def partitions_csv(cfg: SuicideConfig) -> Path:
    """Resolve the partition CSV for a config.

    RAISES on an unknown (flash_size, variant) instead of silently returning the 4MB layout. A wrong
    table bakes ``guardcfg`` at the wrong flash offset (4MB vs 16MB); the firmware then reads no config
    from its real offset, treats the board as unprovisioned, and — per the fail-safe — boots with NO
    password gate at all, while the owner believes the boot password is set. Fail loud instead.
    """
    flash = _canon_flash_size(cfg.flash_size)
    # NO silent fork fallback: guardian on a fork table lacks the `factory` partition the guardian gate
    # needs, which used to crash the provisioner with a cryptic "partition 'factory' not found". Require
    # an exact table and fail with an actionable message instead (this is the fail-loud the docstring means).
    name = _CSV_BY_SIZE.get((flash, cfg.variant))
    if name is None:
        if cfg.variant == "guardian":
            sizes = sorted(k[0] for k in _CSV_BY_SIZE if k[1] == "guardian")
            raise ValueError(
                f"Guardian needs two app slots — a ~1 MB gate plus the full firmware in ota_0 — plus "
                f"filesystems, which don't fit in {cfg.flash_size}. Guardian supports {sizes}; pick "
                f"8MB or 16MB, or use the Fork variant for 4 MB flash."
            )
        raise ValueError(
            f"no partition table for flash_size={cfg.flash_size!r} variant={cfg.variant!r}; "
            f"known combos: {sorted(_CSV_BY_SIZE)}"
        )
    # Prefer a Cyber-Controller-bundled table over the submodule's, so a locally-shipped layout wins.
    local = _LOCAL_PARTS / name
    return local if local.is_file() else _PARTS / name


def _load_provision():
    """Import the Dead Man's Switch host provisioner from the submodule."""
    if not (_HOST / "provision.py").exists():
        raise FileNotFoundError(
            f"Dead Man's Switch provisioner not found at {_HOST}. Initialise the submodule: "
            f"git submodule update --init deadmans-switch"
        )
    if str(_HOST) not in sys.path:
        sys.path.insert(0, str(_HOST))
    import provision  # noqa: E402 — dynamic submodule import
    return provision


def build(cfg: SuicideConfig, password: str, out_dir: str | Path) -> tuple[str, dict, list]:
    """Host-side provisioning: hash *password* (PBKDF2) and bake ``guardcfg`` + bundle into *out_dir*.

    Returns ``(out_dir, manifest, warnings)``. *warnings* lists firmware images not yet present
    (build them to complete the flash bundle). The password buffer is consumed + zeroized by the
    provisioner — it is never stored or logged.
    """
    if not password:
        raise ValueError("password must not be empty")
    # Canonicalize + validate the chip BEFORE anything else so a bad chip fails LOUD here — for both
    # the CLI and programmatic callers — instead of silently defaulting to the classic 0x1000
    # bootloader offset downstream (which soft-bricks an S3/C3/C6/H2 board).
    chip = _canon_chip(cfg.chip)
    prov = _load_provision()
    args = argparse.Namespace(
        partitions=str(partitions_csv(cfg)), out=str(out_dir), variant=cfg.variant, chip=chip,
        build_dir=(cfg.build_dir or None), nvs_gen_dir=None,
        arm_pin=cfg.arm_pin, arm_level=cfg.arm_level, arm_pull=cfg.arm_pull, max_att=cfg.max_att,
        deadman=cfg.deadman, armed=cfg.armed, wipe_ota=cfg.wipe_ota, wipe_nvs=cfg.wipe_nvs,
        wipe_spiffs=cfg.wipe_spiffs, wipe_sd=cfg.wipe_sd, brick=cfg.brick, sd_passes=cfg.sd_passes,
        flash_passes=cfg.flash_passes, fast_wipe=cfg.fast_wipe, kdf_iter=cfg.kdf_iter,
    )
    pw_buf = bytearray(password.encode("utf-8"))
    return prov.build_bundle(args, pw_buf)  # consumes + ZEROIZES pw_buf


def run_cli(argv: list[str] | None = None) -> int:
    """Interactive CLI setup (``cyber-controller --suicide-setup``). Collects config + password
    (via getpass — never on argv), builds the bundle, prints next steps."""
    import getpass

    print("=== Dead Man's Switch — password & duress setup (host-side) ===")
    print("Owner-only DEFENSIVE use on hardware you own. A disarmed/unprovisioned board NEVER wipes.\n")
    cfg = SuicideConfig()

    def ask(prompt: str, default, cast=str):
        raw = input(f"  {prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            print(f"    (invalid — using {default})")
            return default

    cfg.chip = ask("chip (esp32/esp32s3/esp32c3...)", cfg.chip)
    cfg.flash_size = ask("flash size (4MB/8MB/16MB)", cfg.flash_size)
    cfg.variant = ask("variant (fork/guardian)", cfg.variant)
    cfg.arm_pin = ask("arming GPIO pin", cfg.arm_pin, int)
    cfg.arm_level = ask("armed logic level (1=HIGH, 0=LOW)", cfg.arm_level, int)
    # Derive the fail-safe pull from the level (HIGH-armed -> pulldown, LOW-armed -> pullup); the other
    # pairing is rejected by the provisioner, so never leave it at the HIGH-only default for a LOW arm.
    cfg.arm_pull = 2 if cfg.arm_level == 1 else 1
    cfg.max_att = ask("wrong-password attempts before wipe", cfg.max_att, int)
    cfg.armed = ask("ARM now? (0=disarmed safe default, 1=armed)", cfg.armed, int)
    cfg.brick = ask("brick boot chain on wipe? (0=T1 reflashable, 1=T2 brick)", cfg.brick, int)
    cfg.build_dir = ask("firmware build dir (blank = provision guardcfg only)", cfg.build_dir)

    pw = getpass.getpass("  Set boot password: ")
    pw2 = getpass.getpass("  Confirm password: ")
    if not pw or pw != pw2:
        print("Passwords empty or do not match — aborted.")
        return 2
    out = os.path.abspath("suicide_bundle")
    try:
        out_dir, manifest, warnings = build(cfg, pw, out)
    except Exception as exc:
        print(f"Provisioning failed: {exc}")
        return 1
    finally:
        pw = pw2 = None  # drop our local copies

    print(f"\nProvisioned bundle: {out_dir}")
    print(f"  guardcfg.bin minted — PBKDF2-HMAC-SHA256 iter={cfg.kdf_iter}; password hashed + zeroized.")
    print(f"  armed={cfg.armed} (0=disarmed safe) arm_pin={cfg.arm_pin} arm_level={cfg.arm_level} "
          f"max_att={cfg.max_att} brick={cfg.brick}")
    if warnings:
        print(f"  NOTE: {len(warnings)} firmware image(s) not present — build the Dead Man's Switch firmware")
        print("        firmware (build_dir) to complete the bundle, then flash via flash_suicide.")
    if cfg.armed == 1:
        print("  *** armed=1: this board WILL self-destruct on the configured trigger conditions. ***")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
