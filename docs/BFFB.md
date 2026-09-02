# BFFB — Flipper Zero expansion board

The BFFB is a Flipper Zero expansion module that runs ESP32 Marauder. This is how it fits with Cyber Controller.

## What's on the board

Grounded against the [Marauder wiki (BFFB page)](https://github.com/justcallmekoko/ESP32Marauder/wiki/bffb):

- **ESP32-C5** (dual-band 2.4 + 5 GHz) with onboard **GPS** and a **microSD** slot — this is the part you flash with Marauder.
- **Dual CC1101** sub-GHz radios — one tuned for ~400 MHz, one for ~900 MHz.
- **Ebyte NRF24** module (up to 500 mW).
- Status LED (blue = scan/sniff, red = transmit) and module switches on the front-left.
- **Five SMA antennas**, left to right: `2.4 GHz · 400 MHz · 900 MHz · GPS · 2.4 GHz`.

It runs the Marauder "Dev Board Pro" distribution and is driven from the Flipper Zero over the GPIO header.

## Flashing the ESP32-C5 with Cyber Controller

In CC, the BFFB shows up under **ESP32 Marauder** as its own board — **"BFFB (Flipper Zero expansion, Dev Board Pro)"**, chip **ESP32-C5**. That's the only chip you flash here; the CC1101s and NRF24 are peripherals, not separately-flashed MCUs.

1. Connect the board's ESP32-C5 over USB (or via the Flipper's GPIO flash path — see below).
2. Device ▸ Firmware ▸ **ESP32 Marauder** ▸ board **BFFB** (or any ESP32-C5 entry).
3. Flash. CC pulls the latest Marauder release, picks the C5 image, and applies the correct ESP32-C5 offsets automatically — including the **`0x2000` bootloader** offset that trips up generic flashers.

CC verifies the download by SHA-256 and never vendors the binary.

## Driving it: what CC does vs. what the Flipper does

Be clear on the split — it's not all one control path:

- **Cyber Controller (USB serial → the C5's Marauder):** Wi-Fi and BLE scan / sniff / attack, plus **GPS** (`gpsdata`, `nmea`, `gps -g`, `gpspoi`, `gpstracker`, `wardrive`). This is the standard Marauder serial CLI, already wired in CC's Marauder control surface.
- **Flipper Zero (GPIO → the board):** the **WiFi Marauder** app (by 0xchocolate, prebuilt into Momentum / Unleashed / RogueMaster CFW) drives the board from the Flipper UI. The BFFB's **CC1101 sub-GHz (400/900 MHz)** and **NRF24** radios are operated on the Flipper side. That's the intended path for now — you control the sub-GHz side through the Flipper.

> **Direct-from-CC control (future):** driving the CC1101/NRF directly from CC over USB is possible in principle — the standard Marauder USB CLI just doesn't expose those radios today. The likely route is a **simple adapter PCB** the BFFB headers plug into to break the board out to USB, so CC can talk to it without the Flipper in the loop. To be designed later; for now, use the Flipper for the sub-GHz side.

## Using it with the Flipper Zero

From the [Marauder wiki (Flipper Zero page)](https://github.com/justcallmekoko/ESP32Marauder/wiki/Flipper-Zero):

1. **Install a compatible Flipper CFW** — **Momentum** (recommended), Unleashed, Xtreme, or RogueMaster. Each ships the **WiFi Marauder** app prebuilt, so no manual `.fap` copy is needed.
   - If you run stock or a firmware without it, drop the `wifi_marauder.fap` into `apps/GPIO/` on the Flipper SD; the app then appears under **Apps ▸ GPIO ▸ [ESP32] WiFi Marauder**. App logs/pcaps land under `apps_data/wifi_marauder/` on the SD.
2. **Seat the board** on the Flipper's top GPIO header.
3. Open **WiFi Marauder** on the Flipper and run scans/attacks; the board's onboard antennas and module switches select the active radio.

### Flashing the ESP32 through the Flipper

Besides CC over USB, the wiki lists these paths (use whichever you have): the **ESP Flasher** Flipper app over GPIO, the **FZEasyMarauderFlash** / **FZ Marauder Flasher** web installers, the Spacehuhn Web Installer, Arduino IDE, or dropping the image on the microSD.

## Antenna safety

The 400/900 MHz CC1101 and the NRF24 can transmit. Transmitting on those bands is regulated — keep it to authorized, lab-only use. CC flashes firmware and drives the Wi-Fi/BLE/GPS side; it authors no RF transmission.
