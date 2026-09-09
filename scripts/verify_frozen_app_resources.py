#!/usr/bin/env python3
"""Verify a frozen Linux ONEFILE PyInstaller CArchive contains its declared application resources.

Archive contract (deliberately narrow):
  * Target is a PyInstaller *onefile* CArchive (PKG) -- the single-file Linux executable produced by
    ``build.py`` without ``--onedir``. It is read with the installed PyInstaller CArchiveReader (its
    class constants are reused to parse the RAW table-of-contents records). This tool does NOT cover
    the onedir ``_internal`` layout, wheels, or any other packager, and makes no such claim.
  * A member's stored name may use POSIX ``/`` (Linux) or ``\\`` (a fixture written on Windows
    normalizes it); both are canonicalized to ``/`` for matching. Absolute, empty, ``.``/``..`` and
    ``:``-bearing names are rejected. Duplicate members whose canonical names collide directly OR
    case-insensitively are an ambiguous-archive error -- checked against the RAW TOC record list,
    because the reader's ``toc`` dict silently collapses two exact-duplicate stored names into one
    entry (an exact duplicate would otherwise escape the check).

What it does / does not do:
  * Reads declared resource member BYTES and hashes them. It never imports the application, runs an
    archive member, or decrypts anything. Opaque payloads (e.g. encrypted tool ``.pack`` files) are
    inventoried and hashed only -- never decrypted or run.
  * Compares each non-opaque required resource's archive bytes to the manifest's expected SHA256.
    Missing required, wrong bytes, ambiguous names, an archive read/decompression failure, and a
    malformed/unreadable archive are all findings; the process returns nonzero and writes a readable
    JSON report. A bad manifest / missing archive is a finite input error (exit 2). No failure
    escapes as an uncaught traceback.

Source identity (see FR-4): ``source_identity`` is a CALLER LABEL copied from the manifest. This
tool proves agreement between the archive bytes and the manifest's expected bytes; it does NOT bind
those bytes to an immutable Git commit. That binding is the generator's job (its --git-repo mode) or
a separate source-inventory gate; absent that, the report establishes agreement with the supplied
expected bytes only, and the exact-source gate stays with the caller.

Usage:
  verify_frozen_app_resources.py --archive <onefile-exe> --manifest <manifest.json> [--out <report>]
Exit: 0 = all declared checks pass, 1 = findings, 2 = usage/input error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path


class InputError(Exception):
    """A finite usage/input error (exit 2); its message never contains resource content."""


def canonical_member(name: str) -> str:
    """POSIX-canonical member name, or raise ValueError for a hostile/malformed shape."""
    if type(name) is not str or not name or "\0" in name or ":" in name:
        raise ValueError("invalid archive member name")
    canonical = name.replace("\\", "/")
    if canonical.startswith("/") or any(p in ("", ".", "..") for p in canonical.split("/")):
        raise ValueError("absolute, empty, or traversing archive member name")
    return canonical


def raw_member_names(archive) -> list:
    """Return the RAW list of non-option member names from the CArchive TOC, WITH duplicates.

    The reader stores ``toc`` as a dict, so two exact-duplicate stored names collapse to one entry
    before any check sees them. Re-parse the raw TOC records (reusing the reader's class constants)
    so an exact duplicate is visible and can be rejected."""
    from PyInstaller.archive.readers import CArchiveReader as R
    with open(archive._filename, "rb") as fp:
        cookie_start = archive._find_magic_pattern(fp, R._COOKIE_MAGIC_PATTERN)
        fp.seek(cookie_start)
        cookie = fp.read(R._COOKIE_LENGTH)
        _magic, arc_len, toc_off, toc_len, _pyv, _pylib = struct.unpack(R._COOKIE_FORMAT, cookie)
        start = cookie_start + R._COOKIE_LENGTH - arc_len
        fp.seek(start + toc_off)
        data = fp.read(toc_len)
    names, pos = [], 0
    while pos < len(data):
        entry_length = struct.unpack(R._TOC_ENTRY_FORMAT, data[pos:pos + R._TOC_ENTRY_LENGTH])[0]
        typecode = struct.unpack(R._TOC_ENTRY_FORMAT, data[pos:pos + R._TOC_ENTRY_LENGTH])[5]
        pos += R._TOC_ENTRY_LENGTH
        name_length = entry_length - R._TOC_ENTRY_LENGTH
        raw_name = struct.unpack(f"{name_length}s", data[pos:pos + name_length])[0]
        pos += name_length
        if typecode.decode("ascii") != "o":  # 'o' = OPTION entry; not a resource member
            names.append(raw_name.rstrip(b"\0").decode("utf-8"))
    return names


def canonical_name_map(raw_names) -> dict:
    """Map canonical -> raw over the RAW name list, rejecting any collision (direct, canonical, or
    case-insensitive). Retains the raw key for extraction. Raises ValueError on a collision."""
    mapping, casefold = {}, {}
    for raw in raw_names:
        canonical = canonical_member(raw)
        cf = canonical.casefold()
        if canonical in mapping or cf in casefold:
            raise ValueError(f"ambiguous archive member name: {canonical}")
        mapping[canonical] = raw
        casefold[cf] = canonical
    return mapping


def _load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise InputError("manifest is not valid UTF-8") from exc
    except (OSError, ValueError) as exc:
        raise InputError("manifest is missing or not valid JSON") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("resources"), list)
            or not data["resources"]):
        raise InputError("manifest must be an object with a non-empty 'resources' list")
    seen = set()
    for row in data["resources"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not row["path"]:
            raise InputError("each resource needs a non-empty string 'path'")
        try:
            canonical = canonical_member(row["path"])  # path errors normalized here (FR-3b)
        except ValueError as exc:
            raise InputError(f"invalid manifest resource path: {row['path']!r}") from exc
        if canonical in seen:
            raise InputError(f"duplicate resource path in manifest: {canonical}")
        seen.add(canonical)
        row["_canonical"] = canonical
        if (not row.get("opaque") and row.get("required", True)
                and not isinstance(row.get("sha256"), str)):
            raise InputError(f"required non-opaque resource needs an expected sha256: {canonical}")
    return data


def verify(archive_path: Path, manifest: dict) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    try:
        archive = CArchiveReader(str(archive_path))
        name_map = canonical_name_map(raw_member_names(archive))
    except ValueError as exc:
        # Ambiguous member name -- a real, reportable archive fault (not a usage error).
        return {"errors": [f"ambiguous archive: {exc}"], "clean": False, "checked": []}
    except Exception as exc:  # noqa: BLE001 -- unreadable/malformed archive is a reportable failure
        return {"errors": [f"unreadable or malformed archive: {type(exc).__name__}"],
                "clean": False, "checked": []}

    checked, errors = [], []
    for row in manifest["resources"]:
        canonical = row["_canonical"]
        required = row.get("required", True)
        opaque = bool(row.get("opaque"))
        raw = name_map.get(canonical)
        present, actual = False, None
        if raw is None:
            if required:
                errors.append(f"missing required resource: {canonical}")
        else:
            try:
                data = archive.extract(raw)  # may raise on a damaged/compressed member (FR-3a)
            except Exception as exc:  # noqa: BLE001 -- a member read/decompress fault is a finding
                errors.append(f"resource read/decompress error ({type(exc).__name__}): {canonical}")
                data = None
                present = True  # it is in the TOC; the bytes could not be recovered
            else:
                present = data is not None
                actual = hashlib.sha256(data).hexdigest() if data is not None else None
                if data is None and required:
                    errors.append(f"resource present in TOC but no data extracted: {canonical}")
        expected = row.get("sha256")
        matches = None
        if actual is not None and not opaque and expected is not None:
            matches = actual == expected
            if not matches:
                errors.append(f"wrong bytes for resource: {canonical}")
        checked.append({"path": canonical, "required": required, "opaque": opaque,
                        "present": present,
                        "expected_sha256": expected, "actual_sha256": actual, "matches": matches})
    return {"errors": errors, "clean": not errors, "checked": checked}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify frozen onefile CArchive app resources.")
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    try:
        if not args.archive.is_file():
            raise InputError("archive not found")
        manifest = _load_manifest(args.manifest)
        result = verify(args.archive, manifest)
    except InputError as exc:
        # Finite input error: emit a readable diagnostic and, when possible, a report; exit 2.
        report = {"clean": False, "input_error": str(exc)}
        if args.out is not None:
            try:
                args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        print(json.dumps(report), file=sys.stderr)
        return 2

    for row in result["checked"]:
        row.pop("_canonical", None)
    report = {
        "archive": str(args.archive),
        "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        "source_identity": manifest.get("source_identity"),
        "source_identity_note": "caller label from manifest; not verified Git provenance",
        "resource_count": len(result["checked"]),
        "checked": result["checked"], "errors": result["errors"], "clean": result["clean"],
        "limits": ("onefile CArchive member bytes only; no import, member execution, or "
                   "decryption; opaque payloads inventoried/hashed only; agreement with the "
                   "manifest's expected bytes, not native/runtime readiness"),
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"clean": report["clean"], "resource_count": report["resource_count"],
                      "errors": report["errors"]}, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
