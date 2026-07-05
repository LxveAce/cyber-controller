# Changelog

All notable changes to Cyber Controller are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.6.0] — 2026-07-05

The 1.6.0 line, complete. Everything the beta pointed at has landed: a live Flock driving map, concurrent
multi-device wardriving, recommended-hardware guidance, and real relay/node wireless firmware.

### Added
- **Multi-device wardriving** — a new Multi-Wardrive tab drives several boards at once from one shared GPS
  into a single merged WiGLE CSV, with per-board AP counts and a running total. Each board is sent its own
  firmware's native scan command (Marauder, GhostESP, Flock-You, and so on), and captures route through the
  shared DeviceManager, so a board already open in the Devices tab is shared rather than double-opened
  (which on Windows would fail with Access Denied).
- **Live Flock driving map** — the Flock Map tab now records a live drive: located ALPR cameras drop onto the
  map as they're found, each checkpointed to disk for crash-safety. The recorder keeps running while the tab
  is hidden; only the repaint pauses, and the map catches up when you return to it.
- **Wireless relay + node firmware** — real ESP32 sketches for both roles: a crypto-free USB↔ESP-NOW relay
  and an AES-256-GCM sensor node, with a byte-exact wire spec in `firmware/PROTOCOL.md`. The node's on-device
  crypto and anti-replay are contract-tested against the host (the sketches are source-only for now; compile
  and flash them yourself).
- **Parrot OS** in the Software-OS flasher — the version is auto-resolved from the official ISO index with
  SHA-256 + OpenPGP verification and a pinned offline fallback. The OS flasher now covers Kali, Tails, Parrot,
  and Arch.
- **Recommended-specs guide** (`docs/RECOMMENDED-SPECS.md`) — which of the four frontends fits which host,
  from a desktop GUI down to a headless Raspberry Pi.
- **Supported-boards tooltip** on the Firmware tab, and a **board-compatibility hint** that tints a firmware
  green or red against the connected board's chip (advisory only — it never blocks a flash).
- **Companion mobile app** is now its own project (Bluetooth + WiFi now, cellular later).

### Changed
- The wireless **Nodes** feature is real now: the relay/node firmware the host-side groundwork was waiting on
  ships in `firmware/`.
- Tightened the README voice. No facts, versions, flags, or commands changed.

### Fixed
- `from_checkpoint` no longer raises on a malformed checkpoint file — it returns an empty session, as documented.

## [1.6.0-beta.1] — 2026-07-05

First public preview of the 1.6.0 line. More is landing before the full 1.6.0 — a live Flock driving
map, multi-device wardriving, recommended-hardware guidance, and the wireless relay/node firmware. This
beta ships the pieces that are done and tested.

### Added
- **Parrot OS** in the Software-OS flasher — the version is auto-resolved from the official ISO index with
  SHA-256 + OpenPGP verification and a pinned offline fallback. The OS flasher now covers Kali, Tails, Parrot, and Arch.
- **Supported-boards tooltip** on the Firmware tab — hover a firmware to see the boards and chips it targets.
- **Board-compatibility hint** — a firmware is tinted green or red against the connected board's chip. It's
  advisory only: it never blocks a flash and never shows a false red on a guessed chip.
- **Flock Map tab** — the located-ALPR-camera map is a tab in the Operate surface now, next to Wardrive
  (it used to open as a separate Tools window).
- **Crash-safe Flock drives** — a scan checkpoints its located cameras to disk as it runs and can resume from
  that file after a restart. Two drives (or a synced set) merge by the strongest-signal rule.
- **Companion mobile app** is now its own project (Bluetooth + WiFi now, cellular later).

### Changed
- The wireless **Nodes** view is labeled a demo/placeholder — the host-side groundwork is in; the relay/node
  firmware is still to come.
- Tightened the README voice. No facts, versions, flags, or commands changed.

### Fixed
- `from_checkpoint` no longer raises on a malformed checkpoint file — it returns an empty session, as documented.

## [1.5.1] — 2026-07-03

Ships the accumulated work since 1.5.0.

### Security
- Web remote is covered by the persistent brute-force lockout (SEC-A1); the access-gate failure counter is a lost-update-safe read-modify-write (SEC-A2).
- The access gate fails CLOSED on a corrupt config instead of open (SEC-C2); an enabled secure container is never silently downgraded to plaintext (SEC-B1); its key is minted once, not re-minted on status/read (SEC-B2).
- The ACL is granted by the process token SID, not a spoofable env name (SEC-D1); a generated web password is kept out of logs, a tampered vault fails cleanly, and ACL-harden failures are loud. Corrected an audit-trail tamper-evidence over-claim (SEC-C1).

### Added
- **In-app auto-updater** — a silent startup check against GitHub Releases, prompt-to-update with a "don't show again" option (re-prompting when two or more versions behind), graceful offline handling, and a Help-menu / command-palette "Check for Updates".
- **Three firmware profiles** (29 total): ESP8266 as a first-class chip + `esp8266_deauther`, M5Stick NEMO, and RogueMaster Flipper CFW.
- **`nrf_dfu`** flash backend (Nordic nRF52 DFU `.zip`).
- Per-device capability map, a connect-time firmware health probe (CC-7), and firmware autodetect from the handshake reply.

### Changed
- **Grouped tab UI:** the tab strip folds into six top-level surfaces — Flash (Firmware + Software-OS), Connect (Devices + Health), Operate (Targets + Broadcast + Macros + Wardrive), Network (graph + Cross-Comm), Settings — and How-To moves into the Help menu (CC-6).
- Cross-comm hardening: a `CrossCommHub` spine with driver-type dispatch, a Meshtastic Stream-API frame codec, and a binary serial write path.
- Release CI publishes one consolidated `SHA256SUMS.txt` and runs a VirusTotal scan on every release.

### Fixed
- Flash/serial robustness: kill the flash child on Ctrl-C and don't reuse a corrupt cached zip; serialize per-port connection builds (no concurrent-open leak); frame serial lines on CR/CRLF/LF (CR-only firmware was never framed); a loud error on an undetected flash size instead of a silent 4 MB truncation; fail loud instead of flashing a dead board or claiming an unrun verify; hardened the Windows raw-disk write path; robust reconnect + firmware probe; honor a pinned firmware version.
- The esptool version guard now flags 4.0–4.6 (the pin is >=4.7); benign RTL8720 "sync" chatter no longer reads as a flash failure.

## [1.5.0] — 2026-07-01

### Fixed
- **Access gate (data-loss).** A key-only gate under the default policy no longer burns every unlock attempt instantly (which tripped the lockout and any opt-in duress vault-wipe on a normal boot); and an exclusive policy whose factor isn’t configured is now rejected (no self-lockout).
- **Flashing.** Flipper (Momentum/Unleashed) flashes the real downloaded package instead of launching a bare qFlipper and reporting false success; backup detects the real flash size instead of truncating >4 MB boards; the batch flasher enforces the SHA-256 pin; the ESP32-C5 2nd-stage bootloader offset is 0x2000 via an esptool-faithful SSOT helper.
- **Safety.** Network-tab device-node commands are gated + no longer send unfilled `<…>` templates raw; BlueJammer control-map loader defaults `validated=False` (an unmarked map can’t send frames); RF-transmit commands (Tesla opener, SubGHz tx) now hit the lab-only confirmation.
- **Cross-device.** MAC-keyed target scan index is refreshed on re-observation (index actions stop firing at the wrong AP); the AutoRouter cooldown is atomic + bounded; the Dead-Man-Switch auto-auth reply goes to the device that emitted the prompt; per-device line terminator is re-stamped on send; on_line/ingest callbacks no longer stack on reconnect (both serial UIs); `TargetIngestor.attach` is idempotent; a device serial ERROR now reflects into `Device.connected`; targets-tab actions resolve against the pooled target.
- **Integrity.** Settings Save no longer wipes interface mode + loadout; the audit trail survives a torn JSONL line instead of disabling all persistence; Flipper SubGHz captures keep their protocol + key; the Dead-Man-Switch provisioner fails loud on an unknown flash_size instead of silently baking the 4 MB layout; the app version is single-sourced (`src/__init__` re-exports `src/version`).

### Changed
- **LxveAce violet identity theme.** The interactive/brand accent is now LxveAce violet (`#a371f7`), retiring the generic acid-green across all four front-ends (Qt/Tk/TUI/Web); functional green is kept for connected/online status and live serial output. Guarded by `test_no_acid_green`.

### Added
- **dfu-util + UF2 flash backends** *(experimental — HW-validation pending)* behind `FlashEngine._backends` for RP2040 / Pi Pico (DFU) and UF2 mass-storage bootloaders; download-or-local resolve, never fake success; unit-tested. See `docs/ROADMAP-FUTURE.md`.
- **`docs/ROADMAP-FUTURE.md`** — the forward roadmap (more backends/hardware, mission planner, RF/recon).
- **Regression guards:** bundle-manifest test (`build.py` data ↔ runtime `resource_path()`), firmware-profile count drift-lock (README ↔ shipped JSONs), and a `resource_path` frozen-bundle check.
- **Per-device capability map (network integration).** Each firmware/board now declares what it can do
  (`wifi`/`ble`/`subghz`/`nfc`/`ir`/`rfid`/`gps`/`lora`/`nrf24`/`deauth`/`badusb`…) via `BaseProtocol.capabilities`,
  surfaced as a capability chip line in the Devices tab so every connected device reads as a node in the
  network, and exposed via `protocols.capabilities_for(name)` for Broadcast/AutoRouter. `src/protocols/*.py` +
  `src/ui/qt/device_tab.py`; +tests.
- **Argument-entry form for placeholder commands.** Sending a command that contains `<...>` placeholders
  (e.g. `scanap -c <ch>`, `select -a <idx>`, `led -r <v> -g <v> -b <v>`) now pops a small parameter form —
  one field per token, occurrence-ordered so repeated tokens get distinct values — instead of transmitting
  the literal `<ch>`. Values are sanitized (control chars + angle brackets stripped, 64-char cap); hooks the
  single send chokepoint so typed and palette-selected commands are both covered. `src/ui/qt/device_tab.py`; +5 tests.
- **`{index}` target-action substitution + source-restriction.** Target actions that select by scan index
  (e.g. Marauder's `select -a {index}`) previously sent the literal text `{index}`. The resolver now
  substitutes a real per-device scan index when known, and — because a scan index is only valid for the
  device that produced it — **source-restricts** index actions to the discovering device and **drops** them
  when no index is known (rather than firing at the wrong AP). `src/core/action_resolver.py` +
  `src/core/target_ingest.py`; +4 tests.
- **BW16 right-click deauth-by-index.** The BW16 Vampire scan prints index + SSID but no BSSID, so those APs
  used to be dropped from the Target Pool. They now enter under a synthetic **source-tagged** key carrying the
  scan index, and BW16 gets a `Deauth (this index)` action (`AT+DEAUTHIDX={index}`) the resolver offers only
  on the BW16 that scanned it. Completes the one fully-verified firmware. `src/protocols/bw16.py` +
  `src/core/target_ingest.py`; +3 tests.
- **Removed phantom target actions** that referenced commands the firmware never exposes (presented as
  working but doing nothing): HaleHound `analyze`, Meshtastic `relay` (its serial link is protobuf-framed, not
  text), and Flipper `bt spam` (no stock-CLI command) — in both `TARGET_ACTIONS` and the broadcast map.
  `src/protocols/{halehound,meshtastic,flipper}.py`; +3 tests.
- **Per-firmware command terminator (Flipper CR fix).** The serial line terminator now follows the selected
  firmware's protocol — most firmwares submit a line on LF, but the **Flipper Zero** CLI shell only submits on
  CR (`\r`), so CC's commands previously never executed on a Flipper. `BaseProtocol.line_ending` (Flipper =
  `\r`, everyone else `\n`) is applied to the live connection on connect / firmware-change.
  `src/core/serial_handler.py` + `src/protocols/{base,flipper}.py` + `src/ui/qt/device_tab.py`; +3 tests.
- **Command-correctness pass (source-verified against firmware).** Fixed command tokens that didn't match the
  real firmware CLIs: **Marauder** v1.12.3 (`scanap`→`scanall`, `channel -s`, `sniffbt`/`sniffskim`,
  `blespam -t sourapple/windows`, `gpsdata`/`nmea`, `settings -s`, `led -s`) **plus a stateful multi-line AP
  parser** so the Targets tab actually populates (the firmware prints ESSID/BSSID/RSSI on separate lines);
  **GhostESP** (`attack -d`, `beaconspam -r/-rr`, `startportal`/`stopportal`, `capture -eapol/-stop`, `startwd`,
  `list -a`, `chipinfo`); **Bruce** (`ir rx`/`tx`, `subghz rx/tx/tx_from_file`, `badusb run_from_file`; reads its
  `COMMAND:`/`[CLI] Result:` shell; fabricated wifi/ble/nfc serial verbs removed — those run via menu/loader).
  Device-View skins updated to the corrected commands. `src/protocols/{marauder,ghost_esp,bruce}.py` +
  `src/ui/qt/device_view.py`; +tests. (esp32div/halehound kept pending hardware confirmation; Meshtastic is protobuf.)
- **Loadout — tailor the GUI to what you actually use.** On first run, pick the firmwares + hardware you use
  (or **Full Stack** = everything) and Cyber Controller hides the tabs you won't need (de-bloat); change it
  anytime via **View ▸ Loadout**. Orthogonal to Simple/Pro (which controls depth). Core tabs (Flash, Devices,
  Health, Macros, Settings, How-To) always stay; **fail-open** — Full Stack / unconfigured / empty shows
  everything, so nothing is ever stranded. `src/config/loadout.py` + `src/ui/qt/loadout_dialog.py`; +15 tests.
- **BlueJammer remote-controller framework.** `src/core/bluejammer_control.py` — a transport-abstracted
  controller for the BlueJammer's modes: **UART-first (the inter-board wire — no Wi-Fi AP/IP needed)** with
  the web UI (`http://192.168.1.1`) as an option. STOP (Idle) is the primary, ungated action; arming a
  jamming mode requires explicit confirmation (GUI gates it behind an RF-shielded-enclosure attestation,
  per 47 U.S.C. §333). **Fail-safe:** refuses to send until a hardware-captured/validated control map exists
  (a STOP that silently does nothing is the failure mode we avoid). The exact UART frames / HTTP endpoints
  are closed-source and captured on hardware (see the reverse-engineering plan). +10 tests.
- **BlueJammer control / STOP panel — full in-app remote control.** When a BlueJammer-V2 is the active
  firmware, the Devices tab shows a prominent control panel wired to the `BlueJammerController`: a large,
  always-available **STOP** (set Idle); an **RF-shielded-enclosure attestation** that gates the **arm-mode**
  buttons (Bluetooth / BLE / WiFi / RC-Drone), each behind a per-press confirm; a **Load control map…**
  action; an **Open control web UI** launcher; and a live status line. Proper remote control is the safety
  mechanism — arm and, critically, *instantly STOP* without standing next to an active transmitter.
  **Fail-safe:** the control frames are closed-source, so the app never sends guessed frames — live
  transmission activates once a **validated control map captured from your own device** is loaded (UART
  frames or web-UI calls); STOP / web UI / power remain available meanwhile. Illegal to operate outside an
  authorized RF-shielded lab (47 U.S.C. §333). `src/ui/qt/device_tab.py`; +7 panel tests.
- **Device View — Marauder / GhostESP / ESP32-DIV skins (now drives the device).** A new
  **Tools → Device View** opens an on-screen reconstruction of a firmware's on-board TFT menu (header,
  breadcrumb, selection highlight, submenus) at the device's real 240×320 resolution, scaled into a
  resizable window. **Clicking a menu item now sends that firmware's real serial command to the connected
  device** — but only when the active device's firmware matches the skin, and routed through the same safety
  prompts as the Devices tab; otherwise it stays a labelled **preview** (so it never sends one firmware's
  commands to another, and never implies control it doesn't have). Every leaf is grounded against the real
  protocol command set by tests. Honest framing: a faithful *reconstruction*, not a pixel mirror — only the
  Flipper's RPC can be a true mirror (a later phase). `src/ui/qt/device_view.py`; +12 tests. (Plan P2/P3.)
- **Detachable / pop-out tabs.** Any tab can pop out into its own resizable, top-level window — drag it to a
  second monitor, resize it freely — and re-dock seamlessly back onto the tab strip. Detach via the tab-strip
  **⇱** corner button, **double-clicking** a tab, the tab right-click **"Pop out"**, or **Ctrl+Shift+D**;
  re-dock via the **⤓ Re-dock** button or simply **closing** the pop-out (closing re-docks by default, so a
  working panel is never lost). The set of popped-out tabs + their window geometry persists across sessions.
  Foundation for the planned per-firmware "Device View." (`src/ui/qt/detachable_tabs.py`; +10 tests.)
- **Cyberdeck-aware scaling.** The app now adapts to the screen instead of assuming a desktop: the hard
  900×600 minimum is **relaxed to fit small deck panels** (down to ~480×320 on an 800×480 / 1024×600 screen),
  the launch size is clamped to the screen, **high-DPI fractional scaling** is enabled app-wide, and the
  streamlined **Simple interface auto-engages on small/touch screens** (your explicit Simple/Pro choice still
  wins). (`src/ui/qt/screen.py`; +11 tests.)

### Fixed
- **Dead Man's Switch serial auth detection.** The host-side prompt/result patterns now match the boot-gate's
  *actual* serial strings (`suicide-gate: enter …` / `… wrong. attempts left: N` / `… locked for Ns.`), and
  benign `SM>{…}` status JSON is explicitly never treated as an auth prompt (safe to poll). (`deadman_auth.py`;
  +6 tests.) Full suite **488 passed / 2 skipped**.

### Distribution / trust
- **App icon.** Generated `assets/icon.ico` (multi-resolution 16–256 px) so the Windows `.exe` is branded in
  Explorer/taskbar (build.py already passed `--icon` but the file was missing, so it was a no-op until now).
- **Real Windows installer.** New `installer/cyber-controller.iss` (Inno Setup) packages a `--onedir` build
  (added to `build.py`) and registers the standard Add/Remove Programs keys, so the app appears under
  **Settings → Apps → Installed apps** with an uninstaller instead of being a loose `.exe`. CI builds it on
  release (best-effort; unsigned — see below).
- **Published checksums.** The release workflow now emits a `.sha256` next to each binary (Windows/Linux/macOS)
  so downloads are verifiable.
- **Trust guide.** New `docs/WINDOWS-SECURITY.md` explains the SmartScreen "unrecognized app" prompt (and how
  to *More info → Run anyway*), why an unsigned PyInstaller build draws AV false positives, and three ways to
  verify the download yourself (SHA-256, VirusTotal, build from source). Code-signing remains the cert-gated
  follow-up that retires the prompt for good.

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
