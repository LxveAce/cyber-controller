# Changelog

All notable changes to Cyber Controller are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

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
