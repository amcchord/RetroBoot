# Install RetroBoot 2.0.2

The installer erases the target disk when you confirm a new installation. Back up the target disk before you start. For an existing x86 RetroBoot installation, use **Software Update** to keep your Macs and settings.

## x86 PC

1. Sign in to the downloads portal. Choose the **PC and Raspberry Pi 4/5 installer**, or choose the **PC installer ISO** for a VM or DVD. [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#installers)
2. Write the installer to a USB stick using a disk-image writer. Boot the PC from that stick. The installer supports UEFI and legacy BIOS with Secure Boot disabled.
3. Choose the target disk and the Macs you want to install. Review the erase confirmation before continuing.
4. When installation finishes, remove the installation media and restart. Open **Startup Disk** to select or add a Mac later. [Mac disk library](disk-library.md)

## Raspberry Pi

1. Choose the **Pi 4/5 SD image** or the **Zero 2 W SD image**. These are separate images. [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#installers)
2. Write the image to a microSD card of at least 16 GB. With Raspberry Pi Imager, choose **Use custom** and turn off OS customisation.
3. Install the card and power on the Pi. On first start, RetroBoot expands its data partition. Use **Display & Sound → Pi Display** to select a supported display preset.

The Pi 4/5 image supports PowerPC and 68040 Macs. The Zero 2 W image supports 68040 Macs. Back up Macs before replacing a Pi SD image.
