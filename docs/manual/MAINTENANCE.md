# RetroBoot User Manual Maintenance Guide

This guide is the authoritative reference for contributors and AI agents maintaining `docs/manual/`, `docs/images/manual/`, `docs/guide/`, and `README.md` alongside the RetroBoot codebase.

---

## Code-to-Manual Impact Matrix

Whenever you modify any source area listed below, update the corresponding manual chapters, guides, and screenshots in the same worktree or release pass:

| Source File / Directory | Manual Chapters Affected | Guide / Root Docs Affected | Screenshots to Refresh (`docs/images/manual/`) |
| --- | --- | --- | --- |
| `hostd/retroboot/schema.py` | `12-settings-reference.md` (all keys, types, defaults, apply policies, capabilities); also `05-everyday-use-and-peripherals.md`, `06-display-and-3d-graphics.md`, `07-networking-airport-and-web-helper.md`, `08-files-usb-and-printing.md`, `09-web-management-and-remote-screen.md` | `docs/guide/web-management.md` | Affected `panel-*.png` / `web-*.png` |
| `hostd/retroboot/platform.py` | `01-system-requirements.md`, `02-architecture-how-it-works.md`, `03-installation.md`, `05-everyday-use-and-peripherals.md`, `11-updates-recovery-and-troubleshooting.md` | `README.md`, `docs/guide/installing.md`, `docs/guide/upgrading.md` | `pi45-first-setup.png`, `zero2w-system755.png` |
| `hostd/retroboot/cups.py`, `hostd/retroboot/printer.py` | `08-files-usb-and-printing.md`, `09-web-management-and-remote-screen.md` | `README.md`, `docs/guide/families-and-kids.md`, `docs/guide/web-management.md` | `panel-printers-341.png`, `macos9-print-dialog.png`, `web-printers.png` |
| `hostd/retroboot/panel/` (`controller.py`, `installer.py`, `picker.py`, `bluetooth.py`, `update.py`, `ui.py`) | `03-installation.md`, `04-first-start-and-mac-library.md`, `05-everyday-use-and-peripherals.md`, `07-networking-airport-and-web-helper.md`, `08-files-usb-and-printing.md`, `11-updates-recovery-and-troubleshooting.md` | `docs/guide/installing.md`, `docs/guide/first-start.md`, `docs/guide/bluetooth.md`, `docs/guide/software-update.md` | `installer-*.png`, `panel-main-341.png`, `panel-printers-341.png`, `panel-keyboard-mouse.png`, `panel-web-helper.png`, `panel-web-helper-whitelist.png`, `panel-force-reboot.png`, `panel-network-web-access.png`, `panel-startup-disk.png`, `panel-display-sound.png`, `panel-software-update.png`, `panel-bluetooth.png`, `panel-diagnostics.png` |
| `web/js/pages/` (`overview.js`, `screen.js`, `files.js`, `machine.js`, `display.js`, `sound.js`, `printers.js`, `storage.js`, `network.js`, `diagnostics.js`, `recovery.js`, `update.js`, `devices.js`, `pair.js`) | `09-web-management-and-remote-screen.md`, `06-display-and-3d-graphics.md`, `07-networking-airport-and-web-helper.md`, `08-files-usb-and-printing.md`, `11-updates-recovery-and-troubleshooting.md` | `README.md`, `docs/guide/web-management.md`, `docs/guide/files.md`, `docs/guide/screen.md` | `web-pairing.png`, `web-overview.png`, `web-screen.png`, `web-files.png`, `web-machine.png`, `web-display.png`, `web-sound.png`, `web-printers.png`, `web-storage.png`, `web-network.png`, `web-diagnostics.png`, `web-recovery.png`, `web-update.png`, `web-devices.png`, `web-phone-overview.png` |
| `lab/catalogs/retroboot-public.json` | `04-first-start-and-mac-library.md`, `10-creating-and-serving-images.md` | `README.md`, `docs/guide/disk-library.md`, `docs/guide/families-and-kids.md`, `docs/guide/installing.md` | `installer-macs.png`, `finder-mega-deluxe-v7.png` |
| `guest/build/catalog_archive.py` | `10-creating-and-serving-images.md` | `docs/guide/image-servers.md` | — |
| `hostd/retroboot/update/` (`manager.py`, `bundle.py`, `writer.py`, `guard.py`, `slotenv.py`) | `11-updates-recovery-and-troubleshooting.md`, `02-architecture-how-it-works.md` | `README.md`, `docs/guide/upgrading.md`, `docs/guide/software-update.md` | `panel-software-update.png`, `web-update.png` |
| `image/rootfs/usr/lib/retroboot/installer/` (`install-to-disk`, `list-disks`, `rpi_layout.py`) | `01-system-requirements.md`, `02-architecture-how-it-works.md`, `03-installation.md` | `docs/guide/installing.md` | `installer-disk.png`, `installer-erase.png`, `installer-progress.png`, `installer-done.png` |
| `hostd/retroboot/webproxy.py`, `webproxy/` | `07-networking-airport-and-web-helper.md` | `docs/guide/web-browsing.md` | `panel-web-helper.png`, `panel-web-helper-whitelist.png`, `web-network.png` |
| `hostd/retroboot/graphics.py`, `presenter/` | `06-display-and-3d-graphics.md`, `05-everyday-use-and-peripherals.md` | `docs/guide/display-scaling.md`, `docs/guide/rotation-and-touch.md` | `installer-display.png`, `panel-display-sound.png`, `web-display.png`, `macos9-volume-osd.png` |
| `hostd/retroboot/usbshare.py`, `hostd/retroboot/macfiles.py` | `08-files-usb-and-printing.md` | `docs/guide/files.md`, `docs/guide/families-and-kids.md` | `web-files.png` |
| `hostd/retroboot/audio.py`, `hostd/retroboot/bluetooth.py`, `hostd/retroboot/checkpoints.py` | `05-everyday-use-and-peripherals.md`, `11-updates-recovery-and-troubleshooting.md` | `docs/guide/bluetooth.md`, `docs/guide/recovery.md` | `panel-bluetooth.png`, `panel-startup-disk.png`, `web-sound.png`, `web-storage.png` |

---

## Privacy and Provenance Rules (D012, D079, D082)

Follow these non-negotiable rules before generating or committing any screenshot or text change:

1. **Never use `RBUI_ASSETS` or `--platinum` when capturing UI screenshots.**
   - RetroBoot's public manual and Web UI renders must use the built-in **MIT-licensed Platinum theme** (`docs/manual/scripts/capture_manual_shots.py` does this by default).
   - Never commit proprietary Apple fonts, pixmaps, icons, ROMs, disk images, or binaries (decision **D012** / **D082**).
2. **Never publish private secrets or live credentials.**
   - Never include live pairing codes, session tokens, cookie secrets, real Wi-Fi SSIDs/passwords, private hostnames/IPs, or personal user data in screenshots, examples, or captions (decision **D079**).
   - Pairing dialogs in public documentation must show synthetic/example codes only.
3. **Label provenance honestly.**
   - Separate unit tests, local MIT Platinum renders, Proxmox VM captures, and physical hardware measurements. Never present a VM screenshot or offline render as physical-hardware validation.
4. **Keep Linux invisible in user-facing instructions.**
   - Document user workflows through the installer, local **Ctrl+Alt+Delete** Platinum panel, and paired browser Web UI rather than shell commands on the host OS (except documented Pi migration / Gemini installer / custom archive developer workflows).

---

## Regeneration and Verification Workflow

Run all commands from the repository (or worktree) root.

### 1. Regenerate Manual Screenshots

```sh
python3 docs/manual/scripts/capture_manual_shots.py
```

Ensure `RBUI_ASSETS` is **unset** in your environment before running:

```sh
env -u RBUI_ASSETS python3 docs/manual/scripts/capture_manual_shots.py
```

Verify that every updated PNG under `docs/images/manual/` renders cleanly with MIT Platinum chrome and contains no real pairing codes or Wi-Fi credentials.

### 2. Verify All Links, Anchors, and Image Widths

```sh
python3 docs/screenshots/tools/check_links.py
```

`check_links.py` checks every tracked `.md` file and every file under `docs/` for:
- Missing relative file targets and broken `#anchor` headings.
- `<img ... width="N">` attributes wider than **840 px** (which overflow GitHub's README column). Keep every `width` `<= 840`.

### 3. Verify `schema.py` Coverage in Chapter 12

Every setting in `hostd/retroboot/schema.py` (`SETTINGS`) must appear in `docs/manual/12-settings-reference.md`. Run:

```sh
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, "hostd")
from retroboot import schema

doc = pathlib.Path("docs/manual/12-settings-reference.md").read_text(encoding="utf-8")
missing = [s.key for s in schema.SETTINGS if s.key not in doc]
print(f"Checked {len(schema.SETTINGS)} settings; missing={missing}")
raise SystemExit(1 if missing else 0)
PY
```

### 4. Verify Mermaid Diagram Syntax

Before finishing any manual edit, audit all ```` ```mermaid ```` blocks:
- Quote every node label containing parentheses, brackets, colons, slashes, or punctuation—for example, `HOST["Invisible Host (Debian 13 + hostd)"]`.
- **Never** use HTML tags (`<br>`, `<b>`, `<i>`, etc.) inside Mermaid node labels or edge labels.
- Stick to supported GitHub Mermaid diagram types (`flowchart LR`, `flowchart TD`, `sequenceDiagram`, `stateDiagram-v2`).

### 5. Verify Public-Safe Image Links in `docs/guide/` and `README.md`

The public `amcchord/RetroBoot` repository syncs `docs/images/` (including `docs/images/manual/`), not `docs/screenshots/`. Keep all image links in `README.md`, `docs/manual/`, and public `docs/guide/` pages pointing to `docs/images/...` or `../images/...`—never `../screenshots/...` in public-synced pages.

---

## Syncing Both GitHub Repositories

RetroBoot maintains two GitHub repositories:

1. **`amcchord/RetroBoot-archive`** — full development and archive checkout (includes `docs/manual/`, `docs/images/manual/`, `docs/guide/`, `docs/dev/`, `docs/screenshots/`, `lab/`, `apps/`, tests, and reports).
2. **`amcchord/RetroBoot`** — public source and user-documentation checkout prepared by `private/releases/<ver>/sync_public.py`.

When cutting a release or publishing manual updates:

1. Commit verified changes in the main archive repository (`amcchord/RetroBoot-archive`) after running `check_links.py` and the `schema.py` coverage check.
2. Inspect `private/releases/<ver>/sync_public.py`:
   - `doc_prefixes = ("docs/images/",)` automatically syncs `docs/images/manual/*.png` and `docs/images/*`.
   - `keep_manual_docs` (or an updated `doc_prefixes` entry) controls which Markdown documentation files are retained/synced into `public-source`. Ensure `docs/manual/` chapters and updated `docs/guide/` files are included when publishing the manual publicly.
3. Run the release's `sync_public.py` script, inspect the `public-source` diff to confirm zero private ROMs, `.wav` chimes, credentials, `RBUI_ASSETS`, or broken `../screenshots/...` links exist, and run `check_links.py` inside the public checkout before pushing `amcchord/RetroBoot`.
