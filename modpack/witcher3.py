"""The Witcher 3: package mods laid out the game's way (mods/mod<Name>/content, dlc/<name>/content,
bin/config/...). A folder with just a content/ folder becomes mods/mod<Name>/content. The game only
reads packed game files (.bundle + metadata.store) plus loose scripts (.ws); loose game files are
packed with wcc_lite (the official mod tool) when it's found."""

import os
import re
import shutil

from .common import new_report, run_tool, safe_name, walk

LABEL = "The Witcher 3"
OK_IN_CONTENT = (".ws", ".bundle", ".store", ".cache", ".txt", ".reddlc", ".csv.txt")
SCRIPTS_ONLY_DIRS = ("scripts",)


def _layout(code_dir, name):
    """[(path in the game folder, file)] + files left out. Accepts mods/, dlc/, bin/, mod*/ and content/ at the top."""
    files, left = walk(code_dir)
    out, skipped = [], list(left)
    for rel, full in files:
        parts = rel.split("/")
        top = parts[0].lower()
        if top in ("mods", "dlc", "bin") and len(parts) > 1:
            out.append((rel, full))
        elif top.startswith("mod") and len(parts) > 2:
            out.append(("mods/" + rel, full))
        elif top.startswith("dlc") and len(parts) > 2:
            out.append(("dlc/" + rel, full))
        elif top == "content" and len(parts) > 1:
            out.append((f"mods/mod{name}/" + rel, full))
        else:
            skipped.append(rel)
    return out, skipped


def _name(opts, code_dir):
    n = re.sub(r"[^A-Za-z0-9_]+", "", safe_name(opts.get("name") or os.path.basename(code_dir.rstrip("/\\"))))
    n = n[3:] if n.lower().startswith("mod") else n
    return n or "MyMod"


def detect(code_dir):
    try:
        names = {n.lower(): n for n in os.listdir(code_dir)}
    except OSError:
        return 0
    score = 0
    if "mods" in names and any(d.lower().startswith("mod") for d in os.listdir(os.path.join(code_dir, names["mods"]))):
        score += 20
    if any(n.startswith("mod") and os.path.isdir(os.path.join(code_dir, names[n], "content")) for n in names):
        score += 20
    if "content" in names and os.path.isdir(os.path.join(code_dir, names["content"])):
        c = os.path.join(code_dir, names["content"])
        inner = {x.lower() for x in os.listdir(c)}
        if "scripts" in inner or "metadata.store" in inner or any(x.endswith(".bundle") for x in inner):
            score += 18
    if "dlc" in names and os.path.isdir(os.path.join(code_dir, names["dlc"])):
        score += 6
    if os.path.isdir(os.path.join(code_dir, "bin", "config", "r4game")):
        score += 8
    return score


def _wcc_lite():
    """wcc_lite.exe from REDkit or the old Modkit, if it's installed in a Steam library."""
    found = shutil.which("wcc_lite")
    if found:
        return found
    try:
        from gamefiles.tools import _steam_libraries
        libs = _steam_libraries()
    except Exception:
        libs = []
    for lib in libs:
        common = os.path.join(lib, "steamapps", "common")
        try:
            dirs = [d for d in os.listdir(common) if "redkit" in d.lower() or "mod tools" in d.lower() or "modkit" in d.lower()]
        except OSError:
            continue
        for d in dirs:
            for dp, _dn, fs in os.walk(os.path.join(common, d)):
                if dp.count(os.sep) - common.count(os.sep) > 5:
                    continue
                if "wcc_lite.exe" in (f.lower() for f in fs):
                    return os.path.join(dp, next(f for f in fs if f.lower() == "wcc_lite.exe"))
    return None


def _units(files):
    """mods/mod<X> and dlc/<x> folders -> their files (path inside that folder)."""
    units = {}
    for rel, full in files:
        parts = rel.split("/")
        if parts[0].lower() in ("mods", "dlc") and len(parts) > 2:
            units.setdefault("/".join(parts[:2]), []).append(("/".join(parts[2:]), full))
    return units


def check(code_dir, opts):
    rep = new_report("witcher3", LABEL)
    rep["root"] = code_dir
    name = _name(opts, code_dir)
    files, left = _layout(code_dir, name)
    rep["left_out"] = left[:200]
    rep["files"], rep["bytes"] = len(files), sum(os.path.getsize(f) for _r, f in files)
    if not files:
        rep["errors"].append("No Witcher 3 mod files found: expected mods/mod<Name>/content, a mod<Name>/content "
                             "folder, a content/ folder, dlc/<name>/content or bin/config.")
        return rep
    wcc = _wcc_lite()
    loose_units = []
    for unit, members in sorted(_units(files).items()):
        kind, folder = unit.split("/")
        if kind.lower() == "mods" and not folder.lower().startswith("mod"):
            rep["errors"].append(f"{unit}: mod folders must start with “mod” (e.g. mod{folder}) or the game skips them.")
        content = [(r, f) for r, f in members if r.lower().startswith("content/")]
        if not content:
            rep["errors"].append(f"{unit} has no content/ folder; the game only reads <mod>/content.")
            continue
        names = {r.lower() for r, _f in content}
        bundles = [r for r in names if r.endswith(".bundle")]
        if bundles and "content/metadata.store" not in names:
            rep["errors"].append(f"{unit}: has .bundle files but no content/metadata.store, so the game won't load them "
                                 "(wcc_lite metadatastore makes it).")
        loose = [r for r, _f in content if not r.lower().endswith(OK_IN_CONTENT) and "/scripts/" not in "/" + r.lower()]
        if loose:
            if wcc:
                rep["steps"].append(f"{unit}: packs {len(loose)} loose game file(s) into a bundle with wcc_lite.")
                loose_units.append(unit)
            else:
                rep["errors"].append(f"{unit}: {len(loose)} loose game file(s) (e.g. {loose[0]}) that the game won't "
                                     "read until they're packed into a .bundle. Pack them with REDkit, or install "
                                     "REDkit/the Modkit so Amalgam can run wcc_lite.")
        ws = [r for r in names if r.endswith(".ws")]
        if ws:
            outside = [r for r in ws if not r.startswith("content/scripts/")]
            if outside:
                rep["warnings"].append(f"{unit}: scripts outside content/scripts are ignored ({outside[0]}).")
            rep["notes"].append(f"{unit} changes scripts: other script mods that change the same files need "
                                "Script Merger to work together.")
        if kind.lower() == "dlc" and not bundles and not any(r.endswith(".reddlc") for r in names):
            rep["warnings"].append(f"{unit}: a DLC folder normally has its bundle and metadata.store (or a .reddlc).")
    rep["w3"] = {"name": name, "pack": loose_units, "wcc": wcc}
    menus = [r for r, _f in files if r.lower().startswith("bin/config/r4game/user_config_matrix/pc/") and r.lower().endswith(".xml")]
    if menus:
        names = ", ".join(os.path.basename(m) for m in menus[:5])
        rep["notes"].append(f"Menu file(s) {names}: since game version 4.0 each must also be listed in dx11filelist.txt "
                            "and dx12filelist.txt in that folder; tell players to add the line (don't ship those two "
                            "files, they'd overwrite other mods' entries).")
        for r in files:
            if r[0].lower().endswith(("dx11filelist.txt", "dx12filelist.txt")):
                rep["warnings"].append(f"{r[0]}: shipping this overwrites other mods' menu entries; leave it out.")
    if any(r.lower().endswith("input.settings") for r, _f in files):
        rep["warnings"].append("input.settings lives in Documents/The Witcher 3, not the game folder; give players "
                               "the lines to add instead.")
    rep["notes"].append("Installs into the game folder (mods/, dlc/, bin/) with Vortex, The Witcher 3 Mod Manager or by hand.")
    return rep


def prepare(code_dir, opts, rep, stage):
    w = rep["w3"]
    files, _left = _layout(code_dir, w["name"])
    for unit in w["pack"]:
        members = [(r, f) for r, f in files if r.startswith(unit + "/content/")]
        loose = [(r, f) for r, f in members if not r.lower().endswith(OK_IN_CONTENT) and "/scripts/" not in r.lower()]
        raw = stage.file(f"raw/{unit}/x")[:-2]
        for r, f in loose:
            dest = os.path.join(raw, *r[len(unit) + len("/content/"):].split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(f, dest)
        out = stage.file(f"packed/{unit}/content/x")[:-2]
        ok, log = run_tool([w["wcc"], "pack", f"-dir={raw}", f"-outdir={out}"])
        if ok:
            ok, log = run_tool([w["wcc"], "metadatastore", f"-path={out}"])
        if not ok:
            raise RuntimeError(f"wcc_lite couldn't pack {unit}:\n" + log[-800:])
        loose_set = {r for r, _f in loose}
        files = [(r, f) for r, f in files if r not in loose_set]
        for dp, _d, fs in os.walk(out):
            for fn in fs:
                full = os.path.join(dp, fn)
                files.append((f"{unit}/content/" + os.path.relpath(full, out).replace("\\", "/"), full))
        rep["steps"].append(f"Packed {len(loose)} file(s) of {unit} into a bundle with wcc_lite.")
    return files
