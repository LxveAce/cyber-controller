# LxveAce/cyber-controller - Forward Plan

> Status: v1.1.0 code-stable (228 tests pass, 0 open issues) but the shipped Windows .exe almost certainly crashes silently on launch. | Health: YELLOW | Date: _____ (pick up here)

## Where this stands
Cyber Controller is the flagship Python 3.12+ all-in-one security-hardware controller for cyberdecks: it flashes/coordinates ESP32 firmwares (Marauder/Bruce/GhostESP + ~21 profiles), Flipper Zero, and Raspberry Pi gear from one dashboard. Four UIs (PyQt5 GUI, Tkinter, Textual TUI, Flask web remote) via `--ui` or a launcher dialog, plus a Dead Man's Switch anti-forensic provisioning flow (`--deadman-setup`) backed by the `deadmans-switch` git submodule.

- **Entry point:** `src/app.py` (console script `cyber-controller`).
- **Build (local):** `python build.py` -> `dist/CyberController*` (bundles QSS, assets, all UI submodules).
- **Build (release):** `.github/workflows/build-release.yml` on `release: published` -> per-OS `cyber-controller-<tag>-<plat>` assets. **WARNING: CI build args DIVERGE from build.py and are the likely cause of the broken release.**
- **Test:** `python -m pytest -q` -> 228 passed, 2 skipped (ran on 3.13; CI builds on 3.12).
- **Current state:** v1.1.0 latest; master is 8 commits AHEAD of the v1.1.0 tag. Core code clean and security-conscious; the live problems are CI packaging gaps + one unguarded file read.

## P0 - do first

### 1. FIX THE BROKEN WINDOWS .EXE (user directive #1)
**The download link is NOT broken.** WEB recon verified: `curl -sIL` returns HTTP 200, redirects to GitHub's release-assets CDN, and the first bytes are `MZ` (valid PE). cybercontroller.org loads downloads dynamically from the live Releases API (`script.js`), so site buttons point at the same working URLs. No stale/dead link.

**The failure is at RUNTIME.** Most likely root cause (verified by inspection):
- CI `build-release.yml` (lines 26-30) bundles **only** `--add-data "src/config/profiles;..."` and **never bundles the QSS theme**.
- `src/ui/qt/theme/__init__.py:10-11` does `qss_path.read_text()` with **no try/except and no `sys._MEIPASS` handling**.
- Under PyInstaller `--onefile`, the QSS is not in the bundle -> `FileNotFoundError` on launch.
- CI builds with `--windowed`, which **suppresses the console**, so the crash is **silent** -> the exe "does nothing" = a textbook "broken install".
- `build.py` (lines 72-75) DOES bundle the QSS, which is exactly why it works locally but not in the release ("works on my machine").

**Fix:**
1. Harden `apply_theme()` to resolve the QSS via `sys._MEIPASS` with a `try/except` fallback (mirror the existing pattern in `src/core/backends/rtl8720_backend.py:186`) so a missing stylesheet degrades to an unstyled-but-running GUI instead of crashing.
2. Add `--add-data` for the QSS + assets to **all** CI build jobs (align CI with `build.py`).
3. Cut a fresh release and **re-verify on a real Windows host**.

### 2. VERIFY ON REAL WINDOWS (the one thing all 3 recons could not do)
Download `cyber-controller-v1.1.0-windows-x64.exe`, launch it, and capture the actual error (silent crash vs SmartScreen prompt vs missing profiles). Confirm root cause = unbundled QSS **before** shipping. This is the verification gate.

### 3. Fix the second silent-crash vector: profiles path mismatch
Runtime resolves profiles via `Path(__file__).resolve().parents[1]/'config'/'profiles'` (`firmware_vault.py:24`), but CI maps `--add-data` dest to `src/config/profiles` while `build.py` maps to `config/profiles`. The three layouts disagree. Verify profiles actually load in the frozen exe and standardize the destination.

### 4. Re-cut the release
Master is 8 commits ahead of the v1.1.0 tag; the fix release should also pick those up.

## Surface bugs found

| Title | Location | Severity | Note |
|---|---|---|---|
| Shipped Qt GUI crashes on startup: CI omits QSS, apply_theme() reads it unguarded (PRIMARY P0) | `build-release.yml` 26-30/57-62/88-93/116-120; `src/ui/qt/theme/__init__.py` 10-11 | P1 | `--windowed` makes the crash silent. `rtl8720_backend.py:186` already has the `sys._MEIPASS` pattern to copy. |
| CI build diverges from build.py: no assets/icon, no tk/tui/web hidden-imports, no `--collect-submodules`; name differs (`cyber-controller` vs `CyberController`) | `build-release.yml` vs `build.py` 22,54-106 | P2 | Non-default `--ui` modes may fail in the shipped exe. build.py references `assets/icon.ico` which does not exist (only `cc-logo.png`). |
| Profiles `--add-data` destination mismatch (CI vs build.py vs runtime resolver) | `build-release.yml` 27 vs `build.py` 61 vs `firmware_vault.py:24` | P2 | Second silent-crash / empty-profile-list vector in the frozen exe. |
| `--deadman-setup` hard-fails in releases: CI checkout doesn't fetch the submodule | `src/core/suicide_setup.py` 70-80; `build-release.yml` checkout | P2 | `_load_provision()` raises when `deadmans-switch/provision.py` is absent. Add `submodules: recursive`. |
| Windows asset is a single UNSIGNED `--onefile` exe (no installer, no signing) | GitHub release assets | P2 | SmartScreen/Defender friction even after the QSS fix. Consider signing + Inno/NSIS or `--onedir`. |
| Cross-surface count drift: profiles 21/21/19/19+, parsers 9/8, audit findings 10/15 | `README.md`, `CHANGELOG.md`, `src/config/profiles/`, `src/protocols/`, `cybercontroller.org/index.html` | P2 | Disk truth: 21 profile JSONs, 9 parser modules. User must pick canonical numbers. |
| Anti-forensic branding split: code uses `deadmans-switch`, README/UI say "Suicide Marauder" | `.gitmodules`; `suicide_setup.py`; `README.md` 32,253-278 | P2 | Bundles the successor, branded after the predecessor. Unify before adding the new amnesiac features. |
| Inconsistent Windows asset filename casing across releases | v1.0.0 `Cyber-Controller-...` vs v1.1.0 `cyber-controller-...` | P3 | Site is unaffected (case-insensitive match); hardcoded external links would break. |
| Version drift: repo/release v1.1.0 vs site v1.0.1/v1.3.0/v1.5.5/v1.6.0 | `cybercontroller.org/index.html` | P3 | Several are demo DEVICE firmware versions, not the app version -- disambiguate, don't assume. |

## Features to add

### Directive #2 (verbatim): "add ability to flash Tails OS (the amnesiac OS) - same dead-man/amnesiac theme, different application."
Tails is a bootable LIVE ISO written to USB (raw/`dd` image), **not** an ESP32 firmware flash. This needs the SD/raw-image backend lineage. CTX recon notes universal-flasher-ui already shipped an sd-image backend — **reuse that pattern**.
- Add a `tails` profile + an iso/raw-image flash backend.
- **Verify the official Tails signature/checksum before writing** (Tails ships a detached OpenPGP sig + known SHA) — follow the existing BlueJammer-V2 "SHA-256-pinned, fetched at flash time, never vendored" doctrine.
- Surface under the amnesiac/dead-man section in the UI.

### Directive #3 (verbatim): "create physical key - flash a USB stick with a key; the app can only be opened when an admin password is entered AND/OR the physical USB key is plugged in and present."
Two parts:
1. **Create Physical Key** provisioning flow: generate a key blob, write it to a USB stick, optionally bind it to the USB volume/hardware id to resist trivial copy.
2. **Startup gate:** require admin password AND/OR presence of the physical USB key before the app unlocks. Policy configurable (password-only / key-only / both-required).
- Reuse `src/core/deadman_auth.py`, AES-256-GCM session storage, scrypt/PBKDF2 host hashing, and the single-instance lock in `src/app.py`.
- Distinct from the v1.1.0 web-remote auth. Ensure the gate also covers the web UI entry, not just desktop.

### Release hygiene
Fresh tagged release after P0, picking up the 8 commits master is ahead of v1.1.0.

## Red-team / hardening
- Make `apply_theme()` (and all bundled-asset reads) frozen-safe via a shared `resource_path()` helper using `sys._MEIPASS` + graceful fallback. A missing asset must never be fatal.
- Physical-key blob is a secret: store via AES-256-GCM fails-closed storage + NTFS ACLs on `~/.cyber-controller`; never log it; zeroize buffers (the provisioner already does this for passwords). Consider binding to USB hardware id. Document the threat model honestly (defeats casual access, not a forensic adversary) — do not overstate on the public site.
- Tails: enforce signature/checksum verification before any write (supply-chain integrity).
- Add `submodules: recursive` to CI checkout so dead-man provisioning ships functional; keep host-side-only behavior (never burns eFuses).
- Preserve the web-remote posture (127.0.0.1 default bind, `CC_WEB_ALLOW_LAN`/`CC_WEB_ALLOW_DEV_SERVER` gating in `src/ui/web/app.py:485-521`) when wiring the startup gate.
- **PUBLIC REPO:** frame everything as responsible hardening; no step-by-step bypass recipes in commits/README/site.

## Dig deeper (next dedicated session)
1. **Frozen-binary smoke-test matrix:** build with CI args (not build.py) on each OS, launch every `--ui` mode from the onefile exe, confirm none crash on missing bundled resources.
2. **Audit all `Path(__file__)`-relative resource reads** for frozen safety (QSS, `firmware_vault.py:24` profiles, `suicide_setup.py` partitions/host, asset/logo loads). Route all through one `resource_path()` helper.
3. **Reconcile count/version/branding drift** in one pass: pick canonical numbers (disk = 21/9; audit = 10 per CHANGELOG), sweep README + CHANGELOG + site, finish the Suicide-Marauder -> deadmans-switch rename in README + Qt menu strings.
4. **Windows code-signing + installer** (Inno/NSIS or `--onedir`) to kill SmartScreen/Defender false positives.
5. **Exercise the deadmans-switch submodule end-to-end** (`git submodule update --init`); verify the bundled provisioning flow completes. Submodule content was never examined (empty locally).
6. **Confirm the ARM release job actually emits an asset** (it is `continue-on-error`) against the Pi cyberdeck target.
7. **Reproduce CI on Python 3.12 specifically** (local tests ran on 3.13) to rule out 3.12-only packaging behavior.

## Dependencies & cross-repo context
- `esptool>=4.7,<6` (pinned <6, rationale in `pyproject.toml:33-34`); `cryptography>=43` (mandatory; underpins the planned physical-key feature); PyQt5 + PyInstaller (`--onefile`/`--windowed` = proximate crash cause); pyserial, requests, psutil.
- Submodule `deadmans-switch` -> https://github.com/LxveAce/deadmans-switch.git (successor to Suicide-Marauder; standalone clone at `C:\Users\mmrla\repos\deadmans-switch`).
- **Ecosystem lineage (CTX):** cyber-controller SUPERSEDES universal-flasher-ui / universal-flasher / headless-marauder-gui — reuse the sd-image/raw-image backend from universal-flasher-ui for Tails; invest only in cyber-controller.
- **Website:** `C:\Users\mmrla\repos\cybercontroller.org` (index.html + script.js) loads downloads dynamically from the live GitHub Releases API — no hardcoded links to fix, but count/version copy needs reconciling.
- **Continuity sources:** `session-context/SESSION.md`, `Projects/CLAUDE-TRANSFER.md`.

## Open questions
- **UNVERIFIED (all 3 recons):** nobody could run the .exe; exact Windows failure (silent QSS crash vs SmartScreen vs profiles-not-found) is strong inference, not confirmed. Reproduce on real Windows — #1 gate before declaring P0 fixed.
- Canonical numbers? Disk = 21 profiles + 9 parsers; CHANGELOG = 19 + 10 findings; site = 19+/8/15.
- Tails delivery model: write official ISO to USB (raw) vs build custom persistence/dead-man config on top? Scope to confirm.
- Physical key: bind to USB hardware id (resists copy) vs portable keyfile? Does the AND/OR policy cover the web remote or desktop only?
- Is the empty `deadmans-switch` dir a packaging gap or just uninitialized locally?
- Do the site's v1.5.5/v1.6.0 strings refer to the app or to demo device firmware (mostly the latter)?


## Owner feature directives — unified flashing + auto-update + offline + UX (2026-06-27)

Committed roadmap for the flasher line. **cyber-controller and universal-flasher implement the
FLASHING parts CONSISTENTLY (shared engine + catalog); their ROLES differ (see below).** These are
to be kept in sync across both repos.

### 1. Flash more, all in one — firmware vs software, in separate tabs
- Make flashing all-in-one, split into clearly separated tabs so the two audiences never collide:
  - **Firmware tab (hardware projects):** the existing ESP32 firmwares (Marauder, GhostESP, Bruce,
    HaleHound, Meshtastic, ...) + Pi/SBC SD-image firmwares. Add as many more as feasible.
  - **NEW Software tab (PC / USB operating systems):** bootable OS images written to a USB stick —
    **Kali Linux, Tails OS, Arch Linux**, and as many others as feasible (Ubuntu/Debian/Parrot, and a
    Ventoy-style multiboot stretch goal). Reuses the hardened **removable-only raw-image writer +
    mandatory integrity verification** (sha256 / signature) already used for Tails.

### 2. Auto-updating catalog + app, with FULL offline utility
- **Catalog auto-update:** keep the flashable firmware/OS definitions as current as possible
  automatically — resolve each upstream's latest version (GitHub Releases API for ESP32 firmwares;
  official version/checksum/signature feeds for Kali / Tails / Arch / etc.) and refresh the bundled
  profiles. Do it two ways: (a) a **scheduled CI job** (GitHub Action) that updates the profile JSON +
  pinned checksums in the repo so the shipped catalog never goes stale, and (b) an **in-app
  "check for catalog updates"** that pulls the latest profile manifest.
- **App auto-update:** keep the existing self-update path; every project in this line ships auto-update.
- **Offline utility (mandatory, non-negotiable):** everything must work with NO internet — a cached
  catalog + already-downloaded images flash fully offline. Auto-update is an enhancement, never a
  requirement to use the tool in the field.

### 3. UX — discoverability everywhere
- **Hover tooltips on EVERY control** explaining what it does (extend the existing tooltip/glossary
  pattern to 100% coverage).
- **A thorough "How To" / tutorial tab** that walks through every feature, tab, and button with
  step-by-step usage — first-run friendly, offline, and kept in sync as features land.

### Role: cyber-controller = the all-in-one (controller + flasher + logger + pentest + cyberdeck GUI)
The convergence flagship: device **controller**, the unified **flasher** above, session/event
**logger**, and a **pentesting** toolkit — intended to run as the **main GUI for cyberdecks** and
similar field setups. Beyond the shared flashing:
- **Wardriving subsystem:** add wardriving support modeled on the **Biscuit** wardriving project (and
  as much capability as feasible) — GPS-tagged AP/station capture, live logging, and export
  (WiGLE CSV / Kismet-style), surfaced as a first-class panel/tab. Wire into the existing
  Marauder/serial sniff capture + the cyberdeck GPS. (Lawful, owner-authorized use; responsible framing.)
- Everything auto-updates + works fully offline. The How-To tab covers the controller, flasher,
  logger, wardriving, and cyberdeck-GUI usage. Wardriving + logger + controller are CYBER-CONTROLLER
  ONLY (not universal-flasher).

