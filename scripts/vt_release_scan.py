#!/usr/bin/env python3
"""Scan a release's binaries through VirusTotal and merge the results into the GitHub release notes.

Used by `.github/workflows/build-release.yml` so EVERY release posts VirusTotal results. Idempotent: the VT
table is written between HTML markers, so re-running replaces it rather than stacking duplicates.

Usage:  VT_API_KEY=... python scripts/vt_release_scan.py <tag> <bindir>
The key comes from the environment (a GitHub Actions repo secret `VT_API_KEY`) — it is NEVER hard-coded here.
If VT_API_KEY is unset the script exits nonzero: "no scan ran" must never read as a green release
validation, so a missing secret fails the run loudly instead of skipping.
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


# The four last_analysis_stats buckets that mean an engine actually EVALUATED the file. A completed
# scan has at least one; an empty dict, a still-pending analysis, or an all-zero result means no
# real scan evidence and must fail the release rather than read as a clean 0/0.
_EVALUATED = ("malicious", "suspicious", "undetected", "harmless")


def tally(stats) -> tuple[int, int]:
    """Return (detections, engines_evaluated) for a COMPLETED scan, or raise VTError if the evidence
    is incomplete/empty/malformed. A missing-key default alone is not enough — zero evaluated engines
    (a pending or empty analysis) must fail, never pass as 0/0."""
    if stats is None:
        raise VTError("scan did not complete within the poll window (still pending) — incomplete evidence")
    if not isinstance(stats, dict) or not stats:
        raise VTError(f"malformed analysis stats: {stats!r}")
    for k in _EVALUATED:
        v = stats.get(k, 0)
        if type(v) is not int:  # bool is an int subclass; type() check excludes True/False too
            raise VTError(f"malformed analysis stat {k}={v!r}")
    evaluated = sum(stats.get(k, 0) for k in _EVALUATED)
    if evaluated == 0:
        raise VTError("no engine evaluated the file (0 results) — incomplete scan evidence")
    return stats.get("malicious", 0) + stats.get("suspicious", 0), evaluated


def build_rows(files, get_stats, sleep=None):
    """Scan each file via get_stats (injected for testing) and build the notes table rows.

    Returns (rows, failed, flagged): `failed` is True if ANY file lacks complete scan evidence
    (a real VT failure, or a pending/empty/malformed/no-engine result — all fail the release);
    `flagged` lists files with one or more detections, which require manual review before publishing
    and are never silently dismissed as expected PyInstaller heuristics."""
    if sleep is None:  # resolved at call time so tests can neutralise time.sleep on the module
        sleep = time.sleep
    rows: list[str] = []
    failed = False
    flagged: list[str] = []
    if not files:
        # Nothing to scan means no scan evidence at all — the release cannot be validated.
        return rows, True, flagged
    for f in files:
        name = os.path.basename(f)
        try:
            sha, stats = get_stats(f)
            det, evaluated = tally(stats)
        except VTError as e:
            # A real failure OR incomplete evidence (bad/expired key, outage, rate-limit exhaustion,
            # still-pending, empty, malformed, zero engines). Record a clearly-labeled FAILED row and
            # fail the step — never dress incomplete evidence up as a clean or pending result.
            failed = True
            link = f"https://www.virustotal.com/gui/file/{sha256(f)}"
            rows.append(f"| `{name}` | **scan FAILED** | [report]({link}) |")
            print(f"{name}: SCAN FAILED — {e}", file=sys.stderr, flush=True)
            sleep(16)
            continue
        link = f"https://www.virustotal.com/gui/file/{sha}"
        rows.append(f"| `{name}` | {det}/{evaluated} | [report]({link}) |")
        if det > 0:
            flagged.append(name)
        print(f"{name}: {stats}", flush=True)
        sleep(16)
    return rows, failed, flagged


def assert_still_draft(tag: str) -> None:
    """Re-check, immediately before mutating the release notes, that the release is STILL a draft.
    The scan above can run for many minutes; if the release was published meanwhile, editing its
    notes would mutate a public release. Not atomic, but it closes the obvious window."""
    out = subprocess.run(
        ["gh", "release", "view", tag, "--json", "isDraft", "--jq", ".isDraft"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if out != "true":
        raise VTError(f"release {tag} is no longer a draft (isDraft={out!r}) — refusing to edit its notes.")


def main() -> int:
    key = os.environ.get("VT_API_KEY")
    if not key:
        # A missing key means NO scan ran — that must fail the validation, never pass silently.
        print("VT_API_KEY not set — VirusTotal scan cannot run; failing.", file=sys.stderr)
        return 1
    if len(sys.argv) != 3:
        print("usage: vt_release_scan.py <tag> <bindir>", file=sys.stderr)
        return 2
    tag, bindir = sys.argv[1], sys.argv[2]

    files = sorted(
        f for f in glob.glob(os.path.join(bindir, "cyber-controller-*"))
        if not f.endswith(".sha256") and not f.endswith(".txt")
    )
    if not files:
        print(f"no binaries found in {bindir} to scan — failing the release step.", file=sys.stderr)
        return 1

    rows, failed, flagged = build_rows(files, lambda f: stats_for(f, key))

    note = ("Every binary is scanned before release; the full reports are linked below. "
            "Detections on unsigned PyInstaller executables are common but are NOT assumed benign — "
            "review each flagged report before publishing.")
    if flagged:
        note += ("\n\n> **Review required:** detections reported for "
                 + ", ".join(f"`{n}`" for n in flagged) + ".")
    section = (
        f"{BEGIN}\n## VirusTotal\n{note}\n\n"
        "| File | Detections | Report |\n|---|---|---|\n" + "\n".join(rows) + f"\n{END}"
    )

    # Fresh draft check AFTER the (potentially long) scan, before touching the release notes.
    assert_still_draft(tag)
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
        # At least one binary lacked complete scan evidence. The notes carry honest 'scan FAILED'
        # rows; exit nonzero so the release step fails loudly instead of publishing a VirusTotal
        # section that never actually scanned (e.g. a revoked key, or a pending/empty analysis).
        print("One or more binaries lacked complete scan evidence — failing the release step.",
              file=sys.stderr)
        return 1
    if flagged:
        print("Detections reported (" + ", ".join(flagged) + ") — review before publishing.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
