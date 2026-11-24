"""Pure-Python Apple QuickDraw PICT -> PNG/image decoder for Web Files preview (#50).

Supports common Classic Mac PICT v1 and v2 documents:
* Optional 512-byte Mac PICT file header (and headerless PICT resource/stream);
* ``0x8200`` / ``0x8201`` QuickTime embedded JPEG / PNG payloads;
* ``0x0090`` / ``0x0091`` 1-bit BitsRect / BitsRgn;
* ``0x0098`` / ``0x0099`` PackBitsRect / PackBitsRgn (1/2/4/8-bit CLUT & 16/32-bit direct);
* ``0x009A`` / ``0x009B`` DirectBitsRect / DirectBitsRgn (16-bit RGB555 & 32-bit xRGB/ARGB).
"""
from __future__ import annotations

import struct
import zlib

MAX_DIM = 4096
MAX_PICT_BYTES = 32 * 1024 * 1024

# Fixed payload lengths (after opcode) for PICT v1/v2 QuickDraw state opcodes.
_FIXED_OP_LEN = {
    0x0000: 0, 0x0003: 2, 0x0004: 1, 0x0005: 2, 0x0006: 4, 0x0007: 4,
    0x0008: 2, 0x0009: 8, 0x000A: 8, 0x000B: 4, 0x000C: 4, 0x000D: 2,
    0x000E: 4, 0x0010: 8, 0x0015: 2, 0x0016: 2, 0x001A: 6, 0x001B: 6,
    0x001C: 0, 0x001D: 6, 0x001E: 0, 0x001F: 6,
    0x0020: 8, 0x0021: 4, 0x0022: 6, 0x0023: 2,
    0x0030: 8, 0x0031: 8, 0x0032: 8, 0x0033: 8, 0x0034: 8,
    0x0038: 0, 0x0039: 0, 0x003A: 0, 0x003B: 0, 0x003C: 0,
    0x0040: 8, 0x0041: 8, 0x0042: 8, 0x0043: 8, 0x0044: 8,
    0x0048: 0, 0x0049: 0, 0x004A: 0, 0x004B: 0, 0x004C: 0,
    0x0050: 8, 0x0051: 8, 0x0052: 8, 0x0053: 8, 0x0054: 8,
    0x0058: 0, 0x0059: 0, 0x005A: 0, 0x005B: 0, 0x005C: 0,
    0x0060: 12, 0x0061: 12, 0x0062: 12, 0x0063: 12, 0x0064: 12,
    0x0068: 4, 0x0069: 4, 0x006A: 4, 0x006B: 4, 0x006C: 4,
    0x0C00: 24,
}


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def encode_rgb_png(w: int, h: int, rgb: bytes) -> bytes:
    rw = w * 3
    raw = b"".join(b"\0" + rgb[y * rw:(y + 1) * rw] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 4))
            + _png_chunk(b"IEND", b""))


def _unpack_bits(src: bytes, pos: int, out_len: int) -> tuple[bytes, int]:
    """Decode Apple PackBits run-length stream for a single scanline."""
    out = bytearray()
    n = len(src)
    while len(out) < out_len and pos < n:
        flag = struct.unpack_from(">b", src, pos)[0]
        pos += 1
        if flag >= 0:
            count = flag + 1
            out += src[pos:pos + count]
            pos += count
        elif flag != -128:
            count = 1 - flag
            if pos >= n:
                break
            out += src[pos:pos + 1] * count
            pos += 1
    if len(out) < out_len:
        out.extend(b"\0" * (out_len - len(out)))
    return bytes(out[:out_len]), pos


def _unpack_word_bits(src: bytes, pos: int, out_bytes: int) -> tuple[bytes, int]:
    """Decode Apple PackBits 16-bit word runs (packType == 3)."""
    out = bytearray()
    n = len(src)
    while len(out) < out_bytes and pos < n:
        flag = struct.unpack_from(">b", src, pos)[0]
        pos += 1
        if flag >= 0:
            count = (flag + 1) * 2
            out += src[pos:pos + count]
            pos += count
        elif flag != -128:
            count = 1 - flag
            if pos + 2 > n:
                break
            word = src[pos:pos + 2]
            pos += 2
            out += word * count
    if len(out) < out_bytes:
        out.extend(b"\0" * (out_bytes - len(out)))
    return bytes(out[:out_bytes]), pos


def _read_rect(data: bytes, pos: int) -> tuple[tuple[int, int, int, int], int]:
    top, left, bottom, right = struct.unpack_from(">hhhh", data, pos)
    return (top, left, bottom, right), pos + 8


def _decode_pixmap_and_blit(data: bytes, pos: int, has_base_addr: bool, has_rgn: bool,
                            canvas: bytearray, cw: int, ch: int, origin_x: int, origin_y: int) -> tuple[bool, int]:
    if has_base_addr:
        if pos + 4 > len(data):
            return False, len(data)
        pos += 4
    if pos + 10 > len(data):
        return False, len(data)
    raw_rb = struct.unpack_from(">H", data, pos)[0]
    is_pixmap = bool(raw_rb & 0x8000)
    row_bytes = raw_rb & 0x3FFF
    bounds, pos = _read_rect(data, pos + 2)
    bh = max(0, bounds[2] - bounds[0])
    bw = max(0, bounds[3] - bounds[1])
    if not (0 < row_bytes <= 32768 and 0 < bh <= MAX_DIM and 0 < bw <= MAX_DIM):
        return False, len(data)

    if is_pixmap:
        if pos + 36 > len(data):
            return False, len(data)
        (_pm_ver, pack_type, _pack_sz, _hres, _vres, _px_type,
         pixel_size, cmp_count, cmp_size, _plane_bytes, _pm_table, _pm_res) = struct.unpack_from(">HHIIHHHHIIII", data, pos)
        pos += 36
    else:
        pack_type = 0
        pixel_size = 1
        cmp_count = 1
        cmp_size = 1

    palette: list[tuple[int, int, int]] = [(255, 255, 255), (0, 0, 0)]
    if is_pixmap and pixel_size <= 8:
        if pos + 8 > len(data):
            return False, len(data)
        _ct_seed, _ct_flags, ct_size = struct.unpack_from(">IHH", data, pos)
        pos += 8
        count = ct_size + 1
        if count > 256 or pos + count * 8 > len(data):
            return False, len(data)
        palette = [(0, 0, 0)] * 256
        for i in range(count):
            idx, r16, g16, b16 = struct.unpack_from(">HHHH", data, pos + i * 8)
            rgb = (r16 >> 8, g16 >> 8, b16 >> 8)
            slot = idx if 0 <= idx < 256 else (i if i < 256 else 0)
            palette[slot] = rgb
            if i < 256 and idx == 0 and i > 0:
                palette[i] = rgb
        pos += count * 8

    _src_rect, pos = _read_rect(data, pos)
    dst_rect, pos = _read_rect(data, pos)
    pos += 2  # mode (u16)
    if has_rgn:
        if pos + 2 > len(data):
            return False, len(data)
        rgn_sz = struct.unpack_from(">H", data, pos)[0]
        if rgn_sz < 2:
            return False, len(data)
        pos += rgn_sz

    # Read pixel rows
    dx0 = dst_rect[1] - origin_x
    dy0 = dst_rect[0] - origin_y
    dw = max(0, dst_rect[3] - dst_rect[1])
    dh = max(0, dst_rect[2] - dst_rect[0])
    rows_h = min(bh, dh) if dh else bh
    rows_w = min(bw, dw) if dw else bw

    unpacked_rows: list[bytes] = []
    if pack_type == 1 or row_bytes < 8:
        need = row_bytes * bh
        if pos + need > len(data):
            return False, len(data)
        for y in range(bh):
            unpacked_rows.append(data[pos + y * row_bytes:pos + (y + 1) * row_bytes])
        pos += need
    elif pack_type == 2:
        # 32-bit direct with pad byte stripped
        for _ in range(bh):
            if row_bytes > 250:
                bc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
            else:
                bc = data[pos]
                pos += 1
            packed = data[pos:pos + bc]
            pos += bc
            row_bytes_3 = bw * 3
            row, _ = _unpack_bits(packed, 0, row_bytes_3)
            unpacked_rows.append(row)
    elif pack_type == 3:
        for _ in range(bh):
            if row_bytes > 250:
                bc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
            else:
                bc = data[pos]
                pos += 1
            packed = data[pos:pos + bc]
            pos += bc
            row, _ = _unpack_word_bits(packed, 0, row_bytes)
            unpacked_rows.append(row)
    elif pack_type == 4:
        # Planar 32-bit (3 or 4 color component planes per scanline)
        planes = 4 if cmp_count == 4 else 3
        plane_bytes = bw * planes
        for _ in range(bh):
            if row_bytes > 250:
                bc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
            else:
                bc = data[pos]
                pos += 1
            packed = data[pos:pos + bc]
            pos += bc
            row, _ = _unpack_bits(packed, 0, plane_bytes)
            unpacked_rows.append(row)
    else:
        for _ in range(bh):
            if row_bytes > 250:
                bc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
            else:
                bc = data[pos]
                pos += 1
            packed = data[pos:pos + bc]
            pos += bc
            row, _ = _unpack_bits(packed, 0, row_bytes)
            unpacked_rows.append(row)

    crw = cw * 3
    for y in range(rows_h):
        cy = dy0 + y
        if not (0 <= cy < ch):
            continue
        row = unpacked_rows[y]
        for x in range(rows_w):
            cx = dx0 + x
            if not (0 <= cx < cw):
                continue
            r = g = b = 255
            if pixel_size == 1:
                bit = (row[x >> 3] >> (7 - (x & 7))) & 1
                r, g, b = palette[bit]
            elif pixel_size == 2:
                idx = (row[x >> 2] >> (6 - 2 * (x & 3))) & 0x03
                r, g, b = palette[idx]
            elif pixel_size == 4:
                idx = (row[x >> 1] >> (4 if not (x & 1) else 0)) & 0x0F
                r, g, b = palette[idx]
            elif pixel_size == 8:
                r, g, b = palette[row[x]]
            elif pixel_size == 16:
                val = struct.unpack_from(">H", row, x * 2)[0]
                r = ((val >> 10) & 0x1F) * 255 // 31
                g = ((val >> 5) & 0x1F) * 255 // 31
                b = (val & 0x1F) * 255 // 31
            elif pixel_size == 32:
                if pack_type == 4:
                    off = bw if cmp_count == 4 else 0
                    r = row[off + x]
                    g = row[off + bw + x]
                    b = row[off + 2 * bw + x]
                elif pack_type == 2:
                    r, g, b = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
                else:
                    r, g, b = row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            o = cy * crw + cx * 3
            canvas[o], canvas[o + 1], canvas[o + 2] = r, g, b
    return True, pos


def _find_pict_start(data: bytes) -> tuple[int, int, tuple[int, int, int, int]]:
    for base in ((512, 0) if len(data) > 528 else (0,)):
        if base + 12 > len(data):
            continue
        frame, pos = _read_rect(data, base + 2)
        if not (frame[2] > frame[0] and frame[3] > frame[1]):
            continue
        if data[pos:pos + 4] == b"\x00\x11\x02\xff":
            return pos + 4, 2, frame
        if data[pos:pos + 2] == b"\x11\x01":
            return pos + 2, 1, frame
    raise ValueError("Not a recognized QuickDraw PICT image.")


def _extract_embedded_qt_image(chunk: bytes) -> tuple[bytes, str] | None:
    for sig, mime in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png")):
        idx = chunk.find(sig)
        if idx >= 0:
            if mime == "image/png":
                iend = chunk.find(b"IEND", idx)
                if iend > idx:
                    return chunk[idx:iend + 8], mime
            return chunk[idx:], mime
    return None


def decode_pict_to_image(data: bytes) -> tuple[bytes, str]:
    """Decode QuickDraw PICT bytes into ``(image_bytes, mime_type)`` (usually ``image/png``)."""
    if not data or len(data) > MAX_PICT_BYTES:
        raise ValueError("PICT file is empty or too large.")
    pos, ver, frame = _find_pict_start(data)
    w = frame[3] - frame[1]
    h = frame[2] - frame[0]
    if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM):
        raise ValueError("PICT dimensions are out of supported range.")

    canvas = bytearray([255]) * (w * h * 3)
    rendered = False
    n = len(data)

    while pos < n:
        if ver == 2:
            if pos & 1:
                pos += 1
            if pos + 2 > n:
                break
            op = struct.unpack_from(">H", data, pos)[0]
            pos += 2
        else:
            op = data[pos]
            pos += 1

        if op in (0x00FF, 0xFFFF):  # OpEndPic
            break
        if op == 0x0001 or op == 0x0012 or op == 0x0013 or op == 0x0014:
            # Clip / Pattern / BkPixPat / PnPixPat / FillPixPat: region or variable skip
            if op == 0x0001:
                if pos + 2 > n:
                    break
                sz = struct.unpack_from(">H", data, pos)[0]
                pos += max(2, sz)
            else:
                # PixPat skip unsupported reliably in general stream unless v2 short-circuit
                break
        elif op in (0x0002, 0x000F, 0x0017, 0x0018, 0x0019):
            pos += 8 if op != 0x0017 else 0
            if op in (0x0017, 0x0018, 0x0019):
                pass
        elif op == 0x0028:  # LongText
            pos += 4 + (data[pos + 4] + 1 if pos + 5 <= n else 0)
        elif op in (0x0029, 0x002A):  # DHText, DVText
            pos += 1 + (data[pos + 1] + 1 if pos + 2 <= n else 0)
        elif op == 0x002B:  # DHDVText
            pos += 2 + (data[pos + 2] + 1 if pos + 3 <= n else 0)
        elif op == 0x002C:  # fontName
            if pos + 2 > n:
                break
            pos += struct.unpack_from(">H", data, pos)[0]
        elif op in (0x0090, 0x0091, 0x0098, 0x0099, 0x009A, 0x009B):
            ok, pos = _decode_pixmap_and_blit(
                data, pos,
                has_base_addr=op in (0x009A, 0x009B),
                has_rgn=op in (0x0091, 0x0099, 0x009B),
                canvas=canvas, cw=w, ch=h, origin_x=frame[1], origin_y=frame[0])
            if ok:
                rendered = True
        elif op in (0x8200, 0x8201):  # CompressedQuickTime / UncompressedQuickTime
            if pos + 4 > n:
                break
            sz = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            chunk = data[pos:pos + sz]
            pos += sz
            emb = _extract_embedded_qt_image(chunk)
            if emb is not None:
                return emb
        elif op in (0x00A0, 0x00A1):  # ShortComment / LongComment
            if op == 0x00A0:
                pos += 2
            else:
                if pos + 4 > n:
                    break
                sz = struct.unpack_from(">H", data, pos + 2)[0]
                pos += 4 + sz
        elif op in _FIXED_OP_LEN:
            pos += _FIXED_OP_LEN[op]
        elif 0x0100 <= op <= 0x7FFF:
            pos += (op >> 8) * 2
        elif 0x8000 <= op <= 0x80FF:
            pass
        elif 0x8100 <= op <= 0xFFFF:
            if pos + 4 > n:
                break
            pos += struct.unpack_from(">I", data, pos)[0]
        else:
            break

    if not rendered:
        raise ValueError("This PICT file has no raster picture to preview.")
    return encode_rgb_png(w, h, bytes(canvas)), "image/png"
