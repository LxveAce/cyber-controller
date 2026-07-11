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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-zip", default=None, help="build from a local (already-verified) zip")
    args = ap.parse_args()
    build_pack(AIRCRACK, args.from_zip)
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
