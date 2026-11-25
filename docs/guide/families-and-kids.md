# RetroBoot for families and kids

RetroBoot 3.4.1 is built to turn any spare laptop, mini PC, or Raspberry Pi into an approachable, joyful classic Macintosh that kids and families can explore right away.

## Recommended first pick: Mega Deluxe v5 vs. Clean Mac OS 9.2.2

When setting up a PC or choosing a system in **Download a Mac** (`Ctrl+Alt+Delete`), RetroBoot highlights two great starting points right at the top:

- **Kids & Family: Mac OS 9.2.2 Mega Deluxe v5** — Packed with **all 146 classic games, creative programs, and utilities across 8 Launcher tabs** and the **ACTION GoMac** bar (2-second bottom-edge delay), with a clean desktop and classic Apple menu. Favorites include **Kid Pix Studio Deluxe**, **Logical Journey of the Zoombinis**, **The Oregon Trail**, **SimCity 2000**, **DinoPark Tycoon**, **Marble Drop**, and **ClarisWorks 5**.
- **Clean Mac OS 9.2.2** — A fast, minimal system with classic browsers, QuickTime 6, AirPort, and plenty of room to install your own favorite discs.

If you have a **Raspberry Pi Zero 2 W**, choose **Mac OS 7.5.5 Deluxe v2** or **Mac OS 8.1 Deluxe v2** for 23 great 68k games (including After Dark Flying Toasters and ClarisWorks 5) tuned for 512 MB boards.

## Automatic "Fresh Install" safety checkpoints

Kids love experimenting—moving system folders, trying control panels, or switching off the power strip. RetroBoot protects your setup automatically:

1. **Instant golden backup:** Whenever you download or import a Mac through RetroBoot's catalog or importer, RetroBoot automatically saves an instant reflink checkpoint named **Fresh Install**.
2. **Pre-repair safety snapshot:** After an unclean shutdown or power cut, RetroBoot snapshots the disk before repairing it (`fsck.hfsplus`). If a disk ever fails offline repair, RetroBoot pauses with a clear recovery dialog letting you choose **Restore Last Clean Checkpoint** or **Reset to Fresh Install** rather than booting a damaged disk.
3. **Manual checkpoints anytime:** Shut down Mac OS, press **Ctrl+Alt+Delete → Startup Disk → Checkpoints…**, and save or restore checkpoints in seconds.

## Print to PDF from Kid Pix & ClarisWorks

Made a drawing in **Kid Pix Studio Deluxe** or typed a story in **ClarisWorks**? Just choose **File → Print** inside Mac OS:

- RetroBoot's built-in **Virtual Print-to-PDF** printer converts PostScript print jobs straight into crisp `.pdf` files inside **`RetroBoot Share/Printed PDFs/`**.
- Open the paired web page (**Files**) on your phone or tablet to preview or download the finished PDF—and even print it on a modern home printer!
- Web **Files** also previews Apple **`PICT`**, JPEG, PNG, and GIF artwork right in the browser.

## Plug-and-play USB thumb drives

Plug a FAT32 or exFAT USB flash drive into the computer and RetroBoot automatically mounts it inside **`RetroBoot Share/USB Drives/<drive-name>/`** (read/write by default, or safe-eject via **Web Files** or **RetroBoot window**). Copying saved Kid Pix art, ClarisWorks stories, or classic `.sit` downloads back and forth takes seconds without touching a browser.

## Macintosh startup chimes & sound

Turn on your RetroBoot and hear an authentic Macintosh power-on chord right when the boot checkerboard appears (when external `.wav` chime assets are staged in the image or `/usr/share/retroboot/chimes/`).

- Open **Ctrl+Alt+Delete → Display & Sound** (or Web **Sound**) to pick a chime style (**Automatic**, **Mac OS 9**, **Quadra**, **Power Mac**, **Mac II**, **Macintosh SE**, or **Off**).
- Use **Test Sound** to check speakers, HDMI monitors, USB audio adapters, or Bluetooth speakers, and switch **Audio Output** without guessing.

## Playing with USB & Bluetooth game controllers

Most USB gamepads and modern Bluetooth controllers (Xbox, PlayStation, Switch Pro, 8BitDo) work right away in classic Mac games! Pair a Bluetooth controller in **Ctrl+Alt+Delete → Bluetooth…**, or plug in a USB gamepad, then pick **Arrow keys**, **WASD**, or **Off** under **Keyboard & Pointer → Gamepad** in **Display & Sound** (or Web **Display & Input**).
