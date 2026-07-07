#!/usr/bin/env python3
"""Scan a release's binaries through VirusTotal and merge the results into the GitHub release notes.

Used by `.github/workflows/build-release.yml` so EVERY release posts VirusTotal results. Idempotent: the VT
table is written between HTML markers, so re-running replaces it rather than stacking duplicates.

Usage:  VT_API_KEY=... python scripts/vt_release_scan.py <tag> <bindir>
The key comes from the environment (a GitHub Actions repo secret `VT_API_KEY`) — it is NEVER hard-coded here.
If VT_API_KEY is unset the script exits 0 (a no-op) so a missing secret never fails a release.
Requires: requests, and the `gh` CLI authenticated (GH_TOKEN on Actions runners).
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import sys
import time

import requests

BEGIN, END = "<!-- VT:BEGIN -->", "<!-- VT:END -->"


class VTError(RuntimeError):
    """A VirusTotal request failed for real (auth / server / rate-limit) — not merely 'no data yet'.

    Raised so a broken secret or outage fails the release loudly instead of being coerced into an
    innocuous-looking 'scan pending' row.
    """


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def api(method: str, url: str, key: str, **kw):
    headers = {"x-apikey": key}
    timeout = kw.pop("timeout", 120)  # read once so retries keep the caller's timeout
    for _ in range(8):
        # Rewind any upload stream so a 429 retry re-sends the full file, not EOF (0 bytes).
        for spec in (kw.get("files") or {}).values():
            fh = spec[1] if isinstance(spec, (tuple, list)) else spec
            if hasattr(fh, "seek"):
                fh.seek(0)
        r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        if r.status_code == 429:  # public API rate limit — back off
            time.sleep(35)
            continue
        return r
    # Retries exhausted on a persistent 429: this is a real failure, not success. Surface it
    # instead of returning the last rate-limited response (which callers would misread as data).
    raise VTError(f"{method} {url} still rate-limited (HTTP 429) after retries")


def stats_for(path: str, key: str):
    sha = sha256(path)
    r = api("GET", f"https://www.virustotal.com/api/v3/files/{sha}", key)
    if r.status_code == 200:
        return sha, r.json()["data"]["attributes"]["last_analysis_stats"]
    if r.status_code == 404:  # fresh build — upload via the large-file URL, then poll
        uu = api("GET", "https://www.virustotal.com/api/v3/files/upload_url", key).json()["data"]
        with open(path, "rb") as fh:
            aid = api("POST", uu, key, files={"file": (os.path.basename(path), fh)}, timeout=600).json()["data"]["id"]
        for _ in range(60):
            time.sleep(20)
            ra = api("GET", f"https://www.virustotal.com/api/v3/analyses/{aid}", key)
            if ra.status_code != 200:  # poll itself errored (auth expired / server) — don't mask as pending
                raise VTError(f"analysis poll for {os.path.basename(path)} failed: HTTP {ra.status_code}")
            if ra.json().get("data", {}).get("attributes", {}).get("status") == "completed":
                return sha, ra.json()["data"]["attributes"]["stats"]
        # Upload succeeded but the analysis is still running after the poll window: genuinely pending.
        return sha, None
    # Any other status (401/403 bad-or-expired key, 5xx outage, …) is a real failure, not 'no data yet'.
    raise VTError(f"VirusTotal lookup for {os.path.basename(path)} failed: HTTP {r.status_code}")


def main() -> int:
    key = os.environ.get("VT_API_KEY")
    if not key:
        print("VT_API_KEY not set — skipping VirusTotal (no-op).", file=sys.stderr)
        return 0
    if len(sys.argv) != 3:
        print("usage: vt_release_scan.py <tag> <bindir>", file=sys.stderr)
        return 2
    tag, bindir = sys.argv[1], sys.argv[2]

    files = sorted(
        f for f in glob.glob(os.path.join(bindir, "cyber-controller-*"))
        if not f.endswith(".sha256") and not f.endswith(".txt")
    )
    rows = []
    failed = False
    for f in files:
        name = os.path.basename(f)
        try:
            sha, st = stats_for(f, key)
        except VTError as e:
            # A real failure (bad/expired key, outage, rate-limit exhaustion). Record it as a
            # clearly-labeled FAILED row and fail the step — do NOT dress it up as 'scan pending'.
            failed = True
            link = f"https://www.virustotal.com/gui/file/{sha256(f)}"
            rows.append(f"| `{name}` | **scan FAILED** | [report]({link}) |")
            print(f"{name}: SCAN FAILED — {e}", file=sys.stderr, flush=True)
            time.sleep(16)
            continue
        link = f"https://www.virustotal.com/gui/file/{sha}"
        if st is None:
            rows.append(f"| `{name}` | _scan pending_ | [report]({link}) |")
        else:
            det = st.get("malicious", 0) + st.get("suspicious", 0)
            tot = det + st.get("undetected", 0) + st.get("harmless", 0)
            rows.append(f"| `{name}` | {det}/{tot} | [report]({link}) |")
        print(f"{name}: {st}", flush=True)
        time.sleep(16)

    section = (
        f"{BEGIN}\n## VirusTotal\n"
        "Every binary is scanned before release. Unsigned PyInstaller executables normally trip a few "
        "heuristic engines — the full reports:\n\n"
        "| File | Detections | Report |\n|---|---|---|\n" + "\n".join(rows) + f"\n{END}"
    )

    body = subprocess.run(
        ["gh", "release", "view", tag, "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, check=True,
    ).stdout
    body = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", body, flags=re.S).rstrip()
    new_body = f"{body}\n\n{section}\n"
    with open("_vt_body.md", "w", encoding="utf-8") as fh:
        fh.write(new_body)
    subprocess.run(["gh", "release", "edit", tag, "--notes-file", "_vt_body.md"], check=True)
    print(f"VirusTotal section updated on release {tag}.")
    if failed:
        # At least one binary could not be scanned. The notes carry honest 'scan FAILED' rows;
        # exit nonzero so the release step fails loudly (e.g. a revoked VT_API_KEY) instead of
        # silently publishing a VirusTotal section that never actually scanned anything.
        print("One or more binaries could not be scanned — failing the release step.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
