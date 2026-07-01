# Overhaul Marathon — running log

Durable notes for the multi-repo overhaul the owner (LxveAce) kicked off 2026-06-30, run autonomously
via Claude Code. **This file is the command-center log** — every phase, every pushed commit, every
decision left for the owner is recorded here and kept current. All code commits are authored **LxveAce
only** (no co-author). Newest entries appended at the bottom of the Running Log.

## Mission (as directed by the owner)

1. Pick up cyber-controller, get current, fix + harden, overhaul the front end, verify.
2. **Overhaul every LxveAce repo** — one at a time, each via **plan → dev → test → commit → push**.
3. Iterate the whole set again ("re go through it all").
4. Then a **major cyber-controller revamp** + a **future roadmap**, and **add the planned support**
   (more firmware/backends) seen on the CC website.
5. Keep detailed notes here throughout. Don't stop; heartbeat stays enabled.

## Standing guardrails (why some things are *flagged*, not done)

Flagging an owner decision is correctness, not idling — everything around it keeps moving. I will **not**
autonomously: change anti-forensic wipe/eFuse/brick behavior (deadmans-switch/Suicide-Marauder), make a
public-disclosure/PII call (worldviewosnit), decide licensing/legal wording, scrub an employer/client
codename, change a public marketing **firmware/parser/backend count**, cut a release/tag, edit the
original Projects READMEs (add files instead), do subjective visual reinvention I can't view, or claim
hardware/installer validation I can't perform. Those are surfaced in **Owner Decisions** below.

---

## Phase 1 — cyber-controller: reliability + identity (DONE, pushed to `master`)

**Reliability bug-hunt (B12+): 20 confirmed defects fixed, each with a regression test.** Ran an
8-finder adversarial hunt → 21 confirmed → fixed all 20 that were safe to do without hardware/design.
Full Py3.12 suite green throughout; GUI smoke builds all 12 tabs.

| Commit | Fix |
|---|---|
| `380f6b4` | version SSOT: `src/__init__` re-exports from `src/version` (was orphaned at 1.2.1 vs 1.4.0) |
| `7d7a8d2` | access-gate: wait for USB key under default policy (was instant lockout + spurious duress vault-wipe) |
| `b942df0` | network-tab: gate dangerous device-node sends + drop unfilled `<...>` templates |
| `a5d81a5` | access-gate: reject an exclusive policy whose factor isn't configured (was self-lockout) |
| `a201e81` | bluejammer: control-map loader defaults `validated=False` (was sending unvalidated frames) |
| `87fbf90` | audit-trail: survive a torn JSONL line instead of silently disabling all persistence |
| `f1988d3` | deadman: fail loud on unknown flash_size/variant, not silent 4 MB mis-provision |
| `c6b8818` | flash: Flipper flash downloads the real package, never fakes success |
| `a165556` | flash: backup detects real flash size (was truncating >4 MB boards to a partial image) |
| `a57b8d8` | settings: Save no longer wipes interface mode + loadout |
| `b4cc968` | cross-comm: refresh a MAC-keyed target's scan index (was deauthing the wrong AP) |
| `2d16589` | ingest: keep Flipper SubGHz protocol + key (cross-firmware field names) |
| `2848d32` | batch: enforce the SHA-256 pin for pinned firmware |
| `cbca5f8` | safety: danger-annotate RF-transmit commands so the lab-only gate fires |
| `cc94ccf` | device-manager: reflect connection ERROR into `Device.connected` (UI stopped lying) |
| `ac7012c` | cross-comm: make the AutoRouter cooldown atomic + bounded (was double-firing) |
| `cd19f8f` | device-tab: DMS reply to the SOURCE device + per-device terminator + no callback stacking |
| `715cb63` | ingest: make `TargetIngestor.attach` idempotent per port |
| `b877e16` | targets-tab: resolve right-click actions against the pooled target (index actions were dropped) |
| `aa98c1a` | pterm: remove the persistent-terminal on_line callback on disconnect |

**Front-end identity overhaul (LxveAce violet):**

| Commit | Change |
|---|---|
| `4a2953e` | violet identity theme — retire the generic acid-green; centralized in `colors.py` + `cyber_dark.qss` + logo/icon |
| `3baf7cd` | finish retiring acid-green across all 4 UIs (Qt/Tk/TUI/Web), routed by role to tokens; + `test_no_acid_green` guard |
| `deeb6f0` | docs: drop false app-self-update claim; correct stale FORWARD-PLAN status |

Palette: brand/interactive = **violet `#a371f7`**; connected/online/go = green `#3fb950`; live serial
output = green `#7ee787`. Fully revertible if the owner wants a different direction.

---

## Phase 2 — multi-repo overhaul (in progress)

- **Analysis:** deep-analysis + prioritized plan produced for all 21 repos → 127 autonomy-safe items.
  Backlog in the session scratchpad (`overhaul_backlog.json` / `overhaul_index.md`).
- **Execution (running):** parallel pass over 10 code/docs repos — headless-marauder-gui,
  universal-flasher, universal-flasher-ui, Barcode-Tag-Creation, Automated-Tag-Production,
  claude-compact-controller, Projects, LxveAce (profile), cyber-controller-guides,
  vibe-coding-website-security — each: fetch→fix→test→commit(LxveAce)→push verified / hold ambiguous.
- **Held for the owner (not executed autonomously):** deadmans-switch & Suicide-Marauder (anti-forensic
  firmware), worldviewosnit (disclosure), tag-studio (licensing/confidential), BlueJammer-V2
  (supply-chain), the 3 marketing sites (blocked on the firmware-count decision + lxveace.com is
  owner-edited), Catalyst UI / -testing (Electron release decisions).

**First execution pass complete — 53 commits pushed across 10 repos, 1 held for review:**

| Repo | Pushed | Highlights |
|---|---|---|
| headless-marauder-gui | 3 (+1 held) | pytest suite + CI, CHANGELOG firmware-list fix; **held:** PyQt5 import guard (behavior-changing, manual-verified) |
| universal-flasher | 7 | CI, security-guard unit tests, lazy tkinter import, CLI-command doc fixes, hidden-imports, requirements↔pyproject |
| universal-flasher-ui | 6 | pytest suite, dead-code removal, wired Reload-Profiles, **batch-queue auto-drain fix**, reader-thread prune, README |
| Barcode-Tag-Creation | 2 + gh | published release SHA-256, Download section, factual repo description |
| Automated-Tag-Production | 8 | .gitignore, broken-build fix, **parse_hex_color hardening**, empty-CSV guard, version const, pytest suite, cleanup |
| claude-compact-controller | 5 | **token-tracking fix (read usage from transcript JSONL — the headline no-op)**, session-reset, node test suite, README, CI |
| Projects | 3 | pwnagotchi handle 404 fix, compile-check CI, Gold-board chip refs → classic ESP32 (add-files-only respected) |
| LxveAce (profile) | 2 | Roland badge BY-20A→BN-20A, snake.yml actions pinned to SHAs |
| cyber-controller-guides | 7 | missing jammer-detection PDF, 213-invariant test suite, CI, deps pin, canonical PDF engine |
| vibe-coding-website-security | 10 | link-check CI green, dead-link fixes, private-repo-name removal, README/llms.txt/FORWARD-PLAN accuracy |

All authored LxveAce, no co-author. Held/ambiguous behavior-changing code was committed locally but not
pushed. Sensitive + count-blocked + owner-edited repos were untouched (see Owner Decisions).

---

## Owner Decisions (consolidated — surfaced, not acted on)

- **Firmware-profile COUNT** — 26 profile JSONs on disk (23 firmwares + `custom` passthrough +
  `kali_arm` OS + `raspyjack`) vs "21" on the README badge and the websites. Pick the canonical
  definition; it propagates to the README + all 3 sites. *Blocks the website stat reconciliation.*
- **cyber-controller v1.5.0 release cut** — 57 commits are unreleased past the v1.4.0 tag.
- **Windows code-signing** (OV/EV cert) to retire SmartScreen; live-hardware + installer verification.
- **Dead mission-planner scaffolding** (`src/models/mission.py`) — build the planner or delete it.
- **Adding more firmware/backends** (dfu-util/UF2 → HackRF/Proxmark3/RP2040/Chameleon Ultra): code can
  be scaffolded + unit-tested, but the reliability-first ethos means each needs on-hardware validation
  before it's marketed as supported.
- Per-repo owner decisions (licensing, disclosure, PII/codename scrubs, release cuts, counts) are listed
  in the backlog's `ownerDecisions` for each repo.

---

## Running Log

- **2026-06-30 ~17:30–19:30** — Phase 1 complete + pushed (see table above). 23 commits.
- **2026-06-30 ~19:34** — Phase 2 analysis complete (21 repos, 127 safe items); parallel execution of
  the 10 code/docs repos launched.
- **2026-06-30 ~19:5x** — This log created + pushed to make the notes durable in the repo.
- **2026-06-30 ~20:1x** — Phase 2 first execution pass complete: **53 commits pushed across 10
  code/docs repos**, 1 held (see table). Next: continue deep per-project overhauls, then the
  cyber-controller major revamp + future roadmap + added support (Phase 3), then re-iterate.
- **2026-06-30 ~20:2x** — Phase 3 started on branch **`feat/major-revamp`** (pushed, for owner review —
  not master): `docs/ROADMAP-FUTURE.md` landed (near/mid/long-term + the "more support" plan). Next on
  the branch: bundle-manifest reliability test, remaining inline-style→token consolidation, and
  scaffolding the planned backends (dfu-util/UF2) + new profiles — each flagged HW-validation-pending.
