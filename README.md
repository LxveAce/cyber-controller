<div align="center">

# ⬢ Cyber Controller

### The all-in-one security hardware controller for cyberdecks & field deployments.

**Flash. Control. Coordinate.** — every piece of your security hardware, from one dashboard.

[![License](https://img.shields.io/github/license/LxveAce/cyber-controller?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS%20%7C%20ARM-blue?style=for-the-badge)](#ui-modes)
[![ESP32](https://img.shields.io/badge/ESP32-Marauder%20%7C%20Bruce%20%7C%20Ghost__ESP-E7352C?style=for-the-badge&logo=espressif&logoColor=white)](#supported-firmwares)
[![Flipper Zero](https://img.shields.io/badge/Flipper%20Zero-Unleashed%20%7C%20Momentum-FF8200?style=for-the-badge)](#supported-firmwares)
[![Firmwares](https://img.shields.io/badge/firmwares-18%2B-success?style=for-the-badge)](#supported-firmwares)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen?style=for-the-badge)](#contributing)
[![GitHub stars](https://img.shields.io/github/stars/LxveAce/cyber-controller?style=for-the-badge&logo=github)](https://github.com/LxveAce/cyber-controller/stargazers)

[**Website**](https://lxveace.com) · [**ESP32 Marauder Tools Hub**](https://esp32marauder.com) · [**Build Guide**](https://esp32marauder.com/builds.html) · [**Downloads**](https://esp32marauder.com/downloads.html)

</div>

---

## What is this?

Cyber Controller is the flagship convergence of the **Lxve ESP32 security toolchain** — it merges
[Headless Marauder GUI](https://github.com/LxveAce/headless-marauder-gui),
[Universal Flasher](https://github.com/LxveAce/universal-flasher), and
[Universal Flasher & UI](https://github.com/LxveAce/universal-flasher-ui) into a single unified tool,
with [Suicide Marauder](https://github.com/LxveAce/Suicide-Marauder) anti-forensic provisioning built in.
It is built for **cyberdecks, field deployments, and security research** — runs on ARM + x64, on a
7" touchscreen or headless over SSH or a phone.

> Designed to drive a 14-device [Pelican 1300 cyberdeck](https://lxveace.com/security/) — but just as
> happy flashing a single $12 CYD on your desk.

## Three Pillars

### ⚡ Flash
- **19+ firmware profiles** across **5 backends**: `esptool` (ESP32), `qFlipper` (Flipper Zero),
  `ADB` (Android/Orbic), `SD image` (Raspberry Pi), and **`rtl8720` (Realtek AmebaD)** for the
  dual-band 2.4/5 GHz **BW16 / RTL8720DN** — hardware-validated end-to-end (downloads the firmware
  bundle, drives the AmebaD ImageTool, checksum-verifies).
- **Hardware-validated flash core** ported from the field-proven `headless-marauder-gui` lineage:
  chip auto-detection, the critical `--flash_size detect` anti-brick patch, correct per-chip
  bootloader offsets (incl. the **ESP32-C5 `0x2000`** gotcha), and child-process kill-on-error so a
  failed flash never holds the serial port.
- **Offline Firmware Vault** (cache + integrity-pinning), **batch flash** (sequential/parallel),
  **backup & restore**.

### 🎮 Control
- **Protocol-aware serial monitor** with a **per-device firmware selector** + per-firmware command
  palettes for **Marauder, GhostESP, Bruce, Flipper, HaleHound, Meshtastic, ESP32-DIV, and BW16
  (RTL8720DN `AT+` CLI)** — 70+ Marauder commands built in.
- **Safety / disclaimer layer** — dangerous transmit commands (deauth / jam / beacon spam) are
  **labeled and confirmed, never blocked**; a one-time legal disclaimer on first launch plus a
  Settings "suppress all warnings" master toggle. (Full capability is always retained.)
- **Macro recorder & playback** with timing capture and variable substitution.
- **Tamper-evident audit trail** (SHA-256 hash chain) over flashes and serial commands.

### 🔗 Coordinate
- **Shared target pool** across every connected device — one board discovers an AP, another deauths
  it, another sniffs the handshake, all from one screen.
- **Event bus** + **auto-routing rules** for inter-device automation.

## Supported Firmwares

| Firmware | Upstream | Chips | Backend |
|----------|----------|-------|---------|
| **ESP32 Marauder** | [justcallmekoko/ESP32Marauder](https://github.com/justcallmekoko/ESP32Marauder) | ESP32 / S2 / S3 / C5 | esptool |
| **Bruce** | [pr3y/Bruce](https://github.com/pr3y/Bruce) | ESP32 / S3 / C5 | esptool (merged) |
| **GhostESP** | [GhostESP-Revival/GhostESP](https://github.com/GhostESP-Revival/GhostESP) | ESP32 / S2 / S3 / C-series | esptool |
| **HaleHound** | [JesseCHale/HaleHound-CYD](https://github.com/JesseCHale/HaleHound-CYD) | ESP32 (CYD) | esptool |
| **ESP32-DIV** | [cifertech/ESP32-DIV](https://github.com/cifertech/ESP32-DIV) | ESP32-S3 (v2) / ESP32 (v1.1.0 legacy) | esptool |
| **MinigotchiV3** | [dj1ch/minigotchi-ESP32](https://github.com/dj1ch/minigotchi-ESP32) | ESP32 (dual-core) | esptool |
| **Meshtastic** | [meshtastic/firmware](https://github.com/meshtastic/firmware) | ESP32-S3 / Heltec | esptool |
| **Flock-You** | [colonelpanichacks/flock-you](https://github.com/colonelpanichacks/flock-you) | ESP32-S3 | esptool |
| **OUI-Spy** | [colonelpanichacks/oui-spy](https://github.com/colonelpanichacks/oui-spy) | ESP32-S3 | esptool |
| **Sky-Spy** (drone RemoteID) | [colonelpanichacks/Sky-Spy](https://github.com/colonelpanichacks/Sky-Spy) | ESP32-S3 / C6 | esptool |
| **AirTag Scanner** | [MatthewKuKanich/ESP32-AirTag-Scanner](https://github.com/MatthewKuKanich/ESP32-AirTag-Scanner) | ESP32 / S3 | esptool |
| **BW16 Vampire Deauther** | [vampel](https://github.com/vampel/vampel.github.io) · [RTL8720dn-Deauther](https://github.com/tesa-klebeband/RTL8720dn-Deauther) | **RTL8720DN** (AmebaD, **dual-band 2.4/5 GHz** + BLE) | **rtl8720** |
| **Flipper Momentum** | [Next-Flip/Momentum-Firmware](https://github.com/Next-Flip/Momentum-Firmware) | STM32WB55 | qFlipper |
| **Flipper Unleashed** | [DarkFlippers/unleashed-firmware](https://github.com/DarkFlippers/unleashed-firmware) | STM32WB55 | qFlipper |
| **RayHunter** (IMSI-catcher detect) | [EFForg/rayhunter](https://github.com/EFForg/rayhunter) | Orbic RC400L | ADB |
| **Pwnagotchi** | [jayofelony/pwnagotchi](https://github.com/jayofelony/pwnagotchi) | Raspberry Pi | SD image |
| **RaspyJack** | [7h30th3r0n3/RaspyJack](https://github.com/7h30th3r0n3/RaspyJack) | Raspberry Pi | SD image |
| **Kali ARM** | [kali.download](https://kali.download) | Raspberry Pi | SD image |
| **Custom / local .bin** | — | any ESP32 | esptool |

> Each profile tracks its **latest upstream release** at flash time and auto-selects the correct
> per-board binary. Flash parameters follow the proven lineage — see the offset table below.

## Supported Hardware

### ESP32 boards
| Board | Chip | Notes |
|-------|------|-------|
| Lonely Binary ESP32 Gold | ESP32-WROOM-32E | Marauder / Flock / BLE scan |
| Cheap Yellow Display (2.4″/2.8″/3.2″/3.5″) | ESP32 | Marauder GUI, HaleHound, Bruce — use the **resistive** 2.8″ `2432S028R` |
| Waveshare ESP32-C5 | ESP32-C5 | Dual-band 2.4 + 5 GHz WiFi 6 (bootloader `0x2000`) |
| M5Stack Cardputer / Cardputer ADV | ESP32-S3 | Bruce, Marauder, NEMO |
| M5StickC Plus2 | ESP32-PICO-V3 | Bruce, Marauder, NEMO |
| LilyGo T-Embed CC1101 / T-Deck / T-Dongle-S3 | ESP32-S3 | Bruce, Marauder, Meshtastic |
| Flipper Zero WiFi Dev Board | ESP32-S2 | Marauder `flipper`, FlipperHTTP |
| Marauder Mini / Mini v3 (C5) | ESP32 / ESP32-C5 | Official Koko hardware |
| Heltec LoRa V3 | ESP32-S3 | Meshtastic (915 MHz US) |

### Other devices
| Device | Role |
|--------|------|
| Raspberry Pi 5 / Pi Zero 2 W | Central brain · Pwnagotchi · Kali |
| Flipper Zero | Sub-GHz / RFID / NFC (qFlipper backend) |
| Panda PAU0F WiFi 6E | Kismet primary adapter |
| Orbic RC400L | RayHunter IMSI-catcher detector (ADB) |
| VK-162 USB GPS | Shared GPS via gpsd |

> Full board buyer's guide with purchase links, firmware→binary mapping, and flash instructions
> lives at **[esp32marauder.com/builds.html](https://esp32marauder.com/builds.html)**.

### Flash-offset reference (the part that bricks boards if you get it wrong)
| Chip family | bootloader | partitions | boot_app0 | app |
|-------------|-----------|-----------|-----------|-----|
| ESP32, ESP32-S2 | `0x1000` | `0x8000` | `0xE000` | `0x10000` |
| ESP32-S3, C2, C3, C6, H2 | `0x0` | `0x8000` | `0xE000` | `0x10000` |
| **ESP32-C5, P4** | **`0x2000`** | `0x8000` | `0xE000` | `0x10000` |

Merged single-image firmwares (e.g. Bruce) flash at `0x0`. The engine never hardcodes the chip — it
runs `esptool chip_id` first.

## UI Modes

| Mode | Framework | Use case |
|------|-----------|----------|
| Full Dashboard | PyQt5 | Primary — 7″ touchscreen, all features |
| Lightweight | Tkinter | Low-resource ARM systems |
| TUI | Textual | SSH / headless |
| Web Remote | Flash + SocketIO | Phone control of a headless Pi |

## Security

Cyber Controller drives real RF-attack and flashing hardware, so the codebase is hardened to match:

- **Authenticated web remote** — the SocketIO layer rejects unauthenticated sockets and validates a
  per-session CSRF/connection token; the web UI binds **`127.0.0.1` by default** (LAN exposure is an
  explicit opt-in, TLS-encouraged); no usable default credentials (a strong one-time password is
  generated if `CC_WEB_PASS` is unset); constant-time credential checks; CORS allowlist; CSRF +
  per-IP rate limiting; strict security headers; XSS-safe rendering of over-the-air scan data.
- **Supply-chain hardening** — firmware downloads are pinned to an **HTTPS GitHub host allowlist with
  redirect validation (SSRF-safe)**, path-traversal-guarded, size-capped, and support **SHA-256
  integrity pinning**; bundle flashing is TOCTOU-safe with per-file SHA-256 verification.
- **Authenticated encryption** — session storage is **AES-256-GCM (scrypt KDF)** and **fails closed**
  (no unauthenticated fallback).
- **Command-injection defenses** — serial writes reject embedded control characters and the
  auto-router uses safe fixed-placeholder substitution (never `str.format`) on attacker-influenced
  SSID/MAC values.
- **Tamper-evident audit trail** over flash and serial-command actions.

> Authorized security testing, education, and CTF use only — see
> [esp32marauder.com/disclaimer](https://esp32marauder.com/disclaimer.html).

## Quick Start

```bash
# Install (Python 3.12+)
pip install -e .

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
`CC_WEB_CERT` / `CC_WEB_KEY` for TLS.

## Building

```bash
python build.py        # PyInstaller single-file executable in dist/
```

## Development Roadmap

### Phase 1 — Core ✅
- [x] Architecture, offline Firmware Vault, device health, hot-plug manager
- [x] Macro recorder & playback, tamper-evident audit trail
- [x] Hardware-validated flash core (chip detect, anti-brick `--flash_size detect`, C5 `0x2000`)
- [x] Real ADB / SD-image backends, backup + restore, batch flash

### Phase 2 — Intelligence
- [x] Protocol parsers (Marauder, GhostESP, Bruce, Flipper, HaleHound, Meshtastic, **ESP32-DIV, BW16**) + registry
- [x] Shared target pool (APs + **BLE / SubGHz / NFC / rogue-AP**) + cross-comm UI
- [x] **Per-device firmware selector** (any firmware feeds the AutoRouter, not just Marauder)
- [x] **BW16 / RTL8720DN AmebaD flash backend** — HW-validated end-to-end
- [x] **Safety / disclaimer layer** (labels & confirms dangerous TX, never blocks; suppressible)
- [x] Encrypted session storage (AES-256-GCM)
- [ ] Target dossier panel · network topology graph · mission planner · duress mode

### Phase 3 — Orchestration
- [x] Headless web remote (hardened) · settings persistence
- [ ] Attack chain builder · trigger/event system · scheduled task engine

### Phase 4 — Extended
- [ ] Signal heatmap · RF waterfall · PCAP pipeline · recon bridge · mesh relay · plugin system

## Suicide Marauder Integration

[Suicide Marauder](https://github.com/LxveAce/Suicide-Marauder) ships as a git submodule for
owner-only anti-forensic provisioning: a PBKDF2-HMAC-SHA256 boot-password gate, 2-fail automatic wipe,
GPIO dead-man switch, and eFuse + Flash Encryption (T2). Set the password & duress config straight from
the controller — **`cyber-controller --suicide-setup`** (interactive) or **Tools ▸ Suicide Marauder
Setup** in the Qt UI — which hashes the password **host-side** (PBKDF2, zeroized, never stored, never on
argv) and bakes the `guardcfg` bundle. Bundles flash through the controller with **TOCTOU-safe per-file
SHA-256 verification** (no unverified anti-forensic build is ever written).

The on-trigger wipe is **hardware-validated** to obliterate the *entire* flash — bootloader, partition
table, the full running app, NVS/SPIFFS/logs, and the SD card — with a forensic random-overwrite pass,
leaving an all-0xFF chip with no trace (the running app self-erases via a ROM-SPI bypass inside the IDF
flash-only critical section; recoverable only by the owner over UART on T1).

## Ecosystem

| Project | What |
|---------|------|
| [headless-marauder-gui](https://github.com/LxveAce/headless-marauder-gui) | Standalone Marauder controller + flasher (4 UIs) |
| [universal-flasher](https://github.com/LxveAce/universal-flasher) | Multi-firmware flasher + device manager |
| [Suicide-Marauder](https://github.com/LxveAce/Suicide-Marauder) | Anti-forensic firmware provisioner |

## Contributing

Issues and PRs welcome. Run `python -m pytest` before submitting.

## License

MIT — Copyright © 2026 [LxveAce](https://github.com/LxveAce)

## Links

[Portfolio](https://lxveace.com) · [ESP32 Marauder Tools](https://esp32marauder.com) · [GitHub](https://github.com/LxveAce)
