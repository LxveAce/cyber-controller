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

> ⚠️ **Authorized, lawful use only.** A security-research tool — use it only on systems you own or have explicit permission to test. Provided as-is, no warranty; you assume all risk. See [DISCLAIMER.md](DISCLAIMER.md).

<!-- STATUS-ROADMAP:START -->
## Status & Roadmap

**Status:** **v1.4.0** is the latest release — **smart installation / version-aware startup**: on launch
the app reconciles its config against the running version, carries an upgrade forward silently, and on a
downgrade/overwrite prompts to **Keep & Continue** or **Back up & Start Fresh** (the old config is moved
aside, never deleted). It builds on a run of installer/UX releases (v1.3.1–v1.3.3): an **animated startup**
loading screen, a **four-interface launcher**, and a **splash screen** for the slow onefile self-extract —
all on top of the **v1.3.0** security-hardening base (secure container, brute-force lockout, duress
self-wipe, dual-depth Simple/Pro, +4 firmwares).

**Shipped in v1.3.1 – v1.4.0:**
- **Smart installation / version-aware startup (v1.4.0)** — the app recognizes a previous install and
  reconciles it: an **upgrade** carries settings + the encrypted vault forward silently; a **downgrade**
  (an older build over a newer config — the "paths collide / overwrite" case) prompts **Keep & Continue**
  or **Back up & Start Fresh**, and the old config is always **moved aside, never deleted** (restorable).
  A `.installed_version` marker is written; fully silent for headless/CLI use. Single source of truth for
  the version in `src/version.py`.
- **Animated startup (v1.3.3)** — a frameless loading screen (logo + indeterminate progress + status) while
  the dashboard builds, then a cross-fade to the main window. Motion-token-driven; honors reduced-motion.
  Reserved for the full PyQt5 GUI; the lightweight Tk/TUI/web UIs stay unanimated.
- **Four-interface launcher (v1.3.2)** — launching with no `--ui` now offers all four front-ends
  (**Full GUI (PyQt5) · Lightweight (Tkinter) · Terminal UI (Textual) · Web Remote**); the splash closes
  before the launcher, and **Web Remote auto-opens your browser** from a packaged build.
- **Installer splash screen (v1.3.1)** — the Windows onefile shows a splash within ~1–2 s of launch (the
  ~15 s self-extraction previously gave no feedback and read as a failed install), plus a MinigotchiV3
  profile fix.

**Shipped in v1.3.0:**
- **Secure container (opt-in)** — app-internal saves (e.g. recorded command sessions) encrypted at rest
  (AES-256-GCM) in a gate-keyed container that is **sealed/unreadable while the access gate is locked**;
  ciphertext-only writes (no transient plaintext), tamper fails closed. Toggle in **Settings ▸ Secure Container**.
- **Brute-force lockout** on the access gate — a persistent failed-attempt counter (survives restart)
  with exponential-backoff cooldown, constant-time password compare.
- **Duress self-wipe (opt-in, off by default)** — after N consecutive failed unlocks the app securely
  wipes its own footprint (vault, keys, config, container). Honest scope: defeats casual/seizure access,
  not a forensic lab on wear-leveled SSDs.
- **Boot / startup-bypass hardening** — modifying an already-configured gate (clear / change password /
  policy / add key) now requires passing the gate first; the gate is enforced before any UI bootstrap.
- **Dual-depth Simple/Pro interface** — a streamlined Simple view (fewer controls) and the full Pro view
  (default, zero penalty). Switch via **View ▸ Interface Mode**, the status-bar badge, or **Ctrl+M**.
- **4 new firmware profiles** — **T-REX** (LilyGo T-Deck pentest terminal), **MCLite** (MeshCore off-grid
  comms), **ESP32 Bit Pirate**, and **Hydra32 / ESP32-Deauther** (SHA-256-pinned) — all drop-in JSON.
- **esptool range guard** — a clear message if an out-of-range esptool (v6+) is installed, instead of a
  cryptic argparse failure mid-flash.

**Previously shipped (v1.2.1):**
- **Unified flashing in one app, two clearly separate tabs** — a **Firmware tab** for hardware (ESP32 Marauder / GhostESP / Bruce / etc. plus Raspberry Pi SD images) and a **Software (OS) tab** for PC/USB operating systems.
- **Software (OS) tab** — flash verified **Kali Linux, Tails OS, Arch** to USB, with the latest version auto-resolved (and an offline bundled fallback), **SHA-256 + OpenPGP verified** before writing.
- **Auto-updating firmware/OS catalog** so versions are always current, **plus full offline use** — a cached catalog and already-downloaded images flash with no internet; a weekly CI job keeps the bundled OS catalog current; the app also self-updates.
- **In-app tooltips on every control** and a thorough **How-To / tutorial tab**.
- **Wardriving** (Cyber Controller only — the all-in-one controller is also the main GUI for cyberdecks): GPS-tagged Wi-Fi capture exported to **WiGLE CSV**, for **lawful, owner-authorized** use. (The companion **universal-flasher** stays strictly a flasher — firmware + software tabs only, no controller / logger / wardriving.)
- **In-app Access-Gate setup** — provision the admin password / physical USB key / policy straight from **Settings ▸ Access Gate** in the GUI (no longer CLI-only), backed by salted-scrypt verifiers + an encrypted vault. Requires an admin password and/or the physical USB key be present before the app unlocks (policy configurable: password-only, key-only, or both), covering both the desktop and web entry points.
- **Windows one-click `.exe` startup crash fixed and verified** — bundled-asset loading hardened so the packaged GUI starts cleanly on a clean Windows host; a missing bundled resource now degrades gracefully instead of failing.

**Roadmap:**
- Windows **installer** that registers in Add/Remove Programs — **shipped** (`installer/`, built in CI);
  **code-signing** (OV/EV cert) is the remaining step to retire the SmartScreen prompt for good.
<!-- STATUS-ROADMAP:END -->


## Owner access gate & Tails flashing

**Physical-key access gate** — optionally require an admin password and/or a provisioned USB key to
open the app (fail-closed; OFF by default). Owner-only defensive use on hardware you own. As of
**v1.2.1** the whole gate can be set up from the GUI in **Settings ▸ Access Gate** (set the admin
password / physical USB key / policy) — the CLI flags below remain available too:
- `cyber-controller --set-admin-password` — set the admin password.
- `cyber-controller --create-physical-key [--key-drive <USB>]` — provision a USB stick as an unlock key.
- `cyber-controller --gate-policy {both|either|password|key}` — set the policy (default `both` = AND).
- `cyber-controller --gate-status` · `--clear-gate`.

The app then prompts before launching (a Qt dialog in the GUI; console otherwise). The password and
the key secret are stored only as salted **scrypt verifiers** — never in plaintext. This deters
casual access; it is not proof against an adversary who can image the disk/USB.

**Hardening (v1.3.0):** the gate is enforced **before any UI/device bootstrap** and **fails closed**
(an encrypted vault with the gate config removed refuses to start). Failed unlocks are rate-limited
with a **persistent, exponential-backoff lockout**; modifying an already-configured gate requires
**passing it first** (no pre-auth reset). An **opt-in duress self-wipe** can destroy the app's own
footprint after N failed attempts (off by default), and an opt-in **secure container** keeps app saves
encrypted at rest and sealed while locked. See [`SECURITY.md`](SECURITY.md) for the full posture and an
honest statement of what these guarantees do and don't cover.

**Flash Tails OS (amnesiac live USB)** — write the official Tails USB image to a removable USB:
- `cyber-controller --flash-tails --tails-image <tails-amd64-*.img> [--tails-sha256 <hex>] [--tails-sig <file>] [--target <device>]`

`.img` only (an `.iso` is the wrong file). The image's **SHA-256 is checked** against the official
checksum; if `gpg` is present, the **detached signature is verified** against the Tails signing key.
It writes only to a **removable, confirmed** device (the whole USB is erased) and verifies the write
by reading it back.


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

> 📚 **[Hardware Guides →](https://github.com/LxveAce/cyber-controller-guides)** — an in-depth, per-firmware
> walkthrough for every entry below: **what to buy, how to build it, how to flash & run it, how to
> integrate it into Cyber Controller, and troubleshooting** — each with a downloadable PDF.

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
| **T-REX** (LilyGo T-Deck pentest terminal) | [abdallahnatsheh/T-REX-FIRMWARE](https://github.com/abdallahnatsheh/T-REX-FIRMWARE) | ESP32-S3 (T-Deck / T-Deck Plus) | esptool (merged) |
| **MCLite** (MeshCore off-grid comms) | [laserir/MCLite](https://github.com/laserir/MCLite) | ESP32-S3 (T-Deck Plus / T-Watch Ultra LoRa) | esptool (merged) |
| **ESP32 Bit Pirate** | [geo-tp/ESP32-Bit-Pirate](https://github.com/geo-tp/ESP32-Bit-Pirate) | ESP32-S3 (Xiao / Cardputer / T-Embed) | esptool (merged) |
| **Hydra32 / ESP32-Deauther** ⚠ *authorized testing only* | [SameerAlSahab/ESP32-Deauther](https://github.com/SameerAlSahab/ESP32-Deauther) | ESP32 (DevKit V1) | esptool (SHA-256-pinned) |
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

**Dual-depth (Simple / Pro):** within the Qt dashboard, an interface mode toggles between a streamlined
**Simple** view (fewer controls per tab — great to start) and the full **Pro** view (default, every
control). Switch via **View ▸ Interface Mode**, the status-bar badge, or **Ctrl+M**; the choice
persists. Pro has zero feature penalty, and safety/authorization prompts show in **both** modes.

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
python build.py            # PyInstaller single-file executable in dist/
python build.py --onedir   # folder build (instant startup) — what the Windows installer packages
```

CI (`.github/workflows/build-release.yml`) builds Windows, Linux, ARM, and macOS executables on tag,
publishes a `.sha256` next to each one, and (best-effort) compiles the Windows installer
([`installer/`](installer/), Inno Setup) — which registers the app under **Settings → Apps → Installed
apps** with an uninstaller, instead of a loose `.exe`.

**Downloading on Windows?** The build isn't code-signed yet, so SmartScreen may warn and a couple of AV
engines may show a heuristic false positive. That's expected for an unsigned PyInstaller build —
[**docs/WINDOWS-SECURITY.md**](docs/WINDOWS-SECURITY.md) explains why and gives three ways to verify the
download yourself (SHA-256, VirusTotal, build from source).

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

## Credits

Cyber Controller flashes, drives, and coordinates firmware and tools it did not write. It
stands on the work of many upstream firmware authors, flashing-tool maintainers, OS
projects, and Python library developers — none of whom endorse this project. See
[CREDITS.md](CREDITS.md) for full acknowledgments and licenses. Nothing upstream is
vendored or redistributed here; binaries are fetched from their official releases, pinned
and SHA-256 verified. Trademarks and copyrights belong to their respective owners.

## License

MIT — Copyright © 2026 [LxveAce](https://github.com/LxveAce). See [LICENSE](LICENSE).

## Connect

- **Discord:** [discord.gg/lxveace](https://discord.gg/lxveace) — questions, help, or to talk through this project
- **GitHub:** [@LxveAce](https://github.com/LxveAce)
- **Website:** [lxveace.com](https://lxveace.com)
- **Project site:** [cybercontroller.org](https://cybercontroller.org)
