# RetroBoot

RetroBoot turns a spare PC, Intel Mac, Raspberry Pi, or Planet Computers Gemini PDA into a dedicated classic Mac for kids, families, and retro enthusiasts—no collector hardware required. Power on, hear a genuine classic Mac startup chime, and the selected Mac takes over the screen. Press **Ctrl+Alt+Delete** on a PC keyboard (or **Windows+Alt+Backspace** on some laptops) at any time to open the local **RetroBoot** panel, or manage everything from a phone or browser on your local network.

[Download RetroBoot 3.4.0](https://mcchord.net/retroboot-downloads/#devices) · [Install](docs/guide/installing.md) · [Families & kids](docs/guide/families-and-kids.md) · [Upgrade to 3.4.0](docs/guide/upgrading.md) · [Mac disk library](docs/guide/disk-library.md) · [Web management](docs/guide/web-management.md) · [How it works](docs/dev/how-it-works.md)

![A cold boot in Proxmox shows the RetroBoot checkerboard, Mac OS 9 startup and Finder](docs/images/retroboot-proxmox-boot.gif)

*RetroBoot cold-booting straight to the Mac OS 9 Finder in Proxmox. Playback is shortened.*

## 3-step quick start

1. **Download:** Pick the image for your PC, Intel Mac, or Raspberry Pi on the [downloads portal](https://mcchord.net/retroboot-downloads/#devices) (or add `https://mcchord.net/retroboot-downloads/rpi-imager.json` in **Raspberry Pi Imager**).
2. **Flash to USB or SD:** Write it with **balenaEtcher**, **Raspberry Pi Imager**, **Fedora Media Writer**, or **Rufus** in **DD Image** mode.
3. **Boot & pick a Mac:** Choose **Kids & Family: Mac OS 9.2.2 Mega Deluxe** (Kid Pix Studio Deluxe, Logical Journey of the Zoombinis & all 146 classic games and apps in Launcher) or **Mac OS 9.2.2 (Classic Clean desktop)**—or expand **Browse all 13 Library Macs**.

## What's in RetroBoot 3.4.0

- **Automatic time zone detection & live transfer speed/ETA (`3.4.0`).** Prefetches and pre-selects your likely IANA time zone over HTTPS right after Wi-Fi/network setup (instead of defaulting to UTC), adds a local **Detect** button, live local clock preview, and Web **Use This Device’s Time Zone** action, shows live speed (`MB/s` / `KB/s`) and remaining time (`left`) across catalog downloads, determinate A/B Software Updates, USB copies, disk archive unpacking, and Web uploads, and displays your connected IP/SSID directly in **Hardware Check**.
- **HDMI & mixerless volume control (`3.3.0`).** Adds ALSA `softvol` (`RetroBoot Playback Volume` over shared `audio`-group `dmix` / `plughw`) for HDMI codecs and USB DACs that lack hardware mixer controls, enables **Volume**, **Mute**, and **Test Sound** across Web **Sound**, **Display & Sound**, and keyboard volume hotkeys, and syncs mute state cleanly when adjusting volume.
- **Physical, network & USB CUPS printing (`3.3.0`).** Routes classic Mac PostScript/PDF print jobs from LPD (`515`), IPP (`631`), or AppSocket/JetDirect (`9100`) to either **`RetroBoot Share/Printed PDFs/`** or any discovered/manual **CUPS** printer (with optional **Also save a PDF copy**), managed in the **Printers** web page and **Ctrl+Alt+Delete → Printers…** while keeping CUPS isolated on `/run/cups/cups.sock`.
- **Clean Update (`3.3.0`).** Adds a **Clean Update** option in **Software Update** (local panel and Web UI) to start fresh—erasing Macs, checkpoints, shared files, paired browsers, CUPS queues, and settings while preserving saved **Wi-Fi networks**.
- **Download a Mac hotfix (`3.2.1`) & `3.2.0` polish.** Fixes **Download a Mac** when blank disks have `os_version: null`, bounds catalog fetch timeouts with retry-window respect, delivers perceptual volume hotkeys (`amixer -M` / 2.2 curve / Bluetooth `pactl`), **Legacy** mouse tracking default, **Keyboard & Mouse** / **Modern Web Helper** / **Force Reboot…** top-level panel entries, Web **Network** whitelist passthrough, and **Deluxe v10**, **SuperDeluxe v7**, and **Mega Deluxe v5** Mac OS 9 editions.
- **Simple first-boot choice & all 13 ready-to-run Macs built in.** Start straight into **Mac OS 9.2.2 Mega Deluxe** (now packaged as a fast `retroboot-thin-1` archive) or **Mac OS 9.2.2 (Classic Clean desktop)**, browse all 13 Macs from System 7.5.5 to Mac OS X Tiger, add custom HTTPS image servers, or install RetroBoot alone first.
- **Genuine classic Mac startup chimes & sound controls.** Play model-matched chimes (**Automatic**, **Mac OS 9**, **Quadra**, **Power Mac**, **Mac II**, **Macintosh SE**, or **Off**), choose HDMI/speaker/USB/Bluetooth outputs, click **Test Sound**, and toggle live browser audio on **Screen**.
- **Print to PDF, plug-and-play USB drives & Web Files previews.** Print straight from Mac apps into **`RetroBoot Share/Printed PDFs/`**, plug FAT32 or exFAT USB flash drives into **`RetroBoot Share/USB Drives/`**, preview Apple `PICT`, images, PDFs, and text directly in any browser, move files, and download folders as `.zip`.
- **Game controllers & authentic Classic Mac input.** Play games with USB or Bluetooth controllers (`Arrow keys` or `WASD`), toggle **Natural scrolling**, choose PC (`Ctrl`) or Mac (`Win`) **Command key** mapping, and keep native unaccelerated Classic Mac mouse tracking with live **Mouse DPI** control.
- **Safety checkpoints, BitLocker warnings & privacy masking.** Every downloaded or imported catalog Mac automatically saves a zero-cost reflink **Fresh Install** checkpoint, interrupted HFS+ disks snapshot before repair, the installer flags encrypted Windows partitions (`Windows (encrypted)`), and remote **Screen** feeds automatically hide local pairing codes (`PAIRING CODE HIDDEN`).
- **A/B Software Updates & Raspberry Pi Imager JSON.** x86-64 PCs update live via signed A/B slots (`Software Update`) and Raspberry Pi 4/5 gains offline signed `tryboot` `.rbu` bundles alongside [Release `v3.4.0`](https://github.com/amcchord/RetroBoot/releases/tag/v3.4.0).

## Choose your hardware

| Hardware | Install media | Mac runtimes |
| --- | --- | --- |
| **x86-64 PC or Intel Mac** | Hybrid USB/DVD ISO or **Universal Raw USB Image** (`.img.xz`). UEFI & legacy BIOS. | Classic PowerPC, Quadra 800 & PowerPC Mac OS X |
| **Raspberry Pi 4, Pi 5 & Pi 400** | Ready-to-flash `.img.xz` via [Pi Imager JSON](https://mcchord.net/retroboot-downloads/rpi-imager.json) or direct download | Classic PowerPC & Quadra 800 |
| **Raspberry Pi Zero 2 W** | Dedicated microSD image preloaded with System 7.5.5 & Mac OS 8.1 (USB OTG hub required) | 68040 Macs |
| **Planet Computers Gemini PDA** | Installer bundle run from Gemian | Classic PowerPC & Quadra 800 |

Consult the [installation guide](docs/guide/installing.md) and [verify checksums (`SHA256SUMS`)](https://github.com/amcchord/RetroBoot/releases/tag/v3.4.0) before flashing or erasing a disk. [Open RetroBoot downloads](https://mcchord.net/retroboot-downloads/#devices)

## Screenshots

| PC Installer (Two-Option Choice) | Local RetroBoot Panel | Web Overview | Storage & Checkpoints |
| --- | --- | --- | --- |
| [<img src="docs/images/pc-install-machines.png" width="210" alt="Choose Macs to Install showing Mega Deluxe vs Mac OS 9.2.2 clean desktop plus Browse all 13 Library Macs">](docs/guide/installing.md#4-choose-the-macs-to-install) | [<img src="docs/images/retroboot-panel.png" width="210" alt="RetroBoot 3.1.0 local Platinum control panel over Mac OS 9.2.2 Finder">](docs/guide/web-management.md#pairing-a-browser) | [<img src="docs/images/web-overview.jpg" width="210" alt="Paired browser Overview showing the running Mac, startup timeline, and live screen">](docs/guide/web-management.md) | [<img src="docs/images/web-storage.jpg" width="210" alt="Storage and Media showing installed Macs, collapsed catalog checksums, and Checkpoints">](docs/guide/disk-library.md) |

*Boot GIF captured on Proxmox in 3.0.0; installer, local panel, and Web UI screenshots captured live on Proxmox in 3.1.0.*

## Under the hood

The Debian 13 host handles your hardware quietly behind the scenes—Linux stays hidden during normal boot, setup, and recovery. QEMU runs the Mac while RetroBoot's presenter draws the checkerboard, integer-scaled guest display, startup chimes, and Platinum recovery panel. The local panel and paired web app share the same `hostd` service for settings, checkpoints, Print-to-PDF, USB sharing, and lifecycle control. System updates swap A/B slots while preserving your Macs and settings on `RBDATA`. [Architecture and boot sequence](docs/dev/how-it-works.md)

## Documentation

| Guide | Covers |
| --- | --- |
| [Install](docs/guide/installing.md) | PC, Raspberry Pi, Universal USB/SD, BitLocker & flashing safety |
| [Families & kids](docs/guide/families-and-kids.md) | Mega Deluxe (Kid Pix, Zoombinis), Print-to-PDF, gamepads & Fresh Install checkpoints |
| [Upgrade to 3.4.0](docs/guide/upgrading.md) | A/B Software Updates (PC & Pi 4/5) and Zero 2 W / Gemini upgrades |
| [Mac disk library](docs/guide/disk-library.md) | All 13 included guest systems and editions |
| [Image servers](docs/guide/image-servers.md) | Custom HTTPS catalogs and packaging `retroboot-thin-1` disk archives |
| [Web management](docs/guide/web-management.md) | Browser pairing, Overview, Files (PICT/ZIP/Move) & remote Screen audio |
| [Developer & hardware docs](docs/dev/README.md) | Architecture, builds, bring-up checklists and verification |
| [Appliance notices](image/NOTICES.md) | Third-party packages and firmware |

The repository publishes source and component tests. Guest software, ROMs, normalized startup chimes (`RB_CHIMES_DIR`), extracted artwork, and guest disk images remain external. Existing component licenses are documented with the code. A repository-wide license is pending.
