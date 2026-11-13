"""Package a project's code folder as a Skyrim mod archive that Vortex or Mod Organizer 2 can install,
after checking it for the usual packaging mistakes.

The mod's files are found where they live in a Data folder (plugins, meshes/, textures/, scripts/...):
at the top of the code folder, or in a Data/ or mod/ subfolder. Work files (Blender scenes, Photoshop
files, Python scripts, notes) are left out. Optional parts can go in Optional/<name>/ (each laid out
like a Data folder); with the installer option they become checkboxes in a FOMOD installer.
"""

import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape

PLUGIN_EXT = (".esp", ".esm", ".esl")
DATA_DIRS = {"meshes", "textures", "scripts", "source", "interface", "sound", "music", "seq", "skse", "strings",
             "grass", "lodsettings", "materials", "shadersfx", "video", "mcm", "calientetools", "nemesis_engine",
             "dialogueviews", "facegen", "actors", "lightplacer", "distantlod", "terrain", "trees", "shaders",
             "platform", "netscriptframework", "skee", "dyndolod", "parallax", "fomod"}
SKIP_DIRS = {".git", "_packages", "node_modules", "__pycache__", ".vscode", ".idea", ".venv", "venv", "optional"}
WORK_EXT = (".blend", ".blend1", ".blend2", ".psd", ".xcf", ".kra", ".spp", ".sbs", ".sbsar", ".ztl", ".max", ".ma",
            ".mb", ".fbx", ".obj", ".dae", ".gltf", ".glb", ".stl", ".py", ".pyc", ".md", ".log", ".bak", ".tmp",
            ".ps1", ".bat", ".sh", ".zip", ".7z", ".rar", ".git", ".gitignore", ".gitattributes", ".ds_store")
BASE_MASTERS = {"skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm"}
TEXTURE_OK = (".dds",)
MESH_OK = (".nif", ".tri", ".hkx", ".btr", ".bto", ".egm", ".ssf", ".xml", ".txt", ".hko")


def find_root(code_dir):
    """The folder laid out like Skyrim's Data folder (or None)."""
    cands = [code_dir] + [os.path.join(code_dir, n) for n in ("Data", "data", "mod", "Mod", "Package", "package")]
    try:
        subs = [os.path.join(code_dir, n) for n in os.listdir(code_dir)
                if os.path.isdir(os.path.join(code_dir, n)) and n.lower() not in SKIP_DIRS and not n.startswith(".")]
    except OSError:
        return None
    if len(subs) == 1:
        cands.append(subs[0])

    def score(d):
        try:
            names = os.listdir(d)
        except OSError:
            return 0
        return sum(3 for n in names if n.lower().endswith(PLUGIN_EXT + (".bsa",))) + \
            sum(2 for n in names if n.lower() in DATA_DIRS and n.lower() != "fomod" and os.path.isdir(os.path.join(d, n)))
    best = max(cands, key=score)
    return best if score(best) > 0 else None


def _walk(root, include_source=True):
    """(files to package as (relative path, full path), files left out)."""
    keep, left = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            low = rel.lower()
            top = low.split("/")[0]
            if name.lower().endswith(WORK_EXT) or name.startswith("."):
                left.append(rel)
            elif not include_source and (top == "source" or "/source/" in "/" + low or low.endswith(".psc")):
                left.append(rel)
            elif rel_dir == "." and not low.endswith(PLUGIN_EXT + (".bsa", ".ini")) and "readme" not in low:
                left.append(rel)         # loose notes at the top of the folder
            else:
                keep.append((rel, os.path.join(dirpath, name)))
    return keep, left


def _plugin_info(path):
    from gamefiles.tes import Plugin
    with open(path, "rb") as f:
        p = Plugin(f.read())
    return {"name": os.path.basename(path), "masters": p.masters, "author": p.author, "description": p.desc,
            "form_version": p.header.get("version"), "hedr": round(p.version, 2), "records": p.num_records,
            "esm": bool(p.flags & 0x1), "esl": bool(p.flags & 0x200), "localized": p.localized}


def _nif_stream(path):
    """Bethesda stream number of a mesh (83 = Legendary Edition, 100 = Special Edition), or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(200)
        nl = head.index(b"\n")
        if b"Gamebryo" not in head[:nl]:
            return None
        import struct
        user_ver = struct.unpack_from("<I", head, nl + 1 + 4 + 1)[0]
        return struct.unpack_from("<I", head, nl + 1 + 4 + 1 + 4 + 4)[0] if user_ver >= 10 else None
    except (OSError, ValueError, Exception):
        return None


def check(code_dir, opts):
    """What would go in the package, and anything that looks wrong. Errors stop the build."""
    rep = {"errors": [], "warnings": [], "notes": [], "files": 0, "bytes": 0, "plugins": [], "root": None,
           "left_out": [], "optional": [], "existing_fomod": False}
    root = find_root(code_dir)
    if not root:
        rep["errors"].append("This folder doesn't look like a Skyrim mod: no plugin (.esp/.esm/.esl), archive (.bsa) "
                             "or Data folders (meshes, textures, scripts...) at its top, or in a Data or mod subfolder.")
        return rep
    rep["root"] = root
    rep["existing_fomod"] = os.path.isfile(os.path.join(root, "fomod", "ModuleConfig.xml"))
    files, left = _walk(root, opts.get("include_source") is not False)
    rep["left_out"] = left[:200]
    rep["files"] = len(files)
    rep["bytes"] = sum(os.path.getsize(f) for _r, f in files)
    names_low = {}
    se = opts.get("game", "se") != "le"
    plugins = [(r, f) for r, f in files if "/" not in r and r.lower().endswith(PLUGIN_EXT)]
    plugin_names = {os.path.splitext(r)[0].lower() for r, _f in plugins}
    in_mod = {r.lower() for r, _f in plugins}
    for r, f in plugins:
        try:
            info = _plugin_info(f)
        except Exception as e:
            rep["errors"].append(f"{r} couldn't be read as a plugin ({e}). Is it a real .esp/.esm/.esl?")
            continue
        rep["plugins"].append(info)
        fv = info["form_version"]
        if se and fv is not None and fv < 44:
            rep["warnings"].append(f"{r} was last saved for Legendary Edition (form version {fv}). Open and save it once "
                                   "in the Special Edition Creation Kit (or SSEEdit) before releasing it for SE/AE.")
        if not se and fv is not None and fv >= 44:
            rep["warnings"].append(f"{r} is a Special Edition plugin (form version {fv}); it won't load in Legendary Edition.")
        if info["esl"] and not se:
            rep["warnings"].append(f"{r} is ESL-flagged, which Legendary Edition doesn't support.")
        need = [m for m in info["masters"] if m.lower() not in BASE_MASTERS and m.lower() not in in_mod]
        cc = [m for m in need if re.match(r"cc\w+\.es[lmp]$", m, re.I)]
        other = [m for m in need if m not in cc]
        if other:
            rep["notes"].append(f"{r} needs these other mods installed (list them as requirements): {', '.join(other)}.")
        if cc:
            rep["notes"].append(f"{r} needs Creation Club content: {', '.join(cc)}.")
    for r, f in files:
        low = r.lower()
        if low in names_low:
            rep["errors"].append(f"Two files differ only in capital letters: {names_low[low]} and {r} (Windows can't hold both).")
        names_low[low] = r
        top = low.split("/")[0]
        ext = os.path.splitext(low)[1]
        if "/" in low and top not in DATA_DIRS:
            rep["warnings"].append(f"{r}: the game ignores files in a “{r.split('/')[0]}” folder (not a Data folder).")
        if top == "textures" and ext not in TEXTURE_OK:
            rep["warnings"].append(f"{r}: Skyrim only reads .dds textures; convert it (or leave it out).")
        if top == "meshes" and ext not in MESH_OK:
            rep["warnings"].append(f"{r}: unusual file type in meshes/.")
        if ext == ".bsa" and "/" not in r and not any(os.path.splitext(low)[0].startswith(p) for p in plugin_names):
            rep["warnings"].append(f"{r}: an archive is only loaded when its name starts with a plugin's name "
                                   "(e.g. MyMod.bsa or MyMod - Textures.bsa for MyMod.esp).")
        if len(r) > 200:
            rep["warnings"].append(f"{r[:60]}…: a very long path; some tools fail past 260 characters.")
    stem = lambda r: os.path.splitext(os.path.basename(r))[0]
    psc = {stem(r).lower(): stem(r) for r in [r for r, _f in files] + left if r.lower().endswith(".psc")}
    pex = {stem(r).lower() for r, _f in files if r.lower().endswith(".pex")}
    for low in sorted(set(psc) - pex):
        rep["warnings"].append(f"Script {psc[low]}.psc has no compiled {psc[low]}.pex, so it won't run in game. Compile it first.")
    stale = []
    for r, f in files:
        if r.lower().endswith(".nif"):
            s = _nif_stream(f)
            if se and s is not None and s < 100:
                stale.append(r)
            elif not se and s is not None and s >= 100:
                stale.append(r)
        if len(stale) > 20:
            break
    if stale:
        what = "Legendary Edition meshes in a Special Edition mod" if se else "Special Edition meshes in a Legendary Edition mod"
        rep["warnings"].append(f"{len(stale)} {what} (e.g. {stale[0]}). Convert them with Cathedral Assets Optimizer.")
    if not plugins and not any(r.lower().endswith(".bsa") for r, _f in files):
        rep["notes"].append("No plugin: fine for a replacer (textures/meshes only).")
    opt_root = os.path.join(code_dir, "Optional")
    if os.path.isdir(opt_root):
        rep["optional"] = sorted(d for d in os.listdir(opt_root) if os.path.isdir(os.path.join(opt_root, d)))
        if rep["optional"] and not opts.get("fomod") and not rep["existing_fomod"]:
            rep["warnings"].append("There are optional parts in Optional/, but they're only included with the "
                                   "installer (FOMOD) option on.")
    if rep["existing_fomod"]:
        try:
            ET.parse(os.path.join(root, "fomod", "ModuleConfig.xml"))
            rep["notes"].append("Using your own FOMOD installer (fomod/ModuleConfig.xml); the archive keeps your folder layout.")
        except ET.ParseError as e:
            rep["errors"].append(f"fomod/ModuleConfig.xml isn't valid XML ({e}); Vortex and MO2 would fail to read it.")
    rep["warnings"] = rep["warnings"][:60]
    return rep


def _fomod_xml(name, version, author, description, url, optional):
    info = (f'<?xml version="1.0" encoding="utf-8"?>\n<fomod>\n  <Name>{escape(name)}</Name>\n'
            f'  <Author>{escape(author)}</Author>\n  <Version>{escape(version)}</Version>\n'
            f'  <Description>{escape(description)}</Description>\n' +
            (f'  <Website>{escape(url)}</Website>\n' if url else "") + "</fomod>\n")
    steps = ""
    if optional:
        plugins = "".join(
            f'''          <plugin name="{escape(o)}">
            <description>{escape(o)}</description>
            <files><folder source="Optional/{escape(o)}" destination="" priority="0"/></files>
            <typeDescriptor><type name="Optional"/></typeDescriptor>
          </plugin>
''' for o in optional)
        steps = f'''  <installSteps order="Explicit">
    <installStep name="Options">
      <optionalFileGroups order="Explicit">
        <group name="Optional files" type="SelectAny">
          <plugins order="Explicit">
{plugins}          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
'''
    module = f'''<?xml version="1.0" encoding="utf-8"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>{escape(name)}</moduleName>
  <requiredInstallFiles>
    <folder source="Main" destination=""/>
  </requiredInstallFiles>
{steps}</config>
'''
    return info, module


def build(code_dir, opts):
    """Make the archive in <code folder>/_packages. Returns (path or None, report)."""
    rep = check(code_dir, opts)
    if rep["errors"]:
        return None, rep
    root = rep["root"]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", opts.get("name") or "").strip() or \
        (os.path.splitext(rep["plugins"][0]["name"])[0] if rep["plugins"] else os.path.basename(code_dir))
    version = (opts.get("version") or "1.0.0").strip()[:20]
    author = (opts.get("author") or (rep["plugins"][0]["author"] if rep["plugins"] else "") or "").strip()[:80]
    description = (opts.get("description") or (rep["plugins"][0]["description"] if rep["plugins"] else "") or "").strip()[:2000]
    out_dir = os.path.join(code_dir, "_packages")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name} {version}.zip".replace("  ", " "))
    files, _left = _walk(root, opts.get("include_source") is not False)
    fomod = opts.get("fomod") and not rep["existing_fomod"]
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if fomod:
            info, module = _fomod_xml(name, version, author, description, opts.get("url", ""), rep["optional"])
            z.writestr("fomod/info.xml", info)
            z.writestr("fomod/ModuleConfig.xml", module)
            for rel, full in files:
                if not rel.lower().startswith("fomod/"):
                    z.write(full, "Main/" + rel)
            for o in rep["optional"]:
                ofiles, _l = _walk(os.path.join(code_dir, "Optional", o), opts.get("include_source") is not False)
                for rel, full in ofiles:
                    z.write(full, f"Optional/{o}/{rel}")
        else:
            for rel, full in files:
                z.write(full, rel)
    os.replace(tmp, path)
    rep["path"] = path
    rep["package_bytes"] = os.path.getsize(path)
    rep["built_at"] = time.time()
    return path, rep


def summary(rep):
    """A short note for the chat, so the models know how packaging went."""
    if rep.get("path"):
        head = (f"Packaged {os.path.basename(rep['path'])}: {rep['files']} files, "
                f"{rep['package_bytes'] / 1e6:.1f} MB, in {os.path.dirname(rep['path'])}.")
    else:
        head = "Mod check: " + (f"{len(rep['errors'])} problem(s) stop packaging." if rep["errors"] else
                                f"{rep['files']} files ready to package.")
    parts = [head] + [f"Problem: {e}" for e in rep["errors"]] + [f"Warning: {w}" for w in rep["warnings"][:15]] + \
        [f"Note: {n}" for n in rep["notes"]]
    if len(rep["warnings"]) > 15:
        parts.append(f"(and {len(rep['warnings']) - 15} more warnings)")
    return "\n".join(parts)
