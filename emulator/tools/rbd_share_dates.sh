#!/bin/bash
# rbd_share_dates.sh NAME TZ NDRVLOADER [rbd_share.py up args]
#
# Timestamp round trip through the shared folder in one Mac OS session whose
# clock follows TZ (QEMU -rtc base=localtime, as hostd starts it), with the
# driver log on (emulator/reports/sharefix2-20261012.md):
#   host -> Mac: a file (2026-10-12T09:43:00Z) and a folder (2026-01-15T12:00:00Z)
#                must be reported as those instants in the Mac clock's time;
#   Mac -> host: the Finder's Duplicate of each gives the copy its original's
#                date, which must come back to the host as the same UTC time.
# Works in $RB_WORK (default: the current directory): share-NAME/, runs/NAME/.
# Needs the seed os921-full-v2, package r11 and ndrv-r11 on the lab builder.
# KEEP=1 leaves the guest running.  Prints a JSON verdict.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "${RB_WORK:-.}"
T=$HERE/rbd_share.py
name=$1; tz=$2; drv=$3; shift 3
S=$PWD/share-$name; R=runs/$name
rm -rf $S $R; mkdir -p $S/ts-dir
echo "host file" > $S/ts-host.txt; echo inner > $S/ts-dir/inner.txt
touch -d @1791798180 $S/ts-host.txt        # 2026-10-12T09:43:00Z
touch -d @1768478400 $S/ts-dir/inner.txt $S/ts-dir   # 2026-01-15T12:00:00Z (winter)
python3 $T up --run $R --seed /srv/retroboot/int/os921-full-v2/disk.img --pkg /srv/retroboot/wave2/artifacts/qemu/linux-ppc-r11 --ndrv /srv/retroboot/wave2/artifacts/ndrv-r11 --share $S --ndrvloader $drv --clock localtime --tz $tz --driver-log "$@" >/dev/null || exit 1
c() { python3 $T ctl --run $R "$@" >/dev/null; }
c "{\"cmd\":\"click\",\"x\":1231,\"y\":115,\"double\":true}"; sleep 8
c "{\"cmd\":\"click\",\"x\":300,\"y\":35}"; sleep 1
c "{\"cmd\":\"type\",\"text\":\"ts-h\"}"; sleep 1
c "{\"cmd\":\"drag\",\"x0\":53,\"y0\":9,\"x1\":88,\"y1\":145,\"steps\":8,\"step_delay\":0.05,\"hold_after\":0.4}"; sleep 5
c "{\"cmd\":\"type\",\"text\":\"ts-d\"}"; sleep 1
c "{\"cmd\":\"drag\",\"x0\":53,\"y0\":9,\"x1\":88,\"y1\":145,\"steps\":8,\"step_delay\":0.05,\"hold_after\":0.4}"; sleep 6
c "{\"cmd\":\"click\",\"x\":250,\"y\":35}"; sleep 1
c "{\"cmd\":\"drag\",\"x0\":130,\"y0\":9,\"x1\":150,\"y1\":59,\"steps\":8,\"step_delay\":0.05,\"hold_after\":0.5}"; sleep 4
c "{\"cmd\":\"move\",\"x\":700,\"y\":500}"; sleep 2
python3 $T shot --run $R final >/dev/null
macnow=$(TZ=$tz date +%s)
PARSE=$HERE/rbd_driverlog_dates.py python3 - "$R/driver.log" "$S" "$tz" <<"PY"
import json, os, subprocess, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
log, share, tz = sys.argv[1:4]
p = json.loads(subprocess.run(["python3", os.environ["PARSE"], log], capture_output=True, text=True).stdout)
off = int(datetime.now(ZoneInfo(tz)).utcoffset().total_seconds())
E = 2082844800
def mac(u): return u + E + off
def fmt(m): return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(m - E)) if m else None
res = {"tz": tz, "offset_h": off / 3600, "driver_offset": p["offset_lines"]}
for n, u in (("ts-host.txt", 1791798180), ("ts-dir", 1768478400)):
    got = p["dates"].get(n)
    res[n] = {"mac": fmt(got), "expected": fmt(mac(u)), "ok": got == mac(u)}
for n, u in (("ts-host.txt copy", 1791798180), ("ts-dir copy", 1768478400)):
    st = os.stat(os.path.join(share, n)).st_mtime if os.path.exists(os.path.join(share, n)) else None
    res[n] = {"host_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st)) if st else None, "ok": st is not None and int(st) == u,
              "mac": fmt(p["dates"].get(n))}
uf = os.path.join(share, "untitled folder")
if os.path.exists(uf):
    st = int(os.stat(uf).st_mtime); got = p["dates"].get("untitled folder")
    res["untitled folder"] = {"host_utc": time.strftime("%H:%M:%S", time.gmtime(st)), "mac": fmt(got),
                              "mac_minus_expected_s": (got - mac(st)) if got else None}
res["sets"] = [s for s in p["sets"] if s["name"] and "ts-" in s["name"]]
print(json.dumps(res, indent=1))
PY
[ -n "${KEEP:-}" ] || { python3 $T down --run $R >/dev/null; rm -f $R/disk.img; }
