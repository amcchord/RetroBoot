# Build and test RetroBoot source

RetroBoot runs on Linux. Debian 13 x86-64 is the development target for the host tools and PC image. Component tests use generated fixtures and can run without a Mac OS installation. A complete appliance build also needs a compatible QEMU package, firmware and any guest media you are authorized to use.

## Components

| Path | Purpose |
| --- | --- |
| `hostd/` | Settings, storage, network, installer, recovery and guest lifecycle |
| `presenter/` | Boot display, local controls, guest display and input |
| `web/` and `webproxy/` | Device management and the guest's web helper |
| `emulator/` | QEMU patches, firmware build scripts and pinned inputs |
| `image/` | Linux root filesystem, installer and board image builders |
| `guest/`, `guestprep/`, `apps/rbapps/` and `platinum/` | Guest helpers, disk and media tools, and on-device artwork generation |

## Run host tests

From the repository root on Debian 13:

```sh
sudo apt-get install python3-aiohttp python3-cryptography python3-pytest python3-qrcode
python3 -m pytest -q hostd/tests/test_catalogs.py hostd/tests/test_image_picker.py
python3 -m unittest discover -s guestprep/tests
```

The catalog and installer tests use local HTTPS fixtures. Guest preparation tests skip cases that require a private disk image when none is configured.

## Build the presenter

```sh
sudo apt-get install build-essential meson ninja-build pkg-config \
  libdrm-dev libinput-dev libudev-dev libxkbcommon-dev xkb-data
meson setup build/presenter presenter
meson compile -C build/presenter
meson test -C build/presenter --print-errorlogs
```

This builds `rb-presenter`, `rb-splash`, the classic UI library and their tests. Real output and input testing requires an appropriate Linux display and input devices.

## Build the emulator

The lockfiles in `emulator/manifests/` pin QEMU, ClassicMac, OpenBIOS and the patch inputs. `emulator/scripts/build-linux.sh` builds the Classic PowerPC package on Linux x86-64. The 68k and PowerPC Mac OS X runtimes use separate builders and manifests. Prepare each package in its own build directory and verify its package manifest before staging it for an appliance build.

For a Classic PowerPC package, install the QEMU build prerequisites on the builder:

```sh
sudo apt-get install git build-essential ninja-build pkg-config python3-venv \
  libglib2.0-dev libpixman-1-dev libslirp-dev libsdl2-dev libasound2-dev libpng-dev
```

The source firmware route runs the pinned OpenBIOS toolchain in a chroot. Use a disposable Linux builder for the firmware and image steps that require root.

## Appliance image inputs

`image/build.sh` assembles the PC system. Set `RB_WORK_ROOT` to a local build directory and `RB_ARTIFACTS` to the staged package directory. Place emulator packages in `$RB_ARTIFACTS/qemu/<package-name>` and use `RB_QEMU_PACKAGE` to select the Classic PowerPC package.

The builder reads component checkouts under `$RB_WORK_ROOT/src/components`. Each directory holds a `component.json` recording the source revision, plus the source paths below. The installer stage reads the same layout for both x86 and Pi builds.

| Staging directory | Source paths |
| --- | --- |
| `presenter` | `presenter/` |
| `classic-ui` | `presenter/ui/` |
| `hostd` | `hostd/` and `webproxy/` |
| `guestprep`, `apps`, `platinum` | The source directory of the same name |
| `web-ui` | `web/` |

See `image/scripts/fetch-components.sh` for package selection and optional helper stages. Board builders live in `image/rpi/` and `image/gemini/`. Add guest media only to private local builds and keep signing keys and download credentials outside Git.

Use `image/NOTICES.md` and the actual package manifest when reviewing redistribution terms. Source inspection, component tests, a bootable image build and physical validation establish different results.
