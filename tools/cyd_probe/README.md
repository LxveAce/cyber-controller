# CYD detection probe

A tiny ESP32 firmware that identifies which **CYD** (Cheap Yellow Display) board is connected, by
reading the display controller over SPI and the touch bus over I2C. The Flash tab's **Detect board**
button flashes this, reads its serial report, and auto-selects the matching Marauder variant — so the
screen isn't left blank (wrong build) or mirrored/"oriented weird" (ILI9341 build on an ST7789 panel).

CYD boards share the plain-ESP32 chip, so esptool/the bootloader can't tell them apart. Only code
running on the board and reading the panel can.

## What it reports

Over serial at 115200, in repeating blocks:

```
=====CYD_PROBE=====
CYD=yes CONF=high CONTROLLER=ST7789 TOUCH=resistive
VARIANT=cyd_2432S028_2usb
D3=0x00000000 04=0x00000000 09=0x00610000 alive=1 cap_i2c=0x00 LDR=2368
=====END=====
```

`src/core/cyd_detect.py` parses this.

## Detection decision tree

Grounded in measured CYD register values (see `ropg/spi_lcd_read`):

| Signal | Meaning | Variant |
|---|---|---|
| `0xD3` read-ID → `93 41` | ILI9341, 240x320 | `cyd_2432S028` (2.8") |
| `0xD3` → `77 96` | ST7796, 320x480 | `cyd_3_5_inch` (3.5") |
| `0xD3` all-zero, `0x04` → `85..` | ST7789 2-USB | `cyd_2432S028_2usb` |
| `0xD3` all-zero, `0x04` → `81..` / capacitive I2C | ST7789 Guition | `cyd_2432S024_guition` |

Many ST7789 CYD clones return zero for **all** ID registers, so the 2-USB/Guition split falls back to
touch type: capacitive I2C controller present (CST820/GT911/FT6x36) → Guition; else resistive → 2-USB.

**Is it even a CYD?** A bare ESP32 with no display also reads `0xD3 = 0`. To avoid a false positive the
probe requires a positive panel signal: the `0x09` status register answers stably with a non-`00`/`FF`
byte (a floating MISO reads the rails), and the LDR divider on GPIO34 reads mid-range (a floating pin
reads at a rail). No panel + no LDR ⇒ `CYD=no`.

## Build & bundle

Requires PlatformIO. From this directory:

```sh
pio run                                    # builds .pio/build/esp32dev/{bootloader,partitions,firmware}.bin
```

Merge into the single flashable image the app ships (offset 0x0), from the repo root:

```sh
BA=~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin
B=tools/cyd_probe/.pio/build/esp32dev
python -m esptool --chip esp32 merge-bin -o src/config/probes/cyd_probe.bin \
  --flash-mode dio --flash-freq 40m --flash-size 4MB \
  0x1000 "$B/bootloader.bin" 0x8000 "$B/partitions.bin" 0xe000 "$BA" 0x10000 "$B/firmware.bin"
```

`build.py` bundles `src/config/probes/` into the installer.
