#!/bin/bash
# Stage 2: turn the cached Debian base into the RetroBoot appliance root.
#
#   customize-rootfs.sh FLAVOR BASE.tar ROOTDIR COMPONENTS_DIR BOOT_DIR BUILD_ID
#
# COMPONENTS_DIR holds what fetch-components.sh staged: presenter/, hostd/, web/,
# qemu/ (each optional, each with component.json). A missing presenter is
# replaced by the image workstream's stand-in so boot silence can still be tested.
# RB_BOARD=rpi (image/rpi/build.sh): the Raspberry Pi arm64 root; adds image/rpi/rootfs
# over the shared overlay and leaves out the x86-only steps (microcode, efibootmgr,
# x86 KMS module checks, ttyS0). Unset (x86) everything runs as before.
# RB_BOARD=gemini (image/gemini/build.sh): the Gemini PDA arm64 root; adds image/gemini/rootfs
# over the shared overlay, leaves out the same x86-only steps and has no kernel, initramfs
# or microcode of its own (the boot.img on the PARTNAME=boot partition carries them;
# image/gemini/scripts/customize-gemini.sh installs the kernel's modules).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
require_root

flavor=${1:?flavor}; base=${2:?base tar}; root=${3:?rootdir}; comp=${4:?components}; boot=${5:?boot dir}; build_id=${6:?build id}
case "$flavor" in debug|product) ;; *) die "unknown flavor $flavor" ;; esac
version=$(cfg version)
board=${RB_BOARD:-x86}

mounted=()
cleanup() { for ((i=${#mounted[@]}-1; i>=0; i--)); do umount -l "${mounted[i]}" 2>/dev/null || true; done; }
trap cleanup EXIT
chroot_mounts() {
  for d in proc sys dev dev/pts run; do mkdir -p "$root/$d"; done
  mount -t proc proc "$root/proc"; mounted+=("$root/proc")
  mount -t sysfs sys "$root/sys"; mounted+=("$root/sys")
  mount --bind /dev "$root/dev"; mounted+=("$root/dev")
  mount --bind /dev/pts "$root/dev/pts"; mounted+=("$root/dev/pts")
  mount -t tmpfs tmpfs "$root/run"; mounted+=("$root/run")
}
in_root() { chroot "$root" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 "$@"; }

log "extracting base"
release_dev_binds "$root"
rm -rf "$root" "$root.devnodes.json"; mkdir -p "$root"
# A builder that may not create device nodes (an unprivileged container) gets every other
# file, and the base's /dev nodes become bind mounts of the builder's own, so chroot
# commands of the later stages find them.  Their owners and numbers are the builder's, not
# the base's: ROOT.devnodes.json keeps the base's records, which the Gemini payload
# (make-payload.sh) and the PC's update bundle (make-update.py) pack in their place.  Disk
# images, ISOs and the Raspberry Pi can't yet: they stop on such a root.
# The board's build releases the mounts after packing (release_dev_binds).  Any other
# extraction error still stops the build.
if ! tar --numeric-owner --xattrs --xattrs-include='*' -xpf "$base" -C "$root" 2>"$root.tar-err"; then
  if grep -q 'Cannot mknod' "$root.tar-err" &&
     ! grep -v -e 'Cannot mknod: Operation not permitted' -e 'Exiting with failure status' "$root.tar-err" | grep -q .; then
    [ "$board" = gemini ] || [ "$board" = x86 ] ||
      die "this builder may not create device nodes; only the Gemini and PC update builds can stand in for them"
    nodes=$(sed -n 's|^tar: \./dev/\([a-z]*\): Cannot mknod.*|\1|p' "$root.tar-err")
    python3 - "$base" "$root.devnodes.json" $nodes <<'PY' || die "reading the base's device nodes failed"
import json, sys, tarfile
base, out, *names = sys.argv[1:]
want = {f'./dev/{n}' for n in names}
nodes = []
with tarfile.open(base) as t:
    for m in t:
        if m.name in want and (m.ischr() or m.isblk()):
            nodes.append({'name': m.name, 'type': 'chr' if m.ischr() else 'blk', 'mode': m.mode, 'uid': m.uid,
                          'gid': m.gid, 'uname': m.uname, 'gname': m.gname, 'major': m.devmajor,
                          'minor': m.devminor, 'mtime': m.mtime})
if {n['name'] for n in nodes} != want:
    sys.exit(f'device nodes not found in the base: {sorted(want - {n["name"] for n in nodes})}')
json.dump(nodes, open(out, 'w'), indent=1)
PY
    for n in $nodes; do
      [ -c "/dev/$n" ] || die "this builder has no /dev/$n to stand in for the base's"
      : > "$root/dev/$n"; mount --bind "/dev/$n" "$root/dev/$n"
    done
    log "this builder may not create device nodes: /dev/{$(echo $nodes | tr ' ' ,)} bound from the builder's; the payload packs the base's"
  else
    cat "$root.tar-err" >&2; die "extracting $base failed"
  fi
fi
rm -f "$root.tar-err"
rm -f "$root/.rb-packages.tsv"
chroot "$root" dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${Section}\t${source:Package}\n' > "$root/../packages-$flavor.tsv"

log "installing overlay"
cp -a --no-preserve=ownership "$IMAGE_SRC/rootfs/." "$root/"
if [ "$board" = rpi ] || [ "$board" = gemini ]; then
  cp -a --no-preserve=ownership "$IMAGE_SRC/$board/rootfs/." "$root/"
  # x86 early microcode configuration (no microcode packages on the Pi or the Gemini)
  rm -f "$root/etc/default/intel-microcode" "$root/etc/default/amd64-microcode"
fi
chown -R 0:0 "$root/etc" "$root/usr/lib/retroboot" "$root/usr/lib/systemd"
if [ "$flavor" = product ]; then rm -f "$root/etc/ssh/sshd_config.d/50-retroboot-debug.conf"; fi

# ---- components ---------------------------------------------------------
components_json="$root/usr/lib/retroboot/components.json"
echo '{}' > "$components_json"
note_component() { python3 - "$components_json" "$1" "$2" <<'EOF'
import json, sys
path, name, info = sys.argv[1:]
data = json.load(open(path)); data[name] = json.loads(info); json.dump(data, open(path, 'w'), indent=2, sort_keys=True)
EOF
}
install -d "$root/usr/lib/retroboot/bin"
if [ -d "$comp/presenter/dest" ]; then
  log "presenter: $(cat "$comp/presenter/component.json")"
  cp -a --no-preserve=ownership "$comp/presenter/dest/." "$root/"
  note_component presenter "$(cat "$comp/presenter/component.json")"
elif [ -x "$comp/presenter/bin/rb-presenter" ]; then
  log "presenter: $(jq -c . "$comp/presenter/component.json" 2>/dev/null || cat "$comp/presenter/component.json")"
  install -m 0755 "$comp/presenter/bin/rb-presenter" "$root/usr/lib/retroboot/bin/rb-presenter"
  if [ -d "$comp/presenter/packaging" ]; then
    "$here/install-packaging.sh" "$comp/presenter/packaging" "$root"
  fi
  note_component presenter "$(cat "$comp/presenter/component.json")"
else
  log "presenter: NOT AVAILABLE - installing image stand-in (boot-silence testing only)"
  install -m 0755 "$comp/standin/rb-standin-presenter" "$root/usr/lib/retroboot/bin/rb-standin-presenter"
  install -m 0644 "$IMAGE_SRC/standin/retroboot-presenter.service" "$root/usr/lib/systemd/system/retroboot-presenter.service"
  note_component presenter '{"name":"presenter","kind":"standin","source":"image/standin/rb-standin-presenter.c","note":"temporary stand-in: checkerboard only, no guest display"}'
fi
if [ -d "$comp/hostd/dest" ]; then
  log "hostd: installing (hostd/packaging/install.sh)"
  cp -a --no-preserve=ownership "$comp/hostd/dest/." "$root/"
  note_component hostd "$(cat "$comp/hostd/component.json")"
else
  log "hostd: not available (image boots without supervisor)"
  note_component hostd '{"name":"hostd","kind":"absent"}'
fi
if [ -d "$comp/guestprep/rbguestprep" ]; then
  # Offline Mac OS volume repair/preparation, imported by hostd from
  # /usr/lib/retroboot/guestprep (needs root, losetup/mount, hfsplus, fsck.hfsplus).
  install -d "$root/usr/lib/retroboot/guestprep"
  cp -a "$comp/guestprep/rbguestprep" "$root/usr/lib/retroboot/guestprep/"
  find "$root/usr/lib/retroboot/guestprep" -name __pycache__ -prune -exec rm -rf {} +
  note_component guestprep "$(cat "$comp/guestprep/component.json")"
else
  note_component guestprep '{"name":"guestprep","kind":"absent"}'
fi
if [ -d "$comp/apps/rbapps" ]; then
  # Classic Mac media tools, imported by hostd's media library from
  # /usr/lib/retroboot/apps (rbapps.expand; needs unar for archives).
  install -d "$root/usr/lib/retroboot/apps"
  cp -a "$comp/apps/rbapps" "$root/usr/lib/retroboot/apps/"
  find "$root/usr/lib/retroboot/apps" -name __pycache__ -prune -exec rm -rf {} +
  note_component apps "$(cat "$comp/apps/component.json")"
else
  note_component apps '{"name":"apps","kind":"absent"}'
fi
if [ -d "$comp/platinum/rbplatinum" ]; then
  # Install the default local theme independently of the optional guest bundle.
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$comp/platinum" \
    python3 -m rbplatinum builtin --out "$root/usr/lib/retroboot/platinum-theme"
  install -d "$root/usr/lib/retroboot/platinum"
  cp -a "$comp/platinum/rbplatinum" "$root/usr/lib/retroboot/platinum/"
  find "$root/usr/lib/retroboot/platinum" -name __pycache__ -prune -exec rm -rf {} +
  note_component platinum "$(cat "$comp/platinum/component.json")"
else
  note_component platinum '{"name":"platinum","kind":"absent"}'
fi
if [ -d "$comp/web/web" ]; then
  install -d "$root/usr/lib/retroboot/web"; cp -a "$comp/web/web/." "$root/usr/lib/retroboot/web/"
  note_component web "$(cat "$comp/web/component.json")"
else
  note_component web '{"name":"web","kind":"absent"}'
fi
if [ -d "$comp/qemu/bin" ]; then
  log "qemu: installing emulator package"
  install -d "$root/usr/lib/retroboot/qemu"
  cp -a "$comp/qemu/." "$root/usr/lib/retroboot/qemu/"
  rm -f "$root/usr/lib/retroboot/qemu/component.json"
  note_component qemu "$(cat "$comp/qemu/component.json")"
else
  note_component qemu '{"name":"qemu","kind":"absent"}'
fi
if [ -f "$comp/qemu-osx/bin/qemu-system-ppc" ]; then
  log "qemu-osx: installing the separate OS X emulator package"
  install -d "$root/usr/lib/retroboot/qemu-osx"
  cp -a "$comp/qemu-osx/." "$root/usr/lib/retroboot/qemu-osx/"
  rm -f "$root/usr/lib/retroboot/qemu-osx/component.json"
  note_component qemu-osx "$(cat "$comp/qemu-osx/component.json")"
else
  note_component qemu-osx '{"name":"qemu-osx","kind":"absent"}'
fi
if [ -f "$comp/qemu-m68k/bin/qemu-system-m68k" ]; then
  # The 68k Macs' emulator (hostd quadra.py), beside the PowerPC package; never an Apple ROM.
  log "qemu-m68k: installing the 68k emulator package"
  install -d "$root/usr/lib/retroboot/qemu-m68k"
  cp -a "$comp/qemu-m68k/." "$root/usr/lib/retroboot/qemu-m68k/"
  rm -f "$root/usr/lib/retroboot/qemu-m68k/component.json"
  note_component qemu-m68k "$(cat "$comp/qemu-m68k/component.json")"
else
  note_component qemu-m68k '{"name":"qemu-m68k","kind":"absent"}'
fi
if [ -f "$comp/qemu-ndrv/qemu_vga.ndrv" ]; then
  log "qemu-ndrv: installing the video driver variant beside the package"
  install -d "$root/usr/lib/retroboot/qemu-ndrv"
  install -m 0644 "$comp/qemu-ndrv/qemu_vga.ndrv" "$comp/qemu-ndrv/manifest.json" "$root/usr/lib/retroboot/qemu-ndrv/"
  note_component qemu-ndrv "$(cat "$comp/qemu-ndrv/component.json")"
fi
if [ -f "$comp/qemu-classicvirtio/ndrvloader" ]; then
  log "qemu-classicvirtio: installing the shared folder driver variant beside the package"
  install -d "$root/usr/lib/retroboot/qemu-classicvirtio"
  install -m 0644 "$comp/qemu-classicvirtio/ndrvloader" "$comp/qemu-classicvirtio/manifest.json" "$root/usr/lib/retroboot/qemu-classicvirtio/"
  note_component qemu-classicvirtio "$(cat "$comp/qemu-classicvirtio/component.json")"
fi

if [ -f "$comp/rbidle/tree/manifest.json" ]; then
  log "rbidle: installing RetroBoot Idle's tree for the 68k Macs"
  install -d "$root/usr/lib/retroboot/guest"
  rm -rf "$root/usr/lib/retroboot/guest/rbidle"
  cp -a "$comp/rbidle/tree" "$root/usr/lib/retroboot/guest/rbidle"
  note_component rbidle "$(cat "$comp/rbidle/component.json")"
elif [ -f "$comp/qemu-m68k/bin/qemu-system-m68k" ]; then
  die "the 68k emulator is staged without RetroBoot Idle"
fi
if [ -f "$comp/rbdisplay/tree/manifest.json" ]; then
  log "rbdisplay: installing RetroBoot Display's tree for the ATI software installer"
  install -d "$root/usr/lib/retroboot/guest"
  rm -rf "$root/usr/lib/retroboot/guest/rbdisplay"
  cp -a "$comp/rbdisplay/tree" "$root/usr/lib/retroboot/guest/rbdisplay"
  note_component rbdisplay "$(cat "$comp/rbdisplay/component.json")"
fi
# Everything installed under /usr/lib/retroboot belongs to root, whoever owned it where the
# components and artifacts were staged (2.0.0 shipped the guest trees as the staging Mac's
# uid 501, and copies unpacked by a non-root user keep theirs).
chown -R 0:0 "$root/usr/lib/retroboot"

# ---- boot payload for the installer --------------------------------------
install -d "$root/usr/lib/retroboot/boot"
cp -a "$boot/." "$root/usr/lib/retroboot/boot/"

# ---- identity, fstab, users ---------------------------------------------
echo retroboot > "$root/etc/hostname"
cat > "$root/etc/hosts" <<'EOF'
127.0.0.1	localhost
127.0.1.1	retroboot
::1		localhost ip6-localhost ip6-loopback
EOF
: > "$root/etc/machine-id"
rm -f "$root/var/lib/dbus/machine-id"
install -d "$root/boot/efi" "$root/var/lib/retroboot" "$root/var/log/journal"
cat > "$root/etc/fstab" <<'EOF'
# RetroBoot appliance (image/scripts/customize-rootfs.sh; the installer writes the same).
# RBDATA and the ESP are optional at boot: hostd reports a missing data partition
# through the classic panel instead of systemd dropping into an emergency shell.
PARTLABEL=RBROOT_A  /                   ext4  rw,noatime,errors=remount-ro                       0 1
PARTLABEL=RBDATA    /var/lib/retroboot  xfs   noatime,nofail,x-systemd.device-timeout=20s         0 0
PARTLABEL=RBEFI     /boot/efi           vfat  noauto,nofail,x-systemd.automount,x-systemd.idle-timeout=30s,umask=0077,shortname=mixed  0 0
EOF

settings_schema=$(sed -n 's/^SCHEMA_VERSION = \([0-9]*\)$/\1/p' "$root/usr/lib/retroboot/hostd/retroboot/schema.py" 2>/dev/null | head -n1)
cat > "$root/usr/lib/retroboot/release.json" <<EOF
{"product": "RetroBoot", "version": "$version", "flavor": "$flavor", "build_id": "$build_id", "settings_schema": ${settings_schema:-1}}
EOF
# Software Update: trusted signing keys (public halves only; ARCHITECTURE 13).
install -d -m 0755 "$root/usr/lib/retroboot/update/keys"
for k in "$comp"/update-keys/*.pub "$comp"/update-keys/revoked; do
  [ -f "$k" ] && install -m 0644 "$k" "$root/usr/lib/retroboot/update/keys/"
done
install -d "$root/usr/share/doc/retroboot"
cp "$IMAGE_SRC/NOTICES.md" "$root/usr/share/doc/retroboot/NOTICES.md"
stage_startup_chimes "$root"

chroot_mounts
# sysusers/tmpfiles from component packaging (rbguest, rbdata, ...)
in_root systemd-sysusers >/dev/null

# ---- Python bytecode -----------------------------------------------------
# hostd, the guest launcher and the Modern Web Helper are about 28,000 lines of
# Python. The services run with PYTHONDONTWRITEBYTECODE=1, so without shipped
# bytecode every start compiled all of it again, on the way to Mac OS
# (image/reports/hostboot-2026-10-12.md). checked-hash bytecode is used only
# while it matches its source file's hash, so it can never run stale code.
log "compiling Python bytecode for /usr/lib/retroboot"
in_root python3 -m compileall -q -j 0 --invalidation-mode checked-hash /usr/lib/retroboot >/dev/null ||
  die "python bytecode compilation failed"

# ---- services: presets, masks, flavor ------------------------------------
log "systemd presets and masks"
# Disable cups.service and cups.path before enabling cups.socket: Debian's
# cups.service declares Also=cups.socket, so disabling it afterwards removes
# sockets.target.wants/cups.socket.
for unit in cups.service cups.path cups-browsed.service; do
  in_root systemctl disable "$unit" >/dev/null 2>&1 || true
done
for unit in retroboot-presenter.service retroboot-data-layout.service var-log-journal.mount \
            var-lib-bluetooth.mount bluetooth.service \
            retroboot-growdata.service retroboot-bootdisplay.service retroboot-microcode.service retroboot-bootmark.service \
            retroboot-live-toram.service retroboot-bootdiag.service \
            NetworkManager.service avahi-daemon.service cups.socket systemd-timesyncd.service; do
  [ -e "$root/usr/lib/systemd/system/$unit" ] && in_root systemctl enable "$unit" >/dev/null 2>&1
done
[ -e "$root/usr/lib/systemd/system/retroboot-hostd.service" ] && in_root systemctl enable retroboot-hostd.service >/dev/null 2>&1
masks="getty@.service autovt@.service console-getty.service container-getty@.service
       ctrl-alt-del.target debug-shell.service systemd-ask-password-console.path
       systemd-ask-password-console.service systemd-ask-password-wall.path
       systemd-ask-password-wall.service systemd-vconsole-setup.service
       NetworkManager-wait-online.service systemd-networkd-wait-online.service
       cups-browsed.service
       systemd-firstboot.service e2scrub_reap.service e2scrub_all.timer
       apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer
       console-setup.service keyboard-setup.service setvtrgb.service"
if [ "$flavor" = product ]; then
  masks="$masks serial-getty@.service getty.target ssh.service qemu-guest-agent.service"
fi
for unit in $masks; do ln -sf /dev/null "$root/etc/systemd/system/$unit"; done
# systemd-gpt-auto-generator would try to mount other GPT partitions on its own.
install -d "$root/etc/systemd/system-generators"
ln -sf /dev/null "$root/etc/systemd/system-generators/systemd-gpt-auto-generator"
if [ "$flavor" = debug ]; then
  # Lab access: key-only SSH as root with the lab key, serial console on ttyS0
  # (Raspberry Pi, Gemini: on the kernel console, started by systemd's getty generator).
  [ "$board" != x86 ] || in_root systemctl enable serial-getty@ttyS0.service >/dev/null 2>&1
  install -d -m 0700 "$root/root/.ssh"
  install -m 0600 "$comp/lab/authorized_keys" "$root/root/.ssh/authorized_keys"
  install -d "$root/etc/systemd/system/ssh.service.d"
  cat > "$root/etc/systemd/system/ssh.service.d/50-hostkeys.conf" <<'EOF'
[Service]
ExecStartPre=
ExecStartPre=/usr/bin/ssh-keygen -A
ExecStartPre=/usr/sbin/sshd -t
EOF
  in_root systemctl enable ssh.service >/dev/null 2>&1 || true
  # Serial console logs in as root automatically (lab debug only; the PVE serial
  # console is already an administrator-only channel). Root has no password.
  getty_unit=serial-getty@ttyS0.service; [ "$board" != x86 ] && getty_unit=serial-getty@.service
  install -d "$root/etc/systemd/system/$getty_unit.d"
  cat > "$root/etc/systemd/system/$getty_unit.d/50-autologin.conf" <<'EOF2'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,57600,38400,9600 - $TERM
EOF2
else
  in_root passwd -l root >/dev/null
fi
rm -f "$root"/etc/ssh/ssh_host_*

tools="fsck.hfsplus mkfs.hfsplus fsck.hfs losetup sgdisk mkfs.xfs mkfs.vfat mkfs.ext4 efibootmgr growpart unar lsar"
[ "$board" != x86 ] && tools=${tools/efibootmgr /}
for tool in $tools; do
  in_root sh -c "command -v $tool" >/dev/null || die "required tool $tool missing from rootfs"
done

# ---- initramfs and kernel links ------------------------------------------
kver=
if [ "$board" != gemini ]; then
kver=$(basename "$(ls -d "$root"/lib/modules/* | sort -V | tail -n1)")
log "initramfs for $kver"
# sr_mod/usb-storage/uas: a USB CD-ROM drive for Mac OS and Start Up from CD (retroboot/optical.py)
# mac-roman (Debian's Mac OS Roman codepage): rbguestprep.prep68k mounts a 68k Mac's HFS volume
# with codepage=macroman
kmods="hfs hfsplus mac-roman xfs squashfs overlay bochs virtio-gpu i915 amdgpu sr_mod usb-storage uas"
[ "$board" = rpi ] && kmods="hfs hfsplus xfs squashfs overlay vc4 v3d sr_mod usb-storage uas"
for m in $kmods; do
  find "$root/lib/modules/$kver" -name "${m//-/[-_]}.ko*" | grep -q . && continue
  # the Raspberry Pi kernel builds some of them in
  [ "$board" = rpi ] && grep -q "/${m//-/[-_]}\.ko\$" "$root/lib/modules/$kver/modules.builtin" 2>/dev/null && continue
  die "kernel module $m missing"
done
in_root update-initramfs -c -k "$kver" >/dev/null 2>"$root/../initramfs-$flavor.log" ||
  in_root update-initramfs -u -k "$kver" >/dev/null 2>>"$root/../initramfs-$flavor.log" ||
  { cat "$root/../initramfs-$flavor.log" >&2; die "update-initramfs failed"; }
ln -sf "vmlinuz-$kver" "$root/boot/vmlinuz"
ln -sf "initrd.img-$kver" "$root/boot/initrd.img"
rm -f "$root/vmlinuz" "$root/vmlinuz.old" "$root/initrd.img" "$root/initrd.img.old" "$root/boot/vmlinuz.old" "$root/boot/initrd.img.old"
if [ "$board" = x86 ]; then
# Early microcode is its own initrd (image/reports/hostboot-2026-10-12.md): every
# Intel and AMD update for a first boot, and rb-microcode's per-CPU copy on the ESP
# after it. GRUB loads it first; /boot/microcode.cfg names this set for GRUB.
if cpio -t <"$root/boot/initrd.img" 2>/dev/null | grep -q '^kernel/x86/microcode'; then
  die "the initramfs still embeds early microcode (etc/default/*-microcode)"
fi
ucode=$(mktemp -d)
mkdir -p "$ucode/kernel/x86/microcode"
in_root iucode_tool -q --write-to=/run/GenuineIntel.bin /usr/lib/firmware/intel-ucode ||
  die "iucode_tool could not collect the Intel microcode"
mv "$root/run/GenuineIntel.bin" "$ucode/kernel/x86/microcode/GenuineIntel.bin"
find "$root/usr/lib/firmware/amd-ucode" -maxdepth 1 -type f -name 'microcode_amd*.bin' -print0 | LC_ALL=C sort -z |
  xargs -0 -r cat >"$ucode/kernel/x86/microcode/AuthenticAMD.bin"
[ -s "$ucode/kernel/x86/microcode/GenuineIntel.bin" ] && [ -s "$ucode/kernel/x86/microcode/AuthenticAMD.bin" ] ||
  die "microcode missing"
(cd "$ucode" && find . -mindepth 1 | sed 's|^\./||' | LC_ALL=C sort |
   cpio --quiet -o -H newc -R 0:0 --reproducible) >"$root/boot/microcode.cpio"
rm -rf "$ucode"
printf 'set rb_ucode_id=%s\n' "$(sha256sum "$root/boot/microcode.cpio" | cut -c1-16)" >"$root/boot/microcode.cfg"
log "microcode: $(stat -c %s "$root/boot/microcode.cpio") bytes, $(cat "$root/boot/microcode.cfg")"
fi
fi   # board != gemini

# ---- cleanup -------------------------------------------------------------
cleanup; mounted=()
rm -rf "$root"/var/lib/apt/lists/* "$root"/var/cache/apt/*.bin "$root"/var/cache/apt/archives/*.deb \
       "$root"/var/log/*.log "$root"/var/log/apt/* "$root"/root/.bash_history "$root"/tmp/* "$root"/var/tmp/*
find "$root/var/log" -type f -size +0 -delete 2>/dev/null || true
du -sh "$root" | awk '{print "rootfs size: " $1}' >&2
[ -z "$kver" ] || echo "$kver" > "$root/../kernel-version-$flavor"
