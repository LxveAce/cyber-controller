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
- **2026-06-30 ~20:2x** — Phase 3 dev: **dfu-util + UF2 flash backends landed on `feat/major-revamp`**
  (`9856d8d`) — registered behind `FlashEngine._backends`, download-or-local resolve, never fake
  success, `INFO_UF2.TXT` volume auto-detect; +11 unit tests, full suite **672 passed / 2 skipped**.
  Flagged **HW-validation pending** (RP2040 / Pi Pico / HackRF / Proxmark3 / Chameleon Ultra each need a
  board before being marketed as supported). Branch pushed for owner review — not merged to master.
- **2026-06-30 ~20:3x** — Phase 3 dev: **bundle-manifest regression test** landed on
  `feat/major-revamp` (`eb663d2`) — guards that every literal `resource_path()` target and every
  `build.py --add-data` source exists (5 targets / 7 sources), and **removed the stale
  `src/config/missions` bundle line**. Full suite **674 passed / 2 skipped**. **Phase 3 branch now
  complete for owner review** (roadmap + dfu-util/UF2 backends + bundle-manifest guard); remaining
  Phase-3 items are all owner decisions (mission-planner build-vs-delete, HackRF/Proxmark3 HW backends,
  v1.5.0 cut, code-signing, counts). Moving to **Phase 4** — second-pass overhaul sweep of the safe repos.

---

## Phase 4 — second-pass overhaul (DONE) — 2026-06-30 ~20:4x

Re-swept the 10 safe repos: first-pass backlog was already complete everywhere, so the value was a
**light bug-hunt → 12 more commits pushed, 1 held.** Real new bugs found + fixed with tests:

| Repo | Pushed | Notable |
|---|---|---|
| universal-flasher | 1 | **`send_and_capture` silent data-loss** when the 500-line serial ring buffer fills (returned [] — broke get_status/get_nodes on chatty devices); fixed via subscriber-capture + test (`a7700fb`) |
| universal-flasher-ui | 2 | **settings.json corruption → startup crash** hardened; **CrossCommBroker `.format()` crash** on a malformed user rule guarded (`e571030`,`45e0c4d`) |
| Automated-Tag-Production | 2 | **CSV dtype fix** (leading-zero SKUs `007`→`7` were silently reformatted) + **single-column CSV shredding** fix (`37b23e4`,`36eb662`) |
| claude-compact-controller | 2 | install/uninstall dedupe tests + FORWARD-PLAN refresh (`30b6982`,`d0aa0d7`) |
| cyber-controller-guides | 1 | drop dead `boards` field from projects.json (`ca368ac`) |
| Barcode-Tag-Creation | 1 | reconcile lone stale "Code 39" doc → 128B (majority + version.json); *owner: confirm vs the actual exe* (`73587dc`) |
| Projects | 2 | UNIVERSAL-FLASHER.md repoint + FORWARD-PLAN staleness (`1c00fd9`,`1d2f31b`) |
| LxveAce, vibe-coding-website-security | 0 | already clean — nothing safe left |
| headless-marauder-gui | 1 + **held** | .pytest_cache gitignore (`6dadab3`); **HELD `0c7c871`** frozen-binary esptool fix (unit-tested, but needs a real PyInstaller build to validate the reexec path — owner) |

**State of the marathon:** the autonomy-safe backlog across the safe code/docs repos is now
**essentially exhausted** — 2 repos clean, the rest down to owner-decisions + hardware/validation-gated
work. cyber-controller `feat/major-revamp` (roadmap + dfu/uf2 + bundle-guard) awaits review/merge.

## Owner Decisions — the full list (nothing below was done autonomously)

**cyber-controller:** firmware-profile **COUNT** (26 on disk vs "21" marketed — blocks website
stats) · **v1.5.0 release cut** (60+ commits unreleased) · Windows **code-signing** · **mission-planner**
build-vs-delete (`src/models/mission.py`) · new **HW backends** (HackRF/Proxmark3/Chameleon Ultra —
dfu/uf2 scaffolded, need boards) · merge `feat/major-revamp`.
**Held code (unit-tested, needs your validation):** HMG `0c7c871` frozen-esptool reexec.
**Owner-held repos (untouched):** deadmans-switch & Suicide-Marauder (anti-forensic wipe/eFuse),
worldviewosnit (disclosure/PII), tag-studio (licensing + confidential), BlueJammer-V2 (supply-chain),
the 3 marketing sites (count-blocked + lxveace.com owner-edited), Catalyst UI/-testing (Electron release).
**Per-repo decisions** (licensing, PII/codename scrubs, release cuts) are in the session backlog's
`ownerDecisions`.

## Phase 5 — deep flash-engine bug-hunt (universal-flasher) — 2026-06-30 ~20:5x

Deep adversarial hunt on the shared flash engine (`flasher.py` 1.75k lines + backends). **1 real
latent-brick bug found + fixed + pushed; 4 more noted for the owner; SSRF/verify/suicide paths audited
clean.**

- ✅ **`4765476` — wrong ESP32-C5 bootloader offset (brick).** `flasher.py` grouped C5 with the
  `0x0`-bootloader chips; C5's 2nd-stage bootloader is at **`0x2000`** (verified vs esptool 5.3.0:
  c5/p4/h4=0x2000; matches the C5 gotcha in memory). Replaced the inline test with an esptool-faithful
  `_bootloader_offset(chip)` SSOT; parity-checked (only the C5 cell changes). +test. Live trigger is
  narrow today (no shipping path targets C5 full-flash yet) but it's the canonical table the engine +
  the future `uf_core` share — correct-and-pushed. Suite 92 passed.
- 🔑 **Owner-noted (real, need a board/upstream to fix):** DIV `support_files` fetches the S3 bootloader
  for a classic-ESP32 DIV-v1 (wrong-arch, ROM-recoverable); Meshtastic profile lists nRF52/UF2 boards
  but flashes via esptool (fails clean); `batch._flash_one` drops the asset `offset` (app-only 0x10000
  update would write at 0x0); flock-you/oui-spy/sky-spy/cyt-ng tag every release as merged@0x0 without
  confirming. All non-brick or narrow; flagged, not changed.

- **2026-06-30 ~20:5x** — Cross-repo follow-through on the C5 offset bug: **headless-marauder-gui had
  the SAME latent brick** (`marauder_core/flasher.py`, C5 in `_BOOTLOADER_0` → bootloader@0x0 at two
  sites, and the `esp32c5devkitc1` variant is reachable). Fixed with the same esptool-faithful
  `_bootloader_offset()` SSOT + test, pushed (`e5484df`). **cyber-controller was already correct**
  (`_BOOTLOADER_OFFSET={"esp32c5":"0x2000"}` checked first). C5-brick class now closed across the whole
  flasher lineage.

## Marathon status (2026-06-30 ~21:00) — safe backlog worked out; loop → measured cadence

Delivered tonight, all pushed as LxveAce (~95 commits across 12+ repos + the `feat/major-revamp`
branch): Phase 1 cc reliability+identity · Phase 2 (53 commits/10 repos) · Phase 3 cc revamp branch
(roadmap + dfu/uf2 backends + bundle-guard) · Phase 4 second pass (12 commits, real bugs) · Phase 5
deep flash-engine hunt (C5 brick fixed in universal-flasher + HMG). The autonomy-safe backlog is now
**essentially exhausted** — remaining high-value work is owner-gated (see Owner Decisions). The loop
stays alive at a longer cadence and will fold in any real bug a future hunt surfaces, but will not
manufacture marginal changes. **Owner: the decision list above is the highest-leverage next step.**
- **2026-06-30 ~21:2x** — C5-fix follow-up VERIFIED clean: no repo uses `_BOOTLOADER_0` directly for an offset decision (cc `_bootloader_offset` checks the 0x2000 override first; uf/HMG likewise). The `_BOOTLOADER_0` import in cc + uf `backup.py` is unused — backups do a whole-flash `read_flash 0x0 <size>`, so no bootloader offset is applied there; no residual brick trap. Left the two dead imports as-is (no-churn). cc's newer modules (os_catalog/tails/macro/wardrive/health) were already in the B12+ hunt's scope, so no round-2 hunt — no diminishing-returns churn. Loop → 60-min re-check.
- **2026-06-30 ~21:3x** — State-confirmation caught + fixed two self-inflicted gaps: (1) the Phase-4 HELD frozen-esptool fix (HMG `0c7c871`) had been ORPHANED by a later `reset --hard origin/main` — RECOVERED via cherry-pick onto branch **`fix/frozen-esptool`** (`d666219`, 77 tests green, pushed for owner review; it's a real bug — frozen `.exe` esptool invocation re-launches the GUI, so shipped binaries can't flash). (2) The HMG C5 fix used `commit -am`, which skipped the new untracked `tests/test_flash_offset.py` — so `e5484df` shipped the fix WITHOUT its regression test; test now committed + pushed (`b2a053b`). All working trees clean + in sync.

## Phase 6 — owner greenlit EVERYTHING (2026-07-01) — cc release path DONE

Owner said: do it all, push it all, keep going without me. Executed on cyber-controller:
- **Merged `feat/major-revamp` → master** (roadmap + dfu-util/UF2 backends + bundle-guard; clean merge, suite green).
- **Settled the firmware count → 26** (badge + body match the 26 shipped JSONs + the table 1:1; the stale '21' retired) + a drift-lock test (`test_profile_count`). Backends stay marketed at **5** (dfu/uf2 are experimental, HW-validation pending — roadmap, not headline).
- **Cut v1.5.0** (`b688a52`, tag `v1.5.0` pushed → CI builds binaries + installer): version.py + pyproject → 1.5.0, CHANGELOG promoted [Unreleased]→[1.5.0] with the 20 fixes + violet identity + backends, README status updated.
- NEXT: reconcile the 3 marketing sites to **v1.5.0 / 26 firmwares / 5 backends / 9 parsers**, then the owner-held repos' SAFE parts (deadmans/Suicide host-tests+docs, BlueJammer SHA-manifest, tag-studio SECURITY scoping, catalyst safe items). Still NOT touching: anti-forensic wipe/eFuse behavior, worldviewosnit third-party PII/disclosure (whole repo held — too sensitive), licensing/pricing models, employer/client codename scrubs that require exe rebuilds.

### Phase 6 execution — 35 commits pushed across 8 repos (2026-07-01)

| Repo | Pushed | What |
|---|---|---|
| cybercontroller.org | 10 | reconciled to **v1.5.0 / 26 / 5 / 9**; CSP inline-styles extracted (strict CSP kept); +BW16/BlueJammer parser cards (grid=9); a11y parity; security.txt; keyword trim; mojibake; **framed the Firmware Library as a featured selection** (26 ship, jammer/passthrough not front-paged, full set → guides) |
| esp32marauder.com | 3 | reconciled CC counts to v1.5.0/26/5/9; mojibake; sitemap |
| LxveAce.github.io | 6 | null-guard navToggle, mobile nav on 6 subpages, de-orphan /marauder/, stats→v1.5.0/26, removed orphan SVGs, sitemap |
| deadmans-switch | 3 | **host-side + docs only** — pytest for provision.py, serial-contract reconcile, Suicide→Dead-Man strings (firmware/wipe/eFuse untouched) |
| Suicide-Marauder | 5 | **host-side + docs only** — 38-test pytest for provision.py, ci/ reconcile, docstring fix, SPEC sync, CHANGELOG (wipe behavior untouched) |
| BlueJammer-V2 | 4 | **checksums/docs only** — SHA256SUMS provenance manifest, .gitattributes, README folder-name fix, fork-provenance note (binaries untouched, no licensing) |
| tag-studio | 4 | **docs only** — SECURITY.md version-table + Windows-only scoping, README schema, LABEL-TAG doc clarified (licensing/pricing untouched) |

All guardrail-clean: no anti-forensic wipe/eFuse behavior, no licensing/pricing, no third-party PII, no binary modification. REMAINING: **catalyst-ui / catalyst-ui-testing** safe docs items in progress; **worldviewosnit** stays fully owner-held (third-party PII + disclosure — untouched).
