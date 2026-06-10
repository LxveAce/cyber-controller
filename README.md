# Cyber Controller

Flagship cyberdeck-oriented all-in-one security hardware controller.

**Flash. Control. Coordinate.** All your security hardware from one dashboard.

## What is this?

Cyber Controller merges the functionality of [Headless Marauder GUI](https://github.com/LxveAce/headless-marauder-gui), [Universal Flasher](https://github.com/LxveAce/universal-flasher), and [Universal Flasher & UI](https://github.com/LxveAce/universal-flasher-ui) into a single unified tool — built for cyberdecks, field deployments, and security research.

## Three Pillars

- **Flash** — 14+ firmware profiles, 4 backends (esptool, ADB, qFlipper, SD image), offline firmware vault, batch flash, OTA fleet push
- **Control** — Protocol-aware serial monitor, macro recorder, attack chain builder, per-firmware command palette
- **Coordinate** — Shared target pool across devices, event bus, auto-routing rules, inter-device communication

## Supported Hardware

### ESP32 Boards
| Board | Firmware | Arming Pin |
|-------|----------|-----------|
| Lonely Binary ESP32 Gold | Marauder, Flock, BLE scan | GPIO27 |
| Waveshare ESP32-C5 | Dual-band Marauder/Scanner | Grove G2 |
| CYD 2.8" Touchscreen | Marauder GUI, HaleHound | GPIO27 |
| ESP32 WROOM-32 | Drone RemoteID | GPIO27 |
| Heltec LoRa V3 | Meshtastic | N/A |

### Other Devices
| Device | Role |
|--------|------|
| Raspberry Pi 5 8GB | Central brain |
| Flipper Zero | Sub-GHz / RFID / NFC |
| Panda PAU0F WiFi 6E | Kismet primary |
| Orbic RC400L | RayHunter IMSI catcher detector |
| VK-162 USB GPS | Shared GPS via gpsd |

### Supported Firmwares (14+)
Marauder, GhostESP, Bruce, HaleHound, Meshtastic, ESP32-DIV, Flock-You, OUI-Spy, Sky-Spy, AirTag Scanner, CYT-NG, MinigotchiV3, Momentum (Flipper), Unleashed (Flipper)

## UI Modes

| Mode | Framework | Use Case |
|------|-----------|----------|
| Full Dashboard | PyQt5 | Primary — 7" touchscreen, full features |
| Lightweight | Tkinter | Low-resource ARM systems |
| TUI | Textual | SSH access, headless servers |
| Web Remote | Flask + SocketIO | Phone control, headless Pi |

## Quick Start

```bash
# Install
pip install -e .

# Run (PyQt5 dashboard)
cyber-controller

# Run lightweight mode
cyber-controller --ui tk

# Run TUI
cyber-controller --ui tui
```

## Building

```bash
python build.py
```

Produces standalone executables in `dist/`.

## Development Roadmap

### Phase 1 — Core
- [x] Project scaffold and architecture
- [ ] Offline Firmware Vault
- [ ] Device Health Dashboard
- [ ] Hot-Plug Device Manager
- [ ] Macro Recorder & Playback
- [ ] Audit Trail with integrity hashing

### Phase 2 — Intelligence
- [ ] Target Dossier Panel
- [ ] Network Topology Graph
- [ ] Mission Planner
- [ ] Encrypted Session Storage
- [ ] Duress / Panic Mode

### Phase 3 — Orchestration
- [ ] Attack Chain Builder
- [ ] Headless Web Remote
- [ ] Trigger / Event System
- [ ] Scheduled Task Engine

### Phase 4 — Extended
- [ ] Live Signal Heatmap
- [ ] RF Spectrum Waterfall
- [ ] PCAP Pipeline
- [ ] Nmap / Recon Bridge
- [ ] Mesh Relay Mode
- [ ] Plugin / Extension System

## Suicide Marauder Integration

Suicide Marauder is included as a git submodule for anti-forensic features:
- Boot password gating (PBKDF2-HMAC-SHA256)
- 2-fail automatic wipe
- GPIO dead-man switch
- eFuse + Flash Encryption (T2 tier)
- Arm/disarm/provision from Cyber Controller dashboard

## License

MIT License — Copyright 2026 LxveAce

## Links

- [Portfolio](https://lxveace.com)
- [ESP32 Marauder Tools](https://esp32marauder.com)
- [GitHub](https://github.com/LxveAce)
