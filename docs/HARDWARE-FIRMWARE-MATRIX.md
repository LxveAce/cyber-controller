# Hardware × Firmware Capability Matrix

The per-board checklist: for **every owned board**, every firmware that *can* run on it, the
flash details, and live-hardware test status. Built from `Projects/INVENTORY.md` ×
`flash_core.PROFILES` × firmware→chip facts, validated with the user's Python 3.12 +
esptool v5.3.0 against the physical fleet. Supersedes `FLASH-TEST-MATRIX.md` (folded in).

Status legend: **✅ Tested** (flashed + booted on real HW this session) · **🟢 Compatible**
(right chip + a real binary exists, not yet bench-flashed) · **⛔ Blocked** (source-only / no
release binary, or needs a board we don't have) · **➖ N/A** (wrong chip/class).

---

## Boot/flash facts captured this session

- **HaleHound** = touch-driven, **no serial CLI** (every command → silence; output is spontaneous `[WIFI]/[BLE]/[SUBGHZ]/[NFC]/[NRF24]/[IOT]/[GUARDIAN]` + boot tags `[INIT]/[TOUCH]/[UTILS]/[INFO]`, plus `HaleHound-CYD`). Integrate as a **sensor/monitor**, not a command target.
- **ESP32-DIV**: S3 migration landed at **v1.5.0** (Jan 2026). Image headers confirm v1.5.0/v1.5.3/v1.6.0 = **ESP32-S3** (Chip ID 9); **v1.1.0 = classic ESP32** (Chip ID 0, the last classic build). The "wrong firmware version" the user saw = an **S3 bin flashed onto a classic ESP32**. v1.1.0 also has **no serial CLI** (touch/button-driven). Classic v1.1.0 is app-only (1.8 MB @ 0x10000) and flashes fine on the standard classic boot chain (bootloader 0x1000 + min_spiffs partitions 0x8000 + boot_app0 0xe000).
- **BW16** = Realtek **RTL8720DN** (boot banner: `hci_read_rom_check`, `[RTL_HalBleMacInit()]`, `[rltk_wlan_statistic]`, `AI_AutoConnectOnboot`; BT MAC 24:42:e3:45:5e:3b). **Not esptool** — needs the Ameba flash toolchain (Realtek backend, in progress).
- **Source-only firmwares** (no release binary the engine can fetch): minigotchi-v3 (release 404), airtag-scanner, sky-spy, cyt-ng, flock-you, oui-spy. These require a local Arduino/PlatformIO build before they can be flashed.

---

## Matrix by board

### 1. Lonely Binary ESP32 Gold ×3 — classic ESP32 (WROOM-32E, 16 MB, CH34x)
| Firmware | Status | Flash detail |
|---|---|---|
| Marauder | ✅ Tested | esp32 `old_hardware`, app @0x10000, boot chain 0x1000/0x8000/0xe000 |
| Bruce | ✅ Tested | merged `Bruce-*` @0x0 (use a headless/generic build; no display) |
| ESP32-DIV v1.1.0 | ✅ Tested | classic app @0x10000 + standard boot chain (peripherals absent on bare board) |
| Minigotchi / AirTag / Sky-Spy / CYT-NG | ⛔ Blocked | source-only / 404 — needs local build |
| GhostESP / DIV v2 / Flock / OUI | ➖ N/A | S3-only |

### 2. ESP32 WROOM-32 dev board — classic ESP32 (CP210x, currently ESP-AT)
Same compatibility as the Gold (Marauder ✅-pattern, Bruce 🟢, DIV v1.1.0 🟢). Note: COM3 is stuck out of download mode — needs a **BOOT-button tap** while resetting to reflash.

### 3. CYD 2.8" 2432S028 ×2 — classic ESP32 + ILI9341 + XPT2046 touch
| Firmware | Status | Flash detail |
|---|---|---|
| Marauder | ✅ Tested | `cyd_2432S028` variant (boots clean; display OK) |
| Bruce | ✅ Tested | `Bruce-CYD-2432S028.bin` @0x0 (display build) |
| HaleHound | ✅ Tested | `HaleHound-CYD-FULL.bin` @0x0 (native CYD target) |
| ESP32-DIV v1.1.0 | 🟢 Compatible | classic + ILI9341 present — pinout may differ from DIV's expected wiring |

### 4. AITRIP 4.0" ST7796 — classic ESP32 (8 MB, 320×480)
| Firmware | Status | Flash detail |
|---|---|---|
| Marauder | ✅ Tested (serial) | booted over serial (`ESP-IDF`/`SD`); **display variant TBD** (ST7796 4" not a standard Marauder variant) |
| Bruce | 🟢 Compatible | try `Bruce-CYD-3248S035*` / elecrow variants; 4" ST7796 panel mapping TBD |
| HaleHound | 🟢 Compatible | 3.5" QDtech variant exists; 4" not native |

### 5. Heltec LoRa V3 — **ESP32-S3** (SX1262, Meshtastic-dedicated)
| Firmware | Status | Flash detail |
|---|---|---|
| Meshtastic | 🟢 Compatible | Heltec V3 factory build (dedicated; don't disturb unless borrowing) |
| GhostESP / DIV v2 / Bruce-S3 / Marauder-S3 / Flock / OUI | 🟢 Compatible | S3 builds — this is the **only S3 board owned**; sharing it blocks Meshtastic |

### 6. Waveshare ESP32-C5 ×2 — ESP32-C5 (WiFi 6, N16R8)
| Firmware | Status | Flash detail |
|---|---|---|
| Marauder-C5 | 🟢 Compatible | **bootloader @0x2000** (C5 gotcha), app @0x10000 |
| Bruce-C5 | 🟢 Compatible | `Bruce-*c5*` merged |
| GhostESP-C5 | 🟢 Compatible | C5 build exists |

### 7. BW16 — Realtek **RTL8720DN** (dual-band 2.4/5 GHz + BLE, CH340)
| Firmware | Status | Flash detail |
|---|---|---|
| Bad-BW16 (deauther) | 🟡 In progress | **Ameba toolchain** (not esptool); Realtek backend being built |
| AmebaZ2 SDK apps / native WiFi scanner | 🟢 Compatible | Ameba image tool; 5 GHz recon is the lawful cyberdeck role |
| Marauder / Bruce / ESP32 firmwares | ➖ N/A | different architecture (Cortex-M4, no ESP32 port) |
| **Antenna note** | — | unpopulated U.FL/IPEX: reposition the 0Ω/solder-jumper from PCB-antenna to U.FL, add IPEX→SMA pigtail |

### 8. Raspberry Pi Zero 2 W — pwnagotchi (SD-image backend)
| Firmware | Status | Flash detail |
|---|---|---|
| Pwnagotchi (jayofelony) | ⛔ Blocked | SD image; **board reported fried by user** — replacement to be ordered. Waveshare 2.13" V4 e-ink needs `ui.display.type=waveshare_v4` |

### 9. Raspberry Pi 5 — SD-image backend
| Firmware | Status |
|---|---|
| RaspyJack / Kali ARM / Pwnagotchi | 🟢 Compatible (SD image; not bench-tested) |

---

## Coverage summary

- **Flash engine proven** on 4 firmware families across classic-ESP32 boards (Marauder, Bruce, HaleHound, DIV v1.1.0) — all hash-verified, all booted, plus a correct negative (GhostESP refused on classic).
- **Untested but compatible:** all **S3** builds (need to share/borrow the Heltec or buy an S3), all **C5** builds (need a C5 connected), 4" ST7796 display variants, Pi SD images.
- **Blocked:** source-only firmwares (need local builds); BW16 (Realtek backend in progress); Pi Zero (hardware fried).
- **Hardware to acquire to unlock coverage:** ≥1 **ESP32-S3 8 MB** (DIV v2 / GhostESP / Flock / OUI without disturbing Meshtastic), an **IPEX→SMA pigtail** for the BW16, a replacement **Pi Zero 2 W**.
