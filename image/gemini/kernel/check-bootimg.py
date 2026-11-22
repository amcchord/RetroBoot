#!/usr/bin/env python3
"""Decode and verify a Gemini PDA boot.img against what the stock MediaTek LK expects.

Checks (stdlib only):
  * Android boot image v0 header: magic, LK's load addresses, page size 2048, no second
    stage, header id (SHA-1 as mkbootimg computes it), image fits the 16 MiB p22 slot;
  * header cmdline carries bootopt=64S3,32N2,64N2 (LK parses it; without it LK hangs);
  * kernel payload = one gzip member + the appended FDT; both of LK's backward byte-wise
    FDT scans (gemini-lk app/mt_boot/mt_boot.c: the early 512 KiB window read from the
    partition, and the whole-payload scan before decompression) land on that FDT;
  * the gzip member decompresses to an arm64 Image no larger than LK's 0x1C00000
    (28 MiB) output buffer, and its image_size ends below the tags address;
  * the FDT parses, has the Gemini compatible and matches --dtb when given;
  * the ramdisk is a gzip newc cpio with /init (executable), /bin/busybox, /sbin/e2fsck
    and /dev/console, and matches --initrd when given.
Exit status 0 when every check passes; prints a report (and --json writes one).
"""
import argparse
import gzip
import hashlib
import io
import json
import struct
import sys
import zlib

BASE = 0x40000000
WANT = {
    "kernel_addr": BASE + 0x00200000,
    "second_addr": BASE + 0x00F00000,
    "ramdisk_addr": BASE + 0x05000000,
    "tags_addr": BASE + 0x04000000,
    "page_size": 2048,
}
PARTITION_SIZE = 16 * 1024 * 1024
BOOTOPT = "bootopt=64S3,32N2,64N2"
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
LK_DTB_WINDOW = 512 * 1024          # DTB_MAX_SIZE
LK_DECOMPRESS_MAX = 0x1C00000       # decompress_outbuf_size
COMPATIBLE = b"planet,gemini-pda"


class Report:
    def __init__(self):
        self.checks = []
        self.info = {}

    def check(self, name, ok, detail=""):
        self.checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})
        return ok

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks)


def pages(n, page):
    return (n + page - 1) // page * page


def lk_window_scan(img, page, ksize):
    """bldr_load_dtb(): read DTB_MAX_SIZE bytes from ROUNDUP(page + ksize - window, page),
    scan backward from the window end - 4; return the matched image offset or None."""
    offset = page + ksize - LK_DTB_WINDOW
    start = (offset + page - 1) // page * page
    buf = img[start:start + LK_DTB_WINDOW].ljust(LK_DTB_WINDOW, b"\0")
    for i in range(LK_DTB_WINDOW - 4, 0, -1):     # LK: i < DTB_MAX_SIZE - 4 iterations
        if buf[i:i + 4] == FDT_MAGIC:
            return start + i
    return None


def lk_payload_scan(payload):
    """boot_linux_from_storage(): scan backward from the payload end; return the offset."""
    for i in range(len(payload), -1, -1):
        if payload[i:i + 4] == FDT_MAGIC:
            return i
    return None


def parse_fdt(blob):
    """Minimal FDT walk: root compatible/model and /chosen presence."""
    magic, totalsize, off_struct, off_strings, _, version = struct.unpack(">6I", blob[:24])
    strings = blob[off_strings:]
    pos = off_struct
    depth = 0
    path = []
    props = {}
    while pos < totalsize:
        (tok,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        if tok == 1:  # BEGIN_NODE
            end = blob.index(b"\0", pos)
            name = blob[pos:end].decode()
            pos = (end + 4) & ~3
            path.append(name)
            depth += 1
        elif tok == 2:  # END_NODE
            path.pop()
            depth -= 1
        elif tok == 3:  # PROP
            ln, nameoff = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            pname = strings[nameoff:strings.index(b"\0", nameoff)].decode()
            props["/".join(path) + ":" + pname] = blob[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
        elif tok == 4:  # NOP
            continue
        elif tok == 9:  # END
            break
        else:
            raise ValueError(f"bad FDT token {tok} at {pos - 4}")
    return {"totalsize": totalsize, "version": version, "props": props}


def parse_cpio(data):
    """newc entries: name -> (mode, size, rdevmajor, rdevminor)."""
    out = {}
    pos = 0
    while True:
        hdr = data[pos:pos + 110]
        if hdr[:6] != b"070701":
            raise ValueError(f"bad cpio magic at {pos}")
        f = [int(hdr[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        mode, filesize, rmaj, rmin, namesize = f[1], f[6], f[9], f[10], f[11]
        name = data[pos + 110:pos + 110 + namesize - 1].decode()
        pos = (pos + 110 + namesize + 3) & ~3
        if name == "TRAILER!!!":
            break
        out[name] = (mode, filesize, rmaj, rmin)
        pos = (pos + filesize + 3) & ~3
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--dtb", help="the DTB the payload must end with")
    ap.add_argument("--initrd", help="the ramdisk the image must carry")
    ap.add_argument("--payload", help="the Image.gz-dtb the kernel blob must equal")
    ap.add_argument("--json", help="write the report here")
    a = ap.parse_args()
    r = Report()
    img = open(a.image, "rb").read()

    r.check("image fits the 16 MiB boot partition", len(img) <= PARTITION_SIZE, f"{len(img)} bytes")
    r.check("magic ANDROID!", img[:8] == b"ANDROID!")
    (ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, hver, osver) = struct.unpack("<10I", img[8:48])
    name = img[48:64].rstrip(b"\0").decode(errors="replace")
    cmdline = img[64:576].split(b"\0", 1)[0].decode(errors="replace")
    hid = img[576:608]
    extra = img[608:1632].split(b"\0", 1)[0].decode(errors="replace")
    r.info["header"] = {"kernel_size": ksize, "kernel_addr": hex(kaddr), "ramdisk_size": rsize,
                        "ramdisk_addr": hex(raddr), "second_size": ssize, "second_addr": hex(saddr),
                        "tags_addr": hex(tags), "page_size": page, "header_version": hver,
                        "os_version": osver, "name": name, "cmdline": cmdline, "extra_cmdline": extra}
    for k, v in (("kernel_addr", kaddr), ("second_addr", saddr), ("ramdisk_addr", raddr),
                 ("tags_addr", tags), ("page_size", page)):
        r.check(f"header {k} = {WANT[k]:#x}", v == WANT[k], f"{v:#x}")
    r.check("header version 0", hver == 0, hver)
    r.check("no second stage", ssize == 0, ssize)
    r.check("kernel_addr 512 KiB aligned (LK 64-bit rule)", kaddr & 0x7FFFF == 0)
    r.check(f"cmdline has {BOOTOPT}", BOOTOPT in cmdline.split(), cmdline)

    koff = page
    roff = koff + pages(ksize, page)
    end = roff + pages(rsize, page)
    r.check("image length = header + kernel + ramdisk pages", len(img) == end, f"{len(img)} vs {end}")
    payload = img[koff:koff + ksize]
    ramdisk = img[roff:roff + rsize]
    sha = hashlib.sha1()
    for blob in (payload, ramdisk, b""):
        sha.update(blob)
        sha.update(struct.pack("<I", len(blob)))
    r.check("header id = SHA-1 of the blobs", hid[:20] == sha.digest(), hid[:20].hex())

    if a.payload:
        r.check("kernel blob = Image.gz-dtb", payload == open(a.payload, "rb").read())

    # Kernel payload: gzip member, then the FDT.
    r.check("kernel payload starts with gzip magic", payload[:2] == b"\x1f\x8b")
    d = zlib.decompressobj(wbits=31)
    image = d.decompress(payload) + d.flush()
    tail = d.unused_data
    gz_len = len(payload) - len(tail)
    r.check("one gzip member followed by the FDT", d.eof and tail[:4] == FDT_MAGIC, f"gzip {gz_len} bytes, tail {len(tail)} bytes")
    r.check(f"decompressed Image ≤ {LK_DECOMPRESS_MAX:#x} (LK output buffer)", len(image) <= LK_DECOMPRESS_MAX, len(image))
    r.check("arm64 Image magic", image[0x38:0x3C] == b"ARM\x64")
    text_offset, image_size, flags = struct.unpack("<QQQ", image[8:32])
    r.check("Image (with BSS) ends below the tags address", kaddr + image_size < tags, f"image_size {image_size}")
    r.check("Image ends below LK (0x41E00000)", kaddr + len(image) <= 0x41E00000, f"{kaddr + len(image):#x}")
    r.info["kernel"] = {"payload": len(payload), "gzip": gz_len, "image": len(image), "image_size": image_size,
                        "text_offset": text_offset, "flags": flags,
                        "image_sha256": hashlib.sha256(image).hexdigest()}

    fdt = tail
    if fdt[:4] == FDT_MAGIC:
        info = parse_fdt(fdt)
        r.check("FDT totalsize = appended bytes", info["totalsize"] == len(fdt), f"{info['totalsize']} vs {len(fdt)}")
        compat = info["props"].get(":compatible", b"")
        r.check("FDT root compatible planet,gemini-pda", COMPATIBLE in compat.split(b"\0"), compat)
        r.check("FDT has /chosen", any(k.startswith("/chosen:") or k.startswith("chosen:") or "/chosen" in k
                                       for k in info["props"]) or b"chosen" in fdt)
        r.check("no FDT magic inside the FDT body", fdt.find(FDT_MAGIC, 1) == -1)
        r.info["fdt"] = {"size": len(fdt), "version": info["version"],
                         "model": info["props"].get(":model", b"").rstrip(b"\0").decode(errors="replace"),
                         "sha256": hashlib.sha256(fdt).hexdigest(), "offset_in_payload": gz_len}
        if a.dtb:
            r.check("FDT = built DTB", fdt == open(a.dtb, "rb").read())
    want_img_off = koff + gz_len
    w = lk_window_scan(img, page, ksize)
    r.check("LK 512 KiB window scan finds the appended FDT", w == want_img_off, f"found {w}, want {want_img_off}")
    s = lk_payload_scan(payload)
    r.check("LK payload scan finds the appended FDT", s == gz_len, f"found {s}, want {gz_len}")

    # Ramdisk.
    try:
        cpio = gzip.decompress(ramdisk)
        r.check("ramdisk is gzip", True, f"{len(ramdisk)} -> {len(cpio)} bytes")
        ents = parse_cpio(cpio)
        need = {"init": 0o100000, "bin/busybox": 0o100000, "sbin/e2fsck": 0o100000}
        for n, kind in need.items():
            m = ents.get(n)
            r.check(f"ramdisk /{n} is an executable file", m and (m[0] & 0o170000) == kind and m[0] & 0o111, m and oct(m[0]))
        con = ents.get("dev/console")
        r.check("ramdisk /dev/console is char 5:1", con and (con[0] & 0o170000) == 0o020000 and con[2:] == (5, 1), con)
        r.check("ramdisk /lib/retroboot/charger.sh present", "lib/retroboot/charger.sh" in ents)
        r.info["ramdisk"] = {"size": len(ramdisk), "uncompressed": len(cpio), "entries": len(ents),
                             "listing": sorted(n for n in ents if not n.startswith("bin/") or n == "bin/busybox")}
    except Exception as e:  # noqa: BLE001
        r.check("ramdisk is a gzip newc cpio", False, e)
    if a.initrd:
        r.check("ramdisk = initrd file", ramdisk == open(a.initrd, "rb").read())

    r.info["boot_img"] = {"size": len(img), "sha256": hashlib.sha256(img).hexdigest(),
                          "headroom": PARTITION_SIZE - len(img)}
    for c in r.checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"] + (f"  [{c['detail']}]" if c["detail"] else ""))
    print(json.dumps(r.info, indent=2, default=str))
    print("RESULT:", "PASS" if r.ok else "FAIL")
    if a.json:
        json.dump({"ok": r.ok, "checks": r.checks, "info": r.info}, open(a.json, "w"), indent=2, default=str)
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
