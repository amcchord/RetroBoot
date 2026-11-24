# Mac disk library

Download a current Mac archive and its SHA-256 checksum. Keep the archive intact when copying it to a USB disk for import. The library includes Internet Archive references for the original install media and updates. [Public Mac disk library](https://mcchord.net/retroboot-downloads/#macs)

RetroBoot lists this library in the PC installer and in **Download a Mac** (**Ctrl+Alt+Delete**) on every computer, with no setup. The Raspberry Pi Zero 2 W has used it since 3.0.3 and ships with the two standard 68k editions installed. On RetroBoot 3.0.5 and earlier on other computers, press **Ctrl+Alt+Delete**, open **Download a Mac**, choose **Image Servers…** and add `https://mcchord.net/retroboot-downloads/catalog.json`. You can also add it under **Storage & Media → Image Servers** in a paired browser. [Add an image server](image-servers.md)

![Download a Mac with System 7.5.5 selected from the public RetroBoot catalog](../images/download-a-mac.png)

The library groups Macs by runtime and lists OS versions in numerical order. It shows the newest published revision of each edition. Standard and curated editions are separate Macs. Previously downloaded archives can still be imported.

| Runtime | Available images | Requirements |
| --- | --- | --- |
| 68k | System 7.5.5 and Mac OS 8.1, each in standard and Deluxe editions | RetroBoot 1.7.0 or later with the 68k emulator |
| PowerPC Classic | Mac OS 9.2.1 and 9.2.2 standard, plus Mac OS 9.2.2 Deluxe, SuperDeluxe and Mega Deluxe | A RetroBoot build with the Classic PowerPC emulator |
| PowerPC Mac OS X | Jaguar 10.2.8, Panther 10.3.9 and Tiger 10.4.11 Period Editions, plus Tiger Deluxe | RetroBoot 2.0.2 on an x86 PC with Mac OS X support |

The device lists images its installed emulator can start. Add your own HTTPS catalog in [Image Servers](image-servers.md). The browser portal also lists the PC installers, Raspberry Pi SD images and host updates in their own sections.

## Choose an edition

- **Standard** provides the base OS with the drivers and tools selected for that RetroBoot release.
- **Deluxe** adds applications, games and utilities. The available titles depend on the OS version.
- **SuperDeluxe** and **Mega Deluxe** are larger Mac OS 9.2.2 collections. Mega Deluxe v4 includes 111 games and creative activities in Launcher and ACTION GoMac.
- **Period Edition** adds period software to Jaguar, Panther or Tiger. Tiger Deluxe includes Quake III Arena and iLife ’06. Set **Mac OS X memory** to 2048 MB and select **ATI Radeon** for the Tiger Deluxe setup. Allow 21.2 GB of free space to download and install it.

Downloading another edition creates a new Mac. Choose **Not Now** to keep the current Mac running, or **Restart** to start the new Mac now. You can also select it later in **Startup Disk**.

![RetroBoot confirms that System 7.5.5 is installed and offers to restart into it](../images/download-finished.png)
