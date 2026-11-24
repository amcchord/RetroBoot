# Install RetroBoot 3.1.0

Select the installer for your computer and download its checksum. The same page includes step-by-step flashing instructions, the Raspberry Pi Imager custom JSON repository, and the Mac disk library. [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#devices)

If RetroBoot already runs on the device, use the [upgrade guide](upgrading.md). A fresh install to a selected PC disk or SD card erases that disk or card.

## x86-64 PC

1. Choose the **Hybrid Installer ISO** or the **Universal Raw USB Image** (`retroboot-installer-3.1.0-universal-product.img.xz`), and verify it against its `.sha256` checksum.
2. Write it to a USB flash drive as a raw disk image with **balenaEtcher**, **Fedora Media Writer**, **Raspberry Pi Imager** (**Use custom**), `dd`, or **Rufus in DD Image mode** (or attach the ISO to a virtual machine).
3. Boot the installer in UEFI or legacy BIOS mode with **Secure Boot turned off**.
4. Choose your **Command key** preference (Mac or PC keyboard), select the destination disk, review the erase confirmation, then pick **Mac OS 9.2.2 Mega Deluxe** (Kid Pix Studio Deluxe, Zoombinis & 111 classic games), **Mac OS 9.2.2 (Classic Clean desktop)**, expand **Browse all 13 Library Macs**, add a custom catalog via **Image Servers…**, or choose **Install RetroBoot Only…**.
5. Check Display (with 15-second auto-revert when testing modes) and **Time Zone**, optionally scan the Network QR pairing code, complete installation, remove the USB stick or disc, and restart.

![Choose a Disk shows the empty virtual disk in the running PC installer](../images/pc-install-disk.png)

![Choose Macs to Install shows Mega Deluxe and Mac OS 9.2.2 selection plus Browse all 13 Library Macs](../images/pc-install-machines.png)

## Raspberry Pi

1. In **Raspberry Pi Imager** (`v1.9.6+`), add our custom repository URL `https://mcchord.net/retroboot-downloads/rpi-imager.json`, or download the **Pi 4/5 SD image** or separate **Zero 2 W SD image** directly and select **Use custom**. Leave OS customisation off. Write to a microSD card of at least 16 GB.
2. Insert the card and power on the Pi. RetroBoot expands the data partition during the first start.
3. For a supported touchscreen or small display, use **Display & Sound → Pi Display**. Pi 4/5 supports PowerPC and 68040 Macs. Zero 2 W supports 68040 Macs.
4. On Zero 2 W, System 7.5.5 starts by default after setup. Choose Mac OS 8.1 in **Startup Disk**. Both are installed on the card and work offline.

A Pi 4 with an old bootloader may need a firmware update before it can boot a GPT card. The Zero 2 W image uses its own boot layout and requires a USB OTG adapter or hub for USB input. Use the correct image for the board.

## Planet Computers Gemini PDA

1. Download and extract the Gemini bundle. Copy the whole directory into Gemian on the device.
2. Verify `SHA256SUMS` and run the installer from Gemian with root access.

```sh
sudo sh gemini-install.sh --verify
sudo sh gemini-install.sh --activate
```

3. Reboot into RetroBoot. The installer keeps Gemian’s recovery partition and installs RetroBoot beside Gemian’s files. For an existing RetroBoot system follow the [Gemini upgrade procedure](upgrading.md#gemini-pda).

## Add your first Mac

Press **Ctrl+Alt+Delete** and open **Download a Mac**, or import a machine archive from USB. The list shows the RetroBoot Mac library with a recommended Mac selected. **Stop Download** pauses a download, and the next **Download** continues it. If you host your own library, add the catalog URL under **Image Servers…**. Use **Startup Disk** to choose the default Mac. [Image server setup](image-servers.md) · [Mac disk library](disk-library.md) · [Pair a browser](web-management.md#pair-a-browser)
