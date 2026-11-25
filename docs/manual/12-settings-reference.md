[← Previous](11-updates-recovery-and-troubleshooting.md) · [Table of Contents](README.md) · [Next →](MAINTENANCE.md)

# Chapter 12 — Complete Settings & Technical Reference

This chapter is a complete reference for every configuration key in RetroBoot's shared settings schema (`SCHEMA_VERSION = 1` in `hostd/retroboot/schema.py`), along with keyboard/mouse/touch/pen mappings, appliance filesystem paths, and network ports and protocols.

---

## Settings Schema Reference (All 55 Keys)

Both the local Platinum panel and the Web UI read and validate the exact same 55 settings keys. Each setting has one of three apply policies:

- **Live:** Applies immediately with no interruption to Mac OS (`live`).
- **Mac OS Restart:** Saved right away and takes effect the next time Mac OS starts (`guest_restart`).
- **Timed 15s:** Applies immediately and automatically reverts after 15 seconds unless explicitly confirmed (`live` with `confirm=15`).

### Machine & Startup (`machine`, `system`, `power`, `startup`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `machine.active` | Startup machine | `null` | Installed machine ID (`^[a-z0-9][a-z0-9-]{0,47}$`) or `null` | Mac OS Restart | Applies to all families. |
| `machine.ram_mb` | Memory | `512` | `256`, `384`, `512`, `768`, `896` MB | Mac OS Restart | PowerPC Classic (`powermac`, `classic`) only; capped at `768` MB when `machine.cpu` is `g3`. |
| `machine.osx_ram_mb` | Memory | `"auto"` | `"auto"`, `128`, `256`, `384`, `512`, `768`, `896`, `1024`, `1536`, `2048` MB | Mac OS Restart | PowerPC Mac OS X (`powermac`, `macosx-*`) only; `128` MB is disallowed on Tiger (`macosx-10.4`, min `256` MB). |
| `machine.quadra_ram_mb` | Memory | `128` | `64`, `128`, `256` MB | Mac OS Restart | 68k Quadra 800 (`quadra800`) only; capped at `128` MB on Raspberry Pi Zero 2 W. |
| `machine.cpu` | Processor | `"7400"` | `"7400"` (PowerPC G4), `"g3"` (PowerPC G3) | Mac OS Restart | PowerPC Classic (`powermac`, `classic`) only; Mac OS X always uses `7400`. |
| `machine.boot_speed` | Startup speed | `"realtime"` | `"realtime"` *(hidden diagnostic option: `"accelerated"`)* | Mac OS Restart | PowerPC Classic (`powermac`, `classic`) only; Mac OS X always boots in real time. |
| `system.timezone` | Time zone | `"UTC"` | Valid IANA tzdb zone name (e.g., `America/New_York`, `Europe/Berlin`, `UTC`) | Mac OS Restart | Sets Classic/68k Mac OS RTC zone; on Mac OS X, change guest time zone in System Preferences. |
| `system.battery_passthrough` | Show the battery in Mac OS | `true` | `true`, `false` | Mac OS Restart | Requires `battery` capability; PowerPC Classic (`powermac`, `classic`) only (runs Mac OS 9 as a PowerBook). |
| `startup.autostart` | Start Mac OS automatically | `true` | `true`, `false` | Live | When `false`, opens Startup Options instead of auto-starting Mac OS at boot. |
| `startup.progress_indicator` | Show startup progress | `true` | `true`, `false` | Live | Shows the Happy Mac progress bar on the checkerboard; takes effect on the next startup. |

### Display (`display`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `display.pixel_scale` | Pixel size | `"auto"` | `"auto"`, `"1"`, `"2"`, `"3"`, `"4"` | Live | Integer pixel doubling (`1×`–`4×`). Scales whose resulting Mac screen falls below `640 × 480` are disabled. |
| `display.guest_size` | Mac screen size | `"auto"` | `"auto"`, `"640x480"`, `"800x600"`, `"1024x768"`, `"1152x870"`, `"1280x1024"` | Live | On Mac OS X, sets the early startup screen size (desktop size is chosen in Mac OS X Displays). |
| `display.scaling` | Scaling | `"integer"` | `"integer"` (Sharp, whole pixels), `"fit"` (Fill the screen, smooth) | Live | Applies to all families. |
| `display.primary` | Main display | `"internal"` | `"internal"` (Built-in display), `"external"` (External display when connected) | Live | Requires `internal_display` capability (laptops/portable panels). |
| `display.rotation` | Rotation | `"auto"` | `"auto"`, `"0"`, `"90"`, `"180"`, `"270"` | Live | Rotates the primary display (`90`/`270` give Mac OS a portrait screen). |
| `display.rotation_lock` | Rotation Lock | `false` | `true`, `false` | Live | Requires `accelerometer` capability; freezes orientation when `display.rotation` is `"auto"`. |
| `display.graphics` | Graphics card | `"standard"` | `"standard"`, `"ati-radeon"` (3D acceleration) | Mac OS Restart | PowerPC (`powermac`) only; requires capable emulator package, host OpenGL render node, and guest ATI software (Classic) or qualified Tiger (`8S165`). |
| `display.host_mode` | Display resolution | `"auto"` | `"auto"` or `WxH@R` from detected display modes (e.g., `1920x1080@60`) | **Timed 15s** | Requires `display_modes` capability; reverts after 15 seconds unless confirmed. |

### Keyboard, Mouse, Touch & Bluetooth (`input`, `bluetooth`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `input.swap_command_option` | Swap Command and Option | `false` | `true`, `false` | Live | Swaps Alt/Meta (`Option`/`Command`) across local keyboards and non-Mac browsers. |
| `input.recovery_chord` | Recovery keys | `"ctrl+alt+delete"` | `"ctrl+alt+delete"`, `"ctrl+alt+backspace"`, `"ctrl+alt+escape"` | Live | Key chord intercepted by `rb-presenter` to open the RetroBoot window (never forwarded to Mac OS). |
| `input.pointer_speed` | Pointer speed | `1.0` | `0.25` to `4.0` (step `0.25`) | Live | Multiplier for local mouse and trackpad motion. |
| `input.legacy_mouse` | Legacy mouse tracking | `true` | `true`, `false` | Mac OS Restart | Lets Mac OS 7/8/9 track the local mouse via classic pointer scaling/acceleration. |
| `input.mouse_dpi` | Mouse DPI | `0` | `0` (automatic) or `100` to `100000` | Live | Used when `input.legacy_mouse` is active to normalize hardware DPI. |
| `input.touch_gestures` | Touch gestures | `true` | `true`, `false` | Live | Requires `touch` capability; enables tap, drag, long-press/two-finger right-click, and two-finger scroll. |
| `input.gamepad_mode` | Game controller | `"arrows"` | `"arrows"`, `"wasd"`, `"off"` | Live | Maps USB and Bluetooth game controllers to Arrow keys, WASD, or disables mapping. |
| `input.natural_scroll` | Natural scrolling | `false` | `true`, `false` | Live | Reverses scroll wheel / trackpad scroll direction. |
| `input.mac_os_keys` | Volume and brightness keys in Mac OS | `true` | `true`, `false` | Mac OS Restart | Requires `mac_os_keys` capability; PowerPC Classic (`powermac`, `classic`) only (triggers native PowerBook OSD windows). |
| `bluetooth.enabled` | Bluetooth | `true` | `true`, `false` | Live | Requires `bluetooth` capability; powers the Bluetooth radio on or off while keeping pairings. |
| `bluetooth.offer_keyboard_setup` | Offer to pair a keyboard when none is connected | `true` | `true`, `false` | Live | Requires `bluetooth` capability; runs pairing assistant after Finder if no keyboard is attached. |

### Sound (`audio`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `audio.enabled` | Sound | `true` | `true`, `false` | Mac OS Restart | Enables or disables guest audio output (`alsa` vs `none`). |
| `audio.volume` | Volume | `70` | `0` to `100` (%) | Live | Requires `audio` capability; sets host mixer output level. |
| `audio.startup_chime` | Startup chime | `"auto"` | `"auto"`, `"os9"`, `"quadra"`, `"powermac"`, `"macii"`, `"se"`, `"off"` | Live | Selects the classic Macintosh startup chime played at boot. |
| `audio.output` | Sound output | `"auto"` | `"auto"` or detected output ID (e.g., `speakers`, `analog`, `hdmi`, `usb`, `bluetooth`; `^[A-Za-z0-9._:-]{1,64}$`) | Mac OS Restart | Applies to host UI chimes right away and to Mac OS on restart. |

### Networking & Modern Web Helper (`network`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `network.guest` | Mac OS networking | `"nat"` | `"nat"` (Share this computer's connection), `"none"` (No network) | Mac OS Restart | Controls whether QEMU exposes a user-mode NAT network interface to Mac OS. |
| `network.guest_adapter` | Mac OS network adapter | `"ethernet"` | `"ethernet"`, `"airport"` | Mac OS Restart | Requires `airport` capability; PowerPC Classic (`powermac`, `classic`) only (Mac OS X and 68k always use Ethernet). |
| `network.web_proxy` | Modern Web Helper | `"on"` | `"on"`, `"off"` | Live | Requires `web_proxy` capability; intercepts guest HTTP/DNS transparently via `rbwebproxy`. |
| `network.web_proxy_view` | Page view | `"auto"` | `"auto"`, `"simplified"`, `"full"` | Live | Requires `web_proxy` capability; controls article simplification vs. script/CSS-stripped full layout. |
| `network.web_proxy_homepages` | Send old home pages to new ones | `true` | `true`, `false` | Live | Requires `web_proxy` capability; rewrites retired Apple/MSN/Netscape start & search URLs. |
| `network.web_proxy_search` | Search page | `"frogfind"` | `"frogfind"`, `"bing"`, `"duckduckgo"`, `"wiby"`, `"none"` | Live | Requires `web_proxy` capability; destination for redirected vintage browser searches. |
| `network.web_proxy_redirects` | Home page redirects | 16 built-in entries (`system7today.com` & `search`) | List of up to `64` objects `{host, path, to}` (`http(s)://...` or `"search"`) | Live | Requires `web_proxy` capability. |
| `network.web_proxy_lan` | Allow pages on the local network | `true` | `true`, `false` | Live | Requires `web_proxy` capability; allows guest browsers to reach LAN HTTP devices (never RetroBoot itself). |
| `network.web_proxy_bypass` | Sites not to help (whitelist) | `["system7today.com", "frogfind.com", "68k.news", "wiby.me", "theoldnet.com", "oldweb.today"]` | List of up to `128` hostnames or IPv4 addresses | Live | Requires `web_proxy` capability; subdomains are matched automatically. |

### Storage, Media, Sharing, Printing, Web Management & Updates (`media`, `share`, `storage`, `management`, `remote`, `catalogs`, `update`)

| Key | UI Label | Default | Allowed Values / Range | Apply Policy | Platform / Family / Capability Notes |
| --- | --- | --- | --- | --- | --- |
| `media.cd` | CD-ROM drive | `"none"` | `"none"` or registered Media Library disc image name | Mac OS Restart | Mounts the chosen ISO/disc image into Mac OS's CD-ROM drive on next start. |
| `share.enabled` | Shared volume | `true` | `true`, `false` | Mac OS Restart | Exposes **RetroBoot Share** via `virtio-9p`; unavailable on Mac OS X. |
| `share.volume_name` | Shared volume name | `"RetroBoot Share"` | 1–27 chars (`^[A-Za-z0-9 _-]{1,27}$`) | Mac OS Restart | Volume label shown on the Mac OS desktop; unavailable on Mac OS X. |
| `printer.enabled` | Enable Mac printing | `false` | `true`, `false` | Live | Enables RetroBoot's virtual PostScript printer (`LPD 515`, `IPP 631`, `AppSocket 9100`). |
| `printer.target` | Mac printer output | `"pdf"` | `"pdf"` or `cups:<queue-name>` (`^(pdf\|cups:[A-Za-z0-9][A-Za-z0-9_-]{0,47})$`) | Live | Routes Mac OS print jobs to `RetroBoot Share/Printed PDFs` or a configured CUPS queue. |
| `printer.keep_pdf` | Also save a PDF copy in RetroBoot Share | `false` | `true`, `false` | Live | When printing to a CUPS queue, also archives a PDF in `RetroBoot Share/Printed PDFs`. |
| `printer.mac_auto_setup` | Automatically set up Mac OS 9 Desktop Printer | `true` | `true`, `false` | Mac OS Restart | Provisions *RetroBoot Printer* (LPR `10.0.2.2`, queue `retroboot`, AppleTalk off) on Mac OS 9 startup. |
| `usb_share.enabled` | USB flash drives in Share | `true` | `true`, `false` | Live | Exposes plugged-in USB drives inside `RetroBoot Share/USB Drives`. |
| `management.web` | Web management | `"on"` | `"on"`, `"off"` | Live | Enables/disables the HTTPS web portal on LAN interfaces (loopback HTTP stays active). |
| `management.hostname` | Computer name | `"retroboot"` | 1–63 lowercase alphanumeric/hyphen chars (`^(?!-)[a-z0-9-]{1,63}(?<!-)$`) | Live | Sets system hostname and `<hostname>.local` mDNS name. |
| `remote.screen` | Remote screen | `"control"` | `"off"`, `"view"` (View only), `"control"` (View and control) | Live | Controls browser access to `/api/v1/screen/stream` WebSocket. |
| `catalog.urls` | Image server URLs | `[]` | List of up to `8` full `https://` catalog URLs (no credentials, `?`, or `#`) | Live | Custom HTTPS catalogs shown alongside the built-in 13-Mac library. |
| `update.url` | Update server | `""` | Empty string `""` or `https://` URL (max 2,048 chars) | Live | Custom update feed URL (`retroboot-feed/1`); empty uses RetroBoot downloads feed when signed in. |

---

## Keyboard, Mouse, Touch & Pen Shortcuts Cheat Sheet

### PC & External USB / Bluetooth Keyboards

| Physical Key / Action | Mac OS / RetroBoot Action | Notes |
| --- | --- | --- |
| **Windows / Super** | **Command (⌘)** | Reversed when `input.swap_command_option` is `true` |
| **Alt** | **Option (⌥)** | Reversed when `input.swap_command_option` is `true` |
| **Ctrl** | **Control** | — |
| **Ctrl+Alt+Delete** | Open **RetroBoot** window | Intercepted by `rb-presenter`; configurable via `input.recovery_chord` (`Ctrl+Alt+Backspace` or `Ctrl+Alt+Escape`) |
| **Escape** *(on checkerboard)* | Open **Startup Details** | Press **Return** inside Startup Details to open **Startup Options** |
| **Host Power Button** | Mac OS **Power Key** (Shut Down / Restart / Sleep dialog) | Held until Finder if pressed during startup; opens RetroBoot Shutdown alert if Mac OS is stopped |
| **Right Mouse Button** | **Control-click** (Contextual Menu) | Powered by USB Overdrive on Mac OS 9 and native tablet/mouse mapping |
| **Middle Mouse Button** | **Command-click** | Opens links in new window / adds to selection (via USB Overdrive) |
| **Scroll Wheel** | Scroll 3 lines per notch | Delivered via `usb-mouse,wheel-per-report=1` |
| **Volume / Mute / Brightness Keys** | Native Mac OS 9 PowerBook OSD & backlight | Requires `system.battery_passthrough` + `input.mac_os_keys` on PowerPC Classic |

### Apple / Intel Mac Keyboards

- On an Apple USB or Bluetooth keyboard, **⌘ Command** already maps to Command and **⌥ Option** already maps to Option—leave **Swap Command and Option** (`input.swap_command_option`) **off**.
- Hold **Option (⌥)** at power-on to open the Intel Mac firmware startup picker when booting from a RetroBoot USB installer.
- Volume and brightness keys on Apple keyboards work like laptop hardware keys.

### Planet Computers Gemini PDA

| Gemini PDA Key / Action | Mac OS / RetroBoot Action |
| --- | --- |
| **Ctrl + Alt + Backspace** *(or Ctrl+Alt+Fn+Backspace)* | Opens the **RetroBoot** window |
| **Alt** | **Command (⌘)** (e.g., `Alt+Q` quit, `Alt+W` close, `Alt+S` save) |
| **Ctrl** | **Control** |
| **Fn + Alt** or **Fn + Ctrl** | **Option (⌥)** (`Alt+Fn+Esc` = `⌘⌥Esc` Force Quit) |
| **Fn + tap / drag** | **Option-click / Option-drag** |
| **Esc/On** *(tap)* / *(hold ~1 s)* / *(hold ~10 s)* | **Escape** / **Mac power key dialog** / **Force power-off** |
| **Fn + C / V** | Volume down / Volume up |
| **Fn + B / N / T** | Brightness down / Brightness up / Mute |
| **Fn + ↑ / ← / ↓ / →** | Page Up / Home / Page Down / End |
| **Fn + Backspace / Tab** | Forward Delete / Caps Lock |
| **Fn + K, L, M** / **I, O, P, J** / **7, 8, 9, 0** / **1..6** / **`\ \|`** | `;` `"` `'` / `+` `-` `=` `_` / `[` `]` `{` `}` / `~` `` ` `` `£` `€` `<` `>` / `:` |
| **Shift + `,` / `.`** / **Fn + Shift + 1..0** | `/` / `?` / **F1–F10** |
| **Esc/On + silver side button** *(at power-on)* | Boot into **Gemian** recovery |

### Remote Screen (Web Browser)

- **Key bar buttons:** Sends browser-reserved shortcuts directly to Mac OS: `⌘Q`, `⌘W`, `⌘Tab`, `⌘.`, `Esc`, **Force Quit…** (`⌘⌥Esc`), **Shut Down…** (ADB power key), **RetroBoot Window…** (`Ctrl+Alt+Delete`), **Audio** (PCM stream), and sticky modifier toggles (`Shift`, `Control`, `Option`, `⌘`).
- **Full Screen mode:** Captures keys such as `⌘W` in Chromium-based browsers so they reach Mac OS instead of closing the browser tab.
- **Paste Text…:** Types up to 8,000 Mac OS Roman characters into the frontmost Mac app.
- **Mobile browser Trackpad mode:** Relative cursor movement; two-finger tap sends right-click.

### Touchscreen & Stylus / Pen

| Gesture / Pen Input | Mac OS / Panel Behavior |
| --- | --- |
| **Tap** / **Tap twice** | Click / Double-click |
| **Press & drag** | Click and drag icons, windows, or text |
| **Touch and hold** *(or two-finger tap)* | Right-click (`Control-click` contextual menu); on the checkerboard, opens **Startup Options** |
| **Two-finger drag** | Scroll window under pointer (requires USB Overdrive on Mac OS 9) |
| **Stylus hover / tip / barrel button** | Moves pointer / primary mouse button / right-click (`Control-click`) |

---

## System Paths Reference

### Persistent Data Partition (`/var/lib/retroboot/` — XFS `RBDATA`)

| Path | Purpose |
| --- | --- |
| `/var/lib/retroboot/config/settings.json` | Active settings store (`settings.json.bak` keeps prior revision) |
| `/var/lib/retroboot/config/hostd.local.json` | Per-device `hostd` configuration overlay (survives A/B updates & Go Back) |
| `/var/lib/retroboot/config/lab-auth.env` | Optional RetroBoot downloads sign-in (`0600`, never included in logs or `.rbu` bundles) |
| `/var/lib/retroboot/machines/<id>/` | Per-Mac directory: `machine.json`, `disk.img`, `state.json` (plus `Quadra800.rom` & `pram.img` for 68k, or `nvram.img` for OS X) |
| `/var/lib/retroboot/checkpoints/<id>/<ts>/` | Clean-shutdown XFS reflink checkpoints (`checkpoint.json`, `disk.img`, `machine.json`, and `nvram.img` for OS X) |
| `/var/lib/retroboot/roms/<sha256>.rom` | Root-only (`0700`/`0600`) verified Quadra 800 ROM store |
| `/var/lib/retroboot/share/` | Host folder backing **RetroBoot Share** (`NAME`, `NAME.rdump`, `NAME.idump`, `Printed PDFs/`, `USB Drives/`) |
| `/var/lib/retroboot/media/` | Registered Media Library (CD images, archives, disk images) |
| `/var/lib/retroboot/network/connections/` | Root-only (`0700`/`0600`) NetworkManager Wi-Fi keyfiles |
| `/var/lib/retroboot/bluetooth/` | BlueZ pairing store (`0700`), bind-mounted at `/var/lib/bluetooth` |
| `/var/lib/retroboot/state/` | Pairing secrets, TLS cert/key, `known-good.json`, `startup-icon.rbi`, `startup-timing.json`, `airport-names.json` |
| `/var/lib/retroboot/logs/` | Persistent `lifecycle.jsonl`, `hostd.log`, `guest-serial.log`, `evidence/<id>/`, `bundles/<id>.tar.gz` |
| `/var/lib/retroboot/update/` | Software Update `state.json`, `incoming/*.rbu`, and per-build `settings/<build_id>/` snapshots |
| `/var/lib/retroboot/downloads/` | Cached/resumable catalog `.tar.gz` and `.part` downloads |
| `/var/lib/retroboot/assets/platinum/` | Extracted Platinum UI assets (never shipped in Git or base OS) |

### Read-Only System Slot (`/usr/lib/retroboot/`, `/etc/retroboot/`)

| Path | Purpose |
| --- | --- |
| `/usr/lib/retroboot/bin/rb-presenter` | DRM/KMS display compositor and input router (C) |
| `/usr/lib/retroboot/bin/rb-splash` | Initramfs early checkerboard splash binary |
| `/usr/lib/retroboot/hostd/` | `hostd` and `guest-launcher` Python package (`retroboot`) |
| `/usr/lib/retroboot/qemu/` | Pinned PowerPC Classic emulator (`qemu-system-ppc`, OpenBIOS, `qemu_vga.ndrv`, `ndrvloader`) |
| `/usr/lib/retroboot/qemu-osx/` | Pinned PowerPC Mac OS X emulator runtime (`openbios-ppc-osx`, `macosx-flash-seed.img`) |
| `/usr/lib/retroboot/qemu-m68k/` | Optional 68k Quadra 800 emulator (`qemu-system-m68k`, `mac_qfb.rom`, `classicvirtio-68k.rom`, `q800-pram-seed.img`) |
| `/usr/lib/retroboot/qemu-ndrv/` | RetroBoot video driver overlay (`qemu_vga.ndrv`, `manifest.json`) |
| `/usr/lib/retroboot/qemu-classicvirtio/` | RetroBoot `classicvirtio` shared-folder loader overlay (`ndrvloader`, `manifest.json`) |
| `/usr/lib/retroboot/web/` | Static Web Management client |
| `/usr/lib/retroboot/webproxy/rbwebproxy` | Modern Web Helper translating proxy service |
| `/usr/lib/retroboot/apps/rbapps/` | Classic Mac media archive/volume inspection tools |
| `/usr/lib/retroboot/update/keys/` | Trusted Ed25519 Software Update public keys (`*.pub` and optional `revoked`) |
| `/usr/lib/retroboot/release.json` | Installed slot release metadata (`product`, `version`, `flavor`, `build_id`, `settings_schema`) |
| `/etc/retroboot/defaults.json` | Read-only image setting defaults |
| `/etc/retroboot/hostd.json` | Read-only image `hostd` configuration |

### Volatile Runtime State (`/run/retroboot/`)

| Path | Purpose |
| --- | --- |
| `/run/retroboot/display.sock` | `AF_UNIX` `SOCK_SEQPACKET` shared-memory display & input socket (QEMU ⇄ `rb-presenter`) |
| `/run/retroboot/presenter.sock` | `AF_UNIX` `SOCK_STREAM` JSON control socket (`hostd` ⇄ `rb-presenter`) |
| `/run/retroboot/remote.sock` | Frame tap and remote input socket for Web Screen (`rb-presenter` ⇄ `hostd`) |
| `/run/retroboot/airport.sock` | AirPort bridge socket (`hostd` ⇄ QEMU Hermes-I card) |
| `/run/retroboot/power.sock` | Battery, lid, and sleep bridge socket (`hostd` ⇄ QEMU PMU) |
| `/run/retroboot/qmp-control.sock` / `qmp-events.sock` | QEMU QMP sockets for `hostd` and `guest-launcher` |
| `/run/retroboot/guest/argv.json` & `status.json` | Structured QEMU argv and `guest-launcher` session state |
| `/run/retroboot/webproxy.json` | Generated Modern Web Helper configuration (consumed by `retroboot-webproxy.service`) |
| `/run/retroboot-webproxy/status.json` | Modern Web Helper status & request counters |
| `/run/retroboot/update/healthy.json` | Trial-boot health flag checked by `retroboot-slot-guard.service` |

---

## Network Ports & Protocols Reference

| Port / Socket | Protocol | Bind Scope | Purpose & Security Notes |
| --- | --- | --- | --- |
| **443 / TCP** | HTTPS & WebSocket (`wss://`) | `0.0.0.0` (when `management.web = on`) | Web Management UI, REST API (`/api/v1/*`), and Remote Screen WebSocket (`/api/v1/screen/stream`). TLS 1.2+ with per-device self-signed cert; requires paired `rb_session` cookie (`HttpOnly; Secure; SameSite=Strict`), `X-RetroBoot-CSRF`, and validated `Host`/`Origin`. |
| **80 / TCP** | HTTP | `0.0.0.0` | On LAN interfaces, issues `301`/`308` redirect to `https://` (`421` for unknown `Host`). Full API is served over plain HTTP on **loopback (`127.0.0.1` / `::1`) only** for local host tools (Remote Screen WebSocket always requires TLS). |
| **8080 / TCP** | HTTP Proxy (`rbwebproxy`) | `127.0.0.1` (loopback) | Modern Web Helper translating proxy for vintage Mac browsers (`10.0.2.2:8080` inside QEMU user-mode NAT). Restricted to `rbguest` UID via `nftables`. |
| **8053 / TCP & UDP** | DNS Forwarder (`rbwebproxy`) | `127.0.0.1`, `::1` (loopback) | Modern Web Helper DNS responder used when intercepting guest web queries. |
| **515 / TCP** | LPD (RFC 1179) | Host loopback / guest NAT (`10.0.2.2`) | RetroBoot virtual printer queue (`retroboot`) for Mac OS 9 *LaserWriter 8* / LPR Desktop Printer (`Print to PDF` or CUPS forwarding). |
| **631 / TCP** | IPP (RFC 8010/8011) | Host loopback / guest NAT (`10.0.2.2`) | RetroBoot virtual IPP printer for Mac OS X / LPR IPP clients; host CUPS daemon itself communicates strictly over Unix socket `/run/cups/cups.sock`. |
| **9100 / TCP** | AppSocket / JetDirect | Host loopback / guest NAT (`10.0.2.2`) | Raw PostScript / PDF print stream listener for RetroBoot virtual printer. |
| **5353 / UDP** | mDNS / DNS-SD (Avahi) | LAN multicast | Publishes `<hostname>.local` (auto-disambiguates to `retroboot-<4hex>.local` on conflict) and browses network printers (`_ipp._tcp`, `_ipps._tcp`, `_pdl-datastream._tcp`, `_printer._tcp`). |
| **QEMU NAT `10.0.2.0/24`** | User-mode NAT (`slirp`) | Guest `rbguest` UID | Default guest network (`network.guest = nat`). Permanent `nftables` table `inet retroboot_guest` permits `rbguest` access **only** to DNS (`53`, `8053`), Modern Web Helper (`8080`), and virtual printer ports (`515`, `631`, `9100`) on local host addresses—blocking `443`, `80`, SSH, and loopback services with TCP RST / ICMP admin-prohibited. |

---

[← Previous](11-updates-recovery-and-troubleshooting.md) · [Table of Contents](README.md) · [Next →](MAINTENANCE.md)
