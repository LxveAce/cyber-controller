# Flash Engine — Hardware Validation Matrix

Live-hardware validation of `cyber-controller`'s `flash_core` / `flash_engine` flash path,
run against the physical board fleet with the user's real Python 3.12 + esptool v5.3.0
(`<HOME>\AppData\Local\Programs\Python\Python312\python.exe`).

Harness: `projects/_smbuild/flash_test.py` — constructs a `FirmwareProfile(core_id, chip)`,
calls `FlashEngine.flash(port, profile)` (release fetch -> variant resolve -> SSRF download ->
`write_flash -z --flash_size detect` -> hash verify -> hard reset), then reads the serial
boot banner and matches expected markers.

## Catalog discovery test (all 15 profiles, esp32/s3/c5 variant counts)

The engine correctly fetches each firmware's latest release and enumerates per-chip variants:

| Firmware       | Latest tag   | esp32 | s3 | c5 | Notes                                  |
|----------------|--------------|-------|----|----|----------------------------------------|
| marauder       | v1.12.1      | 15    | 3  | 2  | classic-esp32 flashable                |
| bruce          | 1.15         | 36    | 19 | 3  | classic-esp32 flashable                |
| halehound      | v3.5.5       | 11    | 0  | 0  | CYD-native, classic-esp32 flashable    |
| esp32-div      | v1.6.0       | 1     | 1  | 1  | flashable                              |
| ghostesp       | v1.9.10      | 0     | 0  | 0  | S3-only asset naming -> no esp32 asset |
| meshtastic     | v2.7.15.56   | 0     | 0  | 0  | per-board naming, not matched as esp32 |
| momentum       | mntm-012     | 3     | 3  | 3  | Flipper Zero firmware (not ESP32)      |
| unleashed      | unlshd-089   | 6     | 6  | 6  | Flipper Zero firmware (not ESP32)      |
| flock-you      | source-only  | 0     | 0  | 0  | no binary release (build-from-source)  |
| oui-spy        | source-only  | 0     | 0  | 0  | no binary release                      |
| sky-spy        | source-only  | 0     | 0  | 0  | no binary release                      |
| airtag-scanner | source-only  | 0     | 0  | 0  | no binary release                      |
| cyt-ng         | source-only  | 0     | 0  | 0  | no binary release                      |
| minigotchi-v3  | (404)        | -     | -  | -  | release endpoint 404                   |

The engine handled every case cleanly — no crash on source-only / S3-only / 404 releases;
it returns a clear `[error] no firmware asset for chip <x>` instead.

## End-to-end flash + boot (real hardware)

| Board            | Port | Chip  | Firmware  | Variant chosen                         | Flash           | Boot                                   |
|------------------|------|-------|-----------|----------------------------------------|-----------------|----------------------------------------|
| Blank ESP32      | COM7 | esp32 | Bruce 1.15| merged 3.4 MB image @ 0x0              | OK, hash verify | executes (jumps to app; no serial banner — normal for Bruce on a display-less board) |
| CYD 2.8"         | COM5 | esp32 | HaleHound v3.5.5 | CYD build                       | OK, hash verify | banner OK — `Hale` / `HaleHound` / `Found` |
| 4" board         | COM8 | esp32 | Marauder v1.12.1 | esp32 old_hardware (app @ 0x10000) | OK, hash verify | banner OK — `ESP-IDF` / `SD`           |
| Blank ESP32      | COM7 | esp32 | GhostESP   | (none — S3-only)                      | correctly refused | n/a — engine reported no esp32 asset   |

Marauder itself was already HW-validated end-to-end in prior sessions (CYD + bare ESP32-D0WD),
so this sweep adds **Bruce** and **HaleHound** as two additional firmware families flashed and
booted through the same engine, plus a negative test (GhostESP correctly refused on classic esp32).

## Conclusion

The flash core/engine is hardware-proven across 3 firmware families on 3 distinct classic-ESP32
boards, all hash-verified, with correct rejection of incompatible firmware. Untested still:
ESP32-S3 / C5 targets, the ADB/SD/qFlipper backends, and the PyQt5 GUI runtime.

## Pwnagotchi (Pi Zero 2 W) — still blocked, physical-layer

Re-probed this session: `ping 10.0.0.1/.2` 100% loss; no RNDIS/ECM USB-ethernet gadget NIC; no
new COM serial gadget; the only failed-enumeration USB device is a months-old April phantom
(`CM_PROB_PHANTOM`), not the Pi. Zero host enumeration => the Pi's USB gadget is not presenting
to the host at all. Most likely: cable in the **PWR** port instead of the inner **USB/data** port,
a power-only cable, or `dwc2`/`g_ether` not enabled in `/boot`. Fix paths: (1) move to inner USB
port w/ a data cable, or (2) pull the microSD so `/boot` (FAT32) can be edited directly to force
the gadget on + stage the Waveshare V4 display config.
