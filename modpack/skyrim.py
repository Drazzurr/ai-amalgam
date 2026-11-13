"""Skyrim (Special/Anniversary Edition and Legendary Edition): check a mod laid out like the game's
Data folder and package it for Vortex or Mod Organizer 2, optionally packing loose files into a BSA
archive and adding a FOMOD installer for Optional/<name> parts."""

import os
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import bsa
from .common import new_report, safe_name

PLUGIN_EXT = (".esp", ".esm", ".esl")
DATA_DIRS = {"meshes", "textures", "scripts", "source", "interface", "sound", "music", "seq", "skse", "strings",
             "grass", "lodsettings", "materials", "shadersfx", "video", "mcm", "calientetools", "nemesis_engine",
             "dialogueviews", "facegen", "actors", "lightplacer", "distantlod", "terrain", "trees", "shaders",
             "platform", "netscriptframework", "skee", "dyndolod", "parallax", "fomod"}
SKIP_DIRS = {".git", "_packages", "node_modules", "__pycache__", ".vscode", ".idea", ".venv", "venv", "optional"}
LABEL = "Skyrim"
# loose files that must stay loose (SKSE plugins, MCM settings, videos, animation tools...)
KEEP_LOOSE = {"skse", "mcm", "video", "seq", "nemesis_engine", "calientetools", "source", "fomod", "lightplacer", "netscriptframework", "platform", "dyndolod", "skee"}
WORK_EXT = (".blend", ".blend1", ".blend2", ".psd", ".xcf", ".kra", ".spp", ".sbs", ".sbsar", ".ztl", ".max", ".ma",
            ".mb", ".fbx", ".obj", ".dae", ".gltf", ".glb", ".stl", ".py", ".pyc", ".md", ".log", ".bak", ".tmp",
            ".ps1", ".bat", ".sh", ".zip", ".7z", ".rar", ".git", ".gitignore", ".gitattributes", ".ds_store")
BASE_MASTERS = {"skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm"}
TEXTURE_OK = (".dds",)
MESH_OK = (".nif", ".kf", ".tri", ".hkx", ".btr", ".bto", ".egm", ".ssf", ".xml", ".txt", ".hko")


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
    rep = new_report("skyrim", LABEL)
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
    se = edition(opts) != "le"
    rep["label"] = "Skyrim Special Edition" if se else "Skyrim Legendary Edition"
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
    if opts.get("bsa"):
        squeeze = opts.get("compress", True)
        if squeeze and se:
            try:
                import lz4.frame  # noqa: F401  (Special Edition archives are LZ4-compressed)
            except ImportError:
                squeeze = False
        rep["steps"].append("Packs loose meshes, textures, scripts and sounds into BSA archive(s)" + (" (compressed)." if squeeze else "."))
    if opts.get("fomod") and not rep["existing_fomod"]:
        rep["steps"].append("Adds a FOMOD installer" + (f" with {len(rep['optional'])} optional part(s)." if rep["optional"] else "."))
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


def edition(opts):
    e = (opts.get("edition") or opts.get("game") or "se").lower()
    return "le" if e == "le" else "se"


def detect(code_dir):
    """How much this folder looks like a Skyrim mod (0 = not at all)."""
    root = find_root(code_dir)
    if not root:
        return 0
    names = {n.lower() for n in os.listdir(root)}
    score = sum(5 for n in names if n.endswith(PLUGIN_EXT + (".bsa",)))
    score += sum(2 for n in names if n in ("meshes", "textures", "scripts", "interface", "skse", "seq", "sound"))
    return score


def prepare(code_dir, opts, rep, stage):
    """The files to put in the archive: [(path inside the archive, file on disk or bytes)]."""
    root = rep["root"]
    se = edition(opts) != "le"
    files, _left = _walk(root, opts.get("include_source") is not False)
    name = safe_name(opts.get("name") or (os.path.splitext(rep["plugins"][0]["name"])[0] if rep["plugins"] else
                                          os.path.basename(code_dir)))
    author = (opts.get("author") or (rep["plugins"][0]["author"] if rep["plugins"] else "") or "").strip()[:80]
    if opts.get("bsa"):
        files = _pack_bsa(files, opts, rep, stage, name, author, se)
    fomod = opts.get("fomod") and not rep["existing_fomod"]
    if not fomod:
        return files
    version = (opts.get("version") or "1.0.0").strip()[:20]
    description = (opts.get("description") or (rep["plugins"][0]["description"] if rep["plugins"] else "") or "").strip()[:2000]
    info, module = _fomod_xml(name, version, author, description, opts.get("url", ""), rep["optional"])
    out = [("fomod/info.xml", info.encode("utf-8")), ("fomod/ModuleConfig.xml", module.encode("utf-8"))]
    out += [("Main/" + rel, full) for rel, full in files if not rel.lower().startswith("fomod/")]
    for o in rep["optional"]:
        ofiles, _l = _walk(os.path.join(code_dir, "Optional", o), opts.get("include_source") is not False)
        out += [(f"Optional/{o}/{rel}", full) for rel, full in ofiles]
    return out


def _pack_bsa(files, opts, rep, stage, name, author, se):
    """Pack loose assets into <plugin>.bsa (+ <plugin> - Textures.bsa on Special Edition)."""
    plugin = next((p["name"] for p in rep["plugins"] if not p["esl"] or se), None)
    base = os.path.splitext(plugin)[0] if plugin else name
    packable = [(r, f) for r, f in files if "/" in r and r.split("/")[0].lower() not in KEEP_LOOSE
                and not r.lower().endswith((".psc",))]
    if not packable:
        rep["steps"].append("Nothing to put in a BSA (no loose meshes, textures, scripts or sounds).")
        return files
    loose = [(r, f) for r, f in files if (r, f) not in packable]
    groups = [(base + ".bsa", packable)]
    if se:
        tex = [(r, f) for r, f in packable if r.lower().startswith("textures/")]
        rest = [(r, f) for r, f in packable if not r.lower().startswith("textures/")]
        groups = ([(base + ".bsa", rest)] if rest else []) + ([(base + " - Textures.bsa", tex)] if tex else [])
    made = []
    for arc_name, members in groups:
        path = stage.file(arc_name)
        notes = bsa.write_bsa(path, members, version=105 if se else 104, compress=bool(opts.get("compress", True)))
        rep["notes"] += [n for n in notes if n not in rep["notes"]]
        made.append((arc_name, path))
        rep["steps"].append(f"Packed {len(members)} files into {arc_name} ({os.path.getsize(path) / 1e6:.1f} MB).")
    if not plugin:
        esp = base + (".esl" if se else ".esp")
        made.append((esp, bsa.dummy_plugin(author, esl=se)))
        rep["steps"].append(f"Added {esp}, an empty plugin whose only job is to make the game load the archive.")
    return loose + made
