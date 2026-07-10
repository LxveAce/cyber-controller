# Credits & Acknowledgments

Cyber Controller is a controller and flasher — it writes, drives, and coordinates
hardware, but it does not author the firmware that hardware runs. Every device profile,
flash backend, parser, and feature in this project stands on the work of upstream
firmware authors, tool maintainers, OS projects, and library developers who built the
hard parts first. This file exists to thank them, by name, with gratitude.

**None of the projects, people, or organizations listed below endorse, sponsor, or are
affiliated with Cyber Controller.** Cyber Controller is an independent, self-taught hobby
project by the author (LxveAce). All firmware, tools, distributions, trademarks, logos,
and copyrights named here belong to their respective owners. Firmware and OS binaries are
fetched from each project's own official releases at flash time, pinned and SHA-256
verified — they are **never vendored, modified, or redistributed by this repository**.

Licenses below were checked against each project's published license metadata where
possible. Anything that could not be confirmed is marked **`verify:`** rather than
asserted — if you are the author and a marking is wrong, please tell us (see
[Notes](#notes)) and it will be corrected.

---

## Firmware projects (flashed / controlled)

These are the upstream firmwares Cyber Controller can flash and/or talk to. Cyber
Controller pulls each project's official release binaries at flash time; it does not
build, bundle, or alter them.

### ESP32 family (esptool / rtl8720 backends)

| Firmware | Author / Org | Upstream | License |
|----------|-------------|----------|---------|
| ESP32 Marauder | justcallmekoko (JustCallMeKoko) | https://github.com/justcallmekoko/ESP32Marauder | `verify:` (no LICENSE file detected in repo) |
| Bruce | BruceDevices / Pr3y & contributors | https://github.com/BruceDevices/firmware | AGPL-3.0 |
| GhostESP | GhostESP-Revival project | https://github.com/GhostESP-Revival/GhostESP | GPL-3.0 |
| HaleHound (CYD) | Jesse C. Hale (JesseCHale) | https://github.com/JesseCHale/HaleHound-CYD | `verify:` (custom / unrecognized LICENSE in repo) |
| ESP32-DIV | CiferTech | https://github.com/cifertech/ESP32-DIV | MIT |
| MinigotchiV3 | dj1ch | https://github.com/dj1ch/minigotchi-V3 | `verify:` (no LICENSE file detected in repo) |
| Meshtastic firmware | Meshtastic project | https://github.com/meshtastic/firmware | GPL-3.0 |
| Flock-You | colonelpanichacks (Colonel Panic) | https://github.com/colonelpanichacks/flock-you | `verify:` (no LICENSE file detected in repo) |
| OUI-Spy (unified-blue) | colonelpanichacks (Colonel Panic) | https://github.com/colonelpanichacks/oui-spy-unified-blue | `verify:` (no LICENSE file detected in repo) |
| Sky-Spy (drone RemoteID) | colonelpanichacks (Colonel Panic) | https://github.com/colonelpanichacks/Sky-Spy | `verify:` (no LICENSE file detected in repo) |
| AirTag Scanner | Matthew KuKanich (MatthewKuKanich) | https://github.com/MatthewKuKanich/ESP32-AirTag-Scanner | `verify:` (no LICENSE file detected in repo) |
| Chasing Your Tail NG | Argelius Labs | https://github.com/ArgeliusLabs/Chasing-Your-Tail-NG | MIT |
| T-REX firmware | Abdallah Natsheh (abdallahnatsheh) | https://github.com/abdallahnatsheh/T-REX-FIRMWARE | AGPL-3.0 |
| MCLite (MeshCore fork) | laserir | https://github.com/laserir/MCLite | MIT |
| ESP32 Bit Pirate | geo-tp | https://github.com/geo-tp/ESP32-Bit-Pirate | MIT |
| Hydra32 / ESP32-Deauther | SameerAlSahab | https://github.com/SameerAlSahab/ESP32-Deauther | GPL-3.0 |

### RTL8720DN / BW16 (AmebaD, rtl8720 backend)

| Firmware | Author / Org | Upstream | License |
|----------|-------------|----------|---------|
| BW16 Vampire Deauther (pre-built bundles flashed by CC) | vampel | https://github.com/vampel/vampel.github.io | MIT (repo); firmware binaries are **pre-compiled** |
| RTL8720dn-Deauther (the upstream deauther codebase referenced by CC) | tesa-klebeband | https://github.com/tesa-klebeband/RTL8720dn-Deauther | GPL-3.0 |

### Flipper Zero (qFlipper backend)

| Firmware | Author / Org | Upstream | License |
|----------|-------------|----------|---------|
| Momentum Firmware | Next-Flip / Momentum team | https://github.com/Next-Flip/Momentum-Firmware | GPL-3.0 |
| Unleashed Firmware | DarkFlippers | https://github.com/DarkFlippers/unleashed-firmware | GPL-3.0 |

### Single-board computers & devices (SD-image / ADB backends)

| Firmware / Image | Author / Org | Upstream | License |
|------------------|-------------|----------|---------|
| Pwnagotchi (maintained fork) | jayofelony (Jayofelony) | https://github.com/jayofelony/pwnagotchi | `verify:` (custom / unrecognized LICENSE in repo; pwnagotchi lineage is GPL-3.0) |
| RaspyJack | 7h30th3r0n3 | https://github.com/7h30th3r0n3/RaspyJack | MIT |
| RayHunter (IMSI-catcher detector) | Electronic Frontier Foundation (EFF) | https://github.com/EFForg/rayhunter | GPL-3.0 |
| Kali Linux ARM images | OffSec (Offensive Security) | https://www.kali.org/get-kali/ · https://kali.download/arm-images/ | distribution of many packages under their own licenses; "Kali Linux" is a trademark of OffSec |

### Closed-source / pre-compiled firmware

These ship as vendor binaries only — there is no open build to compile from. Cyber
Controller fetches the official release binaries (pinned + SHA-256 verified) and never
redistributes them. They are included strictly as flash-and-study targets; CC exposes no
transmit/operate control for them.

| Firmware | Author / Org | Official source | License / status |
|----------|-------------|-----------------|------------------|
| BlueJammer-V2 (ESP32 engine + BW16 controller) | EmenstaNougat (@emensta) | Official site: https://emensta.pages.dev · Repo: https://github.com/EmenstaNougat/BlueJammer-V2 | **closed-source / pre-compiled** (no LICENSE file detected; binaries pinned, never vendored) |
| BW16 Vampire Deauther (binary bundles) | vampel | https://github.com/vampel/vampel.github.io | **pre-compiled** bundles (repo is MIT; the flashed `.bin` images are prebuilt) |

---

## Flashing & tooling

The actual byte-level flashing is done by these tools — Cyber Controller orchestrates
them, it does not reimplement them.

| Tool | Author / Org | Link | License |
|------|-------------|------|---------|
| esptool | Espressif Systems | https://github.com/espressif/esptool | GPL-2.0 |
| Realtek AmebaD ImageTool / `upload_image_tool` | Realtek Semiconductor | https://www.realtek.com (Ameba IoT SDK) | `verify:` (proprietary Realtek SDK tooling) — **provided by the user, not bundled or redistributed by this project** |
| qFlipper | Flipper Devices Inc. | https://github.com/flipperdevices/qFlipper | GPL-3.0 |

---

## Python libraries

Cyber Controller is written in Python and depends on the following open-source libraries
(from `pyproject.toml`). Deep gratitude to their authors and maintainers.

### Runtime dependencies

| Library | Author / Org | Link | License |
|---------|-------------|------|---------|
| PyQt5 | Riverbank Computing | https://www.riverbankcomputing.com/software/pyqt/ | GPL-3.0 (or commercial) |
| pyserial | Chris Liechti | https://github.com/pyserial/pyserial | BSD-3-Clause |
| esptool | Espressif Systems | https://github.com/espressif/esptool | GPL-2.0 |
| requests | Kenneth Reitz & the Python Software Foundation | https://github.com/psf/requests | Apache-2.0 |
| psutil | Giampaolo Rodolà | https://github.com/giampaolo/psutil | BSD-3-Clause |
| cryptography | Python Cryptographic Authority (PyCA) | https://github.com/pyca/cryptography | Apache-2.0 OR BSD-3-Clause |

### Optional / extras dependencies

| Library | Author / Org | Link | License |
|---------|-------------|------|---------|
| Textual (`tui` extra) | Textualize / Will McGugan | https://github.com/Textualize/textual | MIT |
| Flask (`web` extra) | Pallets | https://github.com/pallets/flask | BSD-3-Clause |
| Flask-SocketIO (`web` extra) | Miguel Grinberg | https://github.com/miguelgrinberg/Flask-SocketIO | MIT |
| pytest (`dev` extra) | Holger Krekel & the pytest-dev team | https://github.com/pytest-dev/pytest | MIT |
| Ruff (`dev` extra) | Astral | https://github.com/astral-sh/ruff | MIT |
| setuptools (build) | Python Packaging Authority (PyPA) | https://github.com/pypa/setuptools | MIT |

---

## Operating-system images (Software-OS writer)

The Software (OS) tab can write these bootable operating systems to a removable USB drive.
Each image is downloaded from the project's own official mirror, **SHA-256 + OpenPGP
integrity-checked**, and written **as-is** by the user. Cyber Controller does **not** host,
mirror, repackage, or redistribute any of these — it only verifies and writes what the user
fetches from the upstream project.

| OS image | Author / Org | Official source | License / status |
|----------|-------------|-----------------|------------------|
| Kali Linux | OffSec (Offensive Security) | https://www.kali.org/get-kali/ | Debian-derived distribution; thousands of packages under their own licenses (GPL/BSD/MIT/…). "Kali Linux" is a trademark of OffSec. |
| Tails (The Amnesic Incognito Live System) | The Tails project | https://tails.net/ | `verify:` GPL-3.0-or-later (project code); bundles Debian packages under their own licenses |
| Arch Linux | The Arch Linux project & maintainers | https://archlinux.org/download/ | Distribution of packages under their own licenses; "Arch Linux" name/logo are trademarks |

---

## Community contributions & feedback

Beyond the upstream authors above, Cyber Controller grows from the ideas, feature requests,
and real-device field testing of its community. Named here with thanks:

- **RedneckNetrunner** (GOS Discord) — firmware-coverage requests and hands-on field testing
  that directly shaped the firmware-expansion work:
  - **M5PORKCHOP** and **M5Gotchi** profiles — from his daily-carry firmware list.
  - **Evil-M5** family support (Evil Cardputer / EvilM5Core / EvilM5Project).
  - **GhostESP** and **ESP32 Marauder** on **M5Cardputer / M5StickC** — including flagging that
    Marauder-on-Cardputer had "until recently been extremely rough," and testing GhostESP on his
    own M5StickC and reporting back that it ran fine.
  - **ESP32 Bus Pirate** and **Bruce** raised as daily-carry firmwares to keep well-supported.
  - Proposed **Cardputer-control-from-the-PC** (a qFlipper-style control surface for the Cardputer)
    and championed the **pop-out / focus-in firmware-menu** direction (blowing up the connect/operate
    panels to focus on them) — realized in the aspect-locked Device-View pop-out (**DV1**: the window holds
    the firmware skin's native ratio on resize instead of letterboxing dead-space), its crisp-zoom modes
    (**DV2**: Fit / integer 1×/2×/3× nearest-neighbor / 1:1, so blowing the skin up stays sharp), and
    per-firmware palettes (**DV3**: each skin reads its own colour scheme so Marauder / GhostESP / ESP32-DIV
    look like their real firmware instead of one shared theme — honest reconstructions, not pixel captures),
    and the **Bruce** skin he flagged as daily-carry (**DV4**: a reconstructed Bruce menu whose every leaf is
    a real Bruce serial command, with the argument-taking ones marked so they don't fire a broken command).
  - Proposed **Cardputer control** — groundwork in per-board Device-View sizing (**CP1**: a skin can now be
    shaped to its real board, e.g. Cardputer 240×135 landscape / M5StickC 135×240, instead of a fixed portrait
    frame; the aspect-lock, zoom, and hit-testing all follow the board's resolution), then the **Cardputer
    Remote** itself (**CP2**: that Cardputer-shaped skin plus a raw CLI console — two input lanes that both drive
    the device through the one guarded send path, so a raw line gets the same firmware-match, safety-confirm, and
    write validation as a menu tap).
  - His firmware-menu direction now reaches every frontend equally (**DV-tk**: the reconstructed skins that
    started as the Qt pop-out are now a navigable Device View on the plain-Tkinter build too, driven by the same
    UI-agnostic menu model — leaves fire the real serial command through the same guarded write + safety-confirm,
    so the lightweight GUI has full parity with the Qt/web views).

  These suggestions and test reports made the firmware coverage broader and better grounded in how
  the hardware actually behaves. Thank you.

## Notes

- **No endorsement.** None of the projects, authors, or organizations above endorse,
  sponsor, or are affiliated with Cyber Controller. Their inclusion documents
  interoperability, not partnership.
- **Nothing is vendored or redistributed.** Firmware and OS binaries are fetched from each
  project's official releases at flash time, version-pinned and **SHA-256 verified** before
  writing. This repository ships no upstream firmware binaries, no OS images, and no
  proprietary vendor tools (e.g. Realtek's `upload_image_tool` must be supplied by the user).
- **Trademarks & copyrights** belong to their respective owners. Project names, logos, and
  marks are used here only for identification and attribution.
- **Licenses marked `verify:`** could not be confidently confirmed from published metadata
  at the time of writing and should be checked against the upstream project before you rely
  on them. Corrections are welcome.
- **Attribution changes / removal.** If you are an upstream author and would like your
  project's attribution corrected, expanded, or removed, that will be honored. Please open
  an issue at https://github.com/LxveAce/cyber-controller/issues and it will be addressed.

_With genuine thanks to everyone above — Cyber Controller would not exist without your work._
