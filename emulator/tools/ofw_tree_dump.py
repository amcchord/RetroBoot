#!/usr/bin/env python3
"""Dump the complete OpenBIOS device tree of a mac99 machine as text.

Boots only the firmware (no guest disk) with the appliance's display and
device arguments, moves the Forth console to the serial socket from
boot-command, and walks every node from the root with a small
Forth program: one ``NODE <depth>`` line per node (depth-first, firmware child
order) followed by every property as ``  name=<hex bytes>``.  Two dumps are
byte-identical exactly when both firmwares built the same tree (names, order,
values, phandle references).  Used to show that a RetroBoot OpenBIOS without
the AirPort card builds the same tree as the pinned firmware.

usage: ofw_tree_dump.py --qemu BIN --support DIR [--firmware ELF] [--airport]
                        --out FILE [--extra-arg ARG ...]
"""
import argparse
import hashlib
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

# Short lines: the OpenBIOS line editor truncates long input lines.
FORTH = r"""
hex
: rbd-hex ( addr len -- )
  bounds ?do i c@ 0 <# # # #> type loop ;
: rbd-props ( ph -- )
  >r 0 0
  begin r@ next-property while
    cr ."   " 2dup type ." ="
    2dup r@ get-package-property
    0= if rbd-hex then
  repeat r> drop ;
: rbd-walk ( ph depth -- )
  >r begin dup while
    cr ." NODE " r@ .
    dup rbd-props
    dup child r@ 1+ recurse
    peer
  repeat drop r> drop ;
cr ." RBD" ." -BEGIN"
0 peer 0 rbd-walk
cr ." RBD" ." -END" cr
"""


def read_until(sock, marker: bytes, timeout: float) -> bytes:
    buf = b""
    end = time.monotonic() + timeout
    while marker not in buf:
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"no {marker!r} on the firmware console; got {buf[-400:]!r}")
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise TimeoutError(f"no {marker!r} on the firmware console; got {buf[-600:]!r}") from None
        if not chunk:
            raise EOFError("console closed")
        buf += chunk
    return buf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qemu", required=True)
    ap.add_argument("--support", required=True, help="share/qemu directory (-L)")
    ap.add_argument("--firmware", help="OpenBIOS ELF (-bios); default: the package's openbios-ppc")
    ap.add_argument("--airport", action="store_true", help="enable the emulated AirPort card")
    ap.add_argument("--via", default="cuda", choices=("cuda", "pmu", "pmu-adb"), help="mac99 VIA configuration")
    ap.add_argument("--battery", action="store_true", help="PMU battery bay (-global via-pmu.battery=on)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--extra-arg", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=90)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="rbd-ofw-") as tmp:
        console = os.path.join(tmp, "console.sock")
        argv = [a.qemu, "-accel", "tcg", "-M", f"mac99,via={a.via},audiodev=snd0", "-cpu", "7400", "-m", "512",
                "-L", a.support, "-vga", "std", "-global", "VGA.vgamem_mb=64", "-global", "VGA.host-resize=on",
                "-g", "1024x768x32", "-display", "none", "-audiodev", "none,id=snd0",
                # The appliance's output-device, plus a boot-command that moves the
                # Forth console to the serial port (ESCC channel A) instead of booting.
                "-prom-env", "output-device=ttya", "-prom-env", 'boot-command=" scca" io',
                "-drive", "if=ide,index=3,media=cdrom,id=cd0,readonly=on",
                "-drive", "if=ide,index=2,media=cdrom,id=tools0,readonly=on",
                "-nic", "user,model=sungem", "-device", "virtio-tablet-pci",
                "-chardev", f"socket,id=con,path={console},server=on,wait=off", "-serial", "chardev:con"]
        if a.firmware:
            argv += ["-bios", a.firmware]
        if a.airport:
            argv += ["-global", "macio-newworld.airport=true"]
        if a.battery:
            argv += ["-global", "via-pmu.battery=on"]
        argv += a.extra_arg
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            sock = None
            for _ in range(200):
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(console)
                    break
                except OSError:
                    sock.close()
                    sock = None
                    time.sleep(0.05)
            if sock is None:
                raise RuntimeError("firmware console socket did not appear")
            read_until(sock, b"0 > ", a.timeout)
            lines = FORTH.strip().splitlines()
            for line in lines[:-2]:
                sock.sendall(line.encode() + b"\r")
                read_until(sock, b"> ", 10)       # prompt: "0 > ", or "1 > " while compiling
            for line in lines[-2:]:
                sock.sendall(line.encode() + b"\r")
            raw = read_until(sock, b"RBD-END", a.timeout)
        finally:
            proc.kill()
            proc.wait()
    text = raw.decode("latin-1")
    body = text[text.rindex("RBD-BEGIN") + len("RBD-BEGIN"):text.rindex("RBD-END")]
    lines = []
    for ln in body.replace("\r", "").split("\n"):
        ln = ln.rstrip()
        if ln.endswith(" ok"):
            ln = ln[:-3].rstrip()
        if not lines and not ln.startswith("NODE"):
            continue                       # echo of the command line
        if ln.startswith("0 > "):
            break                          # prompt after the walk
        if ln:
            lines.append(ln)
    if not lines:
        raise SystemExit(f"unexpected console output: {body[:400]!r}")
    a.out.write_text("\n".join(lines) + "\n")
    nodes = sum(1 for ln in lines if ln.startswith("NODE"))
    print(f"{a.out}: {nodes} nodes, {len(lines) - nodes} properties, "
          f"sha256 {hashlib.sha256(a.out.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
