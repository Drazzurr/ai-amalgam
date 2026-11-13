"""Baldur's Gate 3: check a mod workspace (Mods/<Folder>/meta.lsx, Public/, Localization/...), convert
localization .xml to .loca, set the version in meta.lsx, pack everything into <Folder>.pak, and add
the info.json that BG3 Mod Manager and Vortex read. A ready-made .pak can be packaged too."""

import datetime
import hashlib
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET

from . import larian
from .common import new_report, run_tool, safe_name, walk

LABEL = "Baldur's Gate 3"
TOPS = {"mods", "public", "localization", "generated", "game", "editor", "projects", "scripts"}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
GUSTAV = {"991c9c7a-fb80-40cb-8f0d-b92d4e80e9b1", "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8", "cb555efe-2d9e-131f-8195-a89329d218ea"}


def _find_root(code_dir):
    cands = [code_dir]
    try:
        cands += [os.path.join(code_dir, n) for n in sorted(os.listdir(code_dir))
                  if os.path.isdir(os.path.join(code_dir, n)) and not n.startswith((".", "_"))]
    except OSError:
        return None
    for c in cands:
        mods = next((os.path.join(c, n) for n in os.listdir(c) if n.lower() == "mods" and os.path.isdir(os.path.join(c, n))), None)
        if mods and any(os.path.isfile(os.path.join(mods, d, "meta.lsx")) for d in os.listdir(mods)):
            return c
    return None


def _paks(code_dir):
    try:
        return [os.path.join(code_dir, n) for n in os.listdir(code_dir) if n.lower().endswith(".pak")]
    except OSError:
        return []


def detect(code_dir):
    if _find_root(code_dir):
        return 20
    return 8 if _paks(code_dir) else 0


def _meta(path):
    """The ModuleInfo attributes of a meta.lsx, plus its dependencies."""
    tree = ET.parse(path)
    info, deps = {}, []
    for node in tree.iter("node"):
        if node.get("id") == "ModuleInfo":
            for a in node.findall("attribute"):
                info[a.get("id")] = a.get("value", "")
        if node.get("id") == "ModuleShortDesc":
            d = {a.get("id"): a.get("value", "") for a in node.findall("attribute")}
            if d.get("UUID") and d["UUID"] not in GUSTAV:
                deps.append(d)
    return tree, info, deps


def check(code_dir, opts):
    rep = new_report("bg3", LABEL)
    root = _find_root(code_dir)
    if not root:
        paks = _paks(code_dir)
        if paks:
            return _check_pak(paks[0], rep)
        rep["errors"].append("No BG3 mod found: expected a Mods/<YourMod>/meta.lsx (a mod workspace) or a ready .pak.")
        return rep
    rep["root"] = root
    mods_dir = next(os.path.join(root, n) for n in os.listdir(root) if n.lower() == "mods")
    folders = [d for d in os.listdir(mods_dir) if os.path.isfile(os.path.join(mods_dir, d, "meta.lsx"))]
    if len(folders) > 1:
        rep["warnings"].append(f"Several mods in Mods/ ({', '.join(folders)}); packaging {folders[0]}.")
    folder = folders[0]
    try:
        _tree, info, deps = _meta(os.path.join(mods_dir, folder, "meta.lsx"))
    except ET.ParseError as e:
        rep["errors"].append(f"Mods/{folder}/meta.lsx isn't valid XML ({e}).")
        return rep
    rep["bg3"] = {"folder": folder, "info": info, "deps": deps}
    if not info:
        rep["errors"].append(f"Mods/{folder}/meta.lsx has no ModuleInfo node.")
    if info.get("Folder") != folder:
        rep["errors"].append(f"meta.lsx says Folder “{info.get('Folder', '')}” but the folder is Mods/{folder}; they must match.")
    uuid = info.get("UUID", "")
    if not UUID_RE.match(uuid):
        rep["errors"].append(f"meta.lsx has no valid UUID (got “{uuid}”). Make a new one (e.g. in BG3 Mod Manager, Ctrl+G).")
    elif uuid.lower() in GUSTAV:
        rep["errors"].append("meta.lsx uses the base game's UUID; your mod needs its own.")
    if not info.get("Name"):
        rep["errors"].append("meta.lsx has no Name.")
    v = info.get("Version64") or info.get("Version")
    if v and not str(v).lstrip("-").isdigit():
        rep["errors"].append(f"meta.lsx Version64 must be a number (got “{v}”).")
    if deps:
        rep["requires"] += [d.get("Name") or d.get("Folder") or d["UUID"] for d in deps]
        rep["notes"].append("Needs these mods: " + ", ".join(rep["requires"]) + ".")
    files, left = walk(root, keep=lambda rel: "/" in rel)     # loose files at the top aren't part of a mod
    rep["left_out"] = left[:200]
    for r, _f in files:
        top = r.split("/")[0].lower()
        if top not in TOPS:
            rep["warnings"].append(f"{r}: the game ignores a “{r.split('/')[0]}” folder.")
    locs = 0
    for r, f in files:
        low = r.lower()
        if low.endswith(".lsx"):
            try:
                ET.parse(f)
            except ET.ParseError as e:
                rep["errors"].append(f"{r} isn't valid XML ({e}).")
        if low.startswith("localization/") and low.endswith(".xml"):
            try:
                with open(f, "rb") as fh:
                    _data, n = larian.xml_to_loca(fh.read())
                locs += 1
                if not n:
                    rep["warnings"].append(f"{r} has no <content contentuid=...> lines.")
            except ET.ParseError as e:
                rep["errors"].append(f"{r} isn't valid XML ({e}).")
        if "/scriptextender/" in low and "Script Extender" not in rep["requires"]:
            rep["requires"].append("Script Extender")
            rep["notes"].append("Uses the BG3 Script Extender (list it as a requirement).")
        if "/story/rawfiles/" in low and not any("Osiris" in n for n in rep["notes"]):
            rep["notes"].append("Has Osiris story scripts: they only run once compiled with the BG3 toolkit or LSLib.")
    if locs:
        rep["steps"].append(f"Converts {locs} localization .xml file(s) to .loca (the format the game reads).")
    pubs = sorted({r.split("/")[1] for r, _f in files if r.lower().startswith("public/") and r.count("/") >= 2})
    if pubs and folder not in pubs:
        rep["notes"].append(f"Public/ holds {', '.join(pubs)} but not Public/{folder}: fine if you're overriding those on purpose.")
    rep["files"] = len(files)
    rep["bytes"] = sum(os.path.getsize(f) for _r, f in files)
    rep["plugins"] = [{"name": info.get("Name", folder), "author": info.get("Author", ""),
                       "description": info.get("Description", ""), "esl": False, "esm": False}]
    rep["steps"].append(f"Packs everything into {folder}.pak" + (" with LSLib." if _divine() else
                        " (Amalgam's built-in packer; install the game helpers to use LSLib instead).") +
                        " Adds info.json for BG3 Mod Manager and Vortex.")
    return rep


def _check_pak(pak, rep):
    """A ready-made .pak: read its meta.lsx so info.json can be made."""
    from gamefiles.bg3 import Pak
    rep["root"] = os.path.dirname(pak)
    with open(pak, "rb") as f:
        p = Pak(pak, f.read())
    meta = next((x for x in p.files if re.match(r"^Mods/[^/]+/meta\.lsx$", x["path"], re.I)), None)
    if not meta:
        rep["errors"].append(f"{os.path.basename(pak)} has no Mods/<Folder>/meta.lsx inside, so mod managers can't list it.")
        return rep
    data = p.extract(meta)
    if data[:4] == b"LSOF":
        rep["errors"].append("Its meta.lsx is in binary form; convert it with LSLib or package the mod workspace instead.")
        return rep
    tmp = os.path.join(rep["root"], "_packages", ".meta.lsx")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(data)
    _tree, info, deps = _meta(tmp)
    os.remove(tmp)
    rep["bg3"] = {"folder": info.get("Folder", ""), "info": info, "deps": deps, "pak": pak}
    rep["files"], rep["bytes"] = 1, os.path.getsize(pak)
    rep["plugins"] = [{"name": info.get("Name", ""), "author": info.get("Author", ""),
                       "description": info.get("Description", ""), "esl": False, "esm": False}]
    rep["steps"].append(f"Uses the ready-made {os.path.basename(pak)} and adds info.json.")
    return rep


def _divine():
    try:
        from gamefiles.bg3 import divine_path
        return divine_path()
    except Exception:
        return None


def prepare(code_dir, opts, rep, stage):
    b = rep["bg3"]
    info = dict(b["info"])
    folder = b["folder"]
    if b.get("pak"):
        pak = b["pak"]
    else:
        root = rep["root"]
        files, _left = walk(root)
        pak_root = stage.file("pak/x")[:-2]
        for rel, full in files:
            if "/" not in rel or rel.split("/")[0].lower() not in TOPS:
                continue
            low = rel.lower()
            if low.startswith("localization/") and low.endswith(".xml"):
                with open(full, "rb") as fh:
                    data, _n = larian.xml_to_loca(fh.read())
                dest = os.path.join(pak_root, *rel[:-4].split("/")) + ".loca"
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)
                continue
            dest = os.path.join(pak_root, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
        meta_path = os.path.join(pak_root, "Mods", folder, "meta.lsx")
        if not os.path.isfile(meta_path):     # Mods folder spelled differently on disk
            meta_path = next(os.path.join(dp, f) for dp, _d, fs in os.walk(pak_root) for f in fs if f.lower() == "meta.lsx")
        if opts.get("version"):
            info["Version64"] = str(larian.version64(opts["version"]))
            text = open(meta_path, encoding="utf-8").read()
            text, n = re.subn(r'(<attribute id="Version64" type="int64" value=")[^"]*(")', rf'\g<1>{info["Version64"]}\g<2>', text)
            if not n:
                text = re.sub(r'(<attribute id="Version" type="int32" value=")[^"]*(")', rf'\g<1>{info["Version64"]}\g<2>', text)
            with open(meta_path, "w", encoding="utf-8") as f:
                f.write(text)
            rep["steps"].append(f"Set the version in meta.lsx to {larian.version_text(info['Version64'])}.")
        pak = stage.file(f"{folder}.pak")
        divine = _divine()
        packed = False
        if divine:
            ok, out = run_tool([divine, "-g", "bg3", "-l", "warn", "-a", "create-package", "-s", pak_root, "-d", pak,
                                "-c", "lz4"])
            packed = ok and os.path.isfile(pak)
            if not packed:
                rep["warnings"].append("LSLib couldn't pack it, so Amalgam's built-in packer did: " + out[-300:])
        if not packed:
            pak_files = [(os.path.relpath(os.path.join(dp, f), pak_root).replace("\\", "/"), os.path.join(dp, f))
                         for dp, _d, fs in os.walk(pak_root) for f in sorted(fs)]
            larian.write_pak(pak, pak_files)
        rep["steps"].append(f"Packed {folder}.pak ({os.path.getsize(pak) / 1e6:.1f} MB).")
    h = hashlib.md5()
    with open(pak, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    md5 = h.hexdigest()
    mod = {"Author": opts.get("author") or info.get("Author", ""), "Name": info.get("Name", folder), "Folder": folder,
           "Version": str(info.get("Version64") or info.get("Version") or larian.version64("1.0.0")),
           "Description": opts.get("description") or info.get("Description", ""), "UUID": info.get("UUID", ""),
           "Created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "Group": info.get("UUID", "")}
    if b["deps"]:
        mod["Dependencies"] = [d["UUID"] for d in b["deps"]]
    info_json = json.dumps({"Mods": [mod], "MD5": md5}, indent=2).encode("utf-8")
    return [(f"{safe_name(folder)}.pak", pak), ("info.json", info_json)]
