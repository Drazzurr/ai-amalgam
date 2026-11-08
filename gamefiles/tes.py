"""Skyrim (Special Edition and Legendary) and other Bethesda Creation Engine files:
plugins (.esp/.esm/.esl), archives (.bsa), localized strings, meshes (.nif) and compiled scripts (.pex)."""

import struct
import zlib
from collections import Counter

from .util import Bin, hexdump, lz4_frame, size_text, table, write_out, strings

REC_HDR = 24
COMPRESSED = 0x00040000

# Subrecords worth decoding as text in most record types
TEXT_SUBS = {"EDID", "FULL", "DESC", "MODL", "ICON", "MICO", "TX00", "TX01", "TX02", "TX03", "TX04", "TX05",
             "TX06", "TX07", "CNAM", "SNAM", "MAST", "NAM1", "RNAM", "ITXT", "SCRN", "ANAM", "FNAM", "ONAM"}


# ---------------- plugins ----------------

class Plugin:
    def __init__(self, data):
        self.d = data
        b = Bin(data)
        if data[:4] != b"TES4":
            raise ValueError("not a Skyrim/Creation Engine plugin (no TES4 header)")
        self.header = self._record(0)
        self.flags = self.header["flags"]
        self.localized = bool(self.flags & 0x80)
        self.masters, self.author, self.desc, self.version, self.num_records = [], "", "", 0.0, 0
        for sub, payload in self._subs(self.header):
            if sub == "HEDR" and len(payload) >= 8:
                self.version, self.num_records = struct.unpack_from("<fi", payload)
            elif sub == "MAST":
                self.masters.append(payload.split(b"\0")[0].decode("utf-8", "replace"))
            elif sub == "CNAM":
                self.author = payload.split(b"\0")[0].decode("utf-8", "replace")
            elif sub == "SNAM":
                self.desc = payload.split(b"\0")[0].decode("utf-8", "replace")

    def _record(self, pos):
        typ, size, flags, fid, _vc, ver, _u = struct.unpack_from("<4sIIIIHH", self.d, pos)
        return {"type": typ.decode("ascii", "replace"), "size": size, "flags": flags, "formid": fid,
                "pos": pos, "data_pos": pos + REC_HDR, "version": ver}

    def record_data(self, rec):
        raw = self.d[rec["data_pos"]:rec["data_pos"] + rec["size"]]
        if rec["flags"] & COMPRESSED:
            raw = zlib.decompress(raw[4:])
        return raw

    def _subs(self, rec):
        data = self.record_data(rec)
        i, big = 0, None
        while i + 6 <= len(data):
            sub = data[i:i + 4].decode("ascii", "replace")
            size = struct.unpack_from("<H", data, i + 4)[0]
            i += 6
            if sub == "XXXX":
                big = struct.unpack_from("<I", data, i)[0]
                i += size
                continue
            if big is not None:
                size, big = big, None
            yield sub, data[i:i + size]
            i += size

    def records(self):
        """Every record (walks groups), as dicts with type/formid/pos."""
        pos, end = 0, len(self.d)
        pos += REC_HDR + self.header["size"]
        while pos + REC_HDR <= end:
            typ = self.d[pos:pos + 4]
            if typ == b"GRUP":
                pos += REC_HDR          # step into the group; its records follow
                continue
            rec = self._record(pos)
            yield rec
            pos += REC_HDR + rec["size"]

    def describe(self, rec, full=False):
        edid = name = ""
        for sub, payload in self._subs(rec):
            if sub == "EDID":
                edid = payload.split(b"\0")[0].decode("utf-8", "replace")
            elif sub == "FULL" and not name:
                if self.localized and len(payload) == 4:
                    name = f"<string #{struct.unpack('<I', payload)[0]}>"
                else:
                    name = payload.split(b"\0")[0].decode("utf-8", "replace")
            if edid and name and not full:
                break
        return edid, name


def formid_text(fid, masters):
    idx = fid >> 24
    owner = masters[idx] if idx < len(masters) else "this plugin"
    return f"{fid:08X}", owner


def read_plugin(data, action, arg, out_dir):
    p = Plugin(data)
    kind = "ESM (master)" if p.flags & 1 else "ESL-flagged" if p.flags & 0x200 else "ESP"
    if action in ("", "summary"):
        counts = Counter(r["type"] for r in p.records())
        lines = [f"Plugin: {kind}{', localized (names in .STRINGS files)' if p.localized else ''}",
                 f"Format version {p.version:.2f}, {p.num_records} records listed in header",
                 f"Author: {p.author or '-'}", f"Description: {p.desc or '-'}",
                 "Masters: " + (", ".join(p.masters) or "none"), "",
                 "Record types (count):",
                 ", ".join(f"{t} {n}" for t, n in counts.most_common(60)),
                 "", "Next: 'list <TYPE> [text]' to list records, 'record <EditorID or FormID>' to read one."]
        return "\n".join(lines)
    if action in ("find", "search"):
        action, arg = "list", "* " + arg
    if action == "list":
        parts = arg.split(None, 1)
        typ = parts[0].upper() if parts else ""
        text = parts[1].lower() if len(parts) > 1 else ""
        rows = []
        for r in p.records():
            if typ and typ != "*" and r["type"] != typ:
                continue
            edid, name = p.describe(r)
            if text and text not in edid.lower() and text not in name.lower():
                continue
            fid, owner = formid_text(r["formid"], p.masters)
            rows.append([r["type"], fid, edid, name, owner])
            if len(rows) >= 400:
                break
        if not rows:
            return "No matching records."
        return table(rows, ["Type", "FormID", "EditorID", "Name", "From"]) + \
            ("\n(first 400 shown; narrow with a type or text)" if len(rows) >= 400 else "")
    if action == "record":
        want = arg.strip()
        target = None
        import re
        fid_want = int(want, 16) if re.fullmatch(r"(0x)?[0-9A-Fa-f]{1,8}", want) else None
        for r in p.records():
            if fid_want is not None and r["formid"] == fid_want:
                target = r
                break
            edid, _ = p.describe(r)
            if edid.lower() == want.lower():
                target = r
                break
        if not target:
            return f"No record with EditorID or FormID '{want}'."
        fid, owner = formid_text(target["formid"], p.masters)
        lines = [f"{target['type']} {fid} (from {owner}), {target['size']} bytes"
                 f"{', compressed' if target['flags'] & COMPRESSED else ''}", ""]
        for sub, payload in p._subs(target):
            lines.append(f"{sub} [{len(payload)}]: {guess(sub, payload, p)}")
        return "\n".join(lines)
    raise ValueError("plugin actions: summary, list <TYPE> [text], record <EditorID|FormID>")


def guess(sub, payload, plugin=None):
    """Best-effort readable value for a subrecord."""
    n = len(payload)
    if sub in TEXT_SUBS and n and payload[-1:] == b"\0" and all(32 <= c < 127 or c >= 128 for c in payload[:-1]):
        return '"' + payload[:-1].decode("utf-8", "replace") + '"'
    if sub == "FULL" and plugin and plugin.localized and n == 4:
        return f"string #{struct.unpack('<I', payload)[0]} (see the plugin's .STRINGS file)"
    if n and payload[-1:] == b"\0" and n > 2 and all(32 <= c < 127 for c in payload[:-1]):
        return '"' + payload[:-1].decode("ascii") + '"'
    if n == 4:
        u, f = struct.unpack("<I", payload)[0], struct.unpack("<f", payload)[0]
        hint = f" / float {f:.4g}" if 1e-6 < abs(f) < 1e7 else ""
        return f"{u} (0x{u:08X}){hint}"
    if n == 2:
        return str(struct.unpack("<H", payload)[0])
    if n == 1:
        return str(payload[0])
    if n % 4 == 0 and n <= 64:
        vals = struct.unpack(f"<{n // 4}I", payload)
        return "uint32s: " + " ".join(f"{v:08X}" for v in vals)
    return payload[:48].hex(" ") + (" …" if n > 48 else "")


# ---------------- BSA archives ----------------

class BSA:
    def __init__(self, data):
        b = Bin(data)
        if b.read(4) != b"BSA\0":
            raise ValueError("not a Skyrim/Oblivion BSA archive")
        self.d = data
        self.version = b.u32()
        b.u32()                                           # folder records offset (36)
        self.flags = b.u32()
        nfolders, nfiles = b.u32(), b.u32()
        _fold_len, file_len = b.u32(), b.u32()
        b.u16(); b.u16()
        if self.version not in (103, 104, 105):
            raise ValueError(f"unsupported BSA version {self.version}")
        counts = []
        for _ in range(nfolders):
            b.u64()
            counts.append(b.u32())
            if self.version == 105:
                b.u32(); b.u64()
            else:
                b.u32()
        self.files = []
        for c in counts:
            folder = ""
            if self.flags & 1:
                ln = b.u8()
                folder = b.read(ln).split(b"\0")[0].decode("utf-8", "replace")
            for _ in range(c):
                b.u64()
                size, off = b.u32(), b.u32()
                self.files.append({"folder": folder, "size": size, "offset": off})
        names = []
        if self.flags & 2:
            blob = b.read(file_len)
            names = [x.decode("utf-8", "replace") for x in blob.split(b"\0")[:len(self.files)]]
        for f, n in zip(self.files, names + [""] * (len(self.files) - len(names))):
            f["path"] = (f["folder"] + "\\" + n) if f["folder"] else n

    def extract(self, f):
        size = f["size"] & 0x3FFFFFFF
        compressed = bool(self.flags & 4) != bool(f["size"] & 0x40000000)
        b = Bin(self.d, f["offset"])
        if self.flags & 0x100 and self.version >= 104:
            ln = b.u8()
            b.skip(ln)
            size -= ln + 1
        if not compressed:
            return b.read(size)
        orig = b.u32()
        blob = b.read(size - 4)
        return lz4_frame(blob) if self.version == 105 else zlib.decompress(blob)


def read_bsa(data, action, arg, out_dir):
    a = BSA(data)
    if action in ("", "summary", "list", "find", "search"):
        text = arg.lower()
        rows = [[f["path"], size_text(f["size"] & 0x3FFFFFFF)] for f in a.files if text in f["path"].lower()]
        head = (f"BSA version {a.version} ({'Skyrim SE' if a.version == 105 else 'Skyrim LE' if a.version == 104 else 'Oblivion'}), "
                f"{len(a.files)} files, {'compressed' if a.flags & 4 else 'uncompressed'}\n")
        return head + table(rows[:500], ["Path", "Size"]) + (f"\n({len(rows) - 500} more)" if len(rows) > 500 else "") + \
            "\nNext: 'extract <path or text>' to pull files out."
    if action == "extract":
        want = arg.lower().replace("/", "\\")
        hits = [f for f in a.files if f["path"].lower() == want] or [f for f in a.files if want in f["path"].lower()]
        if not hits:
            return "No file matches that path."
        done = [write_out(out_dir, f["path"], a.extract(f)) for f in hits[:50]]
        return "Extracted:\n" + "\n".join(done) + (f"\n(first 50 of {len(hits)})" if len(hits) > 50 else "")
    raise ValueError("BSA actions: list [text], extract <path or text>")


# ---------------- localized strings ----------------

def read_strings(data, action, arg, out_dir, ext):
    b = Bin(data)
    count, _size = b.u32(), b.u32()
    entries = [(b.u32(), b.u32()) for _ in range(count)]
    base = b.tell()
    rows = []
    for sid, off in entries:
        p = base + off
        if ext in (".dlstrings", ".ilstrings"):
            ln = struct.unpack_from("<I", data, p)[0]
            s = data[p + 4:p + 4 + ln].split(b"\0")[0]
        else:
            s = data[p:data.index(b"\0", p)]
        rows.append((sid, s.decode("utf-8", "replace")))
    text = arg.lower()
    if action == "get":
        want = int(arg, 0)
        return next((s for i, s in rows if i == want), f"No string #{want}.")
    hits = [(i, s) for i, s in rows if not text or text in s.lower() or text == str(i)]
    out = [f"{len(rows)} strings" + (f", {len(hits)} match" if text else "")]
    out += [f"#{i}: {s[:300]}" for i, s in hits[:300]]
    return "\n".join(out)


# ---------------- NIF meshes ----------------

def read_nif(data, action, arg, out_dir):
    nl = data.index(b"\n")
    line = data[:nl].decode("ascii", "replace")
    if "Gamebryo" not in line and "NetImmerse" not in line:
        raise ValueError("not a NIF file")
    b = Bin(data, nl + 1)
    ver = b.u32()
    b.u8()                                           # endian
    user_ver = b.u32()
    nblocks = b.u32()
    bs_ver = 0
    info = [line, f"User version {user_ver}"]
    if user_ver >= 10:
        bs_ver = b.u32()
        rd = lambda: b.read(b.u8()).split(b"\0")[0].decode("ascii", "replace")
        author = rd()
        if bs_ver > 130:
            b.u32()
        proc = rd() if bs_ver < 131 else ""
        export = rd()
        if bs_ver >= 103:
            rd()                                     # max filepath
        game = {83: "Skyrim LE", 100: "Skyrim SE", 130: "Fallout 4", 155: "Fallout 76", 172: "Starfield"}.get(bs_ver, "Bethesda")
        info.append(f"Bethesda stream {bs_ver} ({game}); exported by '{author}' {proc} {export}".strip())
    ntypes = b.u16()
    types = []
    for _ in range(ntypes):
        ln = b.u32()
        types.append(b.read(ln).decode("ascii", "replace"))
    idx = [b.u16() & 0x7FFF for _ in range(nblocks)]
    sizes = [b.u32() for _ in range(nblocks)]
    nstr = b.u32()
    b.u32()
    names = []
    for _ in range(nstr):
        ln = b.u32()
        names.append(b.read(ln).decode("utf-8", "replace"))
    counts = Counter(types[i] for i in idx if i < len(types))
    tex = sorted({s for s in strings(data, 6, 2000) if s.lower().endswith((".dds", ".tga", ".png"))})
    out = info + [f"{nblocks} blocks:", ", ".join(f"{t} ×{n}" for t, n in counts.most_common()),
                  "", f"Names ({len(names)}): " + ", ".join(names[:200]),
                  "", "Textures referenced:", *(tex[:100] or ["(none found)"]),
                  "", "Tip: to see or edit the mesh, open it in Blender with the PyNifly add-on (the models can do "
                  "this through the Blender connection)."]
    if action == "blocks":
        out += ["", table([[i, types[t] if t < len(types) else "?", sizes[i]] for i, t in enumerate(idx)][:500],
                          ["#", "Block type", "Bytes"])]
    return "\n".join(out)


# ---------------- PEX compiled scripts ----------------

def read_pex(data, action, arg, out_dir):
    if data[:4] != b"\xFA\x57\xC0\xDE":
        raise ValueError("not a compiled Papyrus script")
    p = 4
    major, minor, game = data[4], data[5], struct.unpack_from(">H", data, 6)[0]
    p = 16

    def bstr():
        nonlocal p
        ln = struct.unpack_from(">H", data, p)[0]
        s = data[p + 2:p + 2 + ln].decode("utf-8", "replace")
        p += 2 + ln
        return s
    source, user, machine = bstr(), bstr(), bstr()
    n = struct.unpack_from(">H", data, p)[0]
    p += 2
    table_ = [bstr() for _ in range(n)]
    return "\n".join([f"Papyrus script v{major}.{minor} (game id {game}), compiled from {source} by {user}@{machine}",
                      f"String table ({n}): names of the script, its properties, functions, events and text:",
                      ", ".join(table_[:600]),
                      "", "To read the full code, get the original .psc source (Skyrim's Scripts.zip / the mod's "
                      "source folder) or decompile with Champollion."])
