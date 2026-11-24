#!/bin/bash
# Stage 2b (Gemini PDA): Gemini-specific finishing of the root after customize-rootfs.sh
# (RB_BOARD=gemini).
#
#   customize-gemini.sh FLAVOR ROOTDIR KERNEL_STAGE UPSTREAM_DIR BOOTDIR [MACHINE_DIR MACHINE_ID]
#
# KERNEL_STAGE  stage-kernel.py's output: kernel.json, modules tarball, System.map
# UPSTREAM_DIR  fetch-upstream.sh's output: files/ (firmware, battery guard), upstream.json
# BOOTDIR       boot.json (provenance; this script adds to it)
# What it does: the kernel's modules as /usr/lib/modules/<ver> (depmod, no unresolved
# symbols), the pinned upstream firmware and battery guard, identity, an fstab without
# any partition (the initrd mounts the root), the seeded Wi-Fi profiles (names only in
# logs and manifest), the usb0 fallback profile, units, the boot-counter contract, the
# debug flavor's pre-generated SSH host key, and the data directory (with the embedded
# machine for private builds). RB_GEMINI_WIFI_DIR (default /srv/retroboot/wave7/private/wifi)
# holds the private *.nmconnection profiles.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
require_root
flavor=${1:?flavor}; root=${2:?rootdir}; kstage=${3:?kernel stage}; up=${4:?upstream dir}; boot=${5:?boot dir}
machine_dir=${6:-}; machine_id=${7:-}
kver=$(gcfg kernel_version)
in_root() { chroot "$root" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 "$@"; }

# ---- kernel modules -------------------------------------------------------
mtar=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["modules"]["file"])' "$kstage/kernel.json")
log "kernel modules $kver from $(basename "$mtar")"
rm -rf "$root/usr/lib/modules"; install -d "$root/usr/lib/modules"
mtmp=$(mktemp -d); trap 'rm -rf "$mtmp"' EXIT
tar --numeric-owner -xaf "$kstage/$mtar" -C "$mtmp"
msrc=$(find "$mtmp" -maxdepth 4 -type d -path "*/modules/$kver" | head -n1)
[ -n "$msrc" ] && [ -f "$msrc/modules.order" ] || die "modules tarball has no lib/modules/$kver with modules.order"
cp -a "$msrc" "$root/usr/lib/modules/$kver"
chown -R 0:0 "$root/usr/lib/modules"
rm -f "$root/usr/lib/modules/$kver/build" "$root/usr/lib/modules/$kver/source"
# depmod clean: no unresolved symbols against the kernel's System.map (host depmod reads
# arm64 modules), then the module index written by the root's own depmod.
if [ -f "$kstage/System.map" ]; then
  depmod -b "$root" -e -F "$kstage/System.map" -n "$kver" >/dev/null 2>"$boot/depmod-check.log" || die "depmod -e failed"
  if grep -E 'needs unknown symbol|WARNING' "$boot/depmod-check.log" >&2; then die "depmod: unresolved symbols (see above)"; fi
fi
in_root depmod -a "$kver" 2>"$boot/depmod.log" || { cat "$boot/depmod.log" >&2; die "depmod failed"; }
[ ! -s "$boot/depmod.log" ] || { cat "$boot/depmod.log" >&2; die "depmod printed warnings"; }
log "modules: $(find "$root/usr/lib/modules/$kver" -name '*.ko*' | wc -l) files, depmod clean"

# The boot.img this tree belongs to (the installer writes the payload's copy, and
# `gemini-install.sh --rollback previous` writes the restored tree's own).
install -d "$root/usr/lib/retroboot/gemini/boot"
install -m 0644 "$kstage/boot.img" "$root/usr/lib/retroboot/gemini/boot/boot.img"
install -m 0644 "$kstage/kernel.json" "$root/usr/lib/retroboot/gemini/boot/kernel.json"

# ---- pinned upstream files (firmware, battery guard) ---------------------
cp -a --no-preserve=ownership "$up/files/." "$root/"
while IFS='|' read -r dest _ _; do
  [ -n "$dest" ] || continue
  case "$dest" in *.sh) chmod 0755 "$root$dest" ;; *) chmod 0644 "$root$dest" ;; esac
  chown 0:0 "$root$dest"
done < <(gcfg_all upstream_file)
if [ -e "$root/usr/lib/firmware/WIFI_factory.bin" ] || find "$root/data" -name WIFI -type f 2>/dev/null | grep -q .; then
  die "a Wi-Fi NVRAM record ended up in the root (each unit's comes from its own nvdata at boot)"
fi

# ---- identity, fstab -------------------------------------------------------
host=$(gcfg hostname)
echo "$host" > "$root/etc/hostname"
cat > "$root/etc/hosts" <<EOF
127.0.0.1	localhost
127.0.1.1	$host
::1		localhost ip6-localhost ip6-loopback
EOF
cat > "$root/etc/fstab" <<'EOF'
# RetroBoot on the Gemini PDA (image/gemini/scripts/customize-gemini.sh).
# No entries: the boot.img's initrd checks the PARTNAME=linux ext4 file system, mounts it
# read-write and bind-mounts its /retroboot directory as /. /var/lib/retroboot is a plain
# directory of this root; there are no RetroBoot partitions on the Gemini.
EOF
# No ESP, no boot partition, no initramfs-tools on the Gemini.
rmdir "$root/boot/efi" 2>/dev/null || true
rm -rf "$root/etc/initramfs-tools"

# ---- network ---------------------------------------------------------------
nmdir=$root/var/lib/retroboot/network/connections
install -d -m 0700 "$root/var/lib/retroboot/network" "$nmdir"
wifi_src=${RB_GEMINI_WIFI_DIR:-/srv/retroboot/wave7/private/wifi}
profiles=()
for f in "$wifi_src"/*.nmconnection; do
  [ -f "$f" ] || continue
  install -m 0600 -o 0 -g 0 "$f" "$nmdir/$(basename "$f")"
  profiles+=("$(basename "$f")")
done
[ "${#profiles[@]}" -gt 0 ] || log "WARNING: no Wi-Fi profiles in $wifi_src: the Gemini will only be reachable over usb0"
# Check each profile without printing anything from it: a system-wide Wi-Fi profile
# (no user permissions) with its secret in the file, so it joins at boot with nobody
# logged in. A WPA3 (sae) one is changed to WPA2 at boot by retroboot-gemini-wifi-fixup.
python3 - "$boot/wifi-profiles.json" "$nmdir" "${profiles[@]}" <<'PY'
import configparser, json, os, sys
out, d, *names = sys.argv[1:]
res = []
for n in names:
    c = configparser.RawConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read(os.path.join(d, n))
    g = lambda s, k, v='': c.get(s, k, fallback=v).strip()
    flags = g('wifi-security', 'psk-flags', '0') or '0'
    r = {'name': n, 'mode': oct(os.stat(os.path.join(d, n)).st_mode & 0o777),
         'type_wifi': g('connection', 'type') in ('wifi', '802-11-wireless'),
         'system_wide': not g('connection', 'permissions'),
         'autoconnect': g('connection', 'autoconnect', 'true').lower() != 'false',
         'secret_in_file': flags == '0' and (bool(g('wifi-security', 'psk')) or g('wifi-security', 'key-mgmt') in ('', 'none')),
         'key_mgmt_sae': g('wifi-security', 'key-mgmt') == 'sae'}
    r['joins_at_boot'] = r['type_wifi'] and r['system_wide'] and r['autoconnect'] and r['secret_in_file']
    res.append(r)
json.dump(res, open(out, 'w'), indent=2)
for r in res:
    print(f"wifi profile {r['name']}: {r['mode']}, joins at boot without a login: {r['joins_at_boot']}"
          + (" (WPA3: changed to WPA2 at boot)" if r['key_mgmt_sae'] else ''), file=sys.stderr)
if any(not r['joins_at_boot'] for r in res):
    sys.exit('a Wi-Fi profile would not join at boot without a login (see the fields above)')
PY
usb0=$root/usr/lib/NetworkManager/system-connections/retroboot-usb0.nmconnection
sed -i "s|@USB0_ADDRESS@|$(gcfg usb0_address)|" "$usb0"
chown 0:0 "$usb0"; chmod 0600 "$usb0"
grep -q '@' "$usb0" && die "usb0 profile has an unfilled placeholder"

# ---- devices ---------------------------------------------------------------
# BlueZ: no privacy (the MT6630 rejects Set Privacy at power-on and stays unpowered).
grep -q '^Privacy' "$root/etc/bluetooth/main.conf" || sed -i 's/^\[General\]$/[General]\n# Gemini (MT6630): the controller rejects Set Privacy at power-on.\nPrivacy = off/' "$root/etc/bluetooth/main.conf"
grep -q '^Privacy = off' "$root/etc/bluetooth/main.conf" || die "bluetooth main.conf: Privacy not set"
sed -i "s/^BACKLIGHT_PERCENT=.*/BACKLIGHT_PERCENT=$(gcfg backlight_percent)/" "$root/etc/default/retroboot-gemini"
install -d -m 0755 "$root/data/nvram/APCFG/APRDEB" "$root/var/lib/retroboot/gemini"
chmod 0755 "$root"/usr/lib/retroboot/gemini/rb-gemini-*
chmod 0644 "$root/usr/lib/retroboot/gemini/gemini-part.sh"

# ---- boot-attempt counter contract (kernel handoff) ------------------------
python3 - "$kstage/kernel.json" "$flavor" > "$root/usr/lib/retroboot/gemini/boot-counter.conf" <<'PY'
import json, shlex, sys
k = json.load(open(sys.argv[1])); c = k.get('counter') or {}
print('# Written by image/gemini/scripts/customize-gemini.sh from the kernel handoff')
print(f"# ({k.get('build_id', 'unknown kernel build')}). Read by rb-gemini-boot-ok.")
for key, val in (('COUNTER_FILE', c.get('file', '/.retroboot-boot-attempts')),
                 ('COUNTER_RESET', c.get('reset', '0')),
                 ('LINUX_MOUNT', c.get('linux_mount', '')),
                 ('REQUIRE_SSH', '1' if sys.argv[2] == 'debug' else '0')):
    print(f'{key}={shlex.quote(str(val))}')
PY
chmod 0644 "$root/usr/lib/retroboot/gemini/boot-counter.conf"

# ---- units -----------------------------------------------------------------
log "Gemini units"
for unit in retroboot-gemini-wifi-nvram.service retroboot-gemini-wifi.service retroboot-gemini-bt.service \
            retroboot-gemini-wifi-fixup.service retroboot-gemini-wifi-fixup.path retroboot-gemini-battery-guard.service \
            retroboot-gemini-audio.service retroboot-gemini-backlight.service retroboot-gemini-boot-ok.service \
            retroboot-gemini-typec.service; do
  in_root systemctl enable "$unit" >/dev/null 2>&1 || die "cannot enable $unit"
done
# Nothing to grow, no GRUB/ESP display configuration, no RBDATA bind mounts.
for unit in retroboot-growdata.service retroboot-bootdisplay.service var-log-journal.mount var-lib-bluetooth.mount; do
  [ -e "$root/usr/lib/systemd/system/$unit" ] && in_root systemctl disable "$unit" >/dev/null 2>&1 || true
done
# Suspend hangs the Gemini (s2idle with no wake-up source): make it impossible.
for unit in sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target; do
  ln -sf /dev/null "$root/etc/systemd/system/$unit"
done

# ---- debug: SSH host key pinned before the first boot ----------------------
rm -f "$boot/ssh_host_ed25519_key.pub"
if [ "$flavor" = debug ]; then
  rm -f "$root"/etc/ssh/ssh_host_*
  ssh-keygen -q -t ed25519 -N '' -C "root@$host" -f "$root/etc/ssh/ssh_host_ed25519_key"
  chown 0:0 "$root"/etc/ssh/ssh_host_ed25519_key*; chmod 0600 "$root/etc/ssh/ssh_host_ed25519_key"
  chmod 0644 "$root/etc/ssh/ssh_host_ed25519_key.pub"
  cp "$root/etc/ssh/ssh_host_ed25519_key.pub" "$boot/ssh_host_ed25519_key.pub"
  cat > "$root/etc/ssh/sshd_config.d/60-retroboot-gemini.conf" <<'EOF'
# RetroBoot on the Gemini PDA, debug flavor: only the host key generated at build time
# (its fingerprint is in the build's manifest, so it can be pinned before the first boot).
HostKey /etc/ssh/ssh_host_ed25519_key
EOF
  # Replaces customize-rootfs's ssh-keygen -A (which would add RSA and ECDSA keys at boot).
  cat > "$root/etc/systemd/system/ssh.service.d/50-hostkeys.conf" <<'EOF'
[Service]
ExecStartPre=
ExecStartPre=/bin/sh -c '[ -s /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key'
ExecStartPre=/usr/sbin/sshd -t
EOF
  in_root sh -c 'mkdir -p /run/sshd && /usr/sbin/sshd -t -f /etc/ssh/sshd_config; rc=$?; rm -rf /run/sshd; exit $rc' ||
    die "sshd -t failed"
  log "SSH host key: $(ssh-keygen -lf "$boot/ssh_host_ed25519_key.pub")"
fi

# ---- first boot work, debug bundle, data directory -------------------------
arm64_firstboot_done "$root"
arm64_pi_check "$flavor" "$root"
[ -d "$root/usr/share/retroboot/chimes" ] || stage_startup_chimes "$root"
log "data directory /var/lib/retroboot"
arm64_populate_data "$root/var/lib/retroboot" "$root" "$machine_dir" "$machine_id"

# ---- RetroBoot downloads sign-in (private builds, D028) -----------------------
# RB_GEMINI_LAB_AUTH=FILE: /etc/retroboot/lab-auth.env (0600, the lab keys only, as the
# installer media's) and /etc/retroboot/hostd.json naming it, so Download a Mac and the web
# storage page offer RetroBoot downloads beside the Internet catalog. It is part of the root,
# not of /var/lib/retroboot, so `gemini-install.sh --keep-data` keeps every build's own copy;
# hostd.image.json marks the key as the image's, so hostd never moves it into the overlay.
# The root then contains the download password, like the Wi-Fi profiles: keep the build
# private. Only whether it is there reaches the log and the manifest.
signin=false
if [ -n "${RB_GEMINI_LAB_AUTH:-}" ]; then
  install -d -m 0755 "$root/etc/retroboot"
  RB_ISO_LAB_AUTH=$RB_GEMINI_LAB_AUTH RB_ISO_LAB_URL='' \
    "$IMAGE_SRC/scripts/write-lab-auth.sh" "$root/etc/retroboot/lab-auth.env" 2>/dev/null ||
    die "RB_GEMINI_LAB_AUTH: write-lab-auth.sh failed (needs RB_LAB_HTTP_USER, RB_LAB_HTTP_PASS, RB_LAB_BASE_URL)"
  chown 0:0 "$root/etc/retroboot/lab-auth.env"; chmod 0600 "$root/etc/retroboot/lab-auth.env"
  python3 - "$root/etc/retroboot/hostd.json" "$root/usr/lib/retroboot/hostd/hostd.image.json" <<'PY'
import json, os, sys
etc, image = sys.argv[1:3]
try:
    cfg = json.load(open(etc))
except FileNotFoundError:
    cfg = {}
cfg["lab_http_auth_file"] = "/etc/retroboot/lab-auth.env"
for p in (etc, image):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(cfg, f, indent=1, sort_keys=True)
        f.write("\n")
    os.chmod(p, 0o644)
PY
  signin=true
  log "RetroBoot downloads sign-in: /etc/retroboot/lab-auth.env (private: the root contains the download password)"
fi
printf '{"present": %s}\n' "$signin" > "$boot/download-sign-in.json"
echo "$kver" > "$root/../kernel-version-$flavor"
du -sh "$root" | awk '{print "rootfs size: " $1}' >&2
