"""Find your mod managers, their mod lists and the modding tools on this PC.

Read-only: nothing here changes a file. Covers Mod Organizer 2 (installed and portable instances),
Vortex, and the usual tools (LOOT, xEdit, Wrye Bash, BodySlide, Nemesis, Pandora, Synthesis).
"""

import glob
import os
import re

# MO2's game names, Vortex's game ids and the folder under Documents\My Games for each Bethesda game.
BETHESDA = {
    "Skyrim Special Edition": {"vortex": "skyrimse", "mygames": ["Skyrim Special Edition", "Skyrim Special Edition GOG",
                                                                "Skyrim Special Edition EPIC"],
                               "appdata": "Skyrim Special Edition", "se": True,
                               "base": ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm",
                                        "Dragonborn.esm"], "ccc": "Skyrim.ccc"},
    "Skyrim": {"vortex": "skyrim", "mygames": ["Skyrim"], "appdata": "Skyrim", "se": False,
               "base": ["Skyrim.esm", "Update.esm"], "ccc": None},
    "Skyrim VR": {"vortex": "skyrimvr", "mygames": ["Skyrim VR"], "appdata": "Skyrim VR", "se": True,
                  "base": ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
                           "SkyrimVR.esm"], "ccc": None},
}
ALIASES = {"skyrimse": "Skyrim Special Edition", "skyrim special edition": "Skyrim Special Edition",
           "skyrim anniversary edition": "Skyrim Special Edition", "skyrimspecialedition": "Skyrim Special Edition",
           "skyrim": "Skyrim", "skyrimvr": "Skyrim VR", "skyrim vr": "Skyrim VR"}

TOOL_EXES = {
    "LOOT": ["LOOT.exe"],
    "xEdit": ["SSEEdit.exe", "SSEEdit64.exe", "TES5Edit.exe", "xEdit.exe", "xEdit64.exe", "TES5VREdit.exe"],
    "Wrye Bash": ["Wrye Bash.exe"],
    "BodySlide": ["BodySlide x64.exe", "BodySlide.exe"],
    "Nemesis": ["Nemesis Unlimited Behavior Engine.exe"],
    "Pandora": ["Pandora Behaviour Engine+.exe", "Pandora Behaviour Engine.exe"],
    "Synthesis": ["Synthesis.exe"],
    "Mod Organizer 2": ["ModOrganizer.exe"],
    "Vortex": ["Vortex.exe"],
}


def canon_game(name):
    n = (name or "").strip()
    return ALIASES.get(n.lower().replace("_", " "), ALIASES.get(n.lower().replace(" ", ""), n))


def _env(name, default=""):
    return os.environ.get(name) or default


def documents_dirs():
    home = os.path.expanduser("~")
    out = []
    for d in (os.path.join(_env("USERPROFILE", home), "Documents"), os.path.join(_env("OneDrive"), "Documents")
              if _env("OneDrive") else "", os.path.join(home, "Documents")):
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def my_games_dirs(game):
    info = BETHESDA.get(canon_game(game))
    out = []
    for docs in documents_dirs():
        for n in (info["mygames"] if info else [game]):
            p = os.path.join(docs, "My Games", n)
            if os.path.isdir(p):
                out.append(p)
    return out


# ---------------- Mod Organizer 2 ----------------

def _ini(path):
    """A tiny INI reader that copes with MO2's @ByteArray(...) values and duplicate keys."""
    data, section = {}, ""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line[0] in ";#":
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip().lower()
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    v = v.strip()
                    m = re.match(r"^@ByteArray\((.*)\)$", v)
                    if m:
                        v = m.group(1)
                    if len(v) >= 2 and v[0] == v[-1] == '"':
                        v = v[1:-1]
                    data.setdefault(section, {})[k.strip().lower()] = v.replace("\\\\", "\\")
    except OSError:
        pass
    return data


def _mo2_path(value, base):
    if not value:
        return ""
    value = value.replace("%BASE_DIR%", base).replace("/", os.sep)
    return value if os.path.isabs(value) else os.path.join(base, value)


def read_mo2(ini_path):
    """One MO2 instance: game, folders, profiles and the programs added to it."""
    ini = _ini(ini_path)
    folder = os.path.dirname(ini_path)
    gen, sett = ini.get("general", {}), ini.get("settings", {})
    base = _mo2_path(sett.get("base_directory", ""), folder) or folder
    inst = {
        "manager": "Mod Organizer 2",
        "name": os.path.basename(folder),
        "ini": ini_path,
        "portable": os.path.isfile(os.path.join(folder, "portable.txt")) or os.path.isfile(
            os.path.join(folder, "ModOrganizer.exe")),
        "game": canon_game(gen.get("gamename", "")),
        "game_path": (gen.get("gamepath", "") or "").replace("/", os.sep),
        "base": base,
        "mods": _mo2_path(sett.get("mod_directory", ""), base) or os.path.join(base, "mods"),
        "profiles_dir": _mo2_path(sett.get("profiles_directory", ""), base) or os.path.join(base, "profiles"),
        "overwrite": _mo2_path(sett.get("overwrite_directory", ""), base) or os.path.join(base, "overwrite"),
        "downloads": _mo2_path(sett.get("download_directory", ""), base) or os.path.join(base, "downloads"),
        "selected": gen.get("selected_profile", ""),
        "tools": {},
    }
    try:
        inst["profiles"] = sorted(d for d in os.listdir(inst["profiles_dir"])
                                  if os.path.isfile(os.path.join(inst["profiles_dir"], d, "modlist.txt")))
    except OSError:
        inst["profiles"] = []
    exe = ini.get("customexecutables", {})
    for k, v in exe.items():
        m = re.match(r"^(\d+)\\title$", k)
        if m and exe.get(f"{m.group(1)}\\binary"):
            inst["tools"][v] = exe[f"{m.group(1)}\\binary"].replace("/", os.sep)
    return inst


def find_mo2(extra_folders=()):
    """MO2 instances: the global ones under %LOCALAPPDATA%\\ModOrganizer and portable ones in
    the folders you gave Amalgam (and one level inside them)."""
    inis = set()
    local = _env("LOCALAPPDATA")
    if local:
        inis.update(glob.glob(os.path.join(local, "ModOrganizer", "*", "ModOrganizer.ini")))
    for f in extra_folders:
        if not f or not os.path.isdir(f):
            continue
        inis.update(glob.glob(os.path.join(f, "ModOrganizer.ini")))
        inis.update(glob.glob(os.path.join(f, "*", "ModOrganizer.ini")))
    out = []
    for p in sorted(inis):
        try:
            out.append(read_mo2(os.path.realpath(p)))
        except Exception:
            continue
    seen, uniq = set(), []
    for i in out:
        if i["ini"].lower() not in seen:
            seen.add(i["ini"].lower())
            uniq.append(i)
    return uniq


def read_mo2_profile(inst, profile=""):
    """The mods (enabled, in MO2 priority order: lowest first) and plugins of one profile."""
    name = profile or inst.get("selected") or (inst["profiles"][0] if inst["profiles"] else "Default")
    match = next((p for p in inst["profiles"] if p.lower() == name.lower()), None)
    pdir = os.path.join(inst["profiles_dir"], match or name)
    mods = []
    try:
        with open(os.path.join(pdir, "modlist.txt"), encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                flag, mod = line[0], line[1:]
                if flag in "+-*":
                    mods.append({"name": mod, "enabled": flag in "+*", "unmanaged": flag == "*",
                                 "separator": mod.endswith("_separator")})
    except OSError:
        pass
    mods.reverse()       # modlist.txt lists the winner first; keep lowest priority first
    old = not BETHESDA.get(inst.get("game"), {"se": True})["se"]
    plugins = read_plugins_txt(os.path.join(pdir, "plugins.txt"), os.path.join(pdir, "loadorder.txt"), old)
    return {"profile": match or name, "dir": pdir, "mods": mods, "plugins": plugins}


# ---------------- Vortex ----------------

def find_vortex():
    """Vortex staging folders found in the default places, by game."""
    out = []
    appdata = _env("APPDATA")
    for game, info in BETHESDA.items():
        stage = os.path.join(appdata, "Vortex", info["vortex"], "mods") if appdata else ""
        if stage and os.path.isdir(stage):
            out.append({"manager": "Vortex", "game": game, "mods": stage,
                        "plugins_txt": plugins_txt_path(game)})
    return out


def plugins_txt_path(game):
    info = BETHESDA.get(canon_game(game))
    local = _env("LOCALAPPDATA")
    if not info or not local:
        return ""
    return os.path.join(local, info["appdata"], "plugins.txt")


def read_plugins_txt(plugins_txt, loadorder_txt="", old_format=False):
    """[{name, active}] in load order. plugins.txt marks active plugins with '*' (Skyrim LE's
    old format, old_format=True, lists only active ones, without '*'); MO2's loadorder.txt (when
    present) also lists inactive ones in the right place."""
    active, order = {}, []
    try:
        with open(plugins_txt, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                on = line.startswith("*")
                name = line.lstrip("*").strip()
                active[name.lower()] = on
                order.append(name)
    except OSError:
        pass
    if old_format and not any(active.values()):     # Skyrim LE: every listed plugin is active
        active = {k: True for k in active}
    if loadorder_txt and os.path.isfile(loadorder_txt):
        full = []
        with open(loadorder_txt, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    full.append(line)
        if full:
            known = {n.lower() for n in full}
            order = full + [n for n in order if n.lower() not in known]
    return [{"name": n, "active": active.get(n.lower(), False)} for n in order]


# ---------------- tools ----------------

def find_tools(instances=(), extra_folders=()):
    """{tool: path} for modding tools added to MO2 or sitting in common folders."""
    found = {}
    for inst in instances:
        for title, path in inst.get("tools", {}).items():
            base = os.path.basename(path).lower()
            for tool, names in TOOL_EXES.items():
                if base in (n.lower() for n in names) and tool not in found and os.path.isfile(path):
                    found[tool] = path
    places = list(extra_folders)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if _env(var):
            places.append(_env(var))
            places.append(os.path.join(_env(var), "Programs"))
    for drive in "CDE":
        places += [f"{drive}:\\Modding", f"{drive}:\\Modding Tools", f"{drive}:\\Tools", f"{drive}:\\Games\\Tools"]
    for base in places:
        if not base or not os.path.isdir(base):
            continue
        for tool, names in TOOL_EXES.items():
            if tool in found:
                continue
            for n in names:
                hits = glob.glob(os.path.join(base, n)) + glob.glob(os.path.join(base, "*", n))
                if hits:
                    found[tool] = hits[0]
                    break
    return found


def find_crash_logs(game, data_dirs=(), limit=8):
    """Newest crash logs first: Crash Logger / Trainwreck (My Games\\...\\SKSE) and .NET Script
    Framework (Data\\NetScriptFramework\\Crash)."""
    logs = []
    for mg in my_games_dirs(game):
        for pat in ("SKSE/crash-*.log", "SKSE/Crashlogs/*.log", "SKSE/*crash*.log", "SKSE/Trainwreck/*.log"):
            logs += glob.glob(os.path.join(mg, *pat.split("/")))
    for d in data_dirs:
        logs += glob.glob(os.path.join(d, "NetScriptFramework", "Crash", "Crash_*.txt"))
    uniq = list({os.path.normcase(os.path.realpath(p)): p for p in logs}.values())
    uniq.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return uniq[:limit]
