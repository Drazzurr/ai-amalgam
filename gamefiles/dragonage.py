"""Dragon Age: Origins and Dragon Age II: archives (.erf, .rim) and GFF resources
(.utc creatures, .uti items, .dlg dialogue, .are areas, .tlk talk tables, and more).
Dragon Age: Inquisition and The Veilguard use Frostbite, which needs Frosty Toolsuite instead."""

import struct
import zlib

from .util import Bin, size_text, table, write_out

TYPES = {0: "u8", 1: "i8", 2: "u16", 3: "i16", 4: "u32", 5: "i32", 6: "u64", 7: "i64", 8: "f32", 9: "f64",
         10: "vec3", 12: "vec4", 13: "quat", 14: "string", 15: "color", 16: "matrix", 17: "tlk"}
FMT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "Q", 7: "q", 8: "f", 9: "d"}
SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 8, 7: 8, 8: 4, 9: 8, 10: 12, 12: 16, 13: 16, 14: 4, 15: 16, 16: 64, 17: 8}


# ---------------- ERF / RIM archives ----------------

class ERF:
    def __init__(self, data):
        self.d = data
        magic = data[:16].decode("utf-16-le", "replace")
        b = Bin(data, 16)
        self.files = []
        if magic == "ERF V2.0":
            n = b.u32(); b.u32(); b.u32(); b.u32()
            for _ in range(n):
                name = b.read(64).decode("utf-16-le", "replace").split("\0")[0]
                off, size = b.u32(), b.u32()
                self.files.append({"path": name, "offset": off, "packed": size, "size": size})
            self.version = "2.0 (Dragon Age: Origins)"
            self.comp = 0
        elif magic == "ERF V2.2":
            n = b.u32(); b.u32(); b.u32(); b.u32()
            flags = b.u32(); b.u32(); b.read(16)
            self.comp = (flags >> 29) & 7
            for _ in range(n):
                name = b.read(64).decode("utf-16-le", "replace").split("\0")[0]
                off, packed, size = b.u32(), b.u32(), b.u32()
                self.files.append({"path": name, "offset": off, "packed": packed, "size": size})
            self.version = "2.2 (Dragon Age II)"
        elif data[:8] in (b"ERF V3.0", b"RIM V1.0"):
            raise ValueError("ERF 3.0 archives aren't supported yet; use DA2 tools such as the Dragon Age Toolset "
                             "or ERF Editor")
        else:
            raise ValueError("not a Dragon Age .erf archive")

    def extract(self, f):
        blob = self.d[f["offset"]:f["offset"] + f["packed"]]
        if f["packed"] == f["size"] or not f["size"]:
            return blob
        for attempt in (lambda x: zlib.decompress(x), lambda x: zlib.decompress(x, -15),
                        lambda x: zlib.decompress(x[4:]), lambda x: zlib.decompress(x[1:], -15)):
            try:
                return attempt(blob)
            except Exception:
                continue
        raise RuntimeError("couldn't unpack this compressed entry")


def read_erf(data, action, arg, out_dir):
    e = ERF(data)
    if action in ("", "summary", "list", "find", "search"):
        text = arg.lower()
        rows = [[f["path"], size_text(f["size"])] for f in e.files if text in f["path"].lower()]
        return (f"Dragon Age ERF {e.version}, {len(e.files)} files\n" + table(rows[:500], ["File", "Size"]) +
                (f"\n({len(rows) - 500} more)" if len(rows) > 500 else "") +
                "\nNext: 'extract <name or text>', then read the extracted GFF file (.utc, .uti, .dlg...).")
    if action == "extract":
        want = arg.lower()
        hits = [f for f in e.files if f["path"].lower() == want] or [f for f in e.files if want in f["path"].lower()]
        if not hits:
            return "No file matches that name."
        return "Extracted:\n" + "\n".join(write_out(out_dir, f["path"], e.extract(f)) for f in hits[:50])
    raise ValueError("erf actions: list [text], extract <name or text>")


# ---------------- GFF 4 resources ----------------

class GFF4:
    def __init__(self, data):
        b = Bin(data)
        if b.read(4) != b"GFF ":
            raise ValueError("not a GFF file")
        self.version = b.read(4).decode()
        if self.version not in ("V4.0", "V4.1"):
            raise ValueError(f"GFF {self.version} isn't supported (Dragon Age uses V4.0/V4.1)")
        self.platform = b.read(4).decode("ascii", "replace").strip()
        self.ftype = b.read(4).decode("ascii", "replace").strip()
        self.fversion = b.read(4).decode("ascii", "replace").strip()
        nstructs, self.data_off = b.u32(), b.u32()
        self.str_off = 0
        if self.version == "V4.1":
            b.u32()                              # string count
            self.str_off = b.u32()
        self.d = data
        self.structs = []
        for _ in range(nstructs):
            typ = b.read(4).decode("ascii", "replace")
            nfields, foff, size = b.u32(), b.u32(), b.u32()
            fb = Bin(data, foff)
            fields = []
            for _f in range(nfields):
                label, tid, flags, idx = fb.u32(), fb.u16(), fb.u16(), fb.u32()
                fields.append((label, tid, flags, idx))
            self.structs.append({"type": typ, "fields": fields, "size": size})
        self.budget = 4000

    def _string(self, off):
        if off == 0xFFFFFFFF:
            return ""
        if self.version == "V4.1":
            p = self.str_off + off
            end = self.d.index(b"\0", p)
            return self.d[p:end].decode("utf-8", "replace")
        p = self.data_off + off
        n = struct.unpack_from("<I", self.d, p)[0]
        return self.d[p + 4:p + 4 + 2 * n].decode("utf-16-le", "replace").rstrip("\0")

    def _value(self, tid, pos):
        if tid in FMT:
            v = struct.unpack_from("<" + FMT[tid], self.d, pos)[0]
            return round(v, 5) if tid in (8, 9) else v
        if tid in (10, 12, 13, 15):
            n = SIZE[tid] // 4
            return [round(x, 4) for x in struct.unpack_from(f"<{n}f", self.d, pos)]
        if tid == 16:
            return "<matrix>"
        if tid == 14:
            return self._string(struct.unpack_from("<I", self.d, pos)[0])
        if tid == 17:
            sid, off = struct.unpack_from("<II", self.d, pos)
            txt = self._string(off) if off not in (0, 0xFFFFFFFF) else ""
            return f"TLK {sid}" + (f' "{txt}"' if txt else "")
        return f"<type {tid}>"

    def struct_at(self, si, pos, depth=0):
        if self.budget <= 0 or depth > 12:
            return "…"
        self.budget -= 1
        st = self.structs[si]
        out = {"_type": st["type"]}
        for label, tid, flags, idx in st["fields"]:
            fpos = pos + idx
            is_list, is_struct, is_ref = flags & 0x8000, flags & 0x4000, flags & 0x2000
            try:
                if is_list:
                    off = struct.unpack_from("<I", self.d, fpos)[0]
                    if off == 0xFFFFFFFF:
                        out[label] = []
                        continue
                    lp = self.data_off + off
                    n = struct.unpack_from("<I", self.d, lp)[0]
                    items = []
                    for k in range(min(n, 200)):
                        if is_struct:
                            if is_ref:
                                ref = struct.unpack_from("<I", self.d, lp + 4 + 4 * k)[0]
                                items.append(self.struct_at(tid, self.data_off + ref, depth + 1))
                            else:
                                items.append(self.struct_at(tid, lp + 4 + k * self.structs[tid]["size"], depth + 1))
                        else:
                            items.append(self._value(tid, lp + 4 + k * SIZE.get(tid, 4)))
                    out[label] = items + (["…"] if n > 200 else [])
                elif is_struct:
                    if is_ref:
                        ref = struct.unpack_from("<I", self.d, fpos)[0]
                        out[label] = None if ref == 0xFFFFFFFF else self.struct_at(tid, self.data_off + ref, depth + 1)
                    else:
                        out[label] = self.struct_at(tid, fpos, depth + 1)
                else:
                    if is_ref:
                        ref = struct.unpack_from("<I", self.d, fpos)[0]
                        out[label] = None if ref == 0xFFFFFFFF else self._value(tid, self.data_off + ref)
                    else:
                        out[label] = self._value(tid, fpos)
            except (struct.error, IndexError, ValueError) as e:
                out[label] = f"<unreadable: {e}>"
        return out


def _lines(v, indent=0, key=None):
    pad = "  " * indent
    head = f"{pad}{key}: " if key is not None else pad
    if isinstance(v, dict):
        out = [f"{head}{{{v.get('_type', '')}}}"]
        for k, x in v.items():
            if k != "_type":
                out += _lines(x, indent + 1, k)
        return out
    if isinstance(v, list) and v and isinstance(v[0], dict):
        out = [f"{head}[{len(v)} items]"]
        for i, x in enumerate(v):
            out += _lines(x, indent + 1, i)
        return out
    return [f"{head}{v}"]


def read_gff(data, action, arg, out_dir):
    g = GFF4(data)
    tree = g.struct_at(0, g.data_off)
    lines = [f"Dragon Age GFF {g.version} resource, type {g.ftype} {g.fversion} ({g.platform}), "
             f"{len(g.structs)} struct types.",
             "Field labels are numbers (the toolset's names for them aren't stored in the file).", ""]
    body = _lines(tree)
    text = arg.lower()
    if text:
        body = [l for l in body if text in l.lower()]
    return "\n".join(lines + body[:1500])
