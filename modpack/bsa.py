"""Write Skyrim BSA archives: version 105 (Special/Anniversary Edition) and 104 (Legendary Edition).

Layout and hash follow the UESP format pages: a 36-byte header, folder records sorted by hash, each
folder's name and file records (sorted by hash), the file name block, then the file data. Special
Edition compresses with LZ4 frames (only when the lz4 library is installed), Legendary Edition with
zlib. Sound files are never compressed (the game streams them).
"""

import os
import struct
import zlib

NO_COMPRESS = (".wav", ".xwm", ".fuz", ".lip", ".ogg", ".bik")
EXT_BITS = {".kf": 0x80, ".nif": 0x8000, ".dds": 0x8080, ".wav": 0x80000000}
MAX_BSA = 2_000_000_000          # the game can't read archives past about 2 GB


def _sdbm(data):
    h = 0
    for c in data:
        h = (h * 0x1003F + c) & 0xFFFFFFFF
    return h


def tes_hash(name, folder=False):
    """The 64-bit hash Skyrim uses to find a folder (folder=True) or a file name in a BSA."""
    name = name.lower().replace("/", "\\")
    if folder:
        root, ext = name, ""
    else:
        dot = name.rfind(".")
        root, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    r = root.encode("cp1252", "replace")
    e = ext.encode("cp1252", "replace")
    n = len(r)
    h1 = 0
    if n:
        h1 = r[-1] | ((r[-2] << 8) if n >= 3 else 0) | ((n & 0xFF) << 16) | (r[0] << 24)
    h1 |= EXT_BITS.get(ext, 0)
    h2 = _sdbm(r[1:-2]) if n > 3 else 0
    return (((h2 + _sdbm(e)) & 0xFFFFFFFF) << 32) | (h1 & 0xFFFFFFFF)


def _content_flags(rels):
    flags = 0
    for r in rels:
        low = r.lower()
        top, ext = low.split("/")[0], os.path.splitext(low)[1]
        if top == "meshes":
            flags |= 0x1
        elif top == "textures":
            flags |= 0x2
        elif top == "interface":
            flags |= 0x4
        elif top == "sound":
            flags |= 0x10 if "/voice/" in low else 0x8
        elif top == "shadersfx":
            flags |= 0x20
        elif top in ("trees", "grass"):
            flags |= 0x40
        elif ext in (".fnt", ".tex"):
            flags |= 0x80
        else:
            flags |= 0x100
    return flags


def write_bsa(path, files, version=105, compress=False):
    """files: [(path inside the archive with /, full path on disk)]. Returns notes (e.g. why it isn't compressed)."""
    notes = []
    lz4f = None
    if compress and version == 105:
        try:
            import lz4.frame as lz4f
        except ImportError:
            compress = False
            notes.append("Not compressed: compressing Special Edition archives needs the lz4 library "
                         "(Apps → Game files → Install helpers).")
    folders = {}
    for rel, full in files:
        rel = rel.replace("\\", "/").lower()
        folder, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
        folders.setdefault(folder.replace("/", "\\"), []).append((name, full))
    flist = sorted(folders.items(), key=lambda kv: tes_hash(kv[0], folder=True))
    flist = [(f, sorted(items, key=lambda it: tes_hash(it[0]))) for f, items in flist]
    nfiles = sum(len(items) for _f, items in flist)
    folder_names_len = sum(len(f.encode("cp1252", "replace")) + 1 for f, _i in flist)
    file_names = b"".join(n.encode("cp1252", "replace") + b"\0" for _f, items in flist for n, _p in items)
    rec_size = 24 if version == 105 else 16
    header_size = 36
    blocks_start = header_size + rec_size * len(flist)
    blocks_size = sum(1 + len(f.encode("cp1252", "replace")) + 1 + 16 * len(items) for f, items in flist)
    data_start = blocks_start + blocks_size + len(file_names)
    archive_flags = 0x1 | 0x2 | (0x4 if compress else 0)

    tmp = path + ".part"
    with open(tmp, "wb") as out:
        out.write(b"\0" * data_start)
        records = []                 # per folder: [(hash, size field, offset)]
        for _folder, items in flist:
            recs = []
            for name, full in items:
                with open(full, "rb") as fh:
                    raw = fh.read()
                stored, packed = raw, False
                if compress and not name.endswith(NO_COMPRESS):
                    body = lz4f.compress(raw) if version == 105 else zlib.compress(raw, 6)
                    stored, packed = struct.pack("<I", len(raw)) + body, True
                size = len(stored)
                if compress and not packed:
                    size |= 0x40000000          # this one is stored as-is, against the archive default
                off = out.tell()
                if off + len(stored) > 0xFFFFFFFF:
                    raise ValueError("The archive would be over 4 GB; split it up.")
                out.write(stored)
                recs.append((tes_hash(name), size, off))
            records.append(recs)
        end = out.tell()
        if end > MAX_BSA:
            notes.append(f"{os.path.basename(path)} is {end / 1e9:.1f} GB; the game may fail to read archives over 2 GB.")
        # header, folder records, file record blocks, names
        out.seek(0)
        out.write(b"BSA\0" + struct.pack("<IIIIIII", version, header_size, archive_flags, len(flist), nfiles,
                                         folder_names_len, len(file_names)))
        out.write(struct.pack("<HH", _content_flags([f"{f}/{n}".replace("\\", "/") for f, items in flist for n, _p in items]), 0))
        pos = blocks_start
        for (folder, items) in flist:
            if version == 105:
                out.write(struct.pack("<QIIQ", tes_hash(folder, folder=True), len(items), 0, pos + len(file_names)))
            else:
                out.write(struct.pack("<QII", tes_hash(folder, folder=True), len(items), pos + len(file_names)))
            pos += 1 + len(folder.encode("cp1252", "replace")) + 1 + 16 * len(items)
        for (folder, items), recs in zip(flist, records):
            fb = folder.encode("cp1252", "replace") + b"\0"
            out.write(struct.pack("<B", len(fb)) + fb)
            for h, size, off in recs:
                out.write(struct.pack("<QII", h, size, off))
        out.write(file_names)
    os.replace(tmp, path)
    return notes


def dummy_plugin(author="", esl=True):
    """A minimal plugin (no records) whose only job is to make the game load a BSA of the same name."""
    def sub(t, data):
        return t.encode() + struct.pack("<H", len(data)) + data
    body = sub("HEDR", struct.pack("<fiI", 1.71, 0, 0x800))
    if author:
        body += sub("CNAM", author.encode("cp1252", "replace")[:500] + b"\0")
    body += sub("SNAM", b"Loads this mod's archive.\0")
    body += sub("MAST", b"Skyrim.esm\0") + sub("DATA", b"\0" * 8)
    flags = 0x200 if esl else 0
    return b"TES4" + struct.pack("<IIIIHH", len(body), flags, 0, 0, 44, 0) + body
