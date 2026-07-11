#!/usr/bin/env python3
r"""Build the encrypted crack-tool packs that ship with Cyber Controller.

WHY ENCRYPTED: Windows Defender (and other AV) flag aircrack-ng / hashcat as PUA and DELETE the raw
binaries on sight — so they cannot survive as loose ``.exe`` in the repo, in a clone, or extracted
from the built app. An AES archive can't be scanned inside, so the pack survives at rest; CC only
decrypts + extracts it into a folder the user has explicitly excluded from Defender (an opt-in, admin-
consented step shown with a disclaimer). The password is NOT a secret (it's in ``tool_bundle.py``) —
its only job is to stop AV from false-positive-deleting a legitimate, standard FOSS tool before the
user has consented. This is standard practice for distributing security tools, not evasion.

PROVENANCE: aircrack-ng-1.7-win.zip is fetched from the Internet Archive (the official
download.aircrack-ng.org host is down) and verified byte-for-byte against the vendor-published SHA-1
(872ef4f731080626d7cee893ef42c8f630ce90cd, cross-verified from two independent sources) before it is
repacked — so the shipped pack is provably the authentic official binary regardless of mirror.

Run:  python scripts/build_tool_packs.py            # fetch + verify + build the aircrack pack
      python scripts/build_tool_packs.py --from-zip <path>   # build from a local verified zip

Requires pyzipper (a CC runtime dep too). Writes src/config/tools/<name>.pack + <name>.manifest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

import pyzipper

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_ROOT, "src", "config", "tools")

# The pack password is intentionally public (see module docstring) — it only defeats AV pre-scan.
PACK_PASSWORD = b"cyber-controller-tools"

AIRCRACK = {
    "name": "aircrack-ng-1.7-win",
    "tool": "aircrack-ng",
    "version": "1.7",
    "platform": "windows",
    "license": "GPL-2.0",
    "source_url": "https://download.aircrack-ng.org/aircrack-ng-1.7-win.zip",
    "archive_sha1": "872ef4f731080626d7cee893ef42c8f630ce90cd",
    "wayback_url": "https://web.archive.org/web/2id_/https://download.aircrack-ng.org/aircrack-ng-1.7-win.zip",
    "member_prefix": "aircrack-ng-1.7-win/bin/",   # flat bin/ dir (exes + Cygwin DLLs)
    "meta_files": ("LICENSE", "AUTHORS"),          # vendored alongside for GPL compliance
    "primary_exe": "aircrack-ng.exe",
}


def _fetch_verified_zip(spec: dict, local: str | None) -> bytes:
    if local:
        data = open(local, "rb").read()
    else:
        req = urllib.request.Request(spec["wayback_url"], headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=180).read()
    got = hashlib.sha1(data).hexdigest()
    if got != spec["archive_sha1"]:
        raise SystemExit(f"SHA-1 mismatch: got {got}, want {spec['archive_sha1']} — refusing to build")
    print(f"  source verified: {len(data):,} bytes, SHA-1 {got}")
    return data


def build_pack(spec: dict, local_zip: str | None = None) -> None:
    print(f"building {spec['name']}.pack …")
    os.makedirs(_OUT, exist_ok=True)
    data = _fetch_verified_zip(spec, local_zip)
    zf = zipfile.ZipFile(io.BytesIO(data))
    pref = spec["member_prefix"]

    entries: list[dict] = []
    pack_path = os.path.join(_OUT, spec["name"] + ".pack")
    with pyzipper.AESZipFile(pack_path, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as pack:
        pack.setpassword(PACK_PASSWORD)
        for name in zf.namelist():
            if name.startswith(pref) and not name.endswith("/") and "/" not in name[len(pref):]:
                blob = zf.read(name)
                arc = name[len(pref):]
                pack.writestr(arc, blob)
                entries.append({"name": arc, "size": len(blob),
                                "sha256": hashlib.sha256(blob).hexdigest()})
        for meta in spec["meta_files"]:
            m = spec["name"] + "/" + meta
            if m in zf.namelist():
                blob = zf.read(m)
                pack.writestr(meta, blob)
                entries.append({"name": meta, "size": len(blob),
                                "sha256": hashlib.sha256(blob).hexdigest()})

    manifest = {k: spec[k] for k in ("name", "tool", "version", "platform", "license",
                                     "source_url", "archive_sha1", "primary_exe")}
    manifest["files"] = sorted(entries, key=lambda e: e["name"])
    manifest["file_count"] = len(entries)
    with open(os.path.join(_OUT, spec["name"] + ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"  wrote {pack_path} ({os.path.getsize(pack_path):,} bytes, {len(entries)} files) + manifest")


HASHCAT = {
    "name": "hashcat-7.1.2-win",
    "tool": "hashcat",
    "version": "7.1.2",
    "platform": "windows",
    "license": "MIT",
    "source_url": "https://hashcat.net/files/hashcat-7.1.2.7z",
    "primary_exe": "hashcat.exe",
    "exclude": ("hashcat.bin",),   # the Linux binary — dropped from the Windows pack
}


def build_hashcat_pack(local_7z: str | None = None) -> None:
    """hashcat ships only a universal .7z. Download it (self-pin its SHA-256 — the vendor publishes no
    hash, only a PGP sig), extract, and repack the Windows tree (minus hashcat.bin) encrypted.

    MAINTAINER STEP — not runnable in a default env: hashcat's .7z uses the **BCJ2** filter (py7zr can't
    unpack it), so this needs real **7-Zip** (``7z``/``7zr`` on PATH). It also extracts ``hashcat.exe``
    (a PUA binary) to disk, so the OUTPUT DIR must be Defender-excluded first or Windows deletes it
    mid-build. Run on a box with 7-Zip + a temp Defender exclusion for ``src/config/tools``."""
    import shutil as _sh
    import subprocess
    print(f"building {HASHCAT['name']}.pack …")
    os.makedirs(_OUT, exist_ok=True)
    if local_7z:
        data = open(local_7z, "rb").read()
    else:
        req = urllib.request.Request(HASHCAT["source_url"], headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=240).read()
    sha = hashlib.sha256(data).hexdigest()
    print(f"  downloaded {len(data):,} bytes, self-pinned SHA-256 {sha}")

    sevenz_exe = _sh.which("7z") or _sh.which("7zr")
    if not sevenz_exe:
        raise SystemExit("hashcat's .7z uses the BCJ2 filter — install 7-Zip (7z/7zr on PATH) and run "
                         "with src/config/tools Defender-excluded (it extracts PUA binaries).")
    tmp = os.path.join(_OUT, "_hc_tmp")
    if os.path.isdir(tmp):
        _sh.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    sevenz = os.path.join(tmp, "hc.7z")
    with open(sevenz, "wb") as f:
        f.write(data)
    subprocess.run([sevenz_exe, "x", f"-o{tmp}", "-y", sevenz], check=True,
                   capture_output=True, text=True)
    os.remove(sevenz)
    root = next(os.path.join(tmp, d) for d in os.listdir(tmp)
                if d.lower().startswith("hashcat-") and os.path.isdir(os.path.join(tmp, d)))

    entries: list[dict] = []
    pack_path = os.path.join(_OUT, HASHCAT["name"] + ".pack")
    with pyzipper.AESZipFile(pack_path, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as pack:
        pack.setpassword(PACK_PASSWORD)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn in HASHCAT["exclude"]:
                    continue
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, root).replace("\\", "/")
                with open(full, "rb") as f:
                    blob = f.read()
                pack.writestr(arc, blob)
                entries.append({"name": arc, "size": len(blob),
                                "sha256": hashlib.sha256(blob).hexdigest()})
    import shutil as _sh
    _sh.rmtree(tmp, ignore_errors=True)

    manifest = {k: HASHCAT[k] for k in ("name", "tool", "version", "platform", "license",
                                        "source_url", "primary_exe")}
    manifest["archive_sha256"] = sha
    manifest["files"] = sorted(entries, key=lambda e: e["name"])
    manifest["file_count"] = len(entries)
    with open(os.path.join(_OUT, HASHCAT["name"] + ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"  wrote {pack_path} ({os.path.getsize(pack_path):,} bytes, {len(entries)} files) + manifest")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-zip", default=None, help="build aircrack from a local (verified) zip")
    ap.add_argument("--from-7z", default=None, help="build hashcat from a local .7z")
    ap.add_argument("--hashcat", action="store_true", help="also build the hashcat pack")
    args = ap.parse_args()
    build_pack(AIRCRACK, args.from_zip)
    if args.hashcat or args.from_7z:
        build_hashcat_pack(args.from_7z)
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
