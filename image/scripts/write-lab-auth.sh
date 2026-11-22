#!/bin/bash
# Private installer ISOs only: write retroboot/lab-auth.env (hostd/retroboot/labauth.py) for
# make-iso.sh from RB_ISO_LAB_AUTH or RB_ISO_LAB_URL. Values are never printed.
#
#   RB_ISO_LAB_AUTH=FILE write-lab-auth.sh OUT   FILE's RB_LAB_HTTP_USER, RB_LAB_HTTP_PASS and
#                                                RB_LAB_BASE_URL lines only: OUT then holds the
#                                                download password, which the installer also
#                                                copies onto every computer it installs
#   RB_ISO_LAB_URL=URL  write-lab-auth.sh OUT    only RB_LAB_BASE_URL=URL (no secret): the
#                                                installer asks for the sign-in
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
out=${1:?output file}
( umask 077; : > "$out" )
chmod 0600 "$out"
if [ -n "${RB_ISO_LAB_AUTH:-}" ]; then
  [ -f "$RB_ISO_LAB_AUTH" ] || die "RB_ISO_LAB_AUTH is not a file"
  # Only the lab keys: a copy of a whole lab environment file carries other secrets.
  grep -E '^[[:space:]]*(export[[:space:]]+)?RB_LAB_(HTTP_USER|HTTP_PASS|BASE_URL)=' \
    "$RB_ISO_LAB_AUTH" > "$out" || true
  for k in RB_LAB_HTTP_USER RB_LAB_HTTP_PASS RB_LAB_BASE_URL; do
    grep -qE "^[[:space:]]*(export[[:space:]]+)?$k=." "$out" || { rm -f "$out"; die "RB_ISO_LAB_AUTH has no $k"; }
  done
  log "lab-auth.env: RetroBoot downloads with a sign-in (private ISO: it contains a password)"
elif [ -n "${RB_ISO_LAB_URL:-}" ]; then
  case "$RB_ISO_LAB_URL" in https://*) ;; *) rm -f "$out"; die "RB_ISO_LAB_URL must be an https:// address" ;; esac
  printf 'RB_LAB_BASE_URL=%s\n' "$RB_ISO_LAB_URL" > "$out"
  log "lab-auth.env: RetroBoot downloads location (the installer asks for the sign-in)"
else
  rm -f "$out"; die "set RB_ISO_LAB_AUTH or RB_ISO_LAB_URL"
fi
