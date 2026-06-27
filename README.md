<div align="center">

<img src="assets/cc-logo.png" alt="Cyber Controller Logo" width="400">

# Cyber Controller

### The all-in-one security hardware controller for cyberdecks & field deployments.

**Flash. Control. Coordinate.** — every piece of your security hardware, from one dashboard.

[![License](https://img.shields.io/github/license/LxveAce/cyber-controller?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS%20%7C%20ARM-blue?style=for-the-badge)](#ui-modes)
[![ESP32](https://img.shields.io/badge/ESP32-Marauder%20%7C%20Bruce%20%7C%20Ghost__ESP-E7352C?style=for-the-badge&logo=espressif&logoColor=white)](#supported-firmwares)
[![Flipper Zero](https://img.shields.io/badge/Flipper%20Zero-Unleashed%20%7C%20Momentum-FF8200?style=for-the-badge)](#supported-firmwares)
[![Firmwares](https://img.shields.io/badge/firmware%20profiles-21-success?style=for-the-badge)](#supported-firmwares)
[![Latest](https://img.shields.io/github/v/release/LxveAce/cyber-controller?style=for-the-badge&label=release)](https://github.com/LxveAce/cyber-controller/releases)
[![GitHub stars](https://img.shields.io/github/stars/LxveAce/cyber-controller?style=for-the-badge&logo=github)](https://github.com/LxveAce/cyber-controller/stargazers)

[**Website**](https://cybercontroller.org) · [**Releases**](https://github.com/LxveAce/cyber-controller/releases) · [**Changelog**](CHANGELOG.md) · [**Downloads**](https://cybercontroller.org/#downloads)

</div>

---

<!-- STATUS-ROADMAP:START -->
## Status & Roadmap

**Status:** v1.1.0 is code-stable (228 tests pass, 0 open issues); a Windows installer reliability fix is in progress before the next tagged release.

**In progress / known issues:**
- Windows installer reliability fix in progress — hardening bundled-asset loading so the packaged GUI starts cleanly on a clean Windows host (verified against a real Windows install before release).
- Aligning the release build pipeline with the local build so all UI modes ship correctly.
- Reconciling profile/parser counts and finishing the `deadmans-switch` branding rename across docs and UI.

**Roadmap:**
- Flash **Tails OS** (the amnesiac live OS) to USB, with upstream signature/checksum verification before writing — surfaced under the amnesiac / dead-man section.
- **Physical key** access gate: provision a USB key, then require an admin password and/or the physical USB key be present before the app unlocks (policy configurable: password-only, key-only, or both), covering both the desktop and web entry points.
- Frozen-asset hardening so a missing bundled resource degrades gracefully instead of failing.
- Windows code-signing + installer to reduce SmartScreen/Defender friction.
- Fresh tagged release picking up the commits ahead of v1.1.0.
<!-- STATUS-ROADMAP:END -->

## What is this?

Cyber Controller is the flagship convergence of the **Lxve ESP32 security toolchain** — it merges
[Headless Marauder GUI](https://github.com/LxveAce/headless-marauder-gui),
[Universal Flasher](https://github.com/LxveAce/universal-flasher), and
[Universal Flasher & UI](https://github.com/LxveAce/universal-flasher-ui) into a single unified tool,
with [Dead Man's Switch](https://github.com/LxveAce/deadmans-switch) anti-forensic provisioning built in.
It is built for **cyberdecks, field deployments, and security research** — runs on ARM + x64, on a
7" touchscreen, headless over SSH, or from a phone.

> Designed to drive a multi-device cyberdeck — but just as happy flashing a single $12 CYD on your desk.

It is a self-taught hobby project, hardened and tested as it grows. Authorized security testing,
education, and CTF use only.

## Three Pillars

### Flash
- **21 firmware profiles** across **5 backends**: `esptool` (ESP32 family), `qFlipper` (Flipper Zero),
  `ADB` (Android / Orbic), `SD image` (Raspberry Pi), and **`rtl8720` (Realtek AmebaD)** for the
  dual-band 2.4/5 GHz **BW16 / RTL8720DN** — hardware-validated end-to-end (fetches the firmware
  bundle, drives the AmebaD ImageTool, SHA-256-verifies before flashing).
- **Hardware-validated flash core** ported from the field-proven `headless-marauder-gui` /
  `universal-flasher` lineage: chip auto-detection (`esptool chip_id` is run first — the chip is never
  hardcoded), the critical `--flash_size detect` anti-brick patch, correct per-chip bootloader offsets
  (including the **ESP32-C5 `0x2000`** gotcha), and child-process kill-on-error so a failed flash never
  holds the serial port.
- **Offline Firmware Vault** (download cache + integrity pinning), **batch flash** (sequential /
  parallel), **backup & restore**, and handling for the awkward formats: GhostESP `.zip` bundles
  (extract `merged.bin`, flash at `0x0`), Meshtastic per-chip archives, and AmebaD multi-image layouts.

### Control
- **Protocol-aware serial monitor** with a **per-device firmware selector** and per-firmware command
  palettes. Nine native serial parsers ship: **Marauder, GhostESP, Bruce, Flipper, HaleHound,
  Meshtastic, ESP32-DIV, BW16 (RTL8720DN `AT+` CLI), and BlueJammer (telemetry-only)** — with a generic
  raw passthrough as a fallback.
- **Safety / disclaimer layer** — dangerous transmit commands (deauth / jam / beacon spam) are
  **labeled and confirmed, never blocked**; a one-time legal disclaimer on first launch plus a
  Settings "suppress all warnings" master toggle. Full capability is always retained.
- **Macro recorder & playback** with timing capture and variable substitution.
- **Tamper-evident audit trail** — a SHA-256 hash chain over flashes and serial commands, durable
  (append-only, owner-only on disk), loaded and verified on startup.

### Coordinate
- **Unified Action Broadcast** — one intent verb (*Find APs*, *Deauth All*, *BLE Scan*, *SubGHz Scan*,
  *Capture Handshakes*, *Beacon Spam*, *BLE Spam*, *Mesh Status*, *STOP ALL*) fans out to **every
  connected radio at once**, each translated into that firmware's own native command, via per-port
  worker threads. Partial support is first-class (unsupported devices are named and reported); `STOP
  ALL` is never gated.
- **Shared target pool** across every connected device — one board discovers an AP, another deauths
  it, another sniffs the handshake, all from one screen. Results from a broadcast converge back into
  the pool automatically.

## Supported Firmwares

21 firmware profiles ship in `src/config/profiles/`. Each tracks its **latest upstream release** at
flash time and auto-selects the correct per-board binary.

| Firmware | Upstream | Chips | Backend |
|----------|----------|-------|---------|
| **ESP32 Marauder** | [justcallmekoko/ESP32Marauder](https://github.com/justcallmekoko/ESP32Marauder) | ESP32 / S2 / S3 / C5 | esptool |
| **Bruce** | [BruceDevices/firmware](https://github.com/BruceDevices/firmware) | ESP32 / S3 / C-series | esptool (merged) |
| **GhostESP** | [GhostESP-Revival/GhostESP](https://github.com/GhostESP-Revival/GhostESP) | ESP32 / S2 / S3 / C-series | esptool (zip) |
| **HaleHound** | [JesseCHale/HaleHound-CYD](https://github.com/JesseCHale/HaleHound-CYD) | ESP32 (CYD) | esptool |
| **ESP32-DIV** | [cifertech/ESP32-DIV](https://github.com/cifertech/ESP32-DIV) | ESP32-S3 (v2) / ESP32 (legacy) | esptool |
| **MinigotchiV3** | [dj1ch/minigotchi-V3](https://github.com/dj1ch/minigotchi-V3) | ESP32 (dual-core) / S3 | esptool |
| **Meshtastic** | [meshtastic/firmware](https://github.com/meshtastic/firmware) | ESP32-S3 / Heltec | esptool (zip) |
| **Flock-You** | [colonelpanichacks/flock-you](https://github.com/colonelpanichacks/flock-you) | ESP32-S3 | esptool |
| **OUI-Spy** | [colonelpanichacks/oui-spy](https://github.com/colonelpanichacks/oui-spy) | ESP32-S3 | esptool |
| **Sky-Spy** (drone RemoteID) | [colonelpanichacks/Sky-Spy](https://github.com/colonelpanichacks/Sky-Spy) | ESP32-S3 / C6 | esptool |
| **AirTag Scanner** | [MatthewKuKanich/ESP32-AirTag-Scanner](https://github.com/MatthewKuKanich/ESP32-AirTag-Scanner) | ESP32 / S3 | esptool |
| **Chasing Your Tail NG** (counter-surveillance) | [ArgeliusLabs/Chasing-Your-Tail-NG](https://github.com/ArgeliusLabs/Chasing-Your-Tail-NG) | ESP32 | esptool |
| **BW16 / RTL8720 Vampire Deauther** | [RTL8720dn-Deauther](https://github.com/tesa-klebeband/RTL8720dn-Deauther) | **RTL8720DN** (AmebaD, dual-band 2.4/5 GHz + BLE) | **rtl8720** |
| **BlueJammer-V2 — ESP32 engine** ⚠ *lab-only / illegal to operate* | [EmenstaNougat/BlueJammer-V2](https://github.com/EmenstaNougat/BlueJammer-V2) | ESP32-WROOM-32U | esptool |
| **BlueJammer-V2 — BW16 controller** ⚠ *lab-only / illegal to operate* | [EmenstaNougat/BlueJammer-V2](https://github.com/EmenstaNougat/BlueJammer-V2) | RTL8720DN | rtl8720 |
| **Flipper Momentum** | [Next-Flip/Momentum-Firmware](https://github.com/Next-Flip/Momentum-Firmware) | STM32WB55 | qFlipper |
| **Flipper Unleashed** | [DarkFlippers/unleashed-firmware](https://github.com/DarkFlippers/unleashed-firmware) | STM32WB55 | qFlipper |
| **RayHunter** (IMSI-catcher detect) | [EFForg/rayhunter](https://github.com/EFForg/rayhunter) | Orbic RC400L | ADB |
| **Pwnagotchi** | [jayofelony/pwnagotchi](https://github.com/jayofelony/pwnagotchi) | Raspberry Pi | SD image |
| **RaspyJack** | [7h30th3r0n3/RaspyJack](https://github.com/7h30th3r0n3/RaspyJack) | Raspberry Pi | SD image |
| **Kali ARM** | [kali.org](https://www.kali.org/get-kali/) | Raspberry Pi | SD image |
| **Custom / local .bin** | — | any ESP32 | esptool |

> ⚠ **BlueJammer-V2** is included strictly as a **flash-and-study target for an authorized lab**.
> RF jamming is illegal to transmit (FCC 47 U.S.C. 333). Per the project's *label, never block*
> doctrine the profiles are flashable but carry the strongest illegal-transmit label, the closed-source
> binaries are **SHA-256-pinned and fetched at flash time (never vendored)**, and Cyber Controller
> exposes **no serial command channel or operate/transmit control** for the device — its parser is
> telemetry-only.

## Supported Hardware

### ESP32 boards
| Board | Chip | Notes |
|-------|------|-------|
| Lonely Binary ESP32 Gold | ESP32-WROOM-32E | Marauder / Flock / BLE scan |
| Cheap Yellow Display (2.4″/2.8″/3.2″/3.5″) | ESP32 | Marauder GUI, HaleHound, Bruce — use the **resistive** 2.8″ `2432S028R` |
| Waveshare ESP32-C5 | ESP32-C5 | Dual-band 2.4 + 5 GHz WiFi 6 (bootloader `0x2000`) |
| M5Stack Cardputer / Cardputer ADV | ESP32-S3 | Bruce, Marauder, Minigotchi |
| M5StickC Plus2 | ESP32-PICO-V3 | Bruce, Marauder |
| LilyGo T-Embed CC1101 / T-Deck / T-Dongle-S3 | ESP32-S3 | Bruce, Marauder, Meshtastic |
| Flipper Zero WiFi Dev Board | ESP32-S2 | Marauder `flipper`, FlipperHTTP |
| Marauder Mini / Mini v3 (C5) | ESP32 / ESP32-C5 | Official Koko hardware |
| Heltec LoRa V3 | ESP32-S3 | Meshtastic (915 MHz US) |

### Other devices
| Device | Role |
|--------|------|
| Raspberry Pi 5 / Pi Zero 2 W | Central brain · Pwnagotchi · Kali · RaspyJack |
| Flipper Zero | Sub-GHz / RFID / NFC (qFlipper backend) |
| BW16 / RTL8720DN | Dual-band 2.4/5 GHz WiFi + BLE (rtl8720 / AmebaD backend) |
| Orbic RC400L | RayHunter IMSI-catcher detector (ADB) |

### Flash-offset reference (the part that bricks boards if you get it wrong)
| Chip family | bootloader | partitions | boot_app0 | app |
|-------------|-----------|-----------|-----------|-----|
| ESP32, ESP32-S2 | `0x1000` | `0x8000` | `0xE000` | `0x10000` |
| ESP32-S3, C2, C3, C6, H2 | `0x0` | `0x8000` | `0xE000` | `0x10000` |
| **ESP32-C5, P4** | **`0x2000`** | `0x8000` | `0xE000` | `0x10000` |

Merged single-image firmwares (e.g. Bruce, GhostESP `merged.bin`) flash at `0x0`. The engine never
hardcodes the chip — it runs `esptool chip_id` first.

## UI Modes

| Mode | Framework | Use case |
|------|-----------|----------|
| Full Dashboard | PyQt5 | Primary — 7″ touchscreen, all features |
| Lightweight | Tkinter | Low-resource ARM systems |
| TUI | Textual | SSH / headless |
| Web Remote | Flask + SocketIO | Phone control of a headless Pi |

When launched without `--ui`, a picker dialog lets you choose the interface.

## Security

Cyber Controller drives real RF-attack and flashing hardware, so the codebase is hardened to match.
A full security audit (10 findings) was completed in v1.1.0; see [SECURITY.md](SECURITY.md) and the
[Changelog](CHANGELOG.md) for the detail.

- **Authenticated web remote** — the SocketIO layer rejects unauthenticated sockets and validates a
  per-session CSRF/connection token; the web UI binds **`127.0.0.1` by default** (LAN exposure is an
  explicit opt-in, TLS-encouraged); no usable default credentials (a strong one-time password is
  generated if `CC_WEB_PASS` is unset); constant-time scrypt credential checks; CORS allowlist; CSRF +
  per-IP rate limiting; strict security headers; a per-request **CSP nonce** (no `script-src
  'unsafe-inline'`); and XSS-safe `textContent` rendering of over-the-air scan data.
- **Supply-chain hardening** — firmware downloads are pinned to an **HTTPS GitHub host allowlist with
  redirect validation (SSRF-safe)**, path-traversal-guarded, size-capped, and support **SHA-256
  integrity pinning**; bundle flashing is TOCTOU-safe with per-file SHA-256 verification.
- **Authenticated encryption** — session storage is **AES-256-GCM (scrypt KDF)** and **fails closed**
  (no unauthenticated fallback; `cryptography` is a mandatory dependency).
- **Windows-aware secrets** — explicit owner+SYSTEM **NTFS ACLs** on `~/.cyber-controller` and the web
  secret key / encrypted vault / settings (POSIX `0600` is a no-op on the Windows-primary deployment).
- **Command-injection defenses** — serial writes reject embedded control characters and the
  auto-router uses safe fixed-placeholder substitution (never `str.format`) on attacker-influenced
  SSID/MAC values.

> Authorized security testing, education, and CTF use only — see the
> [disclaimer](https://esp32marauder.com/disclaimer.html). To report a vulnerability, email the
> address in [SECURITY.md](SECURITY.md) rather than opening a public issue.

## Quick Start

```bash
# Install (Python 3.12+). Extras: tk / tui / web / full / dev
pip install -e ".[full]"

# Full PyQt5 dashboard
cyber-controller

# Lightweight / TUI / web remote
cyber-controller --ui tk
cyber-controller --ui tui
cyber-controller --ui web                       # binds 127.0.0.1:5000

# Web remote credentials (no default password is shipped)
export CC_WEB_USER=operator
export CC_WEB_PASS='choose-a-strong-one'
cyber-controller --ui web
```

LAN exposure is deliberate: bind `--host 0.0.0.0` only with `CC_WEB_ALLOW_LAN=1`, and provide
`CC_WEB_CERT` / `CC_WEB_KEY` for TLS. (Behind the bundled dev server a non-local bind additionally
requires `CC_WEB_ALLOW_DEV_SERVER=1` — prefer a reverse proxy.)

## Building

```bash
python build.py        # PyInstaller single-file executable in dist/
```

CI (`.github/workflows/build-release.yml`) builds Windows, Linux, ARM, and macOS executables on tag
and attaches them to the GitHub release.

## Development Roadmap

### Phase 1 — Core ✅
- [x] Architecture, offline Firmware Vault, device health, hot-plug manager
- [x] Macro recorder & playback, durable tamper-evident audit trail
- [x] Hardware-validated flash core (chip detect, anti-brick `--flash_size detect`, C5 `0x2000`)
- [x] Real ADB / SD-image / AmebaD backends, backup + restore, batch flash

### Phase 2 — Intelligence ✅
- [x] Protocol parsers (Marauder, GhostESP, Bruce, Flipper, HaleHound, Meshtastic, ESP32-DIV, BW16, BlueJammer) + registry
- [x] Shared target pool (APs + BLE / SubGHz / NFC / rogue-AP) + cross-comm UI
- [x] Per-device firmware selector (any firmware feeds the AutoRouter, not just Marauder)
- [x] BW16 / RTL8720DN AmebaD flash backend — HW-validated end-to-end
- [x] Safety / disclaimer layer (labels & confirms dangerous TX, never blocks; suppressible)
- [x] Encrypted session storage (AES-256-GCM)
- [ ] Target dossier panel · network topology graph · mission planner · duress mode

### Phase 3 — Orchestration
- [x] Headless web remote (hardened) · settings persistence
- [x] Unified Action Broadcast (one verb fans out to every connected radio)
- [ ] Attack chain builder · trigger/event system · scheduled task engine

### Phase 4 — Extended
- [ ] Signal heatmap · RF waterfall · PCAP pipeline · recon bridge · mesh relay · plugin system

### Firmware & backend expansion

Planned additions, rolling out in tiered releases — including new backends (`dfu-util`, `UF2`) to reach
hardware such as the HackRF One, Proxmark3, Pi Pico / RP2040, and Chameleon Ultra. The full plan lives
at [cybercontroller.org](https://cybercontroller.org/#firmware). Targets are added only once a profile
is wired up and (where possible) validated on real hardware — the count above reflects what ships today,
not the plan.

## Dead Man's Switch Integration

[Dead Man's Switch](https://github.com/LxveAce/deadmans-switch) (`deadmans-switch`) ships as a git
submodule for owner-only anti-forensic provisioning: a PBKDF2-HMAC-SHA256 boot-password gate, 2-fail
automatic wipe, GPIO dead-man switch, and eFuse + Flash Encryption (T2). Set the password & duress
config straight from the controller — **`cyber-controller --deadman-setup`** (interactive) or **Tools ▸
Dead Man's Switch Setup** in the Qt UI — which hashes the password **host-side** (PBKDF2, zeroized, never stored, never on
argv) and bakes the `guardcfg` bundle. Bundles flash through the controller with **TOCTOU-safe per-file
SHA-256 verification** — no unverified anti-forensic build is ever written, and a suicide-schema bundle
refuses to flash without a SHA-256 for every file.

The on-trigger wipe is **hardware-validated** to obliterate the *entire* flash — bootloader, partition
table, the full running app, NVS/SPIFFS/logs, and the SD card — with a forensic random-overwrite pass,
leaving an all-`0xFF` chip with no trace (the running app self-erases via a ROM-SPI bypass inside the IDF
flash-only critical section; recoverable only by the owner over UART on T1).

> Cyber Controller itself only **flashes** a bundle the deadmans-switch provisioner already built — it
> never burns eFuses or performs T2 / secure-boot provisioning.

## Ecosystem

| Project | What |
|---------|------|
| [headless-marauder-gui](https://github.com/LxveAce/headless-marauder-gui) | Standalone Marauder controller + flasher (4 UIs) |
| [universal-flasher](https://github.com/LxveAce/universal-flasher) | Multi-firmware flasher + device manager |
| [deadmans-switch](https://github.com/LxveAce/deadmans-switch) | Anti-forensic firmware provisioner |
| [cybercontroller.org](https://cybercontroller.org) | Flagship website — interactive demo, firmware library, downloads |
| [esp32marauder.com](https://esp32marauder.com) | ESP32 security tools hub |

## Contributing

Issues and PRs welcome. Run `python -m pytest` before submitting — the suite covers the flash core,
protocols, backends, the security hardening, and the broadcast engine.

## License

MIT — Copyright © 2026 [LxveAce](https://github.com/LxveAce). See [LICENSE](LICENSE).

## Connect

- **Discord:** [discord.gg/lxveace](https://discord.gg/lxveace) — questions, help, or to talk through this project
- **GitHub:** [@LxveAce](https://github.com/LxveAce)
- **Website:** [lxveace.com](https://lxveace.com)
- **Project site:** [cybercontroller.org](https://cybercontroller.org)
