"""Inspect a frozen Linux binary without executing it. Requires PyInstaller and pyelftools."""
from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

REQUIRED_MODULES = (
    "src.ui.packaged_smoke", "src.ui.launcher", "webview", "webview.platforms.qt", "qtpy",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "flask", "flask_socketio",
    "engineio.async_drivers.threading", "cryptography", "esptool", "pyzipper", "defusedxml",
)
QPA_LIBRARIES = (
    "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
    "libxcb-render-util.so.0", "libxcb-shape.so.0", "libxcb-xinerama.so.0",
    "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
)


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        raise ValueError(f"Invalid glibc version: {value}")
    return tuple(int(part) for part in value.split("."))


def elf_requirements(data: bytes) -> tuple[str, set[str]]:
    from elftools.elf.elffile import ELFFile
    elf = ELFFile(io.BytesIO(data))
    versions = set()
    section = elf.get_section_by_name(".gnu.version_r")
    if section:
        for _library, requirements in section.iter_versions():
            versions.update(item.name[6:] for item in requirements
                            if re.fullmatch(r"GLIBC_\d+\.\d+(?:\.\d+)?", item.name))
    return elf["e_machine"], versions


def module_present(module: str, names: set[str], modules: set[str]) -> bool:
    path = module.replace(".", "/")
    return module in modules or any(name == path + ".py" or name.startswith(path + ".")
                                    for name in names)


def inspect_bundle(path: Path, machine: str, max_glibc: str) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    ceiling = version_tuple(max_glibc)
    archive = CArchiveReader(str(path))
    names = set(archive.toc)
    modules = set(archive.open_embedded_archive("PYZ.pyz").toc)
    errors = []
    for module in REQUIRED_MODULES:
        if not module_present(module, names, modules):
            errors.append(f"Missing required module: {module}")
    basenames = {name.rsplit("/", 1)[-1] for name in names}
    for library in QPA_LIBRARIES:
        if library not in basenames:
            errors.append(f"Missing QPA library: {library}")
    if not any(name.endswith("/platforms/libqxcb.so") for name in names):
        errors.append("Missing xcb platform plugin")
    if not any(name.endswith("QtWebEngineProcess") for name in names):
        errors.append("Missing Chromium renderer process")
    if not any(name.startswith("libpython") and ".so" in name for name in names):
        errors.append("Missing bundled Python shared library")

    requirements = {}

    def inspect_elf(name, data):
        arch, versions = elf_requirements(data)
        if arch != machine:
            errors.append(f"Wrong architecture in {name}: {arch}, expected {machine}")
        high = sorted((v for v in versions if version_tuple(v) > ceiling), key=version_tuple)
        if high:
            errors.append(f"{name} requires GLIBC_{high[-1]}, above {max_glibc}")
        if versions:
            requirements[name] = max(versions, key=version_tuple)

    inspect_elf(path.name, path.read_bytes())
    for name in sorted(names):
        # Include Chromium's executable as well as libraries; inspecting only the bootloader
        # missed the published libpython GLIBC_2.38 requirement.
        if re.search(r"\.so(?:\.|$)", name) or name.endswith("QtWebEngineProcess"):
            data = archive.extract(name)
            if data and data.startswith(b"\x7fELF"):
                inspect_elf(name, data)
    return {"artifact": path.name, "machine": machine, "max_glibc": max_glibc,
            "elf_glibc_requirements": requirements, "errors": errors}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--machine", choices=("EM_X86_64", "EM_AARCH64"), required=True)
    parser.add_argument("--max-glibc", required=True)
    args = parser.parse_args(argv)
    result = inspect_bundle(args.artifact, args.machine, args.max_glibc)
    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
