"""Mass Effect (original trilogy and Legendary Edition) and other Unreal Engine 3 games:
packages (.pcc, .sfm, .u, .upk) and talk tables (.tlk).

Packages are read best-effort: the name table and imports always, exports where the layout is
recognized. Compressed packages are unpacked first (zlib; LZO for ME1/ME2 if python-lzo is
installed; Oodle for Legendary Edition, using the DLL from your game folder)."""

import struct
import zlib
from collections import Counter

from . import oodle
from .util import Bin, strings, table

TAG = 0x9E2A83C1
COMP_ZLIB, COMP_LZO, COMP_LZX, COMP_OODLE = 0x1, 0x2, 0x4, 0x400


def _fstring(b):
    n = b.i32()
    if n == 0:
        return ""
    if n < 0:
        raw = b.read(-n * 2)
        return raw.decode("utf-16-le", "replace").rstrip("\0")
    return b.read(n).decode("latin-1").rstrip("\0")


def _decompress_block(blob, size, flags):
    if flags & COMP_OODLE:
        return oodle.decompress(blob, size)
    if flags & COMP_ZLIB:
        return zlib.decompress(blob)
    if flags & COMP_LZO:
        try:
            import lzo
        except ImportError:
            raise RuntimeError("This ME1/ME2 package is LZO-compressed. Click 'Install helpers' (Apps card → Game files) "
                               "to install python-lzo, or open it in Legendary Explorer.")
        return lzo.decompress(blob, False, size)
    raise RuntimeError(f"unsupported compression flags {flags:#x}")


def _unpack_chunks(data, header_size, flags):
    """Compressed packages: the header stays as is; each chunk starts with the package tag."""
    out = bytearray(data[:header_size])
    pos = header_size
    while pos + 16 <= len(data):
        tag, block_size, csize, usize = struct.unpack_from("<IiiI", data, pos)
        if tag != TAG:
            nxt = data.find(struct.pack("<I", TAG), pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        nblocks = (usize + block_size - 1) // block_size if block_size > 0 else 0
        sizes = [struct.unpack_from("<ii", data, pos + 16 + 8 * k) for k in range(nblocks)]
        p = pos + 16 + 8 * nblocks
        for bc, bu in sizes:
            out += _decompress_block(data[p:p + bc], bu, flags)
            p += bc
        pos = p
    return bytes(out)


def _find_tables(b, total):
    """After the package flags, find (count, offset) x3 for names, exports, imports."""
    start = b.tell()
    vals = [struct.unpack_from("<i", b.d, start + 4 * k)[0] for k in range(10)]
    for skip in range(0, 3):
        v = vals[skip:skip + 6]
        nc, no, ec, eo, ic, io = v
        if 0 < nc < 500000 and 0 < no < total and 0 <= ec < 500000 and no <= eo < total and \
                0 <= ic < 500000 and 0 < io < total:
            return skip, nc, no, ec, eo, ic, io
    raise ValueError("couldn't find the package tables")


class Package:
    def __init__(self, data):
        b = Bin(data)
        if b.u32() != TAG:
            raise ValueError("not an Unreal Engine 3 package")
        self.file_version, self.licensee = b.u16(), b.u16()
        header_size = b.i32()
        self.group = _fstring(b)
        self.flags = b.u32()
        game = {(491, 1008): "Mass Effect", (512, 130): "Mass Effect 2", (684, 194): "Mass Effect 3",
                (684, 171): "Mass Effect 3 (LE)", (685, 171): "Mass Effect 3 (LE)",
                (686, 171): "Mass Effect 3 (LE)"}.get((self.file_version, self.licensee))
        self.game = game or f"UE3 package v{self.file_version}/{self.licensee}"
        self.compressed = bool(self.flags & 0x02000000)
        if self.compressed:
            # the compression flags sit a little further into the header; detect by trying each
            last = None
            for flags in (COMP_ZLIB, COMP_OODLE, COMP_LZO):
                try:
                    data = _unpack_chunks(data, header_size, flags)
                    self.compression = {COMP_ZLIB: "zlib", COMP_OODLE: "Oodle", COMP_LZO: "LZO"}[flags]
                    last = None
                    break
                except Exception as e:
                    last = e
            if last:
                raise last
            b = Bin(data, b.tell())
        self.d = data
        skip, nc, no, ec, eo, ic, io = _find_tables(b, len(data))
        self.names = self._names(nc, no)
        self.imports = self._imports(ic, io)
        self.exports = self._exports(ec, eo)

    def _names(self, count, offset):
        b = Bin(self.d, offset)
        names = []
        # ME1/ME2 store 8 bytes of flags after each name; ME3/LE don't. Try both.
        for flagged in (False, True):
            b.seek(offset)
            names = []
            try:
                for _ in range(count):
                    s = _fstring(b)
                    if flagged:
                        b.u64()
                    if not s or any(ord(c) < 32 for c in s):
                        raise ValueError
                    names.append(s)
                return names
            except Exception:
                continue
        return names

    def name(self, idx, num=0):
        n = self.names[idx] if 0 <= idx < len(self.names) else f"name#{idx}"
        return f"{n}_{num - 1}" if num > 0 else n

    def _imports(self, count, offset):
        b = Bin(self.d, offset)
        out = []
        for _ in range(count):
            pkg, _pn, cls, _cn = b.i32(), b.i32(), b.i32(), b.i32()
            link, obj, objn = b.i32(), b.i32(), b.i32()
            out.append({"package": self.name(pkg), "class": self.name(cls), "name": self.name(obj, objn), "link": link})
        return out

    def _exports(self, count, offset):
        """ME3 / Legendary Edition layout (variable length). Returns [] if it doesn't fit."""
        b = Bin(self.d, offset)
        out = []
        try:
            for _ in range(count):
                cls, sup, link, obj, objn, arch = (b.i32() for _ in range(6))
                b.u64()
                size, off = b.i32(), b.i32()
                b.i32()
                n = b.i32()
                if not 0 <= n < 1000:
                    raise ValueError
                b.skip(4 * n)
                b.read(16)
                b.i32()
                if not (0 <= size <= len(self.d) and 0 <= off <= len(self.d)):
                    raise ValueError
                out.append({"class": cls, "name": self.name(obj, objn), "link": link, "size": size, "offset": off})
        except Exception:
            return []
        self._ex = out
        for e in out:
            e["class"] = self.ref(e["class"]) or "Class"
        for e in out:
            parts, link, guard = [e["name"]], e["link"], 0
            while link > 0 and link - 1 < len(out) and guard < 30:
                guard += 1
                parent = out[link - 1]
                parts.append(parent["name"])
                link = parent["link"]
            e["path"] = ".".join(reversed(parts))
        return out

    def ref(self, ref):
        ex = getattr(self, "_ex", None) or getattr(self, "exports", None) or []
        if ref > 0 and ref - 1 < len(ex):
            return ex[ref - 1]["name"]
        if ref < 0 and -ref - 1 < len(self.imports):
            return self.imports[-ref - 1]["name"]
        return ""

    def properties(self, e, limit=300):
        """Walk UE3 property tags at the start of an export's data (ME3/LE layout, tolerant)."""
        blob = self.d[e["offset"]:e["offset"] + e["size"]]
        for start in (4, 0, 8, 12):
            b, props = Bin(blob, start), []
            try:
                while len(props) < limit:
                    name = self.name(b.i32(), b.i32())
                    if name == "None":
                        return props
                    typ = self.name(b.i32(), b.i32())
                    if not typ.endswith("Property"):
                        raise ValueError
                    size, idx = b.i32(), b.i32()
                    label = name + (f"[{idx}]" if idx else "")
                    if typ == "BoolProperty":
                        props.append((label, typ, bool(b.u8())))
                        continue
                    if typ in ("StructProperty", "ByteProperty"):
                        sub = self.name(b.i32(), b.i32())
                        if typ == "ByteProperty" and size == 8:
                            props.append((label, typ, f"{sub}.{self.name(b.i32(), b.i32())}"))
                            continue
                        if typ == "ByteProperty" and size == 1:
                            props.append((label, typ, b.u8()))
                            continue
                        b.skip(size)
                        props.append((label, typ, f"<{sub}, {size} bytes>"))
                        continue
                    raw = b.read(size)
                    rb = Bin(raw)
                    if typ in ("IntProperty",) and size == 4:
                        val = rb.i32()
                    elif typ == "FloatProperty" and size == 4:
                        val = round(rb.f32(), 5)
                    elif typ == "StringRefProperty" and size == 4:
                        val = f"TLK string {rb.i32()}"
                    elif typ == "ObjectProperty" and size == 4:
                        r = rb.i32()
                        val = self.ref(r) or ("None" if r == 0 else f"#{r}")
                    elif typ == "NameProperty" and size == 8:
                        val = self.name(rb.i32(), rb.i32())
                    elif typ == "StrProperty":
                        val = repr(_fstring(rb)) if size >= 4 else ""
                    elif typ == "ArrayProperty" and size >= 4:
                        val = f"<array of {rb.i32()}, {size} bytes>"
                    else:
                        val = raw[:24].hex(" ")
                    props.append((label, typ, val))
            except Exception:
                continue
        return None


def read_package(data, action, arg, out_dir):
    p = Package(data)
    if action in ("", "summary"):
        classes = Counter(e["class"] for e in p.exports)
        lines = [f"{p.game} package ({'compressed with ' + p.compression if p.compressed else 'uncompressed'}), "
                 f"{len(p.names)} names, {len(p.imports)} imports, {len(p.exports)} exports"]
        if p.exports:
            lines += ["Export classes: " + ", ".join(f"{c} ×{n}" for c, n in classes.most_common(40)),
                      "", "Next: 'exports [text]' to list objects, 'export <name>' for one object's text and values, "
                      "'imports', 'names [text]'."]
        else:
            lines += ["(This package's export table layout wasn't recognized; names and imports are below.)"]
        lines += ["", "Names: " + ", ".join(p.names[:300])]
        return "\n".join(lines)
    text = arg.lower()
    if action in ("find", "search"):
        action = "exports" if p.exports else "names"
    if action == "names":
        return "\n".join(n for n in p.names if text in n.lower())[:20000]
    if action == "imports":
        return table([[i["class"], i["name"], i["package"]] for i in p.imports if text in i["name"].lower()][:500],
                     ["Class", "Object", "Package"])
    if action == "exports":
        return table([[i + 1, e["class"], e["path"], e["size"]] for i, e in enumerate(p.exports)
                      if text in e["path"].lower() or text in e["class"].lower()][:500], ["#", "Class", "Object", "Bytes"])
    if action == "export":
        hits = [e for e in p.exports if e["path"].lower() == text or e["name"].lower() == text] or \
               [e for e in p.exports if text in e["path"].lower()]
        if not hits:
            return "No export by that name."
        e = hits[0]
        blob = p.d[e["offset"]:e["offset"] + e["size"]]
        props = p.properties(e)
        lines = [f"{e['class']} {e['path']}, {e['size']} bytes"]
        if props:
            lines += [f"  {n} ({t.replace('Property', '')}) = {v}" for n, t, v in props]
        else:
            lines += ["(Couldn't read this object's properties; readable text inside it:)",
                      " | ".join(strings(blob, 3, 150))]
        lines += ["", "TLK string numbers can be looked up in the game's .tlk files ('get <number>')."]
        return "\n".join(lines)
    raise ValueError("package actions: summary, names [text], imports [text], exports [text], export <name>")


# ---------------- talk tables ----------------

def read_tlk(data, action, arg, out_dir):
    b = Bin(data)
    if b.read(4) != b"Tlk ":
        raise ValueError("not a Mass Effect .tlk talk table (ME1's TLKs live inside .upk/.sfm packages)")
    b.u32(); b.u32()
    male, female, nodes_n, data_len = b.i32(), b.i32(), b.i32(), b.i32()
    entries = [(b.i32(), b.i32()) for _ in range(male + female)]
    nodes = [(b.i32(), b.i32()) for _ in range(nodes_n)]
    bits = data[b.tell():b.tell() + data_len]

    def decode(bitpos):
        out, node = [], 0
        total = len(bits) * 8
        while bitpos < total:
            bit = (bits[bitpos >> 3] >> (bitpos & 7)) & 1
            bitpos += 1
            nxt = nodes[node][bit]
            if nxt < 0:
                ch = -1 - nxt
                if ch == 0:
                    break
                out.append(chr(ch))
                node = 0
            else:
                node = nxt
        return "".join(out)
    rows = []
    for k, (sid, off) in enumerate(entries):
        if off < 0:
            continue
        rows.append((sid, "female" if k >= male else "male", decode(off)))
    text = arg.lower()
    if action == "get":
        want = int(arg)
        return "\n".join(f"{g}: {t}" for i, g, t in rows if i == want) or "No such string id."
    hits = [r for r in rows if not text or text in r[2].lower() or text == str(r[0])]
    return "\n".join([f"{len(rows)} lines ({male} male, {female} female)" + (f", {len(hits)} match" if text else "")] +
                     [f"{i} ({g}): {t[:300]}" for i, g, t in hits[:300]])
