"""Baldur's Gate 3 (and Divinity: Original Sin 2): .pak archives, .loca text, .lsx/.lsj data,
and .lsf/.lsb/.lsbs/.gr2 through LSLib's Divine tool when it's installed."""

import os
import re
import struct
import subprocess
import tempfile

from .util import Bin, inflate, lz4_block, size_text, table, write_out, zstd

ENTRY18 = 272
METHODS = {0: "none", 1: "zlib", 2: "lz4", 3: "zstd"}


class Pak:
    def __init__(self, path, data):
        self.path, self.d = path, data
        b = Bin(data)
        if b.read(4) != b"LSPK":
            # very old versions keep the header at the end
            raise ValueError("not a BG3/DOS2 .pak (no LSPK header at the start)")
        self.version = b.u32()
        if self.version < 15:
            raise ValueError(f"pak version {self.version} (older Larian games) isn't supported here; "
                             "use LSLib's Divine tool")
        list_off, list_size = b.u64(), b.u32()
        self.flags, self.priority = b.u8(), b.u8()
        b.read(16)
        self.parts = b.u16()
        lb = Bin(data, list_off)
        n = lb.u32()
        if self.version >= 18:
            entry_size = ENTRY18
        else:
            entry_size = 296      # v15/16: name 256, offset u64, size u64, uncompressed u64, part u32, flags u32, crc u32, unk u32
        csize = lb.u32()
        raw = lz4_block(lb.read(csize), n * entry_size)
        self.files = []
        eb = Bin(raw)
        for _ in range(n):
            name = eb.cstr(256)
            if self.version >= 18:
                off = eb.u32() | (eb.u16() << 32)
                part, flags = eb.u8(), eb.u8()
                disk, size = eb.u32(), eb.u32()
            else:
                off, disk, size = eb.u64(), eb.u64(), eb.u64()
                part, flags = eb.u32(), eb.u32()
                eb.u32(); eb.u32()
            self.files.append({"path": name, "offset": off, "part": part, "flags": flags,
                               "disk": disk, "size": size or disk})

    def extract(self, f):
        data = self.d
        if f["part"]:
            base, ext = os.path.splitext(self.path)
            with open(f"{base}_{f['part']}{ext}", "rb") as fh:
                data = fh.read()
        blob = data[f["offset"]:f["offset"] + f["disk"]]
        method = f["flags"] & 0x0F
        if method == 0 or f["size"] == f["disk"] and method == 0:
            return blob
        if method == 1:
            return inflate(blob)
        if method == 2:
            return lz4_block(blob, f["size"])
        if method == 3:
            return zstd(blob, f["size"])
        raise ValueError(f"unknown compression {method}")


def read_pak(path, data, action, arg, out_dir):
    p = Pak(path, data)
    if action in ("", "summary", "list", "find", "search"):
        text = arg.lower()
        rows = [[f["path"], size_text(f["size"]), METHODS.get(f["flags"] & 15, "?")]
                for f in p.files if text in f["path"].lower()]
        head = f"BG3 .pak version {p.version}, {len(p.files)} files, {p.parts} part(s)\n"
        return head + table(rows[:500], ["Path", "Size", "Compression"]) + \
            (f"\n({len(rows) - 500} more; filter with 'list <text>')" if len(rows) > 500 else "") + \
            "\nNext: 'extract <path or text>' to pull files out (then read the extracted .lsf/.lsx/.loca/.dds)."
    if action == "extract":
        want = arg.lower().replace("\\", "/")
        hits = [f for f in p.files if f["path"].lower() == want] or [f for f in p.files if want in f["path"].lower()]
        if not hits:
            return "No file matches that path."
        done = [write_out(out_dir, f["path"], p.extract(f)) for f in hits[:50]]
        return "Extracted:\n" + "\n".join(done) + (f"\n(first 50 of {len(hits)})" if len(hits) > 50 else "")
    raise ValueError(".pak actions: list [text], extract <path or text>")


def read_loca(data, action, arg, out_dir):
    b = Bin(data)
    if b.read(4) != b"LOCA":
        raise ValueError("not a .loca file")
    n, texts = b.u32(), b.u32()
    entries = []
    for _ in range(n):
        key = b.cstr(64)
        b.u16()
        entries.append((key, b.u32()))
    p = texts
    rows = []
    for key, ln in entries:
        rows.append((key, data[p:p + ln].split(b"\0")[0].decode("utf-8", "replace")))
        p += ln
    text = arg.lower()
    if action == "get":
        return next((t for k, t in rows if k.lower() == text), "No such handle.")
    hits = [(k, t) for k, t in rows if not text or text in t.lower() or text in k.lower()]
    return "\n".join([f"{len(rows)} lines" + (f", {len(hits)} match '{arg}'" if text else "")] +
                     [f"{k}: {t[:300]}" for k, t in hits[:300]])


def summarize_lsx(text, arg=""):
    """LSX is XML: show regions, node types and the attributes that carry names and text."""
    regions = re.findall(r'<region id="([^"]+)"', text)
    nodes = re.findall(r'<node id="([^"]+)"', text)
    from collections import Counter
    c = Counter(nodes)
    lines = ["Regions: " + ", ".join(regions), "Nodes: " + ", ".join(f"{k} ×{v}" for k, v in c.most_common(40))]
    want = arg.lower()
    attrs = re.findall(r'<attribute id="([^"]+)" type="([^"]+)" (?:value="([^"]*)"|handle="([^"]*)")', text)
    shown = 0
    lines.append("")
    for aid, typ, val, handle in attrs:
        v = val or handle
        if want and want not in aid.lower() and want not in v.lower():
            continue
        if not want and aid not in ("Name", "DisplayName", "Description", "Stats", "MapKey", "Type", "UUID",
                                    "TemplateName", "Icon", "ParentTemplateId", "Race", "CharacterVisualResourceID"):
            continue
        lines.append(f"{aid} ({typ}) = {v[:200]}")
        shown += 1
        if shown >= 300:
            break
    if len(text) < 60000 and not want:
        lines += ["", "Full file:", text]
    return "\n".join(lines)


# ---------------- LSLib (Divine) for the binary formats ----------------

def divine_path():
    root = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AIAmalgam", "tools", "lslib")
    for dirpath, _d, files in os.walk(root):
        for f in files:
            if f.lower() == "divine.exe":
                return os.path.join(dirpath, f)
    return None


def divine(args, timeout=300):
    exe = divine_path()
    if not exe:
        raise RuntimeError("Reading this BG3 file needs LSLib's Divine tool. Click 'Install helpers' (Apps card → Game files), "
                           "then try again.")
    r = subprocess.run([exe, "-g", "bg3", "-l", "warn"] + args, capture_output=True, timeout=timeout,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0:
        raise RuntimeError("Divine failed:\n" + out[-800:])
    return out


def read_lsf(path, action, arg, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".lsx")
    divine(["-a", "convert-resource", "-s", path, "-d", dest])
    with open(dest, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return f"Converted to {dest}\n\n" + summarize_lsx(text, arg)


def read_gr2(path, action, arg, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0])
    last = None
    for ext in (".glb", ".dae"):
        try:
            divine(["-a", "convert-model", "-s", path, "-d", base + ext])
            return (f"Converted the Granny model to {base + ext}.\nImport it in Blender (File → Import) to see "
                    "or edit it; the models can do that through the Blender connection.")
        except RuntimeError as e:
            last = e
    raise last
