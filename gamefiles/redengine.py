"""REDengine games: The Witcher 3 (.bundle, CR2W files like .w2ent/.w2mesh) and
Cyberpunk 2077 (.archive, CR2W files like .ent/.mesh/.app). Scripts (.ws, .reds, .script)
are plain text and are read directly."""

import struct
from collections import Counter

from . import oodle
from .util import Bin, inflate, lz4_block, size_text, table, write_out


# ---------------- Snappy (pure Python, for Witcher 3 bundles) ----------------

def snappy(src):
    try:
        import snappy as _s
        return _s.uncompress(src)
    except ImportError:
        pass
    i, size, shift = 0, 0, 0
    while True:
        b = src[i]; i += 1
        size |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    out = bytearray()
    n = len(src)
    while i < n:
        tag = src[i]; i += 1
        t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln >= 60:
                nb = ln - 59
                ln = int.from_bytes(src[i:i + nb], "little")
                i += nb
            ln += 1
            out += src[i:i + ln]; i += ln
            continue
        if t == 1:
            ln = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | src[i]; i += 1
        elif t == 2:
            ln = (tag >> 2) + 1
            off = int.from_bytes(src[i:i + 2], "little"); i += 2
        else:
            ln = (tag >> 2) + 1
            off = int.from_bytes(src[i:i + 4], "little"); i += 4
        start = len(out) - off
        for k in range(ln):
            out.append(out[start + k])
    return bytes(out[:size])


# ---------------- Witcher 3 bundles ----------------

class Bundle:
    def __init__(self, data):
        if data[:8] != b"POTATO70":
            raise ValueError("not a Witcher 3 .bundle")
        self.d = data
        b = Bin(data, 8)
        b.u32(); b.u32()
        data_offset = b.u32()
        toc_end = 0x20 + data_offset
        self.files = []
        b.seek(0x20)
        while b.tell() + 0x140 <= toc_end:
            name = b.cstr(0x100)
            b.read(16)
            b.u32()
            size, zsize, off = b.u32(), b.u32(), b.u32()
            b.u64(); b.read(16); b.u32()
            comp = b.u32()
            if not name:
                continue
            self.files.append({"path": name, "size": size, "zsize": zsize, "offset": off, "comp": comp})

    def extract(self, f):
        blob = self.d[f["offset"]:f["offset"] + f["zsize"]]
        c = f["comp"]
        if c == 0:
            return blob
        if c == 1:
            return inflate(blob)
        if c == 2:
            return snappy(blob)
        if c in (4, 5):
            return lz4_block(blob, f["size"])
        if c == 3:
            raise RuntimeError("This file uses Doboz compression, which Amalgam can't unpack. Use WolvenKit 7 "
                               "or QuickBMS with the Witcher 3 script.")
        raise RuntimeError(f"unknown compression type {c}")


COMP_NAMES = {0: "none", 1: "zlib", 2: "snappy", 3: "doboz", 4: "lz4", 5: "lz4hc"}


def read_bundle(data, action, arg, out_dir):
    bd = Bundle(data)
    if action in ("", "summary", "list", "find", "search"):
        text = arg.lower()
        rows = [[f["path"], size_text(f["size"]), COMP_NAMES.get(f["comp"], "?")]
                for f in bd.files if text in f["path"].lower()]
        return (f"Witcher 3 bundle, {len(bd.files)} files\n" + table(rows[:500], ["Path", "Size", "Compression"]) +
                (f"\n({len(rows) - 500} more)" if len(rows) > 500 else "") +
                "\nNext: 'extract <path or text>', then read the extracted file (CR2W files list their contents).")
    if action == "extract":
        want = arg.lower().replace("/", "\\")
        hits = [f for f in bd.files if f["path"].lower() == want] or [f for f in bd.files if want in f["path"].lower()]
        if not hits:
            return "No file matches that path."
        done = [write_out(out_dir, f["path"], bd.extract(f)) for f in hits[:50]]
        return "Extracted:\n" + "\n".join(done)
    raise ValueError("bundle actions: list [text], extract <path or text>")


# ---------------- CR2W resource files (both games) ----------------

def read_cr2w(data, action, arg, out_dir):
    b = Bin(data)
    if b.read(4) != b"CR2W":
        raise ValueError("not a CR2W file")
    version, _flags = b.u32(), b.u32()
    b.u64(); b.u32()
    b.u32(); b.u32(); b.u32()
    b.u32()                                   # number of chunks
    tables = [(b.u32(), b.u32(), b.u32()) for _ in range(10)]
    s_off, s_size, _ = tables[0]
    strings_blob = data[s_off:s_off + s_size]

    def string_at(o):
        end = strings_blob.find(b"\0", o)
        return strings_blob[o:end if end >= 0 else None].decode("utf-8", "replace")
    n_off, n_cnt, _ = tables[1]
    names = []
    nb = Bin(data, n_off)
    for _ in range(n_cnt):
        o = nb.u32(); nb.u32()
        names.append(string_at(o))
    i_off, i_cnt, _ = tables[2]
    imports = []
    ib = Bin(data, i_off)
    for _ in range(i_cnt):
        o = ib.u32(); cls = ib.u16(); ib.u16()
        imports.append((string_at(o), names[cls] if cls < len(names) else "?"))
    e_off, e_cnt, _ = tables[4]
    exports = []
    xb = Bin(data, e_off)
    for _ in range(e_cnt):
        cls = xb.u16(); xb.u16()
        parent = xb.u32(); size = xb.u32(); xb.u32(); xb.u32(); xb.u32()
        exports.append((names[cls] if cls < len(names) else "?", parent, size))
    game = "Witcher 3" if version <= 163 else "Cyberpunk 2077"
    counts = Counter(c for c, _p, _s in exports)
    out = [f"CR2W resource ({game}, version {version}): {len(exports)} objects, {len(imports)} referenced files",
           "Objects: " + ", ".join(f"{c} ×{n}" for c, n in counts.most_common(50)),
           "", "Referenced files:", *[f"  {p}  ({c})" for p, c in imports[:200]],
           "", f"Names used ({len(names)}): " + ", ".join(n for n in names[1:400] if n)]
    if game == "Cyberpunk 2077":
        out += ["", "For every property value, convert it to JSON with WolvenKit (installed by 'Install game file "
                "helpers'): read the file with 'json'."]
    return "\n".join(out)


# ---------------- Cyberpunk 2077 archives ----------------

def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text.lower().replace("/", "\\").encode("utf-8"):
        h ^= ch
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class Archive:
    def __init__(self, data):
        b = Bin(data)
        if b.read(4) != b"RDAR":
            raise ValueError("not a Cyberpunk 2077 .archive")
        self.d = data
        self.version = b.u32()
        index_pos, _index_size = b.u64(), b.u32()
        ib = Bin(data, index_pos)
        ib.u32(); ib.u32(); ib.u64()
        nfiles, nsegs, ndeps = ib.u32(), ib.u32(), ib.u32()
        self.files = []
        for _ in range(nfiles):
            h = ib.u64(); ib.i64()
            ib.u32()
            s0, s1 = ib.u32(), ib.u32()
            ib.u32(); ib.u32(); ib.read(20)
            self.files.append({"hash": h, "segs": (s0, s1)})
        self.segs = [(ib.u64(), ib.u32(), ib.u32()) for _ in range(nsegs)]

    def size(self, f):
        return sum(self.segs[i][2] for i in range(*f["segs"]))

    def extract(self, f):
        out = bytearray()
        for i in range(*f["segs"]):
            off, zsize, size = self.segs[i]
            blob = self.d[off:off + zsize]
            if zsize == size:
                out += blob
            elif blob[:4] == b"KARK":
                out += oodle.decompress(blob[8:], struct.unpack_from("<I", blob, 4)[0])
            else:
                out += oodle.decompress(blob, size)
        return bytes(out)


def read_archive(data, action, arg, out_dir, names=None):
    a = Archive(data)
    names = names or {}
    if action in ("", "summary", "list", "find", "search"):
        rows = []
        text = arg.lower()
        for f in a.files:
            name = names.get(f["hash"], f"{f['hash']:016X}")
            if text and text not in name.lower():
                continue
            rows.append([name, size_text(a.size(f))])
        return (f"Cyberpunk 2077 archive v{a.version}, {len(a.files)} files. Files are stored by the hash of their "
                f"path, so names show only for paths Amalgam knows; 'extract <full path>' works for any path.\n" +
                table(rows[:300], ["Path or hash", "Size"]))
    if action == "extract":
        want = arg.strip()
        h = int(want, 16) if all(c in "0123456789abcdefABCDEF" for c in want) and len(want) == 16 else fnv1a64(want)
        f = next((f for f in a.files if f["hash"] == h), None)
        if not f:
            return f"'{want}' isn't in this archive."
        return "Extracted:\n" + write_out(out_dir, names.get(h, want if not want.isalnum() else f"{h:016X}.bin"),
                                          a.extract(f))
    raise ValueError("archive actions: list [text], extract <path or hash>")
