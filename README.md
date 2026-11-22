# RetroBoot

RetroBoot runs classic Mac OS and early PowerPC Mac OS X on dedicated computers. A Linux host runs QEMU and boots into the guest. RetroBoot provides local setup and recovery, a startup disk picker, and device management through a paired browser.

## Get started

- [Installation guide](docs/guide/installing.md) for PCs and Raspberry Pi.
- [Mac disk library](docs/guide/disk-library.md) for available versions, editions and import instructions.
- [Build and test RetroBoot](docs/dev/source-builds.md) for the emulator, presenter, host tools and appliance image.

| Guest | Runtime |
| --- | --- |
| System 7.5.5 and Mac OS 8.1 | 68040 Quadra 800 |
| Mac OS 9.2.1 and 9.2.2 | Classic PowerPC |
| Mac OS X Jaguar, Panther and Tiger | PowerPC Mac OS X on compatible x86 PC builds |

The current deployment release is **2.0.2**. RetroBoot has run on x86 PCs and in Proxmox. Raspberry Pi 4/5 and Zero 2 W SD builds reached responsive guest desktops in virtual tests. Physical board, display and touch checks are pending.

## Source and media

This repository contains the RetroBoot source snapshot, build scripts, emulator patches and component tests. Mac OS software, ROMs, guest images, extracted artwork and ready-to-install builds are outside this repository. The separate downloads portal requires sign-in. [RetroBoot downloads](https://mcchord.net/retroboot-downloads/)

Third-party code and artwork retain their existing licenses. A repository-wide license is pending. See the [appliance notices](image/NOTICES.md) and licensing files in the relevant components.
