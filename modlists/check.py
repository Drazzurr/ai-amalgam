"""Check a Bethesda load order and mod setup for the problems that most often stop a game
from starting or make it crash: missing masters, masters in the wrong place, Skyrim LE plugins
in Special Edition, plugin-count limits, SKSE / Address Library mismatches and file conflicts.

Read-only. Reads plugin headers only (a few KB per plugin), never whole plugins.
"""

import glob
import mmap
import os
import re
import struct

from .managers import BETHESDA, canon_game

FULL_LIMIT = 254       # 0x00-0xFD; 0xFE holds the light plugins
LIGHT_LIMIT = 4096
MAX_FILES = 400_000    # stop walking mod folders after this many files


# ---------------- plugin headers ----------------

def read_header(path):
    """Masters, flags and versions from a plugin's TES4 header record."""
    from gamefiles.tes import Plugin
    with open(path, "rb") as f:
        head = f.read(24)
        if len(head) < 24 or head[:4] != b"TES4":
            raise ValueError("not a plugin (no TES4 header)")
        size = struct.unpack_from("<I", head, 4)[0]
        body = f.read(min(size, 4 * 1024 * 1024))
    p = Plugin(head + body)
    ext = os.path.splitext(path)[1].lower()
    return {"masters": p.masters, "version": round(p.version, 2), "form": p.header["version"],
            "esm": bool(p.flags & 1) or ext in (".esm", ".esl"), "light": bool(p.flags & 0x200) or ext == ".esl",
            "records": p.num_records, "author": p.author}


# ---------------- game version (no extra libraries) ----------------

def exe_version(path):
    """File version of a Windows .exe, read from its VS_FIXEDFILEINFO block."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            i = m.rfind(b"\xbd\x04\xef\xfe")
            if i < 0:
                return ""
            ms, ls = struct.unpack_from("<II", m, i + 8)
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, ValueError):
        return ""


# ---------------- the data folder as the game sees it ----------------

class DataView:
    """Data-folder files from several sources, later sources winning (MO2's priority order)."""

    def __init__(self):
        self.sources = []          # (label, folder), lowest priority first
        self.files = {}            # lower relative path -> [labels], lowest priority first
        self.real = {}             # lower relative path -> real path of the winning copy
        self.truncated = False
        self.count = 0

    def add(self, label, folder, top_only=False):
        if not folder or not os.path.isdir(folder):
            return
        self.sources.append((label, folder))
        for dirpath, dirs, names in os.walk(folder):
            rel_dir = os.path.relpath(dirpath, folder)
            if top_only:
                dirs[:] = [d for d in dirs if d.lower() in ("skse", "netscriptframework")] if rel_dir == "." else dirs
            for n in names:
                if rel_dir == "." and n.lower() in ("meta.ini",):
                    continue
                rel = n if rel_dir == "." else os.path.join(rel_dir, n)
                key = rel.replace("\\", "/").lower()
                self.files.setdefault(key, []).append(label)
                self.real[key] = os.path.join(dirpath, n)
                self.count += 1
                if self.count >= MAX_FILES:
                    self.truncated = True
                    return

    def plugin(self, name):
        return self.real.get(name.lower())

    def glob_top(self, pattern):
        rx = re.compile(pattern, re.I)
        return [k for k in self.files if rx.fullmatch(k)]


# ---------------- the checks ----------------

def check(setup):
    """setup: {game, game_path, plugins: [{name, active}], view: DataView, manager, profile, mods}.
    Returns {"problems": [(level, text)], "facts": [text], "order": [...]}."""
    game = canon_game(setup.get("game") or "Skyrim Special Edition")
    info = BETHESDA.get(game, BETHESDA["Skyrim Special Edition"])
    view = setup["view"]
    game_path = setup.get("game_path") or ""
    problems, facts = [], []

    # load order: the game's own plugins first, then Creation Club, then plugins.txt
    listed = setup.get("plugins") or []
    names_lower = {p["name"].lower() for p in listed}
    implicit = [b for b in info["base"] if view.plugin(b)]
    ccc = os.path.join(game_path, info["ccc"]) if info.get("ccc") and game_path else ""
    if ccc and os.path.isfile(ccc):
        with open(ccc, encoding="utf-8", errors="replace") as f:
            implicit += [l.strip() for l in f if l.strip() and view.plugin(l.strip())]
    order = [{"name": n, "active": True, "implicit": True} for n in implicit if n.lower() not in names_lower]
    seen = set()
    for p in listed:
        if p["name"].lower() in seen:
            problems.append(("warn", f"{p['name']} is listed twice in plugins.txt; only the first counts."))
            continue
        seen.add(p["name"].lower())
        order.append(dict(p, implicit=False))
    active = [p for p in order if p["active"]]
    pos = {p["name"].lower(): i for i, p in enumerate(active)}

    headers, unreadable = {}, []
    for p in active:
        path = view.plugin(p["name"])
        if not path:
            problems.append(("error", f"{p['name']} is switched on but its file isn't in the data folder or any "
                                      f"enabled mod. Reinstall the mod or switch the plugin off."))
            continue
        try:
            headers[p["name"].lower()] = h = read_header(path)
            p["header"] = h
        except Exception as e:
            unreadable.append(p["name"])
            problems.append(("error", f"{p['name']} can't be read as a plugin ({e}). It may be damaged."))

    full = light = 0
    for p in active:
        h = headers.get(p["name"].lower())
        if not h:
            continue
        if h["light"]:
            light += 1
        else:
            full += 1
        i = pos[p["name"].lower()]
        for m in h["masters"]:
            j = pos.get(m.lower())
            if j is None:
                where = "is installed but switched off" if view.plugin(m) else "isn't installed"
                problems.append(("error", f"{p['name']} needs {m}, which {where}. The game won't start (or will "
                                          f"crash on load) until {m} is active, or {p['name']} is switched off."))
            elif j > i:
                problems.append(("error", f"{p['name']} loads before its master {m}. Move {m} above it (LOOT does "
                                          f"this when you sort)."))
        if info["se"] and (0 < h["version"] < 1.0 or 0 < h["form"] < 44) and not p.get("implicit"):
            problems.append(("warn", f"{p['name']} was made for Skyrim LE (header {h['version']}, form version "
                                     f"{h['form']}). Use the mod's Special Edition version, or open it in the SE "
                                     f"Creation Kit and save it once. LE plugins can crash SE, especially with "
                                     f"new records or meshes."))
        if h["version"] >= 1.71 and not p.get("implicit"):
            facts.append(f"{p['name']} uses the newer 1.71 plugin header, which needs Skyrim 1.6.1130 or newer.")
        if h["esm"] is False and h["light"] is False:
            later_masters = [q["name"] for q in active[i + 1:] if headers.get(q["name"].lower(), {}).get("esm")
                             and not headers.get(q["name"].lower(), {}).get("light")]
            if later_masters and len(later_masters) <= 3:
                problems.append(("info", f"{p['name']} (a regular plugin) sits above master file(s) "
                                         f"{', '.join(later_masters)}. The game loads masters first anyway; sort "
                                         f"with LOOT so the list matches what really happens."))
    if full > FULL_LIMIT:
        problems.append(("error", f"{full} full plugins are active; the limit is {FULL_LIMIT}. Merge some, ESL-flag "
                                  f"small ones (xEdit can check which are safe), or switch some off."))
    elif full > FULL_LIMIT - 10:
        problems.append(("warn", f"{full} full plugins are active, close to the {FULL_LIMIT} limit."))
    if light > LIGHT_LIMIT:
        problems.append(("error", f"{light} light (ESL) plugins are active; the limit is {LIGHT_LIMIT}."))
    facts.append(f"{len(active)} active plugins ({full} full, {light} light); {len(order) - len(active)} switched off.")

    # SKSE, Address Library, game version
    dlls = view.glob_top(r"skse/plugins/[^/]+\.dll")
    exe_name = {"Skyrim Special Edition": "SkyrimSE.exe", "Skyrim": "TESV.exe", "Skyrim VR": "SkyrimVR.exe"}[game]
    loader = {"Skyrim Special Edition": "skse64_loader.exe", "Skyrim": "skse_loader.exe",
              "Skyrim VR": "sksevr_loader.exe"}[game]
    gver = exe_version(os.path.join(game_path, exe_name)) if game_path else ""
    if gver:
        facts.append(f"Game version: {exe_name} {gver}.")
    if dlls:
        facts.append(f"{len(dlls)} SKSE plugin DLLs installed.")
        if game_path and not os.path.isfile(os.path.join(game_path, loader)):
            problems.append(("error", f"{len(dlls)} mods need SKSE, but {loader} isn't in the game folder. Install "
                                      f"SKSE for your game version (skse.silverlock.org) and start the game through "
                                      f"{loader} (in MO2/Vortex, use the SKSE entry)."))
        elif game_path and gver:
            short = "_".join(gver.split(".")[:3])
            sk = glob.glob(os.path.join(game_path, "skse64_*.dll"))
            sk = [s for s in sk if re.search(r"skse64_\d+_\d+_\d+\.dll$", s, re.I)]
            if sk and not any(short in os.path.basename(s) for s in sk):
                problems.append(("error", f"SKSE in the game folder ({', '.join(os.path.basename(s) for s in sk)}) "
                                          f"doesn't match the game ({gver}). Install the SKSE build for {gver}, or "
                                          f"downgrade the game. This alone stops every SKSE mod from loading."))
        if game == "Skyrim Special Edition":
            libs = view.glob_top(r"skse/plugins/version(lib)?-[\d-]+\.bin")
            if not libs:
                problems.append(("warn", "SKSE plugins are installed but Address Library for SKSE Plugins isn't. "
                                         "Most SKSE mods need it; get the 'All in one' file for your game version."))
            elif gver:
                want = gver.replace(".", "-")
                if not any(want in l for l in libs):
                    problems.append(("warn", f"Address Library files ({', '.join(os.path.basename(l) for l in libs)}) "
                                             f"don't include your game version {gver}. Update Address Library."))

    # loose-file conflicts between mods (MO2)
    conflicts = {}
    for key, labels in view.files.items():
        if len(labels) > 1:
            winner = labels[-1]
            for loser in set(labels[:-1]):
                if loser != winner:
                    conflicts.setdefault((loser, winner), []).append(key)
    dll_fights = [k for k, l in view.files.items() if k.endswith(".dll") and len(set(l)) > 1]
    for k in dll_fights[:10]:
        problems.append(("warn", f"{k} comes from more than one mod ({', '.join(dict.fromkeys(view.files[k]))}); "
                                 f"the last one wins. Two versions of one SKSE plugin often crash at start-up."))
    if view.truncated:
        facts.append(f"Stopped looking at files after {MAX_FILES:,}; conflict counts are partial.")
    return {"problems": problems, "facts": facts, "order": order, "conflicts": conflicts, "game": game,
            "game_version": gver}


def report(result, title, conflicts_limit=15, show_order=False):
    lines = [title]
    lines += ["  " + f for f in result["facts"]]
    order = {"error": 0, "warn": 1, "info": 2}
    probs = sorted(result["problems"], key=lambda p: order[p[0]])
    label = {"error": "PROBLEM", "warn": "WARNING", "info": "NOTE"}
    if probs:
        lines.append(f"\n{sum(1 for p in probs if p[0] == 'error')} problems, "
                     f"{sum(1 for p in probs if p[0] == 'warn')} warnings:")
        lines += [f"- {label[l]}: {t}" for l, t in probs[:80]]
        if len(probs) > 80:
            lines.append(f"- ... and {len(probs) - 80} more")
    else:
        lines.append("\nNo load-order problems found.")
    conf = sorted(result["conflicts"].items(), key=lambda kv: -len(kv[1]))
    real = [c for c in conf if not c[0][0].startswith("[")]
    if real:
        lines.append(f"\nFile conflicts between mods (the second mod wins; {len(real)} pairs, biggest first):")
        for (loser, winner), files in real[:conflicts_limit]:
            sample = ", ".join(files[:3])
            lines.append(f"- {winner} overwrites {len(files)} file(s) from {loser} (e.g. {sample})")
        lines.append("Conflicts are normal when a patch or a newer mod is meant to win. Check the ones that look "
                     "backwards (e.g. an old body mesh winning over a newer body).")
    if show_order:
        lines.append("\nLoad order (active plugins):")
        for i, p in enumerate([p for p in result["order"] if p["active"]]):
            h = p.get("header") or {}
            tag = " [ESL]" if h.get("light") else " [ESM]" if h.get("esm") else ""
            lines.append(f"{i:3d} {p['name']}{tag}")
    return "\n".join(lines)
