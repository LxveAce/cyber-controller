"""Structural invariants for .github/workflows/build-release.yml — the guarantees that keep a
staged build from ever mutating a published release or building the wrong source. Offline: parses
the YAML, no GitHub calls. If one of these fails, the release pipeline lost a safety property."""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WF = _ROOT / ".github" / "workflows" / "build-release.yml"

RAW = _WF.read_text(encoding="utf-8")
# The workflow's comments legitimately DISCUSS the banned constructs (why always() and the
# release action are absent) — string-level assertions must only see functional lines.
CODE = "\n".join(l for l in RAW.splitlines() if not l.lstrip().startswith("#"))
DOC = yaml.safe_load(RAW)
# PyYAML parses the bare `on:` key as boolean True.
ON = DOC.get("on", DOC.get(True))
JOBS = DOC["jobs"]

BUILD_JOBS = ["build-windows", "build-linux", "build-linux-arm", "build-macos"]
CHECKOUT_JOBS = ["preflight", *BUILD_JOBS, "checksums", "virustotal"]


def _steps(job):
    return JOBS[job]["steps"]


def _run_blocks(job):
    return [s["run"] for s in _steps(job) if "run" in s]


def test_dispatch_only_no_release_trigger():
    """Publishing the draft must trigger NOTHING, so the verified draft assets ship untouched."""
    assert set(ON) == {"workflow_dispatch"}, f"unexpected triggers: {set(ON)}"
    assert ON["workflow_dispatch"]["inputs"]["tag"]["required"] is True


def test_same_tag_runs_are_serialized():
    conc = DOC["concurrency"]
    assert "${{ inputs.tag }}" in conc["group"]
    assert conc["cancel-in-progress"] is False, "a running build must finish, not be killed mid-upload"


def test_expected_job_set():
    assert set(JOBS) == {"preflight", *BUILD_JOBS, "checksums", "virustotal"}


def test_every_job_needs_preflight():
    for job in JOBS:
        if job == "preflight":
            continue
        needs = JOBS[job].get("needs")
        needs = [needs] if isinstance(needs, str) else needs
        assert needs and "preflight" in needs, f"{job} does not need preflight"


def test_no_bare_always_conditions():
    """always() would run a job even after a failed preflight (or a cancellation) — banned."""
    assert "always()" not in CODE


def test_aggregation_jobs_gate_on_preflight_success():
    for job in ("checksums", "virustotal"):
        cond = JOBS[job]["if"]
        assert "!cancelled()" in cond, f"{job} may run on cancellation"
        assert "needs.preflight.result == 'success'" in cond, f"{job} not gated on preflight"
    assert "needs.checksums.result == 'success'" in JOBS["virustotal"]["if"]


def test_every_checkout_pins_the_exact_tag():
    """Every job that reads repo content — including the scanner — must build/run the tagged source."""
    for job in CHECKOUT_JOBS:
        checkouts = [s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout")]
        assert checkouts, f"{job} has no checkout"
        for s in checkouts:
            assert s["with"]["ref"] == "${{ inputs.tag }}", f"{job} checkout not pinned to inputs.tag"


def test_uploads_use_gh_cli_only_never_release_action():
    """softprops/action-gh-release PUBLISHES an existing draft unless `draft: true` is repeated on
    every call (per its docs) — one omission would publish a half-built release. `gh release upload`
    touches assets only and cannot change draft status."""
    assert "softprops/action-gh-release" not in CODE
    uploads = [r for job in JOBS for r in _run_blocks(job) if "gh release upload" in r]
    assert len(uploads) >= 6  # 4 platform binaries + installer + SHA256SUMS.txt
    for r in uploads:
        assert '"$TAG"' in r


def test_no_template_expansion_inside_run_blocks():
    """Run scripts must reference $TAG (env), never ${{ … }} — templating user input into shell
    text is an injection vector. (The docker job's workspace mount is the one vetted exception.)"""
    for job in JOBS:
        for r in _run_blocks(job):
            for line in r.splitlines():
                if "${{" in line:
                    assert "${{ github.workspace }}" in line, f"template expansion in {job} run: {line!r}"


def test_tag_env_is_inputs_only():
    """No release.tag_name fallback left — the workflow has no release event anymore."""
    assert "github.event.release" not in RAW
    for job in JOBS:
        env = JOBS[job].get("env", {})
        if "TAG" in env:
            assert env["TAG"] == "${{ inputs.tag }}"


def test_preflight_and_reverification_calls():
    """preflight validates draft+tag; checksums re-verifies draft + the five binaries; virustotal
    re-verifies the full six-asset shape. All via the offline-tested script."""
    assert any("release_preflight.py" in r for r in _run_blocks("preflight"))
    assert any("release_preflight.py" in r and "--require-assets" in r
               for r in _run_blocks("checksums"))
    assert any("release_preflight.py" in r and "--require-assets" in r and "--require-checksums" in r
               for r in _run_blocks("virustotal"))
    # Both consumers download by the exact expected names, not a glob.
    for job in ("checksums", "virustotal"):
        assert any("--list-expected" in r and "gh release download" in r for r in _run_blocks(job))


def test_paginated_release_list_is_slurped():
    """gh api --paginate without --slurp concatenates JSON arrays (invalid JSON for the script)."""
    for job in ("preflight", "checksums", "virustotal"):
        for r in _run_blocks(job):
            if "repos/$GITHUB_REPOSITORY/releases" in r:
                assert "--slurp" in r, f"{job} lists releases without --slurp"


def test_virustotal_requires_secret_and_never_prints_it():
    runs = "\n".join(_run_blocks("virustotal"))
    assert 'if [ -z "$VT_API_KEY" ]' in runs and "exit 1" in runs, \
        "missing VT_API_KEY must FAIL validation, not skip"
    # The key VALUE may be expanded only in that emptiness test — never echoed elsewhere.
    assert runs.replace('-z "$VT_API_KEY"', "").count("$VT_API_KEY") == 0
    assert JOBS["virustotal"]["env"]["VT_API_KEY"] == "${{ secrets.VT_API_KEY }}"


def test_no_other_secrets_referenced():
    assert RAW.count("secrets.") == 1  # only VT_API_KEY; github.token is not a secrets.* ref
