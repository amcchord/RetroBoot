#!/bin/bash
# Build-level checks of a Gemini PDA root, partly inside it through qemu-user (no boot).
#
#   check-gemini.sh FLAVOR ROOTDIR KERNEL_STAGE [MACHINE_ID]
#
# No Raspberry Pi or x86 boot pieces and no partition in the fstab; the kernel's modules
# (depmod clean, every module the units load present); the pinned firmware; the units
# (enabled, `systemd-analyze verify` clean); Wi-Fi profiles and the usb0 profile (0600,
# root); SSH (debug) or none (product); hostd imports and detects a board; the embedded
# Mac OS machine (private builds); every ELF AArch64. Prints a report; exits 1 on the
# first failed check.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
require_root
flavor=${1:?flavor}; root=${2:?rootdir}; kstage=${3:?kernel stage}; machine_id=${4:-}
kver=$(gcfg kernel_version)
fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }
in_root() { chroot "$root" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 "$@"; }
mounted=()
cleanup() { for ((i=${#mounted[@]}-1; i>=0; i--)); do umount -l "${mounted[i]}" 2>/dev/null || true; done; }
trap cleanup EXIT

# ---- what must not be there ------------------------------------------------
pk=$(in_root dpkg-query -W -f='${Package}\n')
bad=$(echo "$pk" | grep -E '^(linux-image-|linux-headers-|raspi-firmware|raspberrypi-|grub|shim-|initramfs-tools|efibootmgr|firmware-brcm80211|bluez-firmware|intel-microcode|amd64-microcode|sddm|plasma-|kde|calamares|labwc|mesa-panfrost)' || true)
[ -z "$bad" ] || fail "boot or desktop packages in the root: $(echo $bad)"
for p in boot/firmware boot/efi usr/lib/retroboot/image/rb-bootdisplay-rpi etc/initramfs-tools usr/lib/raspi-firmware; do
  [ ! -e "$root/$p" ] || fail "/$p exists"
done
[ -z "$(ls -A "$root/boot" 2>/dev/null)" ] || fail "/boot is not empty: $(ls "$root/boot" | paste -sd' ' -)"
grep -qE '^[^#]' "$root/etc/fstab" && fail "fstab has entries: $(grep -E '^[^#]' "$root/etc/fstab")"
grep -qE 'RBROOT_A|RBROOT_B|RBDATA|RBEFI' <(grep -v '^#' "$root/etc/fstab") && fail "fstab names RetroBoot partitions"
ok "no kernel, initramfs, GRUB, Raspberry Pi or desktop packages; /boot empty; fstab without entries"

# ---- kernel modules --------------------------------------------------------
md=$root/usr/lib/modules/$kver
[ -f "$md/modules.dep" ] && [ -f "$md/modules.order" ] || fail "no modules.dep/modules.order for $kver"
[ "$(ls "$root/usr/lib/modules")" = "$kver" ] || fail "module trees: $(ls "$root/usr/lib/modules" | paste -sd' ' -)"
[ -s "$kstage/../boot/depmod.log" ] && fail "depmod warnings: $(head -3 "$kstage/../boot/depmod.log")"
[ -s "$kstage/../boot/depmod-check.log" ] && fail "depmod -e: $(head -3 "$kstage/../boot/depmod-check.log")"
want="mtk_wcn wlan_gen3 hci_stp $(grep -hv '^\s*#' "$root"/etc/modules-load.d/retroboot-gemini.conf | awk NF | paste -sd' ' -)"
missing=
for m in $want; do
  in_root modinfo -k "$kver" -n "$m" >/dev/null 2>&1 || missing="$missing $m"
done
[ -z "$missing" ] || fail "modules missing from $kver:$missing"
ok "modules $kver: $(find "$md" -name '*.ko*' | wc -l) files, depmod clean$([ -f "$kstage/System.map" ] && echo ' (no unresolved symbols against System.map)'); present: $want"
cmp -s "$kstage/boot.img" "$root/usr/lib/retroboot/gemini/boot/boot.img" || fail "the root's boot.img is not the kernel stage's"
ok "boot.img in the root: $(sha256sum "$kstage/boot.img" | cut -c1-16)... ($(stat -c %s "$kstage/boot.img") bytes; kernel build $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("build_id"))' "$kstage/kernel.json"))"

# ---- firmware --------------------------------------------------------------
while IFS='|' read -r dest _ sha; do
  [ -n "$dest" ] || continue
  [ "$(sha256sum "$root$dest" 2>/dev/null | cut -d' ' -f1)" = "$sha" ] || fail "$dest missing or not the pinned file"
done < <(gcfg_all upstream_file)
[ ! -e "$root/usr/lib/firmware/WIFI_factory.bin" ] && [ -z "$(find "$root/data" -type f 2>/dev/null)" ] || fail "a Wi-Fi NVRAM record is in the root"
ok "pinned upstream files: $(gcfg_all upstream_file | cut -d'|' -f1 | xargs -n1 basename | paste -sd' ' -); no Wi-Fi NVRAM record shipped"

# ---- network ---------------------------------------------------------------
nm=$root/var/lib/retroboot/network/connections
[ "$(stat -c '%a %u' "$nm")" = "700 0" ] || fail "$nm is $(stat -c '%a %u' "$nm")"
n=0
for f in "$nm"/*.nmconnection; do
  [ -f "$f" ] || continue
  [ "$(stat -c '%a %u %g' "$f")" = "600 0 0" ] || fail "$(basename "$f") is $(stat -c '%a %u %g' "$f")"
  n=$((n + 1))
done
usb0=$root/usr/lib/NetworkManager/system-connections/retroboot-usb0.nmconnection
[ "$(stat -c '%a %u' "$usb0")" = "600 0" ] && grep -qx "address1=$(gcfg usb0_address)" "$usb0" && grep -qx 'never-default=true' "$usb0" ||
  fail "usb0 profile"
grep -qx 'path=/var/lib/retroboot/network/connections' "$root/etc/NetworkManager/conf.d/50-retroboot-keyfile.conf" || fail "NetworkManager keyfile path"
ok "Wi-Fi profiles: $n in /var/lib/retroboot/network/connections (0600 root; NetworkManager's keyfile directory), usb0 $(gcfg usb0_address) (0600, never default)"
la=$root/etc/retroboot/lab-auth.env
if [ -e "$la" ]; then
  [ "$(stat -c '%a %u %g' "$la")" = "600 0 0" ] || fail "lab-auth.env is $(stat -c '%a %u %g' "$la")"
  [ "$(grep -cE '^(export )?RB_LAB_(HTTP_USER|HTTP_PASS|BASE_URL)=.' "$la")" = 3 ] &&
    [ "$(grep -cv '^[[:space:]]*$' "$la")" = 3 ] || fail "lab-auth.env must hold exactly the three RB_LAB_ keys"
  python3 - "$root/etc/retroboot/hostd.json" "$root/usr/lib/retroboot/hostd/hostd.image.json" <<'PY' || fail "hostd.json/hostd.image.json do not name /etc/retroboot/lab-auth.env"
import json, sys
etc, image = (json.load(open(p)) for p in sys.argv[1:3])
assert etc.get("lab_http_auth_file") == "/etc/retroboot/lab-auth.env" and etc == image
PY
  ok "RetroBoot downloads sign-in: /etc/retroboot/lab-auth.env (0600 root, three keys), named by hostd.json and hostd.image.json"
else
  ok "no RetroBoot downloads sign-in (RB_GEMINI_LAB_AUTH unset): Internet catalog only until one is entered"
fi

# ---- units -----------------------------------------------------------------
units="retroboot-gemini-wifi-nvram.service retroboot-gemini-wifi.service retroboot-gemini-bt.service
       retroboot-gemini-wifi-fixup.service retroboot-gemini-wifi-fixup.path retroboot-gemini-battery-guard.service
       retroboot-gemini-audio.service retroboot-gemini-backlight.service retroboot-gemini-boot-ok.service
       retroboot-hostd.service retroboot-presenter.service NetworkManager.service systemd-zram-setup@zram0.service"
for u in $units; do
  case "$u" in systemd-zram-setup@*) continue ;; esac
  [ "$(in_root systemctl is-enabled "$u" 2>/dev/null)" = enabled ] || fail "$u is not enabled"
done
for u in retroboot-growdata.service retroboot-bootdisplay.service retroboot-microcode.service retroboot-bootmark.service \
         var-log-journal.mount var-lib-bluetooth.mount retroboot-gemini-wdt-reboot.service; do
  st=$(in_root systemctl is-enabled "$u" 2>/dev/null || true)
  case "$st" in enabled|enabled-runtime) fail "$u is enabled" ;; esac
done
for u in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  [ "$(readlink "$root/etc/systemd/system/$u")" = /dev/null ] || fail "$u not masked"
done
[ -f "$root/etc/systemd/zram-generator.conf" ] && [ -x "$root/usr/lib/systemd/system-generators/zram-generator" ] || fail "zram generator"
for d in proc sys dev run; do mkdir -p "$root/$d"; done
mount -t proc proc "$root/proc"; mounted+=("$root/proc")
mount -t sysfs sys "$root/sys"; mounted+=("$root/sys")
mount --bind /dev "$root/dev"; mounted+=("$root/dev")
mount -t tmpfs tmpfs "$root/run"; mounted+=("$root/run")
vunits=$(cd "$root/usr/lib/systemd/system" && ls retroboot-gemini-* retroboot-hostd.service retroboot-presenter.service retroboot-data-layout.service 2>/dev/null | sed 's|^|/usr/lib/systemd/system/|' | paste -sd' ' -)
verify=$(in_root systemd-analyze verify --man=no --recursive-errors=no $vunits multi-user.target 2>&1 || true)
# A chroot has no running manager: drop what only says so.
verify=$(echo "$verify" | grep -vE 'Failed to (connect|read|determine)|System has not been booted|^$|Cannot add dependency job|Unit .* not found|is not a directory|sys-kernel-debug\.mount' || true)
[ -z "$verify" ] || fail "systemd-analyze verify: $verify"
ok "units: $(echo $units | wc -w) enabled (zram0 by the generator), growdata/bootdisplay/microcode/bootmark/RBDATA mounts off, suspend masked; systemd-analyze verify clean for multi-user.target and $(echo "$vunits" | wc -w) units: $(echo "$vunits" | xargs -n1 basename | paste -sd' ' -)"
# (systemctl list-dependencies refuses to run in a chroot: read the wants directories)
graph=$(ls "$root/etc/systemd/system/multi-user.target.wants" "$root/usr/lib/systemd/system/multi-user.target.wants" 2>/dev/null |
        grep -E '^(retroboot|NetworkManager|ssh|bluetooth|avahi)' | sort -u | paste -sd' ' - || true)
ok "multi-user.target wants: $graph"

# ---- SSH -------------------------------------------------------------------
if [ "$flavor" = debug ]; then
  [ -s "$root/etc/ssh/ssh_host_ed25519_key" ] && [ "$(stat -c '%a' "$root/etc/ssh/ssh_host_ed25519_key")" = 600 ] || fail "ssh host key"
  [ -z "$(ls "$root"/etc/ssh/ssh_host_*_key 2>/dev/null | grep -v ed25519)" ] || fail "other ssh host keys present"
  [ "$(stat -c '%a %u' "$root/root/.ssh/authorized_keys")" = "600 0" ] || fail "authorized_keys"
  in_root sh -c 'mkdir -p /run/sshd && /usr/sbin/sshd -t' || fail "sshd -t"
  eff=$(in_root sh -c 'mkdir -p /run/sshd && /usr/sbin/sshd -T -C user=root,host=x,addr=127.0.0.1' 2>/dev/null)
  echo "$eff" | grep -qx 'permitrootlogin without-password' && echo "$eff" | grep -qx 'passwordauthentication no' &&
    echo "$eff" | grep -qx 'hostkey /etc/ssh/ssh_host_ed25519_key' || fail "sshd effective config"
  [ "$(in_root systemctl is-enabled ssh.service)" = enabled ] || fail "ssh.service not enabled"
  ok "sshd (debug): key-only root login, lab key, host key $(ssh-keygen -lf "$root/etc/ssh/ssh_host_ed25519_key.pub" | awk '{print $2}') only, sshd -t clean"
else
  [ ! -e "$root/usr/sbin/sshd" ] || fail "sshd in the product flavor"
  [ "$(readlink "$root/etc/systemd/system/ssh.service")" = /dev/null ] || fail "ssh.service not masked"
  ok "product: no sshd (ssh.service masked)"
fi
grep -q "^REQUIRE_SSH=$([ "$flavor" = debug ] && echo 1 || echo 0)\$" "$root/usr/lib/retroboot/gemini/boot-counter.conf" || fail "boot-counter.conf REQUIRE_SSH"
ok "boot health: $(grep -E '^(COUNTER_FILE|COUNTER_RESET|LINUX_MOUNT|REQUIRE_SSH)=' "$root/usr/lib/retroboot/gemini/boot-counter.conf" | paste -sd' ' -)"

# ---- hostd and identity ----------------------------------------------------
smoke=$(in_root env PYTHONPATH=/usr/lib/retroboot/hostd PYTHONDONTWRITEBYTECODE=1 python3 -c '
import retroboot.hostd, retroboot.config, retroboot.platform as p
print(p.detect().family, retroboot.config.__name__)' 2>&1) || fail "hostd import: $smoke"
ok "hostd imports (retroboot.hostd, config, platform; platform.detect() in the chroot: $(echo "$smoke" | awk '{print $1}'))"
[ "$(cat "$root/etc/hostname")" = "$(gcfg hostname)" ] || fail "hostname"
[ -x "$root/usr/lib/retroboot/qemu/bin/qemu-system-ppc" ] && [ -x "$root/usr/lib/retroboot/bin/rb-presenter" ] || fail "emulator or presenter missing"
ok "hostname $(gcfg hostname); rb-presenter, qemu-system-ppc ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["qemu"].get("path","?").rsplit("/",1)[-1])' "$root/usr/lib/retroboot/components.json")) present"
if [ -n "$machine_id" ]; then
  m=$root/var/lib/retroboot/machines/$machine_id
  [ -f "$m/disk.img" ] && [ -f "$m/machine.json" ] || fail "embedded machine $machine_id incomplete"
  [ "$(stat -c %G "$m/disk.img" 2>/dev/null || true)" != root ] || [ -z "$(awk -F: '$1=="rbdata"' "$root/etc/group")" ] || fail "machine not group rbdata"
  ok "embedded machine $machine_id: disk.img $(stat -c %s "$m/disk.img") bytes ($(du -k "$m/disk.img" | cut -f1) KiB allocated), machine.json, Platinum bundle: $([ -d "$root/var/lib/retroboot/assets/platinum" ] && echo yes || echo no)"
else
  [ -z "$(ls -A "$root/var/lib/retroboot/machines" 2>/dev/null)" ] || fail "a machine is embedded in a non-private build"
  ok "no embedded machine (non-private build)"
fi
arm64_check_elves "$root" || exit 1
