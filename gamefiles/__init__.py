"""Read game files without exporting them first.

read(path, query="", out_dir=...) returns readable text. The query picks an action, for example
"list armor", "extract meshes\\armor\\x.nif", "record IronSword", "export Conversation_01".
Extracted files and converted images go to out_dir.

Supported: Skyrim/Creation Engine (.esp .esm .esl .bsa .strings .dlstrings .ilstrings .nif .pex),
Baldur's Gate 3 (.pak .loca .lsx .lsj; .lsf/.lsb/.lsbs/.gr2 via LSLib), Witcher 3 (.bundle, CR2W
files), Cyberpunk 2077 (.archive, CR2W files; JSON via WolvenKit), Mass Effect / UE3 (.pcc .sfm .upk
.u .tlk), Dragon Age: Origins / II (.erf, GFF files), textures (.dds .tga .png ...), and anything
text-based. Unknown binaries get a hex dump and the readable text inside them.
"""

import json
import os

from .util import hexdump, size_text, strings

TEXT_EXT = {".txt", ".ini", ".json", ".xml", ".lsx", ".lsj", ".psc", ".ws", ".reds", ".yaml", ".yml", ".csv",
            ".lua", ".script", ".cfg", ".toml", ".md", ".log", ".xml", ".nss", ".2da", ".js", ".py", ".cs", ".hlsl"}
IMAGE_EXT = {".dds", ".tga", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".xbm", ".tif", ".tiff"}
GFF_EXT = {".utc", ".uti", ".utp", ".dlg", ".are", ".gda", ".tlk", ".cut", ".cub", ".stg", ".mor", ".mop", ".mmh",
           ".msh", ".phy", ".mao", ".arl", ".ldf", ".tnt", ".gff", ".plo", ".bic", ".ute", ".utm", ".uts", ".utt"}
MAX_TEXT = 60000


def _split(query):
    query = (query or "").strip()
    if not query:
        return "", ""
    parts = query.split(None, 1)
    if parts[0].lower() not in ACTIONS:          # plain text: treat it as a search
        return "find", query
    return parts[0].lower(), (parts[1] if len(parts) > 1 else "")


ACTIONS = {"summary", "list", "extract", "record", "get", "find", "search", "names", "imports", "exports",
           "export", "json", "hex", "strings", "blocks"}


def image_to_png(path, out_dir):
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    with Image.open(path) as im:
        info = f"{im.format} {im.size[0]}×{im.size[1]} {im.mode}"
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        dest = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".png")
        im.save(dest)
        if max(im.size) > 1024:
            small = dest[:-4] + "_preview.png"
            im.thumbnail((1024, 1024))
            im.save(small)
            dest = small
    return info, dest


def read(path, query="", out_dir="."):
    if not os.path.isfile(path):
        return f"File not found: {path}"
    action, arg = _split(query)
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    head = f"{os.path.basename(path)} ({size_text(size)})\n"

    if ext in IMAGE_EXT:
        try:
            info, dest = image_to_png(path, out_dir)
            return head + f"Image: {info}. Saved as PNG: {dest}\nOpen that PNG with your image/file reader to look at it."
        except Exception as e:
            if ext != ".dds":
                return head + f"Couldn't open the image: {e}"
            return head + f"Couldn't decode this DDS texture ({e}). Header info:\n" + hexdump(open(path, 'rb').read(148), 0, 148)

    if ext in TEXT_EXT or ext in (".lsx",):
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if ext == ".lsx":
            from .bg3 import summarize_lsx
            return head + summarize_lsx(text, arg if action in ("find", "search") else "")
        if action in ("find", "search") and arg:
            hits = [f"{i + 1}: {l}" for i, l in enumerate(text.splitlines()) if arg.lower() in l.lower()]
            return head + "\n".join(hits[:500]) if hits else head + "No matches."
        return head + (text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[... cut; use 'find <text>' ...]")

    if ext in (".lsf", ".lsb", ".lsbs", ".lsbc", ".lsfx"):
        from .bg3 import read_lsf
        return head + read_lsf(path, action, arg, out_dir)
    if ext == ".gr2":
        from .bg3 import read_gr2
        return head + read_gr2(path, action, arg, out_dir)

    if size > 3 * 1024 ** 3:
        return head + "That file is too big to read in one go (over 3 GB)."
    with open(path, "rb") as f:
        data = f.read()

    if action == "hex":
        parts = arg.split()
        start = int(parts[0], 0) if parts else 0
        length = int(parts[1], 0) if len(parts) > 1 else 512
        return head + hexdump(data, start, min(length, 8192))
    if action == "strings":
        found = [s for s in strings(data, 4, 3000) if not arg or arg.lower() in s.lower()]
        return head + "\n".join(found[:1500])

    magic = data[:8]
    try:
        if magic[:4] == b"TES4":
            from .tes import read_plugin
            return head + read_plugin(data, action, arg, out_dir)
        if magic[:4] == b"BSA\0":
            from .tes import read_bsa
            return head + read_bsa(data, action, arg, out_dir)
        if ext in (".strings", ".dlstrings", ".ilstrings"):
            from .tes import read_strings
            return head + read_strings(data, action, arg, out_dir, ext)
        if data[:20].startswith((b"Gamebryo File Format", b"NetImmerse File Format")):
            from .tes import read_nif
            return head + read_nif(data, action, arg, out_dir)
        if magic[:4] == b"\xFA\x57\xC0\xDE":
            from .tes import read_pex
            return head + read_pex(data, action, arg, out_dir)
        if magic[:4] == b"LSPK":
            from .bg3 import read_pak
            return head + read_pak(path, data, action, arg, out_dir)
        if magic[:4] == b"LOCA":
            from .bg3 import read_loca
            return head + read_loca(data, action, arg, out_dir)
        if magic == b"POTATO70":
            from .redengine import read_bundle
            return head + read_bundle(data, action, arg, out_dir)
        if magic[:4] == b"CR2W":
            from .redengine import read_cr2w
            if action == "json":
                from .tools import wolvenkit_json
                return head + wolvenkit_json(path, out_dir)
            return head + read_cr2w(data, action, arg, out_dir)
        if magic[:4] == b"RDAR":
            from .redengine import read_archive
            from .tools import cp77_names
            return head + read_archive(data, action, arg, out_dir, cp77_names())
        if magic[:4] == b"\xC1\x83\x2A\x9E":
            from .ue3 import read_package
            return head + read_package(data, action, arg, out_dir)
        if magic[:4] == b"Tlk ":
            from .ue3 import read_tlk
            return head + read_tlk(data, action, arg, out_dir)
        if data[:16].decode("utf-16-le", "ignore").startswith("ERF V") or magic in (b"ERF V3.0", b"RIM V1.0"):
            from .dragonage import read_erf
            return head + read_erf(data, action, arg, out_dir)
        if magic[:4] == b"GFF ":
            from .dragonage import read_gff
            return head + read_gff(data, action, arg, out_dir)
        if ext in (".sb", ".toc", ".cas", ".cat"):
            return head + ("This is a Frostbite file (Dragon Age: Inquisition / The Veilguard, Mass Effect: "
                           "Andromeda). Frostbite data is spread over many files and needs Frosty Toolsuite "
                           "(FrostyEditor) to browse and export. Readable text inside:\n" +
                           "\n".join(strings(data[:4_000_000], 6, 200)))
        if data[:1] in (b"{", b"[") or ext == ".lsj":
            try:
                return head + json.dumps(json.loads(data), indent=1)[:MAX_TEXT]
            except ValueError:
                pass
    except Exception as e:
        return head + f"Couldn't read it as {ext or 'this format'}: {e}\n\nStart of the file:\n" + hexdump(data, 0, 256)

    found = strings(data, 5, 300)
    return (head + f"Unknown format (starts with {data[:8]!r}). Start of the file:\n" + hexdump(data, 0, 256) +
            "\n\nReadable text inside:\n" + "\n".join(found[:300]))
