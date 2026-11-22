#!/bin/sh
# Install hostd into an image root: packaging/install.sh DESTDIR
# (used by the image workstream; no network, no shell evaluation of inputs).
set -eu
DEST=${1:?usage: install.sh DESTDIR}
SRC=$(cd "$(dirname "$0")/.." && pwd)
install -d "$DEST/usr/lib/retroboot/hostd" "$DEST/usr/lib/systemd/system" \
           "$DEST/usr/lib/sysusers.d" "$DEST/usr/lib/tmpfiles.d"
rm -rf "$DEST/usr/lib/retroboot/hostd/retroboot"
cp -R "$SRC/retroboot" "$DEST/usr/lib/retroboot/hostd/retroboot"
find "$DEST/usr/lib/retroboot/hostd/retroboot" -name __pycache__ -prune -exec rm -rf {} +
install -m 0644 "$SRC/README.md" "$DEST/usr/lib/retroboot/hostd/README.md"
install -m 0644 "$SRC/packaging/retroboot-hostd.service" "$SRC/packaging/retroboot-guest.service" \
        "$SRC/packaging/retroboot-slot-guard.service" "$SRC/packaging/retroboot-guestnet.service" \
        "$DEST/usr/lib/systemd/system/"
install -m 0644 "$SRC/packaging/retroboot.sysusers" "$DEST/usr/lib/sysusers.d/retroboot.conf"
install -m 0644 "$SRC/packaging/retroboot.tmpfiles" "$DEST/usr/lib/tmpfiles.d/retroboot.conf"
install -d "$DEST/etc/systemd/system/multi-user.target.wants"
ln -sf /usr/lib/systemd/system/retroboot-hostd.service \
       "$DEST/etc/systemd/system/multi-user.target.wants/retroboot-hostd.service"
ln -sf /usr/lib/systemd/system/retroboot-guestnet.service \
       "$DEST/etc/systemd/system/multi-user.target.wants/retroboot-guestnet.service"
# Software Update: the trial-boot guard (runs only with retroboot.trial=1).
install -d "$DEST/etc/systemd/system/sysinit.target.wants"
ln -sf /usr/lib/systemd/system/retroboot-slot-guard.service \
       "$DEST/etc/systemd/system/sysinit.target.wants/retroboot-slot-guard.service"
# The Modern Web Helper (webproxy/) is a separate unprivileged service hostd starts and stops.
if [ -x "$SRC/../webproxy/packaging/install.sh" ]; then
  "$SRC/../webproxy/packaging/install.sh" "$DEST"
fi
echo "hostd installed into $DEST (Debian deps: python3-aiohttp python3-cryptography python3-qrcode nftables)"
