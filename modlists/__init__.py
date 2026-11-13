"""Mod lists: look at your mod manager setups the way a modding helper would (PC only).

run(request, game_folders) answers a model's MODS: line with readable text:
  overview                  mod managers, profiles, games, tools and recent crash logs found
  check [setup] [order]     load-order and setup check (missing masters, LE plugins, limits, SKSE,
                            Address Library, SKSE log errors, file conflicts); "order" adds the load order
  mods [setup]              the enabled mods in priority order, with separators
  crash [n | path]          the newest crash log (or the n-th newest, or a path), explained
  tools                     LOOT / xEdit / other tools found, and the exact commands to run them
  nexus <words>             a Nexus Mods search link
[setup] picks an MO2 instance or profile by name ("Skyrim", "Default", "Skyrim/Survival") or "vortex".

Everything here only reads. Running LOOT or xEdit changes files, so those go through the normal
RUN approval (in a project with a code folder) or the user runs them.
"""

import os
import re
import time
import urllib.parse

from . import managers as mg
from .check import DataView, check, report

GAME_EXES = {"SkyrimSE.exe": "Skyrim Special Edition", "TESV.exe": "Skyrim", "SkyrimVR.exe": "Skyrim VR"}
NEXUS_GAME = {"Skyrim Special Edition": "skyrimspecialedition", "Skyrim": "skyrim", "Skyrim VR": "skyrimspecialedition"}


def game_installs(game_folders=()):
    """{game: folder} for Bethesda games among Amalgam's game folders."""
    out = {}
    for f in game_folders or ():
        for exe, game in GAME_EXES.items():
            if f and os.path.isfile(os.path.join(f, exe)) and game not in out:
                out[game] = f
    return out


def _setups(game_folders):
    inst = mg.find_mo2(game_folders)
    vortex = mg.find_vortex()
    games = game_installs(game_folders)
    return inst, vortex, games


def summary(game_folders=()):
    """A quick look for the Apps card (no file walking)."""
    inst, vortex, games = _setups(game_folders)
    tools = mg.find_tools(inst, game_folders)
    crash = []
    for g in set(list(games) + [i["game"] for i in inst] + [v["game"] for v in vortex]) or {"Skyrim Special Edition"}:
        data = [os.path.join(games[g], "Data")] if g in games else []
        crash += [{"path": p, "time": os.path.getmtime(p)} for p in mg.find_crash_logs(g, data, 3)]
    crash.sort(key=lambda c: -c["time"])
    return {"mo2": [{"name": i["name"], "game": i["game"], "profiles": i["profiles"], "selected": i["selected"],
                     "folder": i["base"]} for i in inst],
            "vortex": [{"game": v["game"], "folder": v["mods"]} for v in vortex],
            "games": games, "tools": tools, "crash_logs": crash[:3]}


def roots(game_folders=()):
    """Folders MODS may read from: game folders, mod-manager folders and My Games."""
    inst, vortex, games = _setups(game_folders)
    out = list(game_folders or [])
    for i in inst:
        out += [i["base"], i["mods"], i["profiles_dir"], i["overwrite"], os.path.dirname(i["ini"])]
    out += [v["mods"] for v in vortex]
    for g in set(list(games) + [i["game"] for i in inst]) | {"Skyrim Special Edition"}:
        out += mg.my_games_dirs(g)
        p = mg.plugins_txt_path(g)
        if p:
            out.append(os.path.dirname(p))
    return [o for o in dict.fromkeys(out) if o]


def _pick(arg, inst, vortex, games):
    """Choose the setup a request means. Returns (kind, instance_or_game, profile)."""
    words = arg.strip()
    low = words.lower()
    if "vortex" in low or (not inst and (vortex or games)):
        g = next((v["game"] for v in vortex), None) or next(iter(games), "Skyrim Special Edition")
        return "vortex" if vortex else "game", g, ""
    if not inst:
        return None, None, None
    if not words:
        return "mo2", inst[0], ""
    a, _, b = words.partition("/")
    for i in inst:
        if a.lower() in (i["name"].lower(), i["game"].lower()):
            return "mo2", i, b.strip()
    for i in inst:                            # a profile name on its own
        for p in i["profiles"]:
            if p.lower() == words.lower():
                return "mo2", i, p
    for i in inst:
        if a.lower() in i["name"].lower() or a.lower() in i["game"].lower():
            return "mo2", i, b.strip()
    return "mo2", inst[0], ""


def _skse_log_errors(game):
    out = []
    for mgd in mg.my_games_dirs(game):
        for name in ("skse64.log", "skse.log", "sksevr.log"):
            p = os.path.join(mgd, "SKSE", name)
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if re.search(r"couldn't load|disabled|incompatible|not compatible|failed|error", line, re.I):
                            out.append(line.strip()[:220])
    return list(dict.fromkeys(out))[:25]


def build(kind, target, profile, games):
    """The setup dict check() needs."""
    view = DataView()
    if kind == "mo2":
        inst = target
        game = inst["game"]
        game_path = inst["game_path"] if os.path.isdir(inst["game_path"] or "") else games.get(game, "")
        prof = mg.read_mo2_profile(inst, profile)
        view.add("[Game Data]", os.path.join(game_path, "Data") if game_path else "")
        for m in prof["mods"]:
            if m["enabled"] and not m["separator"] and not m["unmanaged"]:
                view.add(m["name"], os.path.join(inst["mods"], m["name"]))
        view.add("[Overwrite]", inst["overwrite"])
        title = f"Mod Organizer 2 · {inst['name']} · profile {prof['profile']} ({game})"
        return {"game": game, "game_path": game_path, "plugins": prof["plugins"], "view": view,
                "mods": prof["mods"], "title": title, "profile_dir": prof["dir"]}
    game = target
    game_path = games.get(game, "")
    view.add("[Game Data]", os.path.join(game_path, "Data") if game_path else "")
    old = not mg.BETHESDA.get(game, {"se": True})["se"]
    plugins = mg.read_plugins_txt(mg.plugins_txt_path(game), "", old)
    title = f"{'Vortex' if kind == 'vortex' else 'Game folder'} · {game}" + \
            (" (Vortex deploys into the game's Data folder, so that's what was checked)" if kind == "vortex" else "")
    return {"game": game, "game_path": game_path, "plugins": plugins, "view": view, "mods": [], "title": title}


def _overview(game_folders):
    s = summary(game_folders)
    lines = ["Mod setups found on this PC:"]
    for i in s["mo2"]:
        lines.append(f"- Mod Organizer 2 instance '{i['name']}' ({i['game'] or 'game not set'}) at {i['folder']}; "
                     f"profiles: {', '.join(i['profiles']) or 'none'}" + (f"; selected: {i['selected']}" if i['selected'] else ""))
    for v in s["vortex"]:
        lines.append(f"- Vortex ({v['game']}), mods staged in {v['folder']}")
    for g, p in s["games"].items():
        lines.append(f"- {g} installed at {p}")
    if len(lines) == 1:
        lines.append("- none found. MO2 instances are found in %LOCALAPPDATA%\\ModOrganizer, and portable ones in "
                     "folders added under Apps → Game files. Ask the user to add their MO2 folder there.")
    lines.append("Tools: " + (", ".join(f"{k} ({v})" for k, v in s["tools"].items()) or "none found"))
    if s["crash_logs"]:
        lines.append("Newest crash logs:")
        for c in s["crash_logs"]:
            lines.append(f"- {c['path']} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(c['time']))})")
    lines.append("Next: MODS: check <setup> for a full check, MODS: crash to explain the newest crash.")
    return "\n".join(lines)


def _tools(game_folders):
    inst, _v, games = _setups(game_folders)
    tools = mg.find_tools(inst, game_folders)
    lines = ["Modding tools found: " + (", ".join(tools) or "none")]
    for k, v in tools.items():
        lines.append(f"- {k}: {v}")
    loot, xedit = tools.get("LOOT"), tools.get("xEdit")
    lines.append("\nRunning these changes files, so the user must approve (RUN in a project with a code folder) "
                 "or run them themselves:")
    if loot:
        lines.append(f'- Sort the load order with LOOT and quit: & "{loot}" --game="Skyrim Special Edition" --auto-sort')
    if xedit:
        lines.append(f'- Clean one plugin with xEdit (Quick Auto Clean, makes a backup): & "{xedit}" -qac -autoexit '
                     f'-autoload "<Plugin.esp>"  (never clean the game\'s own masters unless the user asks)')
    if inst:
        lines.append("- With Mod Organizer 2, run LOOT and xEdit from inside MO2 (its program list) so they see the "
                     "mods. Run outside MO2 they only see the game's Data folder.")
    if not loot:
        lines.append("- LOOT isn't found: it's free from loot.github.io.")
    if not xedit:
        lines.append("- xEdit (SSEEdit) isn't found: it's free on Nexus Mods and GitHub (TES5Edit/TES5Edit).")
    return "\n".join(lines)


def _mods(kind, target, profile, games):
    if kind != "mo2":
        return ("Vortex keeps its mod list in its own database, which Amalgam doesn't read. The load-order check "
                "(MODS: check vortex) works from the game's Data folder and plugins.txt.")
    prof = mg.read_mo2_profile(target, profile)
    on = [m for m in prof["mods"] if m["enabled"] and not m["separator"]]
    lines = [f"MO2 '{target['name']}', profile {prof['profile']}: {len(on)} mods on, "
             f"{sum(1 for m in prof['mods'] if not m['enabled'] and not m['separator'])} off. Lowest priority first "
             f"(later mods win file conflicts):"]
    for m in prof["mods"]:
        if m["separator"]:
            lines.append(f"── {m['name'][:-10]} ──")
        else:
            lines.append(("  " if m["enabled"] else "  (off) ") + m["name"])
    return "\n".join(lines[:1500])


def run(request, game_folders=(), workspace=""):
    action, _, arg = (request or "overview").strip().partition(" ")
    action = action.lower().strip(":")
    arg = arg.strip()
    try:
        if action in ("", "overview", "list", "setups"):
            return _overview(game_folders)
        if action == "tools":
            return _tools(game_folders)
        if action == "nexus":
            inst, vortex, games = _setups(game_folders)
            g = next(iter(games), None) or next((i["game"] for i in inst), "Skyrim Special Edition")
            slug = NEXUS_GAME.get(g, "skyrimspecialedition")
            return (f"Nexus Mods search for '{arg}': https://www.nexusmods.com/{slug}/search/?gsearch="
                    f"{urllib.parse.quote_plus(arg)}&gsearchtype=mods\nGive the user this link (downloads happen in "
                    "their browser or mod manager; Amalgam never downloads mods).")
        if action == "crash":
            inst, vortex, games = _setups(game_folders)
            from . import crashlog
            allowed = roots(game_folders) + ([workspace] if workspace else [])
            if arg and not arg.isdigit():
                full = os.path.realpath(os.path.expanduser(arg.strip('"')))
                if not any(_inside(full, r) for r in allowed):
                    return "Not allowed: that file isn't in a game, mod-manager or My Games folder."
                return crashlog.report(full)
            logs = []
            for g in set(list(games) + [i["game"] for i in inst]) | {"Skyrim Special Edition"}:
                data = [os.path.join(games[g], "Data")] if g in games else []
                logs += mg.find_crash_logs(g, data, 10)
            logs = sorted(set(logs), key=os.path.getmtime, reverse=True)
            if not logs:
                return ("No crash logs found. Skyrim only writes one with a crash logger installed: suggest "
                        "'Crash Logger SSE AE VR' (an SKSE plugin) from Nexus, then reproduce the crash. Logs go to "
                        "Documents\\My Games\\Skyrim Special Edition\\SKSE.")
            n = int(arg) - 1 if arg.isdigit() else 0
            n = max(0, min(n, len(logs) - 1))
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(logs[n])))
            return f"(crash log {n + 1} of {len(logs)}, written {when})\n" + crashlog.report(logs[n])
        if action in ("check", "order", "mods"):
            show_order = action == "order" or bool(re.search(r"\border\b", arg, re.I))
            arg = re.sub(r"\border\b", "", arg, flags=re.I).strip()
            inst, vortex, games = _setups(game_folders)
            kind, target, profile = _pick(arg, inst, vortex, games)
            if not kind:
                return "No mod manager or Skyrim install found. " + _overview(game_folders)
            if action == "mods":
                return _mods(kind, target, profile, games)
            setup = build(kind, target, profile, games)
            if not setup["game_path"]:
                return (f"{setup['title']}: couldn't find the game folder. Ask the user to add it under Apps → "
                        f"Game files.")
            result = check(setup)
            text = report(result, setup["title"], show_order=show_order)
            errs = _skse_log_errors(setup["game"])
            if errs:
                text += "\n\nSKSE's own log reports:\n- " + "\n- ".join(errs)
            return text
        return f"Unknown MODS action '{action}'. Use overview, check, order, mods, crash, tools or nexus."
    except Exception as e:
        return f"The mod check hit a problem: {e}"


def _inside(path, root):
    try:
        r = os.path.realpath(root)
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(r)]) == os.path.normcase(r)
    except ValueError:
        return False
