# Upgrade to RetroBoot 3.0.3

Choose the method for the computer running RetroBoot. Shut down your Macs cleanly before moving storage or replacing the operating system. Keep a backup of the old card, disk or system tree until you have opened an existing Mac and its files on 3.0.3.

| Computer | Upgrade path | Recovery |
| --- | --- | --- |
| x86-64 PC | Signed Software Update, matching the installed product or debug flavor | **Go Back to the Previous Version** or boot automatic rollback |
| Raspberry Pi 4/5 or Zero 2 W | Flash the matching 3.0.3 SD image to a second card, then copy the old card’s data | Reinsert the original card |
| Planet Computers Gemini PDA | Install the Gemini payload from Gemian with `--keep-data --activate` | Re-enter Gemian and choose `--rollback previous` |

## x86 PC

1. If the computer already uses the RetroBoot downloads update feed, open **Software Update → Check for Updates**. Choose 3.0.3 for your installed flavor. For a computer with no update server, use the signed bundle below.
2. Choose **Restart to Update** when staging finishes. RetroBoot tries the new system slot and keeps the previous one. The Macs, profiles and device settings stay on the data partition.
3. Once the machine is running, open an existing Mac and check a saved document. The update appears under **This RetroBoot**.

For an offline update, download the matching **3.0.3 product** or **3.0.3 debug** `.rbu` and its checksum. Select the bundle in **Software Update → Choose Update File…** or put it on a USB drive and open Software Update in the local control panel. If you installed a pre-A/B development image, use the installer’s **Update RetroBoot on This Disk** action when it recognizes the installed layout. Keep a full backup before opening an installer with an older system. [RetroBoot downloads](https://mcchord.net/retroboot-downloads/#updates)

## Raspberry Pi

1. Write the matching **Pi 4/5** or **Zero 2 W** 3.0.3 SD image to a second card. Keep the original. Start the new card once so RetroBoot expands its data partition, then shut it down. On the Zero 2 W, the two factory Macs can stay in the library.
2. Connect both cards to a Linux computer. Install `rsync` and the XFS tools, then identify each card’s partition with filesystem label `RBDATA`. Unmount both cards if the desktop mounted them.
3. Run the migration helper with the **old** partition first and the **new** partition second. The old partition is mounted read-only. The script verifies the copied files and can resume an interrupted copy. If a factory Mac has the same ID as an existing Mac, your existing Mac takes its place. The original factory copy is kept in a hidden backup directory on the new card.

```sh
lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL
sudo bash image/rpi/scripts/migrate-data.sh /dev/OLD_DATA_PARTITION /dev/NEW_DATA_PARTITION
```

Reconnect only the new card and start the Pi. Check Startup Disk, Wi-Fi and the existing Macs. Keep the original card as a ready-to-boot fallback. [Migration helper](../../image/rpi/scripts/migrate-data.sh)

## Gemini PDA

Extract the Gemini installer bundle and verify its checksums. Boot Gemian and copy the whole bundle directory to the device. Use an account with root access. The installer needs room for the new system and a copy of the existing data so the previous tree remains recoverable.

```sh
cd retroboot-3.0.3-gemini
sudo sh gemini-install.sh --verify
sudo sh gemini-install.sh --keep-data --activate
```

Restart and check the version, an existing Mac and its saved files. The installer retains the earlier tree as `/retroboot.old` and leaves the Gemian recovery partition in place. To restore the previous tree, boot Gemian, run `sudo sh gemini-install.sh --rollback previous`, then select RetroBoot in the boot target again. Keep a copy of any Macs changed since the upgrade before rolling back.
