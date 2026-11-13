"""Cyberpunk 2077: package a mod laid out like the game folder (archive/pc/mod, r6/scripts, r6/tweaks,
bin/x64/plugins/cyber_engine_tweaks/mods, red4ext/plugins, mods/<REDmod>), or a WolvenKit project
(source/archive packed into an .archive with WolvenKit; source/resources copied as they are).
Game files must be packed into an .archive: the game doesn't load loose ones."""

import json
import os
import shutil

from .common import new_report, run_tool, safe_name, walk

LABEL = "Cyberpunk 2077"
TOPS = {"archive", "r6", "bin", "red4ext", "mods", "engine", "tools"}
FRAMEWORK_FILES = {"bin/x64/version.dll", "bin/x64/global.ini", "bin/x64/winmm.dll", "red4ext/red4ext.dll",
                   "bin/x64/plugins/cyber_engine_tweaks.asi", "bin/x64/d3d11.dll", "bin/x64/powrprof.dll"}


def _project(code_dir):
    """A WolvenKit project: *.cpmodproj and/or a source/ folder with archive/ or resources/."""
    src = os.path.join(code_dir, "source")
    has_proj = any(n.lower().endswith(".cpmodproj") for n in os.listdir(code_dir))
    if has_proj or os.path.isdir(os.path.join(src, "archive")):
        return src
    return None


def _wolvenkit():
    try:
        from gamefiles.tools import _wolvenkit_exe
        exe = _wolvenkit_exe()
    except Exception:
        exe = None
    return exe or shutil.which("wolvenkit.cli") or shutil.which("WolvenKit.CLI")


def detect(code_dir):
    try:
        names = {n.lower() for n in os.listdir(code_dir)}
    except OSError:
        return 0
    if _project(code_dir):
        return 25
    score = 0
    if os.path.isdir(os.path.join(code_dir, "archive", "pc")):
        score += 15
    if os.path.isdir(os.path.join(code_dir, "r6")):
        score += 12
    if os.path.isdir(os.path.join(code_dir, "red4ext")):
        score += 12
    if os.path.isdir(os.path.join(code_dir, "bin", "x64", "plugins")):
        score += 10
    if any(n.endswith(".archive") for n in names):
        score += 10
    return score


def _layout(code_dir):
    """[(path in the game folder, file)] for a game-folder layout, plus files left out."""
    files, left = walk(code_dir, skip_dirs=("source", "packed", "layout"))
    keep, out = [], list(left)
    for rel, full in files:
        low = rel.lower()
        top = low.split("/")[0]
        if "/" not in rel:
            if low.endswith(".archive") or low.endswith(".xl"):
                keep.append(("archive/pc/mod/" + rel, full))    # loose archives at the top: where they belong
            else:
                out.append(rel)
        elif top in TOPS:
            keep.append((rel, full))
        else:
            out.append(rel)
    return keep, out


def check(code_dir, opts):
    rep = new_report("cyberpunk", LABEL)
    rep["root"] = code_dir
    proj = _project(code_dir)
    files, left = _layout(code_dir)
    rep["left_out"] = left[:200]
    if proj:
        arch = os.path.join(proj, "archive")
        n_arch = sum(len(fs) for _dp, _d, fs in os.walk(arch)) if os.path.isdir(arch) else 0
        res, _l = walk(os.path.join(proj, "resources")) if os.path.isdir(os.path.join(proj, "resources")) else ([], [])
        files += res
        if n_arch:
            name = safe_name(opts.get("name") or os.path.basename(code_dir.rstrip("/\\")))
            if _wolvenkit():
                rep["steps"].append(f"Packs {n_arch} files from source/archive into archive/pc/mod/{name}.archive with WolvenKit.")
            else:
                packed = _packed_archives(code_dir)
                if packed:
                    rep["warnings"].append("WolvenKit isn't installed here, so the .archive WolvenKit last packed (in packed/) "
                                           "is used. Re-pack in WolvenKit first if you changed anything since.")
                else:
                    rep["errors"].append("source/archive has game files that must be packed into an .archive, which needs "
                                         "WolvenKit: Apps → Game files → Install helpers (or pack the project in WolvenKit).")
        rep["files"] = len(files) + n_arch
    else:
        rep["files"] = len(files)
    if not files and not proj:
        rep["errors"].append("No Cyberpunk mod files found: expected folders like archive/pc/mod, r6/scripts, r6/tweaks, "
                             "red4ext/plugins or bin/x64/plugins/cyber_engine_tweaks/mods, or a WolvenKit project.")
        return rep
    rep["bytes"] = sum(os.path.getsize(f) for _r, f in files)
    req = []
    cet_mods = set()
    for rel, full in files:
        low = rel.lower()
        name = low.rsplit("/", 1)[-1]
        if low in FRAMEWORK_FILES or low.startswith("bin/x64/plugins/cyber_engine_tweaks/") and not \
                low.startswith("bin/x64/plugins/cyber_engine_tweaks/mods/"):
            rep["warnings"].append(f"{rel}: a framework file (CET, RED4ext...). Don't ship frameworks; list them as requirements.")
        if name.endswith(".archive"):
            if not low.startswith("archive/pc/mod/"):
                if low.startswith("archive/pc/content/"):
                    rep["errors"].append(f"{rel}: never put mod archives in archive/pc/content; they go in archive/pc/mod.")
                elif not low.startswith("mods/"):
                    rep["warnings"].append(f"{rel}: .archive files go in archive/pc/mod (or a REDmod's archives folder).")
        if name.endswith(".xl"):
            req.append("ArchiveXL")
            if not low.startswith("archive/pc/mod/"):
                rep["warnings"].append(f"{rel}: ArchiveXL files (.xl) go in archive/pc/mod next to the archive.")
        if name.endswith(".reds"):
            req.append("redscript")
            if not low.startswith("r6/scripts/") and not low.startswith("mods/"):
                rep["warnings"].append(f"{rel}: redscript files (.reds) go in r6/scripts.")
        if name.endswith((".yaml", ".yml", ".tweak")):
            if low.startswith("r6/tweaks/"):
                req.append("TweakXL")
                _yaml_check(rel, full, rep)
        if low.startswith("bin/x64/plugins/cyber_engine_tweaks/mods/"):
            req.append("Cyber Engine Tweaks")
            parts = rel.split("/")
            if len(parts) > 6:
                cet_mods.add("/".join(parts[:6]))
        if low.startswith("red4ext/plugins/") and name.endswith(".dll"):
            req.append("RED4ext")
        if low.startswith("mods/") and rel.count("/") == 2 and name == "info.json":
            req.append("REDmod (free DLC)")
            try:
                with open(full, encoding="utf-8") as fh:
                    info = json.load(fh)
                if not info.get("name") or not info.get("version"):
                    rep["errors"].append(f"{rel} needs a “name” and a “version”.")
            except (ValueError, OSError) as e:
                rep["errors"].append(f"{rel} isn't valid JSON ({e}).")
        if low.startswith("archive/pc/patch/"):
            rep["warnings"].append(f"{rel}: archive/pc/patch is from before game version 1.3; use archive/pc/mod.")
    for m in sorted(cet_mods):
        if not os.path.isfile(os.path.join(code_dir, *m.split("/"), "init.lua")) and not any(
                r.lower() == (m + "/init.lua").lower() for r, _f in files):
            rep["errors"].append(f"{m}: a Cyber Engine Tweaks mod needs an init.lua in its folder.")
    if any(r.lower().startswith("r6/scripts/") and r.count("/") == 2 for r, _f in files):
        rep["notes"].append("Tip: put your .reds files in r6/scripts/<YourMod>/ so they don't mix with other mods.")
    rep["requires"] = sorted(set(req))
    names = [r.rsplit("/", 1)[-1] for r, _f in files if r.lower().endswith(".archive")]
    if names:
        rep["notes"].append("Archives load in alphabetical order (A loads before Z, and the first one wins a conflict): " +
                            ", ".join(sorted(names)[:8]) + ".")
    return rep


def _yaml_check(rel, full, rep):
    try:
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return
    if any(line.startswith("\t") or line[:len(line) - len(line.lstrip())].count("\t") for line in text.splitlines()):
        rep["errors"].append(f"{rel}: YAML can't use tabs for indenting (use spaces); TweakXL would fail to read it.")
        return
    try:
        import yaml
        yaml.safe_load(text)
    except ImportError:
        pass
    except Exception as e:
        rep["errors"].append(f"{rel} isn't valid YAML ({str(e).splitlines()[0]}).")


def _packed_archives(code_dir):
    p = os.path.join(code_dir, "packed", "archive", "pc", "mod")
    return [os.path.join(p, n) for n in os.listdir(p) if n.endswith(".archive")] if os.path.isdir(p) else []


def prepare(code_dir, opts, rep, stage):
    files, _left = _layout(code_dir)
    proj = _project(code_dir)
    if not proj:
        return files
    if os.path.isdir(os.path.join(proj, "resources")):
        res, _l = walk(os.path.join(proj, "resources"))
        files += res
    arch = os.path.join(proj, "archive")
    if not (os.path.isdir(arch) and any(fs for _dp, _d, fs in os.walk(arch))):
        return files
    name = safe_name(opts.get("name") or os.path.basename(code_dir.rstrip("/\\")))
    exe = _wolvenkit()
    if exe:
        src = stage.file(f"src/{name}/x")[:-2]
        shutil.rmtree(src, ignore_errors=True)
        shutil.copytree(arch, src)
        out_dir = stage.file("out/x")[:-2]
        ok, out = run_tool([exe, "pack", "-p", src, "-o", out_dir])
        made = [os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.endswith(".archive")]
        if not ok or not made:
            raise RuntimeError("WolvenKit couldn't pack the archive:\n" + out[-800:])
        rep["steps"].append(f"Packed archive/pc/mod/{name}.archive with WolvenKit ({os.path.getsize(made[0]) / 1e6:.1f} MB).")
        files.append((f"archive/pc/mod/{name}.archive", made[0]))
    else:
        for p in _packed_archives(code_dir):
            files.append(("archive/pc/mod/" + os.path.basename(p), p))
    return files
