# Upgrade to RetroBoot 3.4.0

Choose the method for the computer running RetroBoot. Shut down your Macs cleanly before moving storage or updating the system.

| Computer | Upgrade path | Recovery |
| --- | --- | --- |
| **x86-64 PC** | Signed A/B **Software Update** (online or `.rbu` file), matching Product or Debug | **Go Back to the Previous Version** or automatic rollback |
| **Raspberry Pi 4, Pi 5 & Pi 400** | Signed A/B **Software Update** (`tryboot`, online or `-rpi-.rbu` file), or flash a second card and migrate | Automatic `tryboot` fallback, **Go Back**, or keep previous card |
| **Raspberry Pi Zero 2 W** | Flash the 3.4.0 Zero 2 W SD image to a second card, then migrate `RBDATA` | Reinsert original card |
| **Planet Computers Gemini PDA** | Extract bundle & run installer from Gemian with `--keep-data --activate` | Re-enter Gemian and run `--rollback previous` |

## x86 PC

1. Open **Ctrl+Alt+Delete → Software Update…** (or **Software Update** in the paired browser) and choose **Check for Updates**, then select `3.4.0` matching your installed flavor (`Product` or `Debug`).
2. Or download the signed `.rbu` bundle manually (`retroboot-update-3.4.0-product.rbu` or `-debug.rbu`) from [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#advanced) and choose **Choose Update File…** or **Install from USB Disk**.
3. Click **Restart to Update** when staging finishes. Your Macs, checkpoints, Wi-Fi profiles, and settings remain safe on `RBDATA`.

## Raspberry Pi

### Raspberry Pi 4, Pi 5 & Pi 400 (OTA Software Update)

1. Open **Ctrl+Alt+Delete → Software Update…** (or **Software Update** in the paired browser) and choose **Check for Updates**, or use a signed `retroboot-update-3.4.0-rpi-product.rbu` bundle from [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#advanced).
2. Click **Restart to Update**. RetroBoot updates the inactive root partition and uses the Raspberry Pi firmware's `tryboot` flag for the trial boot—falling back automatically if the trial fails.

### Raspberry Pi Zero 2 W (or second-card migration)

1. Flash `retroboot-3.4.0-rpi-zero2w-product.img.xz` (or `retroboot-3.4.0-rpi-product.img.xz`) to a spare card and boot it once so `RBDATA` expands, then shut down cleanly.
2. Connect both cards to a Linux computer with `xfsprogs` and `rsync` installed, check partition labels (`lsblk`), unmount both if auto-mounted, and run:

```sh
lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL
sudo bash image/rpi/scripts/migrate-data.sh /dev/OLD_DATA_PARTITION /dev/NEW_DATA_PARTITION
```

3. Boot from the new card and keep the original card as a ready backup. See [Migration helper](../../image/rpi/scripts/migrate-data.sh).

## Gemini PDA

Extract `retroboot-3.4.0-gemini.tar`, verify its SHA-256, copy it to the device under Gemian, and run:

```sh
cd retroboot-3.4.0-gemini
sudo sh gemini-install.sh --verify
sudo sh gemini-install.sh --keep-data --activate
```

To revert on Gemini, boot Gemian and run `sudo sh gemini-install.sh --rollback previous`.
