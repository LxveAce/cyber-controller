"""Suicide Marauder — password & duress setup (host-side provisioning wrapper).

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

_SUBMODULE = Path(__file__).resolve().parents[2] / "deadmans-switch"
_HOST = _SUBMODULE / "host"
_PARTS = _SUBMODULE / "firmware" / "partitions"

# (flash_size, variant) -> partition CSV. guardcfg/otadata offsets are READ from the CSV by the
# provisioner, never hardcoded here.
_CSV_BY_SIZE = {
    ("4MB", "fork"): "suicide_4MB.csv",
    ("8MB", "fork"): "suicide_8MB.csv",
    ("16MB", "fork"): "suicide_16MB.csv",
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


def partitions_csv(cfg: SuicideConfig) -> Path:
    """Resolve the partition CSV for a config."""
    name = (_CSV_BY_SIZE.get((cfg.flash_size, cfg.variant))
            or _CSV_BY_SIZE.get((cfg.flash_size, "fork"))
            or "suicide_4MB.csv")
    return _PARTS / name


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
    prov = _load_provision()
    args = argparse.Namespace(
        partitions=str(partitions_csv(cfg)), out=str(out_dir), variant=cfg.variant, chip=cfg.chip,
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

    print("=== Suicide Marauder — password & duress setup (host-side) ===")
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
        print(f"  NOTE: {len(warnings)} firmware image(s) not present — build the Suicide-Marauder")
        print("        firmware (build_dir) to complete the bundle, then flash via flash_suicide.")
    if cfg.armed == 1:
        print("  *** armed=1: this board WILL self-destruct on the configured trigger conditions. ***")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
