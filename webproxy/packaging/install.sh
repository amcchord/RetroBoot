#!/bin/sh
# Install the Modern Web Helper into an image root: packaging/install.sh DESTDIR
# (called by hostd/packaging/install.sh; no network, no shell evaluation of inputs).
# Debian runtime: python3-aiohttp python3-lxml python3-readability python3-pil
# python3-cairosvg nftables.
set -eu
DEST=${1:?usage: install.sh DESTDIR}
SRC=$(cd "$(dirname "$0")/.." && pwd)
install -d "$DEST/usr/lib/retroboot/webproxy" "$DEST/usr/lib/systemd/system"
rm -rf "$DEST/usr/lib/retroboot/webproxy/rbwebproxy"
cp -R "$SRC/rbwebproxy" "$DEST/usr/lib/retroboot/webproxy/rbwebproxy"
find "$DEST/usr/lib/retroboot/webproxy/rbwebproxy" -name __pycache__ -prune -exec rm -rf {} +
install -m 0644 "$SRC/README.md" "$DEST/usr/lib/retroboot/webproxy/README.md"
install -m 0644 "$SRC/packaging/retroboot-webproxy.service" "$DEST/usr/lib/systemd/system/"
echo "Modern Web Helper installed into $DEST"
