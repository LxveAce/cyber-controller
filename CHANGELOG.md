# Changelog

All notable changes to Cyber Controller are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [1.4.0] — 2026-06-29

### Added
- **Smart installation / version-aware startup.** Cyber Controller now reconciles the persistent config
  in `~/.cyber-controller` against the running version on launch (`src/core/install.py`):
  - **Upgrade** (config from an older version) → carried forward silently (settings already deep-merge
    onto defaults; the AES-GCM vault carries its own format version) and the version marker is advanced.
  - **Downgrade** (config from a *newer* version — the "paths collide / overwrite old install" case) →
    the GUI prompts: **Keep & Continue**, or **Back up & Start Fresh** (the old config is *moved aside,
    never deleted*, so it's restorable).
  - **Fresh / same** → recorded, proceed. A `.installed_version` marker is written so future launches can
    classify correctly. Safe + silent for headless/CLI use.
- Single source of truth for the app version (`src/version.py`), used by the window title + the installer
  logic. +9 tests.

## [1.3.3] — 2026-06-29

### Added
- **Animated startup for the PyQt5 desktop.** A frameless animated loading screen (logo + indeterminate
  progress sweep + status text) now appears while the dashboard builds, then cross-fades to the main
  window. Motion follows the project's motion-design tokens (fade-in OutQuart ~320ms, linear looping
  progress, fade-out OutCubic ~260ms). Honors reduced-motion (`interface.reduced_motion` /
  `CC_REDUCED_MOTION`). Reserved for the full GUI only — the lightweight Tk/TUI/web UIs are unanimated.

## [1.3.2] — 2026-06-29

Launcher / interface-selection fixes.

### Fixed
- **The "Select Interface" launcher was hidden by the splash and missing a mode.** When you launch
  without `--ui` (i.e. double-click the exe), the app shows a chooser for which interface to run — but
  (a) the always-on-top startup splash (added in 1.3.1) was covering it, so it looked like the app never
  asked, and (b) it only listed **3** of the **4** advertised UIs. Now: the splash is closed *before* the
  launcher appears, and the launcher offers all four — **Full GUI (PyQt5) · Lightweight (Tkinter) ·
  Terminal UI (Textual) · Web Remote (Flask+SocketIO)** — matching the website. Dialog resized to fit.
- **Web Remote was unusable from a packaged build** — a `--windowed` exe has no console to print the
  server URL, so picking "Web Remote" appeared to do nothing. It now **opens your default browser** at
  the server URL automatically once the server is warming up.

## [1.3.1] — 2026-06-29

Installer-UX + reliability fixes.

### Fixed
- **"Installation error" on the Windows .exe was a slow, feedback-less startup.** The `--onefile
  --windowed` build self-extracts ~80 MB to a temp dir on launch (~15 s on a cold first run) with **no
  visual feedback**, so users saw nothing for 15+ seconds and assumed it failed. Added a **PyInstaller
  splash screen** (the cc logo) that appears within ~1–2 s of launch and is closed by `launch_qt()` once
  the main window is ready (`pyi_splash.close()`). Verified end-to-end: splash visible at t≈2 s →
  clean hand-off to the GUI. (Full diagnosis: command-center `cc-installer-investigation.md`.)
- **MinigotchiV3 profile crashed the Flash UI.** Its resolver pointed at `dj1ch/minigotchi-V3`, which
  upstream renamed to `dj1ch/minigotchi-ESP32` (the old path now 404s) and — unlike the other
  source-only profiles — had no `on_error` fallback, so resolving it raised an uncaught HTTP 404.
  Corrected the repo + added the graceful `source_only_empty` fallback. `halehound` given the same
  defensive fallback (builds from source; no `.bin` release assets).

### Notes
- Recommended follow-ups for the installer (documented, not all doable in CI here): a `--onedir` build +
  real installer (Inno Setup) for near-instant startup, bundle slimming, and code-signing (unsigned exes
  can trip SmartScreen/AV — the most likely cause of a genuine failure on an end user's machine).

## [1.3.0] — 2026-06-29

Security-hardening + UX release.

### Added
- **Secure container (opt-in).** App-internal saves (e.g. recorded command sessions) are encrypted at
  rest with AES-256-GCM in a gate-keyed container (`~/.cyber-controller/secure`) that is **sealed and
  unreadable while the access gate is locked** — the key lives only inside the unlocked vault.
  Ciphertext-only writes (no transient plaintext), GCM-authenticated (tamper fails closed), 0600 + ACL.
  Toggle in **Settings → "Secure Container"**; integrated with the duress wipe.
- **Brute-force lockout** on the access gate — persistent failed-attempt counter (survives restart) +
  exponential-backoff cooldown; constant-time password compare.
- **Duress self-wipe (opt-in, off by default).** After a configurable number of consecutive failed
  unlocks, securely wipe the app's own footprint (vault/keys/config/container). Honest scope documented:
  defeats casual/seizure access, not a forensic lab on wear-leveled SSDs.
- **Dual-depth Simple/Pro interface.** A streamlined Simple view (fewer controls per tab) and the full
  Pro view (default, zero penalty). Toggle via **View → Interface Mode**, the status-bar badge, or
  **Ctrl+M**; persists. Streamlines Flash/Settings/Health/Software/Macro/Cross-Comm.
- **4 new firmware profiles** (drop-in JSON): **T-REX** (LilyGo T-Deck), **MCLite** (MeshCore),
  **ESP32 Bit Pirate**, **Hydra32 / ESP32-Deauther** (multi-file, SHA-256-pinned; offsets verified from
  the upstream partitions.csv). Registry → 22 profiles (21 flashable firmware + custom loader).
- **esptool range guard** — a clear "install esptool>=4.7,<6" message if an out-of-range esptool (v6+)
  is installed, instead of a cryptic argparse failure mid-flash.

### Changed / Hardened
- **Boot/startup-bypass resistance.** Modifying an already-configured access gate (clear / change
  password / change policy / add key) now requires **passing the gate first** — no pre-auth reset.
- Locked-state data-leak audit + offline-posture audit completed (see `SECURITY.md`); no telemetry,
  all network is user-initiated firmware/OS download.

### Internal
- New test suites: secure container + macro-container integration, access-gate mutation auth, esptool
  version guard, dual-depth UI, Hydra32 profile. Full suite **452 passed / 2 skipped**; flash-argv
  golden regenerated (purely additive).

## [1.2.1] — 2026-06-27

### Added
- **In-app access-gate setup.** The admin password / physical USB key / unlock policy can now be
  configured from the GUI — **Settings → "Access Gate (Security)" → "Set up access gate…"** — not just
  the CLI. Backed by the same salted-scrypt verifiers + gate-keyed encrypted vault; changes apply on
  the next launch. Verified end-to-end (set/clear password, policy change, vault provisioned and
  ciphertext-at-rest, wrong password rejected).

## [1.2.0] — 2026-06-27

Major feature + reliability release.

### Fixed
- **Windows one-click `.exe` launched to a silent crash.** CI never bundled the Qt QSS theme and
  `apply_theme()` read it with no fallback under `--windowed`, so the frozen GUI died at startup. Builds
  now go through `build.py` (single source of truth) which bundles the theme + every resource, and theme
  loading degrades gracefully. Verified: the built `.exe` launches the full GUI past startup.

### Added
- **Software-OS flashing** — a *Software OS* tab + `--list-os` / `--flash-os` CLI to write verified
  bootable operating systems (**Kali, Tails, Arch**) to USB, separate from firmware. Latest version
  auto-resolved online; the bundled catalog works fully offline; integrity-verified (SHA-256 + OpenPGP).
- **Wardriving** (lawful, owner-authorized) — a *Wardrive* tab + core that logs GPS-tagged Wi-Fi as
  **WiGLE CSV** (`WigleWifi-1.6`) from a GPS NMEA port + the ESP32 Marauder scan output.
- **Physical-key access gate + encrypted vault** — gate the app behind an admin password and/or a
  physical USB key; vault data stays encrypted at rest until unlocked (fail-closed; no boot-sequence
  bypass).
- **Tails flashing** (`--flash-tails`) and **Dead Man's Switch** setup, working in the frozen build.
- **How-To tab** — an in-app guide covering every tab/feature; tooltips across the new UI.
- A weekly CI job that refreshes the bundled OS catalog versions/checksums.

### Internal
- New test suites for the OS catalog (14), wardriving (8), and the new Qt tabs; full suite green.

## [1.1.0] — 2026-06-12

A large feature + hardening release. Every change below was validated against the test suite
(green) and, where noted, on real hardware. New capabilities are backward-compatible.

### Added
- **Unified Action Broadcast** — one intent verb (e.g. *Find APs*, *BLE Scan*, *Deauth All*) fans
  out to **every connected radio at once**, each in its own native command, with results converging
  back into the shared Target Pool. New `src/core/broadcast.py` engine, a `BROADCAST_CAPABILITIES`
  map on all protocols, and a Broadcast tab in the GUI. *Live-validated:* "Find APs" → BW16
  `AT+SCAN` (dual-band) + GhostESP `scanap` (94 APs) simultaneously.
- **GhostESP `.zip` bundle flashing** — GhostESP ships per-board zips containing a `merged.bin`;
  the profile now extracts and flashes that at `0x0`. Path-safe `download_and_extract()` with cache
  reuse. *Validated end-to-end on a classic ESP32.*
- **Meshtastic per-chip zip flashing** — handles the 128 MB per-chip archives (factory bin @0x0,
  bleota @0x260000, littlefs @0x300000) with a curated board list. *Validated on a Heltec LoRa V3.*
- **BW16 / RTL8720DN (Realtek AmebaD) flash backend** — first-class support for a non-ESP32 radio
  (dual-band 2.4/5 GHz), including the `bw16` serial protocol. *Validated on real hardware.*

### Security (full audit — all 10 findings closed)
- **H-1** Pin + verify the SHA-256 of the BW16/RTL8720 firmware bundle before flashing.
- **H-2** Never silently serve LAN traffic on the Werkzeug dev server — require a reverse proxy or
  an explicit `CC_WEB_ALLOW_DEV_SERVER=1` opt-in for a non-local bind.
- **M-1** `subscribe_serial` is idempotent + rate-limited and `remove_line_callback` was added,
  killing a callback-leak / serial-emit amplification DoS.
- **M-2** The firmware vault's GitHub *API* GETs now go through the same SSRF allowlist (manual
  redirect re-validation) as the binary download path.
- **M-3** Session + CSRF token are rotated on successful login (session-fixation defense).
- **M-4** `install_rayhunter` validates `admin_ip` as a literal IP before it reaches argv / a URL.
- **L-1** Explicit owner+SYSTEM **NTFS ACLs** on `~/.cyber-controller` and the web secret key /
  encrypted vault / settings (POSIX `0600` is a no-op on Windows).
- **L-2** The hash-chained **audit trail is now durable** — append-only, owner-only, loaded and
  verified on startup; the web remote warns if it has no audit sink.
- **L-3** Removed the misleading no-op password "zeroization" in the dead-man auth relay.
- **L-4** Strict **CSP nonce** for `script-src` (dropped `'unsafe-inline'`); all inline scripts are
  nonce-tagged and former inline `on*=` handlers moved to `addEventListener`.

### Changed / Performance
- UI perf (no visual or behavior change): HealthTab reads the cached system-health snapshot instead
  of a 100 ms GUI-thread `psutil` block; per-class memoization of protocol command lists; bounded
  terminal/log memory via `maximumBlockCount`.
- Bruce firmware repo updated to the canonical **BruceDevices/firmware** (formerly `pr3y/Bruce`).
- Firmware profile count corrected to the verified **19**.

## [1.0.0] — 2026-06-11

First official release: flash-core overhaul, web remote + security baseline, and the initial
firmware/profile set.

[1.1.0]: https://github.com/LxveAce/cyber-controller/releases/tag/v1.1.0
[1.0.0]: https://github.com/LxveAce/cyber-controller/releases/tag/v1.0.0
