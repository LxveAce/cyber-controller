r"""Bundled crack-tool packs — the "it comes with it" storage layer.

CC ships the crack tools (aircrack-ng, ...) as AES-encrypted ``.pack`` files in ``src/config/tools/``
because Windows Defender flags the raw binaries as PUA and DELETES them on sight (see
``scripts/build_tool_packs.py`` for the why + provenance). An encrypted archive can't be scanned
inside, so the pack survives at rest in the repo / clone / built app.

This module only LISTS the packs and EXTRACTS one into a destination directory the caller has already
prepared. It never touches antivirus and never extracts on its own — the opt-in, disclaimer, and the
one-time Defender exclusion (so the extracted binaries aren't re-quarantined) live in the UI + a
platform helper. Extraction verifies every file against the manifest's SHA-256, fail-closed.

The pack password is intentionally NOT secret (it lives here in the open): its only purpose is to stop
AV from false-positive-deleting a legitimate, standard FOSS tool before the user has consented — not
to hide anything. Anyone can regenerate the packs with ``scripts/build_tool_packs.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional

from .resources import resource_path

Line = Callable[[str], None]

#: Public by design — see the module docstring + scripts/build_tool_packs.py.
PACK_PASSWORD = b"cyber-controller-tools"


def packs_dir() -> str:
    """Where the shipped ``.pack`` / ``.manifest.json`` files live (dev + frozen, via resource_path)."""
    return str(resource_path("src", "config", "tools"))


@dataclass(frozen=True)
class ToolPack:
    """One bundled tool pack + its manifest (provenance + per-file hashes)."""

    name: str
    tool: str
    version: str
    platform: str
    primary_exe: str
    pack_path: str
    manifest: dict


def list_packs() -> list[ToolPack]:
    """Every bundled pack that has both a ``.manifest.json`` and its ``.pack`` on disk."""
    directory = packs_dir()
    out: list[ToolPack] = []
    if not os.path.isdir(directory):
        return out
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".manifest.json"):
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        pack_path = os.path.join(directory, str(m.get("name", "")) + ".pack")
        if os.path.isfile(pack_path):
            out.append(ToolPack(
                name=str(m.get("name", "")), tool=str(m.get("tool", "")),
                version=str(m.get("version", "")), platform=str(m.get("platform", "")),
                primary_exe=str(m.get("primary_exe", "")), pack_path=pack_path, manifest=m))
    return out


def pack_for_tool(tool: str, platform_key: str) -> Optional[ToolPack]:
    """The bundled pack for *tool* on *platform_key* (e.g. ``"aircrack-ng"``, ``"windows"``), or None."""
    return next((p for p in list_packs() if p.tool == tool and p.platform == platform_key), None)


def extract_pack(pack: ToolPack, dest_dir: str, on_line: Optional[Line] = None) -> str:
    """Decrypt + extract *pack* into *dest_dir* and return the primary-exe path.

    The caller MUST have already excluded *dest_dir* from Defender (else the extracted PUA binaries are
    re-quarantined). Verifies each file against the manifest SHA-256 before writing; a mismatch raises
    RuntimeError and the extraction is abandoned (fail-closed). Requires ``pyzipper`` (a CC dep)."""
    import pyzipper  # local import: only needed when actually extracting (keeps import graph light)

    log: Line = on_line or (lambda *_a: None)
    os.makedirs(dest_dir, exist_ok=True)
    want = {f["name"]: f["sha256"] for f in pack.manifest.get("files", []) if "sha256" in f}
    with pyzipper.AESZipFile(pack.pack_path) as z:
        z.setpassword(PACK_PASSWORD)
        for name in z.namelist():
            blob = z.read(name)
            exp = want.get(name)
            if exp and hashlib.sha256(blob).hexdigest() != exp:
                raise RuntimeError(f"{name}: SHA-256 mismatch on extract — refusing to install")
            # zip-slip guard: the resolved path must stay inside dest_dir.
            out_path = os.path.join(dest_dir, name)
            if not os.path.realpath(out_path).startswith(os.path.realpath(dest_dir) + os.sep):
                raise RuntimeError(f"unsafe pack member path: {name!r}")
            os.makedirs(os.path.dirname(out_path) or dest_dir, exist_ok=True)  # hashcat has kernels/ etc.
            with open(out_path, "wb") as out:
                out.write(blob)
            log(f"[tools] extracted {name}")
    exe = os.path.join(dest_dir, pack.primary_exe)
    log(f"[tools] {pack.tool} {pack.version} unpacked into {dest_dir}")
    return exe


def enable_dir() -> str:
    """The folder bundled tools are extracted into — the one the user Defender-excludes. Parallel to the
    resolver's tools dir so :func:`crack_pipeline.detect_tools` finds the enabled tools afterward."""
    from .tool_installer import default_tools_dir
    return default_tools_dir()


def enable_bundled(pack: ToolPack, on_line: Optional[Line] = None) -> tuple[bool, str]:
    """Extract *pack* into ``enable_dir()/<tool>/`` and confirm the primary exe actually runs.

    The caller MUST have already added a Defender exclusion for :func:`enable_dir` (else the extracted
    PUA binary is re-quarantined). Returns (ok, message) — a Defender block is reported honestly, never
    as a fake success."""
    from . import defender
    log: Line = on_line or (lambda *_a: None)
    dest = os.path.join(enable_dir(), pack.tool)
    try:
        exe = extract_pack(pack, dest, log)
    except Exception as exc:  # noqa: BLE001
        return (False, f"extract failed: {exc}")
    if not os.path.isfile(exe):
        return (False, "extracted, but the tool binary is missing — Windows Defender likely quarantined "
                       "it. Add the exclusion (see the notice) for this folder and try again.")
    if defender.is_windows() and not defender.exe_runs(exe):
        return (False, "extracted, but the tool won't launch — Defender is still blocking it. Make sure "
                       "the exclusion covers this folder, then try again.")
    return (True, f"{pack.tool} enabled: {exe}")
