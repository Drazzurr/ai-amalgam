"""Mass Effect Legendary Edition: package DLC mods (DLC_MOD_<Name>/CookedPCConsole/...) for ME3Tweaks
Mod Manager, writing (or updating) the moddesc.ini it installs from. LE2 and LE3 DLC mods need a
Mount.dlc; LE1 DLC mods load through an AutoLoad.ini instead."""

import configparser
import os
import re

from .common import new_report, walk

LABEL = "Mass Effect Legendary Edition"
GAMES = {"LE1": "Mass Effect 1 (LE)", "LE2": "Mass Effect 2 (LE)", "LE3": "Mass Effect 3 (LE)"}
CMMVER = "8.0"


def _dlc_dirs(root):
    try:
        return sorted(n for n in os.listdir(root) if n.upper().startswith("DLC_") and os.path.isdir(os.path.join(root, n)))
    except OSError:
        return []


def _find_root(code_dir):
    cands = [code_dir]
    try:
        cands += [os.path.join(code_dir, n) for n in sorted(os.listdir(code_dir))
                  if os.path.isdir(os.path.join(code_dir, n)) and not n.startswith((".", "_"))]
    except OSError:
        return None
    for c in cands:
        if os.path.isfile(os.path.join(c, "moddesc.ini")) or _dlc_dirs(c):
            return c
    return None


def detect(code_dir):
    root = _find_root(code_dir)
    if not root:
        return 0
    score = 10 if os.path.isfile(os.path.join(root, "moddesc.ini")) else 0
    for d in _dlc_dirs(root):
        if os.path.isdir(os.path.join(root, d, "CookedPCConsole")) or os.path.isdir(os.path.join(root, d, "cookedpcconsole")):
            score += 15
    return score


def _cooked(root, d):
    return next((os.path.join(root, d, n) for n in os.listdir(os.path.join(root, d)) if n.lower() == "cookedpcconsole"), None)


def _read_moddesc(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None, inline_comment_prefixes=(";",))
    cp.optionxform = str.lower
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        cp.read_string(f.read())
    return cp


def _guess_game(root, dlcs):
    for d in dlcs:
        if os.path.isfile(os.path.join(root, d, "AutoLoad.ini")) or os.path.isfile(os.path.join(root, d, "autoload.ini")):
            return "LE1"
    return None


def check(code_dir, opts):
    rep = new_report("masseffect", LABEL)
    root = _find_root(code_dir)
    if not root:
        rep["errors"].append("No Mass Effect DLC mod found: expected a DLC_MOD_<Name> folder (with CookedPCConsole inside) "
                             "and/or a moddesc.ini.")
        return rep
    rep["root"] = root
    dlcs = _dlc_dirs(root)
    md_path = os.path.join(root, "moddesc.ini")
    md = None
    if os.path.isfile(md_path):
        try:
            md = _read_moddesc(md_path)
        except configparser.Error as e:
            rep["errors"].append(f"moddesc.ini can't be read ({str(e).splitlines()[0]}).")
    game = (opts.get("me_game") or "").upper()
    md_game = (md.get("ModInfo", "game", fallback="") if md else "").upper()
    if not game:
        game = md_game or _guess_game(root, dlcs) or ""
    if game not in GAMES:
        rep["errors"].append("Pick which game this is for (LE1, LE2 or LE3).")
    elif md_game and md_game != game:
        rep["warnings"].append(f"moddesc.ini says game = {md_game}, but you picked {game}; the package uses {game}.")
    rep["me"] = {"game": game, "dlcs": dlcs, "has_moddesc": bool(md)}
    if md:      # lets the window fill in the name and author from the existing moddesc.ini
        rep["plugins"] = [{"name": md.get("ModInfo", "modname", fallback=""), "author": md.get("ModInfo", "moddev", fallback=""),
                           "description": md.get("ModInfo", "moddesc", fallback="").replace("<br>", " ")}]
    if game in GAMES:
        rep["label"] = GAMES[game]
    if not dlcs:
        rep["errors"].append("No DLC folder (DLC_MOD_<Name>) next to moddesc.ini.")
    for d in dlcs:
        if not d.upper().startswith("DLC_MOD_"):
            rep["warnings"].append(f"{d}: custom DLC folders are usually named DLC_MOD_<Name> so they never clash with BioWare's.")
        cooked = _cooked(root, d)
        if not cooked:
            rep["errors"].append(f"{d} has no CookedPCConsole folder; the game finds a DLC's files there.")
            continue
        names = {n.lower() for n in os.listdir(cooked)}
        if game in ("LE2", "LE3") and "mount.dlc" not in names:
            rep["errors"].append(f"{d}/CookedPCConsole has no Mount.dlc; {game} won't load the DLC without it "
                                 "(make one with ME3Tweaks Mod Manager's Starter Kit or Legendary Explorer's Mount Editor).")
        if game == "LE1" and not os.path.isfile(os.path.join(root, d, "AutoLoad.ini")):
            rep["errors"].append(f"{d} has no AutoLoad.ini; LE1 DLC mods load through it.")
        if game in ("LE2", "LE3") and "mount.dlc" in names:
            rep["notes"].append(f"{d}: make sure its mount priority is unique (ME3Tweaks keeps a list of known DLC mods).")
        if not any(n.endswith((".pcc", ".tlk", ".ini", ".bin", ".tfc", ".bik", ".afc", ".isb", ".m3m", ".xml"))
                   for n in names):
            rep["warnings"].append(f"{d}/CookedPCConsole has no game files (.pcc, .tlk...) yet.")
    if md:
        sd = md.get("CUSTOMDLC", "sourcedirs", fallback="")
        listed = [x.strip() for x in re.split(r"[;,]", sd) if x.strip()]
        missing = [x for x in listed if x not in dlcs]
        if missing:
            rep["errors"].append(f"moddesc.ini lists {', '.join(missing)} in [CUSTOMDLC] sourcedirs, but there's no such folder.")
        unlisted = [d for d in dlcs if d not in listed]
        if listed and unlisted:
            rep["warnings"].append(f"{', '.join(unlisted)} isn't listed in moddesc.ini, so Mod Manager won't install it.")
        ver = md.get("ModInfo", "modver", fallback="")
        if ver and not re.match(r"^\d+(\.\d+){0,3}$", ver.strip()):
            rep["warnings"].append(f"moddesc.ini modver “{ver}”: Mod Manager expects a number like 1, 1.2 or 1.2.3.")
        rep["steps"].append("Uses your moddesc.ini" + (", with the version you entered." if opts.get("version") else "."))
    else:
        rep["steps"].append("Writes a moddesc.ini so ME3Tweaks Mod Manager can install it.")
    files, left = walk(root, keep=lambda rel: rel.split("/")[0] in dlcs or rel.lower() in ("moddesc.ini",) or
                       rel.lower().endswith((".jpg", ".png")) and "/" not in rel)
    rep["left_out"] = left[:200]
    rep["files"] = len(files)
    rep["bytes"] = sum(os.path.getsize(f) for _r, f in files)
    rep["notes"].append("Install with ME3Tweaks Mod Manager: drag the .zip onto it.")
    return rep


def _modver(v):
    nums = re.findall(r"\d+", v or "")[:4]
    return ".".join(nums) if nums else "1.0"


def prepare(code_dir, opts, rep, stage):
    root, me = rep["root"], rep["me"]
    files, _left = walk(root, keep=lambda rel: rel.split("/")[0] in me["dlcs"] or "/" not in rel and
                        rel.lower().endswith((".jpg", ".png")))
    md_path = os.path.join(root, "moddesc.ini")
    if me["has_moddesc"]:
        with open(md_path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        text = re.sub(r"(?im)^(\s*game\s*=\s*).*$", lambda m: m.group(1) + me["game"], text, count=1)
        if opts.get("version"):
            if re.search(r"(?im)^\s*modver\s*=", text):
                text = re.sub(r"(?im)^(\s*modver\s*=\s*).*$", lambda m: m.group(1) + _modver(opts["version"]), text, count=1)
        moddesc = text.encode("utf-8")
    else:
        desc = (opts.get("description") or "").replace("\r", "").strip().replace("\n", "<br>") or "A DLC mod."
        dirs = ";".join(me["dlcs"])
        lines = ["[ModManager]", f"cmmver = {CMMVER}", "", "[ModInfo]", f"game = {me['game']}",
                 f"modname = {opts.get('name') or me['dlcs'][0].replace('DLC_MOD_', '').replace('_', ' ')}",
                 f"moddev = {opts.get('author') or 'Unknown'}", f"moddesc = {desc}",
                 f"modver = {_modver(opts.get('version') or '1.0')}"]
        if opts.get("url"):
            lines.append(f"modsite = {opts['url']}")
        lines += ["", "[CUSTOMDLC]", f"sourcedirs = {dirs}", f"destdirs = {dirs}", ""]
        moddesc = "\n".join(lines).encode("utf-8")
    return [("moddesc.ini", moddesc)] + files
