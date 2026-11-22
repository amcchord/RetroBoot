# RetroBoot appliance image notices

The appliance root filesystem is assembled from Debian 13 ("trixie") packages.
Each package's license is in `/usr/share/doc/<package>/copyright` (kept in the
image; other documentation is excluded). The image manifest
(`retroboot-<ver>-<flavor>.manifest.json`) lists every package, its version and
its archive component.

Packages from outside Debian `main`:

| Package | Component | License / status |
| --- | --- | --- |
| hfsprogs | non-free | Apple Public Source License 2.0 (APSL). Debian classifies the APSL as non-free (DFSG); redistribution is permitted under the APSL's terms, which require source availability for modifications. Used offline by hostd to check/repair the Mac OS HFS+ volume. |
| firmware-intel-graphics, firmware-iwlwifi, firmware-sof-signed, intel-microcode | non-free-firmware | Redistributable binary firmware under the vendors' licenses (see each copyright file). Needed for Intel graphics, Wi-Fi and audio on the N150 target. |
| firmware-cirrus | non-free-firmware | Redistributable binary firmware under Cirrus Logic's license (LICENSE.cirrus; binary-redist-Cirrus in the copyright file). DSP firmware and per-model tuning for CS35L41/CS35L54/CS35L56 laptop speaker amplifiers (ThinkPad Z13 Gen 1 and others). |

RetroBoot-built components:

- GRUB 2.12 (GPL-3.0-or-later), rebuilt from Debian source package grub2
  2.12-9+deb13u2 with `--enable-quiet-boot` and the patch in
  `image/grub/patches/` (cursor hidden at start, optional screen preservation).
  Corresponding source: the Debian source package plus that patch.
- The QEMU emulator package (GPL-2.0) and other RetroBoot components carry
  their own notices under `/usr/lib/retroboot/`.

Guest operating system software (Mac OS ROMs, disks, applications) is never part
of a distributable image. Private lab images that embed a guest fixture stay in
private lab storage.
