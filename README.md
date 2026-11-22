# RetroBoot

RetroBoot runs Mac OS on a dedicated PC, Raspberry Pi or Planet Computers Gemini PDA. Power on and the selected Mac takes over the screen. Press **Ctrl+Alt+Delete** on a PC keyboard to open the local RetroBoot controls. A paired browser can manage the same computer over your local network.

[Install RetroBoot](docs/guide/installing.md) · [Upgrade an existing system](docs/guide/upgrading.md) · [Mac disk library](docs/guide/disk-library.md) · [Host an image server](docs/guide/image-servers.md) · [How it works](docs/dev/how-it-works.md)

![RetroBoot control panel preview on the checkerboard](docs/images/retroboot-panel.png)

## RetroBoot 3.0.0

- **Bring your own Mac images.** Add HTTPS catalog URLs in the installer, the local Download a Mac panel, or the paired browser. Import a machine archive directly if you already have the file.
- **Set up the computer first.** Install RetroBoot without a guest, then choose or import a Mac after the first boot.
- **Move an existing installation forward.** Use signed Software Update on a PC, copy your data to a new Pi card, or retain your Gemini data and previous system tree during installation.

## Choose a download

| Hardware | Install media | Mac runtimes |
| --- | --- | --- |
| **x86-64 PC** | Hybrid USB/DVD installer ISO. UEFI or legacy BIOS. | Classic PowerPC, Quadra 800 and PowerPC Mac OS X |
| **Raspberry Pi 4 or 5** | Ready-to-flash microSD image | Classic PowerPC and Quadra 800 |
| **Raspberry Pi Zero 2 W** | Separate microSD image | Quadra 800 |
| **Planet Computers Gemini PDA** | Installer bundle run from Gemian | Classic PowerPC and Quadra 800 |

Download the installer or update for your computer without signing in. Consult the installation guide before writing an image or choosing a target disk. [Download RetroBoot](https://mcchord.net/retroboot-downloads/#installers)

## Start using it

1. Install RetroBoot on the machine you want to use.
2. In **Startup Disk**, choose **Install Another…**. Select an available image, add your own catalog, or import a machine archive from USB.
3. Choose the Mac that starts by default. Use the Mac OS **Shut Down** and **Restart** commands for normal guest operation.
4. For browser management, open **Network & Web Access** on RetroBoot and pair the browser using the displayed address and code.

## Under the hood

The Debian host handles your hardware. QEMU runs the Mac, while RetroBoot's presenter draws the startup screen, guest and recovery. The local panel and web app use the same host service for settings and lifecycle commands. PC updates replace a system slot while keeping machines and settings on a separate data volume. [Architecture and boot sequence](docs/dev/how-it-works.md)

## Documentation

| Guide | Covers |
| --- | --- |
| [Install](docs/guide/installing.md) | PC, Pi and Gemini installation |
| [Upgrade and recover](docs/guide/upgrading.md) | Data preservation and rollback by platform |
| [Mac disk library](docs/guide/disk-library.md) | Supported guest versions and editions |
| [Image servers](docs/guide/image-servers.md) | Configure a URL, package a disk and publish a catalog |
| [Build and test](docs/dev/source-builds.md) | Debian build prerequisites and verification |
| [Appliance notices](image/NOTICES.md) | Third-party packages and firmware |

The repository publishes source and component tests. Guest software, ROMs, extracted artwork and guest disk images remain separate. Existing component licenses are documented with the code. A repository-wide license is pending.
