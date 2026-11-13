"""Write Baldur's Gate 3 files: .pak archives (LSPK version 18) and .loca text tables.

When LSLib's Divine tool is installed (Apps → Game files → Install helpers) the .pak is made by it,
since it's the tool the modding community uses. Otherwise Amalgam writes it itself: same layout the
reader in gamefiles/bg3.py reads (header, file data, then an LZ4-compressed file list at the end).
"""

import hashlib
import os
import re
import struct
import xml.etree.ElementTree as ET

ENTRY = 272


def lz4_block(data):
    """Compress with LZ4 when the library is installed; otherwise a valid LZ4 block of plain literals."""
    try:
        import lz4.block
        return lz4.block.compress(data, store_size=False)
    except ImportError:
        pass
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out = bytearray([0xF0])
    rem = n - 15
    while rem >= 255:
        out.append(255)
        rem -= 255
    out.append(rem)
    return bytes(out) + data


def write_pak(path, files, priority=0):
    """files: [(path inside the pak with /, full path)]. Files are LZ4-compressed when the library is
    installed, stored otherwise (both are read by the game)."""
    try:
        import lz4.block as lz4b
    except ImportError:
        lz4b = None
    entries, md5 = [], hashlib.md5()
    tmp = path + ".part"
    with open(tmp, "wb") as out:
        out.write(b"\0" * 40)
        for rel, full in files:
            name = rel.replace("\\", "/").encode("utf-8")
            if len(name) > 255:
                raise ValueError(f"Path too long for a .pak (255 bytes max): {rel}")
            with open(full, "rb") as fh:
                raw = fh.read()
            stored, flags, size = raw, 0, 0
            if lz4b and len(raw) > 64:
                comp = lz4b.compress(raw, store_size=False)
                if len(comp) < len(raw):
                    stored, flags, size = comp, 0x22, len(raw)     # LZ4, default level
            off = out.tell()
            out.write(stored)
            md5.update(stored)
            entries.append(struct.pack("<256sIHBBII", name, off & 0xFFFFFFFF, off >> 32, 0, flags, len(stored), size))
        listing = lz4_block(b"".join(entries))
        list_off = out.tell()
        out.write(struct.pack("<II", len(entries), len(listing)) + listing)
        out.seek(0)
        out.write(b"LSPK" + struct.pack("<IQIBB", 18, list_off, len(listing) + 8, 0, priority) + md5.digest() +
                  struct.pack("<H", 1))
    os.replace(tmp, path)


def xml_to_loca(xml_bytes):
    """Localization .xml (contentList/content) -> .loca bytes. Returns (data, number of lines)."""
    root = ET.fromstring(xml_bytes)
    rows = []
    for c in root.iter("content"):
        key = (c.get("contentuid") or "").strip()
        if not key:
            continue
        try:
            ver = int(c.get("version") or 1)
        except ValueError:
            ver = 1
        rows.append((key, ver, (c.text or "").encode("utf-8") + b"\0"))
    head = b"LOCA" + struct.pack("<II", len(rows), 12 + 70 * len(rows))
    table = b"".join(struct.pack("<64sHI", k.encode("utf-8")[:63], v & 0xFFFF, len(t)) for k, v, t in rows)
    return head + table + b"".join(t for _k, _v, t in rows), len(rows)


def version64(text):
    """'1.2.3.4' -> BG3's packed Version64 (major<<55 | minor<<47 | revision<<31 | build)."""
    nums = [int(x) for x in re.findall(r"\d+", text or "")][:4]
    nums += [0] * (4 - len(nums))
    major, minor, rev, build = nums
    return (min(major, 255) << 55) | (min(minor, 255) << 47) | (min(rev, 65535) << 31) | min(build, 0x7FFFFFFF)


def version_text(v64):
    v = int(v64)
    return f"{v >> 55}.{(v >> 47) & 0xFF}.{(v >> 31) & 0xFFFF}.{v & 0x7FFFFFFF}"
