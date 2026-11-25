"""Provision a default LPR Desktop Printer in a stopped Mac OS 9 HFS+ disk.

Never ships proprietary Apple binaries or icons.  Constructs only standard
resource-fork records (``PAPA``, ``PORT``, ``SIZE``, ``STR#``, ``cnfg``,
``stop``, ``alis``, ``STR ``) referencing the guest's existing ``LaserWriter 8``,
``Desktop Printer Spooler``, and built-in ``Printer Descriptions`` PPD.
"""
from __future__ import annotations

import os
import shutil
import struct
import uuid

from . import hfsplus, prep

DEFAULT_NAME = "RetroBoot Printer"
DEFAULT_HOST = "10.0.2.2"
DEFAULT_QUEUE = "retroboot"
LASERWRITER = "LaserWriter 8"
PAPA_TYPE = "LaserWriter"
PAPA_ZONE = "=LPR"
DESKTOP_PRINTER_ALIAS_ID = 18772
STR_CUR_PRINTER_ID = -8192

PRINTER_EXTENSIONS = (
    "Desktop Printer Spooler",
    "Desktop PrintMonitor",
    "Desktop Printer Menu",
)
PRINTER_CONTROL_STRIP = ("Printer Selector",)

PRINTER_FILE_TYPES = (b"dtpl", b"dplt", b"dtpc")
PRINTER_CREATOR = b"dcdc"
SPOOLER_TYPE_CREATOR = (b"appe", b"dtps")
CONFIG_TYPE_CREATOR = (b"dtpc", b"dcdc")
DB_TYPE_CREATOR = (b"dtpl", b"dcdc")

SIZE_RESOURCE = bytes.fromhex("5cc0000578000003e800")
SYS_HEAP_REF_ATTRS = 0xC0  # resSysRef (0x80) | resSysHeap (0x40)


def _pstr(text: str, limit: int = 255) -> bytes:
    raw = text.encode("mac_roman")[:limit]
    return bytes([len(raw)]) + raw


def build_papa(name: str = DEFAULT_NAME, host: str = DEFAULT_HOST,
               queue: str = DEFAULT_QUEUE) -> bytes:
    """Build a 1024-byte LaserWriter 8 LPR ``'PAPA'`` (-8192) resource."""
    header = _pstr(name, 32) + _pstr(PAPA_TYPE, 32) + _pstr(PAPA_ZONE, 32)
    if len(header) > 104:
        raise ValueError("PAPA header fields exceed 104 bytes")
    buf = bytearray(1024)
    buf[:len(header)] = header

    tags = bytearray(b"TAGS\x00\x02\x00\x04")
    for tag, val in ((b"Q   ", queue.encode("mac_roman")),
                     (b"TCP ", host.encode("mac_roman")),
                     (b"TYPE", PAPA_ZONE.encode("mac_roman"))):
        tags.extend(tag)
        tags.extend(struct.pack(">H", len(val)))
        tags.extend(val)
        if len(val) & 1:
            tags.append(0)
    if 104 + len(tags) > 1024:
        raise ValueError("PAPA record exceeds 1024 bytes")
    buf[104:104 + len(tags)] = tags
    return bytes(buf)


def parse_papa_lpr(data: bytes) -> dict | None:
    """Extract ``(name, type, zone, host, queue)`` from a ``'PAPA'`` resource."""
    if len(data) < 108:
        return None
    pos = 0
    fields = []
    for _ in range(3):
        if pos >= 104:
            return None
        ln = data[pos]
        pos += 1
        if pos + ln > 104:
            return None
        fields.append(data[pos:pos + ln].decode("mac_roman", errors="replace"))
        pos += ln
    tags_out: dict[str, str] = {}
    if data[104:108] == b"TAGS":
        p = 112
        while p + 6 <= len(data):
            tag = data[p:p + 4]
            if tag == b"\x00\x00\x00\x00":
                break
            vlen = struct.unpack(">H", data[p + 4:p + 6])[0]
            p += 6
            if p + vlen > len(data):
                break
            tags_out[tag.decode("mac_roman", errors="replace")] = data[p:p + vlen].decode(
                "mac_roman", errors="replace"
            )
            p += vlen + (vlen & 1)
    return {
        "name": fields[0],
        "type": fields[1],
        "zone": fields[2],
        "host": tags_out.get("TCP ", ""),
        "queue": tags_out.get("Q   ", ""),
        "tag_type": tags_out.get("TYPE", ""),
    }


def build_str_list(items: tuple[str, ...] | list[str]) -> bytes:
    """Build a Mac OS ``'STR#'`` string list resource."""
    out = bytearray(struct.pack(">H", len(items)))
    for item in items:
        out.extend(_pstr(item))
    return bytes(out)


def build_cnfg(driver: str = LASERWRITER) -> bytes:
    """Build the 40-byte ``'cnfg'`` (id=1) Desktop Printer configuration record."""
    buf = bytearray(40)
    prefix = b"nlprNLPR" + _pstr(driver, 31)
    buf[:len(prefix)] = prefix
    return bytes(buf)


def build_alias(
    *,
    kind: int,
    vol_name: str,
    vol_cdate: int,
    parent_name: str,
    parent_cnid: int,
    target_name: str,
    target_cnid: int,
    target_cdate: int,
    full_path: str,
    type_creator: tuple[bytes, bytes] | None = None,
    cnid_chain: tuple[int, ...] | None = None,
) -> bytes:
    """Build a Mac OS Alias v2 (``'alis'``) record matching Alias Manager layout."""
    hdr = bytearray(150)
    struct.pack_into(">HH", hdr, 6, 2, kind)
    vp = _pstr(vol_name, 27)
    hdr[10:10 + len(vp)] = vp
    struct.pack_into(">I", hdr, 38, vol_cdate)
    hdr[42:46] = b"BD\x00\x00"
    struct.pack_into(">I", hdr, 46, parent_cnid)
    tp = _pstr(target_name, 63)
    hdr[50:50 + len(tp)] = tp
    struct.pack_into(">II", hdr, 114, target_cnid, target_cdate)
    if type_creator is not None:
        hdr[122:130] = type_creator[0] + type_creator[1]
    struct.pack_into(">hhHH", hdr, 130, -1, -1, 0, 0)

    tags = bytearray()

    def _add_tag(code: int, val: bytes) -> None:
        tags.extend(struct.pack(">HH", code, len(val)))
        tags.extend(val)
        if len(val) & 1:
            tags.append(0)

    _add_tag(0, parent_name.encode("mac_roman"))
    chain = cnid_chain if cnid_chain is not None else (parent_cnid,)
    _add_tag(1, b"".join(struct.pack(">I", c) for c in chain))
    _add_tag(2, full_path.encode("mac_roman"))
    tags.extend(struct.pack(">HH", 0xFFFF, 0))

    out = hdr + tags
    struct.pack_into(">H", out, 4, len(out))
    return bytes(out)


def upsert_resource_fork(
    fork: bytes,
    updates: list[tuple[str, int, str, int, bytes]],
) -> bytes:
    """Insert or replace ``(rtype, rid, name, attrs, data)`` in ``fork``,
    preserving all existing resource attributes, names, order, and map flags."""
    if not fork or len(fork) < 16:
        return hfsplus.build_resource_fork([(t, i, n, d) for t, i, n, _a, d in updates])
    doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
    if moff + mlen > len(fork) or doff + dlen > len(fork):
        raise hfsplus.HFSError("resource fork header out of range")
    rmap = fork[moff:moff + mlen]
    rmap_meta = rmap[16:24]
    tl_off, nl_off = struct.unpack(">HH", rmap[24:28])
    tlist = rmap[tl_off:]
    nlist = rmap[nl_off:]
    ntypes = (struct.unpack(">H", tlist[:2])[0] + 1) & 0xFFFF

    items: list[list] = []
    seen: dict[tuple[str, int], int] = {}
    for i in range(ntypes):
        rtype_b, count, ref = struct.unpack(">4sHH", tlist[2 + 8 * i:10 + 8 * i])
        rtype = rtype_b.decode("mac_roman")
        for j in range(count + 1):
            rid, noff, attr_off = struct.unpack(">hHI", tlist[ref + 12 * j:ref + 12 * j + 8])
            attrs = (attr_off >> 24) & 0xFF
            data_off = doff + (attr_off & 0xFFFFFF)
            length = struct.unpack(">I", fork[data_off:data_off + 4])[0]
            name_raw = b""
            if noff != 0xFFFF:
                nlen = nlist[noff]
                name_raw = nlist[noff + 1:noff + 1 + nlen]
            blob = fork[data_off + 4:data_off + 4 + length]
            seen[(rtype, rid)] = len(items)
            items.append([rtype, rid, name_raw, attrs, blob])

    for rtype, rid, name_str, attrs, blob in updates:
        nraw = name_str.encode("mac_roman") if name_str else b""
        key = (rtype, rid)
        if key in seen:
            items[seen[key]] = [rtype, rid, nraw, attrs, blob]
        else:
            seen[key] = len(items)
            items.append([rtype, rid, nraw, attrs, blob])

    data = bytearray()
    offsets: list[int] = []
    by_type: dict[str, list[int]] = {}
    for idx, (rtype, _rid, _nraw, _attrs, blob) in enumerate(items):
        offsets.append(len(data))
        data += struct.pack(">I", len(blob)) + blob
        by_type.setdefault(rtype, []).append(idx)

    type_list = bytearray(struct.pack(">H", (len(by_type) - 1) & 0xFFFF))
    refs = bytearray()
    names = bytearray()
    ref_base = 2 + 8 * len(by_type)
    for rtype, idxs in by_type.items():
        type_list += rtype.encode("mac_roman") + struct.pack(">HH", len(idxs) - 1, ref_base + len(refs))
        for idx in idxs:
            _rt, rid, nraw, attrs, _b = items[idx]
            noff = 0xFFFF
            if nraw:
                noff = len(names)
                names += bytes([len(nraw)]) + nraw
            attr_off = ((attrs & 0xFF) << 24) | (offsets[idx] & 0xFFFFFF)
            refs += struct.pack(">hHII", rid, noff, attr_off, 0)

    types_and_refs = type_list + refs
    new_rmap = bytearray(16) + rmap_meta + struct.pack(">HH", 28, 28 + len(types_and_refs)) + types_and_refs + names
    header = struct.pack(">IIII", 256, 256 + len(data), len(data), len(new_rmap))
    new_rmap[0:16] = header
    return header + fork[16:256] + bytes(data) + bytes(new_rmap)


def choose_guest_ppd(names: list[str]) -> str:
    """Pick the best built-in PostScript PPD name from ``Printer Descriptions``."""
    cleaned = [n for n in names if n and not n.startswith(".")]
    for preferred in (
        "LaserWriter Color 12/600 PS",
        "Color LaserWriter 12/600 PS",
        "LaserWriter Color 12/660 PS",
        "LaserWriter 8500",
        "LaserWriter 16/600 PS",
        "Generic",
    ):
        for candidate in cleaned:
            if candidate.lower().startswith(preferred.lower()):
                return candidate
    for candidate in cleaned:
        if "laserwriter" in candidate.lower():
            return candidate
    return cleaned[0] if cleaned else "Generic"


def find_guest_ppd(vol: hfsplus.Volume, sys_folder: list[str]) -> str:
    """Return the selected PPD name found in ``System Folder:Extensions:Printer Descriptions``."""
    try:
        desc_dir = vol.lookup(sys_folder + ["Extensions", "Printer Descriptions"])
    except FileNotFoundError:
        return "Generic"
    ppds = [
        r.name for r in vol.records()
        if r.parent_id == desc_dir.cnid and r.is_file
    ]
    return choose_guest_ppd(ppds)


def _children_by_parent(vol: hfsplus.Volume) -> dict[int, list[hfsplus.CatalogRecord]]:
    by_parent: dict[int, list[hfsplus.CatalogRecord]] = {}
    for rec in vol.records():
        by_parent.setdefault(rec.parent_id, []).append(rec)
    return by_parent


def _desktop_printers(
    vol: hfsplus.Volume,
    by_parent: dict[int, list[hfsplus.CatalogRecord]],
) -> list[tuple[hfsplus.CatalogRecord, dict | None]]:
    """Return ``(CatalogRecord, parsed_papa_or_None)`` for every Desktop Printer
    folder or file found in ``Desktop Folder``."""
    try:
        desktop = vol.lookup(["Desktop Folder"])
    except FileNotFoundError:
        return []
    found: list[tuple[hfsplus.CatalogRecord, dict | None]] = []
    for rec in by_parent.get(desktop.cnid, []):
        if rec.is_file:
            tc = rec.type_creator
            if tc[0] in PRINTER_FILE_TYPES or tc[1] == PRINTER_CREATOR:
                papa = None
                try:
                    for rt, rid, _nm, _p, data in hfsplus.parse_resource_fork(vol.read_fork(rec, "rsrc")):
                        if rt == "PAPA" and rid == -8192:
                            papa = parse_papa_lpr(data)
                            break
                except Exception:
                    pass
                found.append((rec, papa))
            continue
        children = {c.name.lower(): c for c in by_parent.get(rec.cnid, [])}
        cfg = children.get("configuration")
        spool = children.get("desktop printer spooler")
        is_printer_dir = (
            (cfg is not None and cfg.is_file and cfg.type_creator == CONFIG_TYPE_CREATOR)
            or (spool is not None and spool.is_file and spool.type_creator == SPOOLER_TYPE_CREATOR)
        )
        if not is_printer_dir:
            continue
        papa = None
        if cfg is not None:
            try:
                for rt, rid, _nm, _p, data in hfsplus.parse_resource_fork(vol.read_fork(cfg, "rsrc")):
                    if rt == "PAPA" and rid == -8192:
                        papa = parse_papa_lpr(data)
                        break
            except Exception:
                pass
        found.append((rec, papa))
    return found


def _disabled_enable_plan(
    vol: hfsplus.Volume,
    by_parent: dict[int, list[hfsplus.CatalogRecord]],
    sys_folder: list[str],
) -> list[tuple[list[str], list[str], str]]:
    """Return ``(src_folder, dst_folder, filename)`` moves needed to enable
    Desktop Printing extensions and Printer Selector Control Strip module."""
    moves: list[tuple[list[str], list[str], str]] = []
    pairs = (
        (sys_folder + ["Extensions (Disabled)"], sys_folder + ["Extensions"], PRINTER_EXTENSIONS),
        (sys_folder + ["Control Strip Modules (Disabled)"], sys_folder + ["Control Strip Modules"], PRINTER_CONTROL_STRIP),
    )
    for src_path, dst_path, names in pairs:
        try:
            src_dir = vol.lookup(src_path)
            dst_dir = vol.lookup(dst_path)
        except FileNotFoundError:
            continue
        src_items = {r.name.lower(): r.name for r in by_parent.get(src_dir.cnid, [])}
        dst_items = {r.name.lower() for r in by_parent.get(dst_dir.cnid, [])}
        for target in names:
            key = target.lower()
            if key not in dst_items and key in src_items:
                moves.append((src_path, dst_path, src_items[key]))
    return moves


def _matches_retroboot(
    papa: dict | None,
    *,
    name: str,
    host: str,
    queue: str,
) -> bool:
    if papa is None:
        return False
    return (
        papa["name"] == name
        and papa["type"] == PAPA_TYPE
        and papa["zone"] == PAPA_ZONE
        and papa["host"] == host
        and papa["queue"] == queue
    )


def _lw8_papa_ok(
    vol: hfsplus.Volume,
    sys_folder: list[str],
    *,
    name: str,
    host: str,
    queue: str,
) -> bool:
    try:
        lw8_rec = vol.lookup(sys_folder + ["Extensions", LASERWRITER])
        for rt, rid, _nm, _p, data in hfsplus.parse_resource_fork(vol.read_fork(lw8_rec, "rsrc")):
            if rt == "PAPA" and rid == STR_CUR_PRINTER_ID:
                return _matches_retroboot(parse_papa_lpr(data), name=name, host=host, queue=queue)
    except Exception:
        pass
    return False


def _system_chooser_ok(vol: hfsplus.Volume, sys_folder: list[str]) -> bool:
    try:
        sys_rec = vol.lookup(sys_folder + ["System"])
        recs = hfsplus.parse_resource_fork(vol.read_fork(sys_rec, "rsrc"))
    except Exception:
        return False
    has_str = any(
        rt == "STR " and rid == STR_CUR_PRINTER_ID and data.startswith(_pstr(LASERWRITER))
        for rt, rid, _nm, _p, data in recs
    )
    has_alis = any(rt == "alis" and rid == STR_CUR_PRINTER_ID for rt, rid, _nm, _p, _d in recs)
    return has_str and has_alis


def _db_has_printer_alias(vol: hfsplus.Volume, printer_cnid: int) -> bool:
    try:
        db_rec = vol.lookup(["TheVolumeSettingsFolder", "DesktopPrinters DB"])
    except FileNotFoundError:
        return False
    if not db_rec.is_file or db_rec.type_creator != DB_TYPE_CREATOR:
        return False
    try:
        recs = hfsplus.parse_resource_fork(vol.read_fork(db_rec, "rsrc"))
    except Exception:
        return False
    for rt, _rid, _nm, _p, data in recs:
        if rt == "alis" and len(data) >= 118:
            target = struct.unpack(">I", data[114:118])[0]
            if target == printer_cnid:
                return True
    return False


def state(
    image: str,
    *,
    name: str = DEFAULT_NAME,
    host: str = DEFAULT_HOST,
    queue: str = DEFAULT_QUEUE,
) -> dict:
    """Read-only inspection of Mac OS 9 Desktop Printer status on ``image``."""
    with hfsplus.Volume(image) as vol:
        sys_folder = prep._system_folder(vol)
        try:
            vol.lookup(sys_folder + ["Extensions", LASERWRITER])
        except FileNotFoundError:
            return {"result": "no-laserwriter8"}
        by_parent = _children_by_parent(vol)
        printers = _desktop_printers(vol, by_parent)
        ppd = find_guest_ppd(vol, sys_folder)
        other_printers = [rec for rec, _papa in printers if rec.name.lower() != name.lower()]
        if other_printers:
            return {
                "result": "existing-printer-kept",
                "printers": [rec.name for rec, _ in printers],
                "ppd": ppd,
            }
        ours = [(rec, papa) for rec, papa in printers if rec.name.lower() == name.lower()]
        if ours:
            rec, papa = ours[0]
            if not _matches_retroboot(papa, name=name, host=host, queue=queue):
                return {
                    "result": "existing-printer-kept",
                    "printers": [rec.name],
                    "ppd": ppd,
                }
            pending_moves = _disabled_enable_plan(vol, by_parent, sys_folder)
            if (
                not pending_moves
                and _db_has_printer_alias(vol, rec.cnid)
                and _system_chooser_ok(vol, sys_folder)
                and _lw8_papa_ok(vol, sys_folder, name=name, host=host, queue=queue)
            ):
                return {
                    "result": "retroboot-configured",
                    "printer": rec.name,
                    "host": host,
                    "queue": queue,
                    "ppd": ppd,
                }
            return {
                "result": "needs-repair",
                "printer": rec.name,
                "host": host,
                "queue": queue,
                "ppd": ppd,
            }
        # Also honor a non-empty DesktopPrinters DB whose alias targets another folder/file.
        try:
            db_rec = vol.lookup(["TheVolumeSettingsFolder", "DesktopPrinters DB"])
            if db_rec.is_file:
                cnids = {r.cnid: r for recs in by_parent.values() for r in recs}
                for rt, _rid, _nm, _p, data in hfsplus.parse_resource_fork(vol.read_fork(db_rec, "rsrc")):
                    if rt == "alis" and len(data) >= 118:
                        target_cnid = struct.unpack(">I", data[114:118])[0]
                        target = cnids.get(target_cnid)
                        if target is not None and target.name.lower() != name.lower():
                            return {
                                "result": "existing-printer-kept",
                                "printers": [target.name],
                                "ppd": ppd,
                            }
        except Exception:
            pass
        return {"result": "missing", "ppd": ppd}


def enable_printer_extensions(image: str) -> list[str]:
    """Move Desktop Printing extensions and Printer Selector from ``(Disabled)``
    folders into active ``System Folder`` folders when present."""
    with hfsplus.Volume(image) as vol:
        sys_folder = prep._system_folder(vol)
        by_parent = _children_by_parent(vol)
        moves = _disabled_enable_plan(vol, by_parent, sys_folder)
    if not moves:
        return []
    enabled: list[str] = []
    with prep.mounted(image) as mnt:
        for src_path, dst_path, fname in moves:
            src_dir = os.path.join(mnt, *[prep._linux_name(p) for p in src_path])
            dst_dir = os.path.join(mnt, *[prep._linux_name(p) for p in dst_path])
            os.makedirs(dst_dir, exist_ok=True)
            src = os.path.join(src_dir, prep._linux_name(fname))
            dst = os.path.join(dst_dir, prep._linux_name(fname))
            if os.path.exists(src) and not os.path.exists(dst):
                os.replace(src, dst)
                enabled.append(fname)
    return enabled


def _write_lw8_papa(image: str, sys_folder: list[str], papa: bytes) -> None:
    """Write the 1024-byte active ``'PAPA' -8192`` resource inside
    ``System Folder:Extensions:LaserWriter 8`` in place."""
    with hfsplus.Volume(image, writable=True) as vol:
        lw8_rec = vol.lookup(sys_folder + ["Extensions", LASERWRITER])
        for rt, rid, _nm, pos, data in hfsplus.parse_resource_fork(vol.read_fork(lw8_rec, "rsrc")):
            if rt == "PAPA" and rid == STR_CUR_PRINTER_ID:
                if len(data) != len(papa):
                    raise prep.PrepError(f"LaserWriter 8 PAPA size {len(data)} != {len(papa)}")
                vol.write_fork_in_place(lw8_rec, "rsrc", pos, papa)
                vol.flush()
                return
    raise prep.PrepError("LaserWriter 8 'PAPA' -8192 resource not found")


def _write_system_chooser(image: str, sys_folder: list[str]) -> None:
    """Set ``System`` ``'STR ' -8192`` (with sys-heap attr ``0xC0``) and
    ``'alis' -8192`` pointing at ``System Folder:Extensions:LaserWriter 8``."""
    with hfsplus.Volume(image) as vol:
        vh = vol._read_abs(vol.offset + 1024, 512)
        vol_cdate = struct.unpack(">I", vh[16:20])[0]
        root = next(r for r in vol.records() if r.parent_id == 1 and r.cnid == hfsplus.ROOT_FOLDER_ID)
        sf_rec = vol.lookup(sys_folder)
        ext_rec = vol.lookup(sys_folder + ["Extensions"])
        lw8_rec = vol.lookup(sys_folder + ["Extensions", LASERWRITER])
        lw8_cdate = struct.unpack(">I", lw8_rec.data[12:16])[0]
        sys_rec = vol.lookup(sys_folder + ["System"])
        old_desc = sys_rec.fork("rsrc")
        old_rsrc = vol.read_fork(sys_rec, "rsrc")
        alis_blob = build_alias(
            kind=0,
            vol_name=root.name,
            vol_cdate=vol_cdate,
            parent_name=ext_rec.name,
            parent_cnid=ext_rec.cnid,
            target_name=lw8_rec.name,
            target_cnid=lw8_rec.cnid,
            target_cdate=lw8_cdate,
            full_path=f"{root.name}:{sf_rec.name}:{ext_rec.name}:{lw8_rec.name}",
            type_creator=(b"PRER", b"vgrd"),
            cnid_chain=(ext_rec.cnid, sf_rec.cnid),
        )
        new_rsrc = upsert_resource_fork(
            old_rsrc,
            [
                ("STR ", STR_CUR_PRINTER_ID, "", SYS_HEAP_REF_ATTRS, _pstr(LASERWRITER)),
                ("alis", STR_CUR_PRINTER_ID, "", 0, alis_blob),
            ],
        )

    alloc_bytes = old_desc.total_blocks * vol.block_size
    if len(new_rsrc) <= alloc_bytes:
        with hfsplus.Volume(image, writable=True) as vol:
            sys_rec = vol.lookup(sys_folder + ["System"])
            new_desc = hfsplus.Fork(len(new_rsrc), old_desc.clump_size, old_desc.total_blocks, old_desc.extents)
            vol.set_fork_record(sys_rec, "rsrc", new_desc)
            vol._write_fork(new_desc, 0, new_rsrc)
            vol.flush()
    else:
        tmp_name = f"rbprep-sys-{uuid.uuid4().hex[:8]}"
        linux_sf = os.path.join(*[prep._linux_name(p) for p in sys_folder])
        with prep.mounted(image) as mnt:
            with open(os.path.join(mnt, linux_sf, tmp_name), "wb") as f:
                f.write(new_rsrc)
                f.flush()
                os.fsync(f.fileno())
        try:
            with hfsplus.Volume(image, writable=True) as vol:
                sys_rec = vol.lookup(sys_folder + ["System"])
                tmp_rec = vol.lookup(sys_folder + [tmp_name])
                moved = tmp_rec.fork("data")
                if moved.logical_size != len(new_rsrc) or moved.covered_blocks() < moved.total_blocks:
                    raise prep.PrepError("System resource fork update fragmented across >8 extents")
                vol.set_fork_record(sys_rec, "rsrc", moved)
                vol.set_fork_record(tmp_rec, "data", old_desc)
                vol.flush()
        finally:
            with prep.mounted(image) as mnt:
                tmp_path = os.path.join(mnt, linux_sf, tmp_name)
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)


def _copy_spooler_into_printer(image: str, sys_folder: list[str], printer_folder: list[str]) -> None:
    """Copy the guest's ``Extensions:Desktop Printer Spooler`` (both data and
    resource forks + FinderInfo) into the Desktop Printer folder."""
    src_path = sys_folder + ["Extensions", "Desktop Printer Spooler"]
    dst_path = printer_folder + ["Desktop Printer Spooler"]
    with hfsplus.Volume(image) as vol:
        try:
            src_rec = vol.lookup(src_path)
        except FileNotFoundError:
            return
        src_rsrc = vol.read_fork(src_rec, "rsrc")
        src_tc = src_rec.type_creator
        src_flags = src_rec.data[56:58]

    linux_src = os.path.join(*[prep._linux_name(p) for p in src_path])
    linux_dst_dir = os.path.join(*[prep._linux_name(p) for p in printer_folder])
    tmp_rsrc = f"rbprep-spl-{uuid.uuid4().hex[:8]}"
    with prep.mounted(image) as mnt:
        dst_file = os.path.join(mnt, linux_dst_dir, "Desktop Printer Spooler")
        shutil.copyfile(os.path.join(mnt, linux_src), dst_file)
        finfo = src_tc[0] + src_tc[1] + src_flags + bytes(22)
        os.setxattr(dst_file, prep.FINDER_INFO_XATTR, finfo)
        if src_rsrc:
            with open(os.path.join(mnt, linux_dst_dir, tmp_rsrc), "wb") as f:
                f.write(src_rsrc)
                f.flush()
                os.fsync(f.fileno())
    if src_rsrc:
        with hfsplus.Volume(image, writable=True) as vol:
            dst_rec = vol.lookup(dst_path)
            tmp_rec = vol.lookup(printer_folder + [tmp_rsrc])
            moved = tmp_rec.fork("data")
            if moved.logical_size != len(src_rsrc) or moved.covered_blocks() < moved.total_blocks:
                raise prep.PrepError("Desktop Printer Spooler resource fork is fragmented")
            vol.set_fork_record(dst_rec, "rsrc", moved)
            vol.set_fork_record(tmp_rec, "data", hfsplus.Fork.empty())
            vol.flush()
        with prep.mounted(image) as mnt:
            os.unlink(os.path.join(mnt, linux_dst_dir, tmp_rsrc))


def ensure_printer(
    image: str,
    *,
    enable_extensions: bool = True,
    queue: str = DEFAULT_QUEUE,
    host: str = DEFAULT_HOST,
    name: str = DEFAULT_NAME,
) -> dict:
    """Ensure a default LPR Desktop Printer exists for Mac OS 9 on ``image``.

    Preserves any user-created or user-modified Desktop Printer untouched
    (``"existing-printer-kept"``).  All printer structures are created from
    structured records and the guest's own ``LaserWriter 8`` / ``Desktop
    Printer Spooler`` files.
    """
    st = state(image, name=name, host=host, queue=queue)
    if st["result"] in ("no-laserwriter8", "existing-printer-kept"):
        return st
    if st["result"] == "retroboot-configured":
        return st

    existed = st["result"] == "needs-repair"
    enabled: list[str] = []
    if enable_extensions:
        enabled = enable_printer_extensions(image)

    with hfsplus.Volume(image) as vol:
        sys_folder = prep._system_folder(vol)
        ppd = find_guest_ppd(vol, sys_folder)
        ppd_alis: bytes | None = None
        if ppd and ppd != "Generic":
            try:
                vh = vol._read_abs(vol.offset + 1024, 512)
                vol_cdate = struct.unpack(">I", vh[16:20])[0]
                root = next(r for r in vol.records() if r.parent_id == 1 and r.cnid == hfsplus.ROOT_FOLDER_ID)
                sf_rec = vol.lookup(sys_folder)
                ext_rec = vol.lookup(sys_folder + ["Extensions"])
                pd_rec = vol.lookup(sys_folder + ["Extensions", "Printer Descriptions"])
                ppd_rec = vol.lookup(sys_folder + ["Extensions", "Printer Descriptions", ppd])
                ppd_alis = build_alias(
                    kind=0,
                    vol_name=root.name,
                    vol_cdate=vol_cdate,
                    parent_name=pd_rec.name,
                    parent_cnid=pd_rec.cnid,
                    target_name=ppd_rec.name,
                    target_cnid=ppd_rec.cnid,
                    target_cdate=struct.unpack(">I", ppd_rec.data[12:16])[0],
                    full_path=f"{root.name}:{sf_rec.name}:{ext_rec.name}:{pd_rec.name}:{ppd_rec.name}",
                    type_creator=ppd_rec.type_creator,
                    cnid_chain=(pd_rec.cnid, ext_rec.cnid, sf_rec.cnid),
                )
            except Exception:
                ppd_alis = None

    printer_folder = ["Desktop Folder", name]
    with prep.mounted(image) as mnt:
        os.makedirs(os.path.join(mnt, "Desktop Folder", prep._linux_name(name)), exist_ok=True)
        os.makedirs(os.path.join(mnt, "TheVolumeSettingsFolder"), exist_ok=True)

    # 1. Configuration file (invisible dtpc/dcdc resource file) inside printer folder.
    cfg_resources = [
        ("PAPA", -8192, "", build_papa(name=name, host=host, queue=queue)),
        ("PORT", 128, "", b"\xff"),
        ("SIZE", -1, "", SIZE_RESOURCE),
        ("STR#", 128, "", build_str_list([PAPA_ZONE, name, PAPA_TYPE])),
        ("cnfg", 1, "", build_cnfg(LASERWRITER)),
        ("stop", 0, "", b"\x00\x01"),
    ]
    if ppd_alis is not None:
        cfg_resources.append(("alis", STR_CUR_PRINTER_ID, "", ppd_alis))
    cfg_fork = hfsplus.build_resource_fork(cfg_resources)
    prep._replace_resource_fork(image, printer_folder, "Configuration", cfg_fork, CONFIG_TYPE_CREATOR)
    # Mark Configuration invisible (kIsInvisible = 0x4000) via FinderInfo.
    with prep.mounted(image) as mnt:
        cfg_path = os.path.join(mnt, "Desktop Folder", prep._linux_name(name), "Configuration")
        os.setxattr(cfg_path, prep.FINDER_INFO_XATTR, b"dtpcdcdc\x40\x00" + bytes(22))

    # 2. Copy Desktop Printer Spooler from System Folder:Extensions into printer folder.
    _copy_spooler_into_printer(image, sys_folder, printer_folder)

    # 3. Register Desktop Folder:<name> in TheVolumeSettingsFolder:DesktopPrinters DB.
    with hfsplus.Volume(image) as vol:
        vh = vol._read_abs(vol.offset + 1024, 512)
        vol_cdate = struct.unpack(">I", vh[16:20])[0]
        root = next(r for r in vol.records() if r.parent_id == 1 and r.cnid == hfsplus.ROOT_FOLDER_ID)
        desktop_rec = vol.lookup(["Desktop Folder"])
        printer_rec = vol.lookup(printer_folder)
        printer_cdate = struct.unpack(">I", printer_rec.data[12:16])[0]
        alis_blob = build_alias(
            kind=1,
            vol_name=root.name,
            vol_cdate=vol_cdate,
            parent_name=desktop_rec.name,
            parent_cnid=desktop_rec.cnid,
            target_name=printer_rec.name,
            target_cnid=printer_rec.cnid,
            target_cdate=printer_cdate,
            full_path=f"{root.name}:{desktop_rec.name}:{printer_rec.name}",
            type_creator=None,
            cnid_chain=(desktop_rec.cnid,),
        )
    db_fork = hfsplus.build_resource_fork([("alis", DESKTOP_PRINTER_ALIAS_ID, "", alis_blob)])
    prep._replace_resource_fork(image, ["TheVolumeSettingsFolder"], "DesktopPrinters DB", db_fork, DB_TYPE_CREATOR)
    with prep.mounted(image) as mnt:
        db_path = os.path.join(mnt, "TheVolumeSettingsFolder", "DesktopPrinters DB")
        os.setxattr(db_path, prep.FINDER_INFO_XATTR, b"dtpldcdc\x40\x00" + bytes(22))

    # 4. Select LaserWriter 8 as current Chooser printer in System resource fork
    #    and point LaserWriter 8's own 'PAPA' -8192 at the new Desktop Printer.
    _write_system_chooser(image, sys_folder)
    _write_lw8_papa(image, sys_folder, build_papa(name=name, host=host, queue=queue))

    # 5. Verify catalog/resource read-back + offline fsck.
    after = state(image, name=name, host=host, queue=queue)
    if after.get("result") != "retroboot-configured":
        raise prep.PrepError(f"printer verification failed: {after}")
    prep.fsck_verify(image)

    return {
        "result": "updated" if existed else "created",
        "printer": name,
        "host": host,
        "queue": queue,
        "ppd": ppd,
        "extensions_enabled": enabled,
    }
