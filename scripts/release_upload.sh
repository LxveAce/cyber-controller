#!/usr/bin/env bash
# Draft-guarded release asset upload — the ONE wrapper every asset mutation in build-release.yml
# goes through.
#
# Immediately before uploading, it re-verifies (via release_preflight.py) that the tag still has
# EXACTLY ONE release and it is still a DRAFT, then uploads with --clobber. This fresh check is
# required because GitHub's "re-run failed jobs" / "re-run this job" REUSES an earlier successful
# `preflight` job WITHOUT re-executing it — so re-running only a build job AFTER the release was
# published would otherwise clobber public assets. The check narrows that window but is NOT atomic:
# the person publishing the release must first confirm no run for the tag is in progress or queued.
#
# Usage: release_upload.sh <tag> <file> [file ...]
set -euo pipefail

tag="$1"; shift

# `python` on the setup-python runners (Windows/Linux/macOS), `python3` on a bare Ubuntu host
# (the ARM job has no setup-python step).
py="$(command -v python || command -v python3)"

gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/releases" \
  | "$py" scripts/release_preflight.py "$tag"

gh release upload "$tag" "$@" --clobber
