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
import importlib.util
import inspect
import os
import subprocess
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
    provision_path = _HOST / "provision.py"
    if not provision_path.is_file():
        raise FileNotFoundError(_runtime_unavailable_message())
    if str(_HOST) not in sys.path:
        sys.path.insert(0, str(_HOST))
    spec = importlib.util.spec_from_file_location("_cc_dms_provision", provision_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Dead Man's Switch provisioner at {provision_path}")
    provision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provision)
    return provision


def _runtime_unavailable_message() -> str:
    """Explain an absent DMS runtime without prescribing a source-only fix to wheel users."""
    checkout_root = Path(__file__).resolve().parents[2]
    if (checkout_root / ".gitmodules").is_file():
        return (
            f"Dead Man's Switch provisioner not found at {_HOST}. This source checkout needs: "
            "git submodule update --init deadmans-switch"
        )
    return (
        "Dead Man's Switch setup is unavailable in this installation because its host provisioner "
        "is not packaged. Use a distribution that explicitly includes the DMS runtime, or run from "
        "a source checkout with the deadmans-switch submodule initialized."
    )


def _nvs_generator_interface_error(selection) -> str | None:
    """Return why a discovered NVS generator cannot support the provisioner's real call path.

    This is a dependency/interface probe only: it never supplies configuration, a password, or an
    output path. Callable package APIs are accepted directly. A CLI-only implementation must prove
    that the exact ``generate`` subcommand is loadable through a bounded ``--help`` invocation.
    """
    try:
        if not isinstance(selection, tuple) or len(selection) != 2:
            return "the provisioner returned an invalid generator descriptor"
        kind, target = selection
        if kind == "module":
            # Mirror the pinned provisioner's exact target selection: the nested module wins when
            # it merely exposes ``generate`` (even a non-callable value), because provisioning then
            # uses the nested module name for its CLI fallback.  Probing the parent here would report
            # a different path ready from the one that will actually run after the password prompt.
            missing = object()
            selected = target
            nested = getattr(target, "nvs_part_gen", missing)
            if nested is not missing and getattr(nested, "generate", missing) is not missing:
                selected = nested

            generate = getattr(selected, "generate", None)
            if callable(generate):
                call_impl = getattr(generate, "__call__", None)
                deferred = any(
                    detector(candidate)
                    for candidate in (generate, call_impl)
                    if candidate is not None
                    for detector in (
                        inspect.iscoroutinefunction,
                        inspect.isasyncgenfunction,
                        inspect.isgeneratorfunction,
                    )
                )
                if deferred:
                    # The pinned provisioner invokes generate(ns) synchronously and treats a normal
                    # return as success.  A coroutine/generator return therefore produces no image
                    # and never reaches its exception-driven CLI fallback.
                    return "the selected module's generator does not execute synchronously"
                try:
                    signature = inspect.signature(generate)
                    signature.bind(argparse.Namespace())
                except (TypeError, ValueError):
                    # The real provisioner catches an incompatible in-process call and falls back
                    # to ``python -m <selected module> generate ...``.  Validate that exact fallback
                    # below instead of declaring any arbitrary callable ready.
                    pass
                else:
                    return None

            module_name = getattr(selected, "__name__", "")
            if not isinstance(module_name, str) or not module_name:
                return (
                    "the selected module has neither a compatible generator nor a runnable "
                    "module name"
                )
            command = [sys.executable, "-m", module_name, "generate", "--help"]
            label = f"module {module_name!r}"
        elif kind == "script":
            if not isinstance(target, (str, os.PathLike)):
                return "the provisioner returned an invalid generator script path"
            script = Path(target)
            if not script.is_file():
                return f"the selected generator script does not exist: {script}"
            command = [sys.executable, str(script), "generate", "--help"]
            label = f"script {script}"
        else:
            return f"the provisioner returned unsupported generator kind {kind!r}"

        # In a PyInstaller/frozen app ``sys.executable`` is CyberController.exe, not Python.  The
        # pinned provisioner currently uses that same executable for its CLI fallback, so treating
        # the app's unrelated zero-exit help as generator readiness would only defer failure until
        # after secret collection.  A frozen distribution needs a callable packaged generator API
        # (accepted above) or an explicit embedded runner before this fallback is genuinely usable.
        if getattr(sys, "frozen", False):
            return (
                "the selected generator only exposes a Python CLI fallback, which is unavailable "
                "in this frozen application"
            )

        probe = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
        if probe.returncode == 0:
            return None
        output = (
            probe.stdout.decode("utf-8", "replace")
            if isinstance(probe.stdout, bytes)
            else probe.stdout
        )
        detail = " ".join(str(output or "").split())[:240]
        suffix = f": {detail}" if detail else ""
        return f"the selected CLI-only generator rejected its generate interface ({label}){suffix}"
    except Exception as exc:  # noqa: BLE001 - readiness is a fail-closed UI boundary
        return f"the selected NVS generator interface could not be validated: {exc}"


def dms_runtime_status() -> tuple[bool, str]:
    """Validate the complete minimum setup runtime without collecting a password or writing files."""
    try:
        provision = _load_provision()
    except Exception as exc:  # noqa: BLE001 - optional runtime may fail through any import dependency
        return (False, str(exc))

    build_bundle = getattr(provision, "build_bundle", None)
    if not callable(build_bundle):
        return (False, "Dead Man's Switch provisioner has no compatible build_bundle entry point.")
    try:
        build_impl = getattr(build_bundle, "__call__", None)
        if any(
            detector(candidate)
            for candidate in (build_bundle, build_impl)
            if candidate is not None
            for detector in (
                inspect.iscoroutinefunction,
                inspect.isasyncgenfunction,
                inspect.isgeneratorfunction,
            )
        ):
            return (False, "Dead Man's Switch build_bundle entry point must execute synchronously.")
        inspect.signature(build_bundle).bind(argparse.Namespace(), bytearray())
    except Exception:  # noqa: BLE001 - optional runtime interface inspection must fail closed
        return (
            False,
            "Dead Man's Switch provisioner has an incompatible build_bundle(args, pw_buf) entry "
            "point.",
        )
    find_nvs_gen = getattr(provision, "_find_nvs_gen", None)
    if not callable(find_nvs_gen):
        return (False, "Dead Man's Switch provisioner cannot validate its NVS generator dependency.")
    try:
        # build_bundle passes args.nvs_gen_dir (None in this wrapper) into the pinned finder.  Calling
        # a no-argument lookalike here would report ready and then fail only after password collection.
        inspect.signature(find_nvs_gen).bind(None)
        generator = find_nvs_gen(None)
    except Exception as exc:  # noqa: BLE001 - provisioner exposes a user-facing dependency error
        return (False, f"Dead Man's Switch NVS generator is unavailable: {exc}")
    generator_error = _nvs_generator_interface_error(generator)
    if generator_error:
        return (False, f"Dead Man's Switch NVS generator is unavailable: {generator_error}")

    missing_tables = []
    tables = []
    try:
        for flash_size, variant in _CSV_BY_SIZE:
            table = partitions_csv(SuicideConfig(flash_size=flash_size, variant=variant))
            if not table.is_file():
                missing_tables.append(table.name)
            else:
                tables.append((variant, table))
    except Exception as exc:  # noqa: BLE001 - availability must remain a reasoned false, not crash UI
        return (False, f"Dead Man's Switch partition data cannot be validated: {exc}")
    if missing_tables:
        return (False, "Dead Man's Switch partition data is incomplete: " + ", ".join(missing_tables))

    parse_partitions = getattr(provision, "parse_partitions_csv", None)
    require_partition = getattr(provision, "require_partition", None)
    if not callable(parse_partitions) or not callable(require_partition):
        return (
            False,
            "Dead Man's Switch provisioner cannot structurally validate its partition data.",
        )
    guard_name = getattr(provision, "GUARDCFG_PART", "guardcfg")
    otadata_name = getattr(provision, "OTADATA_PART", "otadata")
    try:
        for variant, table in tables:
            parts = parse_partitions(str(table))
            required = [
                (str(guard_name), require_partition(parts, guard_name)),
                (str(otadata_name), require_partition(parts, otadata_name)),
            ]
            if variant == "guardian":
                required.extend(
                    [
                        ("factory", require_partition(parts, "factory")),
                        ("ota_0", require_partition(parts, "ota_0")),
                    ]
                )
            for name, record in required:
                if not isinstance(record, dict):
                    raise ValueError(f"partition {name!r} has an invalid record")
                for field in ("offset", "size"):
                    value = record.get(field)
                    if type(value) is not int or value <= 0:
                        raise ValueError(
                            f"partition {name!r} has an invalid {field}: {value!r}"
                        )
            guard = required[0][1]
            if guard.get("subtype") != "nvs":
                raise ValueError(
                    f"partition {guard_name!r} must have subtype 'nvs' "
                    f"(found {guard.get('subtype')!r})"
                )
            if guard["size"] < 0x3000 or guard["size"] % 0x1000:
                raise ValueError(
                    f"partition {guard_name!r} size must be a 0x1000-aligned value of at least "
                    f"0x3000 (found 0x{guard['size']:X})"
                )
    except Exception as exc:  # noqa: BLE001 - optional pinned parser exposes user-facing failures
        return (False, f"Dead Man's Switch partition data is invalid: {exc}")
    return (True, "ready")


def dms_runtime_available() -> bool:
    """Whether ``--deadman-setup`` has a loadable provisioner, generator, and partition data."""
    return dms_runtime_status()[0]


# Range/domain checks mirroring the provisioner's argparse ``choices=``. build() constructs the
# Namespace by hand (bypassing argparse), so a programmatic/GUI caller or the loose run_cli ask()
# helper can pass out-of-range values (e.g. arm_level=5, armed=2) that would otherwise be written to
# NVS as a nonsense u8 and make the firmware misread the gate. Fail loud here instead. NOTE: the
# unsafe arm_pull/arm_level PAIRING and kdf_iter/max_att floors stay the provisioner's job — this is
# only the independent per-field domain check the hand-built Namespace skips.
_CFG_DOMAINS = {
    "arm_pin": (0, 48), "arm_level": (0, 1), "arm_pull": (0, 2), "max_att": (0, 255),
    "deadman": (0, 1), "armed": (0, 1), "wipe_ota": (0, 1), "wipe_nvs": (0, 1),
    "wipe_spiffs": (0, 1), "wipe_sd": (0, 1), "brick": (0, 1), "sd_passes": (0, 255),
    "flash_passes": (0, 255), "fast_wipe": (0, 1),
}


def _validate_cfg(cfg: SuicideConfig) -> None:
    """Range-check the gate config before it is baked into NVS. RAISES ValueError on anything out of
    domain, so a mistyped ``arm_level=5`` / ``armed=2`` fails LOUD here instead of silently writing a
    nonsense u8 to the board's guardcfg."""
    for field, (lo, hi) in _CFG_DOMAINS.items():
        val = getattr(cfg, field)
        if not isinstance(val, int) or not (lo <= val <= hi):
            raise ValueError(f"{field}={val!r} out of range [{lo}..{hi}]")
    if cfg.kdf_iter < 1000:
        raise ValueError(f"kdf_iter={cfg.kdf_iter} too low (min 1000 PBKDF2 iterations)")


def build(cfg: SuicideConfig, password: str, out_dir: str | Path) -> tuple[str, dict, list]:
    """Host-side provisioning: hash *password* (PBKDF2) and bake ``guardcfg`` + bundle into *out_dir*.

    Returns ``(out_dir, manifest, warnings)``. *warnings* lists firmware images not yet present
    (build them to complete the flash bundle). The password buffer is consumed + zeroized by the
    provisioner — it is never stored or logged.
    """
    if not password:
        raise ValueError("password must not be empty")
    _validate_cfg(cfg)
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

    runtime_ok, runtime_reason = dms_runtime_status()
    if not runtime_ok:
        print(f"Provisioning unavailable: {runtime_reason}", file=sys.stderr)
        return 1

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
    cfg.build_dir = ask("firmware build dir (blank = guardcfg-only PREVIEW, not flashable)", cfg.build_dir)

    # Explicit consent gate for a self-destruct-capable config: a bare numeric 1 at the ARM prompt is
    # too easy to fat-finger. Echo the dangerous config back and require a typed token before minting;
    # anything but the exact token reverts to disarmed (fail-safe).
    if cfg.armed == 1:
        print("\n  *** You are about to mint an ARMED dead-man bundle. ***")
        print(f"      armed=1  brick={cfg.brick} "
              f"({'T2 PERMANENT brick' if cfg.brick else 'T1 reflashable'})  "
              f"arm_pin={cfg.arm_pin}  arm_level={cfg.arm_level}  max_att={cfg.max_att}")
        print("      Once flashed and physically wired, the configured trigger will IRREVERSIBLY erase"
              + (" and BRICK" if cfg.brick else "") + " the board.")
        token = input("      Type ARM (uppercase) to confirm, anything else to disarm: ").strip()
        if token != "ARM":
            print("      Not confirmed — reverting to armed=0 (disarmed, safe).")
            cfg.armed = 0

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
        print(f"  NOTE: this is a guardcfg-ONLY bundle — {len(warnings)} firmware image(s) not present.")
        print("        It is a config PREVIEW and is NOT flashable as-is: flash_suicide requires an")
        print("        integrity hash for every image, and that hash is recorded only at provision time.")
        print("        To get a flashable bundle: build the Dead Man's Switch firmware, then RE-RUN this")
        print("        setup with 'firmware build dir' set to that build output. (Dropping .bins into")
        print("        this bundle dir afterward will NOT work — the flasher rejects images that have no")
        print("        provisioned hash.)")
    if cfg.armed == 1:
        print("  *** armed=1: this board WILL self-destruct on the configured trigger conditions. ***")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
