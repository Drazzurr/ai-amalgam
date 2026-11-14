"""What the Mod Forge needs to know about each game: how its mods are laid out, what to check once a pack is
built (missing requirements, conflicts, frameworks), the order things load in, how the user installs the
pack, and what the models should keep in mind while planning.

Every profile works on a "mod" record built during the build:
    {"name", "files": [paths inside the game folder/data folder, forward slashes], "read": fn(path) -> bytes}
"""

import json
import re
import xml.etree.ElementTree as ET

def _plugin_info(data):
    """(masters, master-flagged, light, form version) from the start of a Bethesda plugin."""
    try:
        from gamefiles.tes import Plugin
        p = Plugin(data)
        return list(p.masters), bool(p.flags & 0x1), bool(p.flags & 0x200), p.header.get("version", 0)
    except Exception:
        return [], False, False, 0


class Game:
    id = ""
    label = ""
    tools = set()          # exe names that may run without asking in an approved pack
    data_like = set()      # top-level folders that show an archive is laid out from the game/data folder
    root_files = ()        # file types that sit at that top level
    install_with = "Vortex"
    rules = ""
    after_install = ""     # what "I've installed it" asks the models to do
    after_install_hint = ""
    workshop = ""          # how an original mod's folder is laid out

    def data_root(self, names):
        """The folder inside an archive that maps to where the game expects the files ('' = the top)."""
        tops = {n.split("/")[0] for n in names}
        if any("/" not in n and n.lower().endswith(self.root_files) for n in names) or \
                {t.lower() for t in tops} & self.data_like:
            return ""
        if len(tops) == 1:
            t = next(iter(tops))
            inner = [n[len(t) + 1:] for n in names if n.startswith(t + "/")]
            if inner:
                sub = self.data_root(inner)
                return t + ("/" + sub if sub else "")
        for n in names:
            if n.lower().startswith("data/"):
                return n.split("/")[0]
        return ""

    def inspect(self, mod):
        """Fill in what this mod provides and needs. Returns a dict kept with the mod."""
        return {}

    def check(self, mods):
        """mods: [(name, info)] in install order. Returns (load order lines, problems, notes)."""
        return [], [], []

    def load_order_file(self, order):
        return ""

    def install_steps(self):
        return f"Install the numbered files in order with {self.install_with}. Installer choices are already applied, " \
               "so they install without questions. Later files overwrite earlier ones."


# ---------------- Skyrim ----------------

class Skyrim(Game):
    id = "Skyrim Special Edition"
    label = "Skyrim Special Edition / Anniversary Edition"
    tools = {"loot.exe", "sseedit.exe", "sseedit64.exe", "xedit.exe", "xedit64.exe", "tes5edit.exe"}
    data_like = {"meshes", "textures", "scripts", "interface", "sound", "skse", "seq", "music", "strings", "materials",
                 "lodsettings", "grass", "shadersfx", "calientetools", "netscriptframework", "video"}
    root_files = (".esp", ".esm", ".esl", ".bsa")
    base = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
    install_with = "Vortex or Mod Organizer 2"
    rules = ("only Skyrim SE/AE versions (never LE/Oldrim files); SKSE plugins need the user's game version (ask which "
             "if it matters: 1.5.97, or the latest AE); add patches for mods that conflict; don't add two mods that do "
             "the same job.")
    after_install = ("I've installed this modpack in my mod manager. Please check the setup (MODS: check), sort the load "
                     "order with LOOT, clean any plugins that need it with xEdit, and tell me if anything else needs fixing.")
    after_install_hint = "then let the models check, sort (LOOT) and clean (xEdit)."
    workshop = ("laid out like Skyrim's Data folder (plugins at the top, meshes/, textures/, scripts/ and so on)")

    def inspect(self, mod):
        plugins = {}
        for f in mod["files"]:
            if "/" not in f and f.lower().endswith((".esp", ".esm", ".esl")):
                try:
                    plugins[f] = _plugin_info(mod["read"](f, 262144))
                except Exception:
                    plugins[f] = ([], False, False, 0)
        return {"plugins": plugins}

    def check(self, mods):
        info, owner = {}, {}
        for name, i in mods:
            for p, v in (i.get("plugins") or {}).items():
                info[p] = v
                owner[p.lower()] = name
        have = {b.lower() for b in self.base} | {p.lower() for p in info}
        problems = []
        for p, (masters, _m, _l, form) in info.items():
            for m in masters:
                if m.lower() not in have:
                    hint = " (Creation Club / Anniversary content)" if m.lower().startswith("cc") else ""
                    problems.append(f"{p} ({owner[p.lower()]}) needs {m}{hint}, which isn't in the pack or the base game.")
            if form and form < 44 and p.lower().endswith((".esp", ".esm")):
                problems.append(f"{p} ({owner[p.lower()]}) is a Skyrim LE (Oldrim) plugin; it needs converting for SE/AE.")
        names = list(info)
        rank = {p: i for i, p in enumerate(names)}
        heavy = lambda p: 0 if (p.lower().endswith(".esm") or info[p][1]) else 1
        done, order, visiting = set(), [], set()

        def visit(p):
            if p.lower() in done or p.lower() in visiting:
                return
            visiting.add(p.lower())
            for m in info[p][0]:
                real = next((q for q in names if q.lower() == m.lower()), None)
                if real:
                    visit(real)
            visiting.discard(p.lower())
            done.add(p.lower())
            order.append(p)
        for p in sorted(names, key=lambda p: (heavy(p), rank[p])):
            visit(p)
        full = [p for p in order if not info[p][2] and not p.lower().endswith(".esl")]
        if len(full) + 5 > 254:
            problems.append(f"{len(full)} full plugins: over Skyrim's limit of 254 (with the base game). Make some light "
                            "(ESL) or merge them.")
        return order, problems, []

    def load_order_file(self, order):
        return ("# Load order for this modpack (plugins.txt format). Vortex and LOOT sort automatically;\n"
                "# in Mod Organizer 2 you can match this order in the right-hand Plugins list.\n"
                + "".join(f"*{p}\n" for p in order))


# ---------------- Baldur's Gate 3 ----------------

BG3_BASE = {"gustav", "gustavdev", "gustavx", "shared", "shareddev", "engine", "game", "honour", "honourx", "mainui",
            "modbrowser", "crossplayui", "photomode"}


def _lsx_meta(text):
    """(info, deps) from a meta.lsx: info = {uuid, name, folder}; deps = [{uuid, name}]"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, []

    def attrs(node):
        return {a.get("id"): a.get("value", "") for a in node.findall("attribute")}
    info, deps = None, []
    for node in root.iter("node"):
        if node.get("id") == "ModuleInfo":
            a = attrs(node)
            info = {"uuid": a.get("UUID", "").lower(), "name": a.get("Name", ""), "folder": a.get("Folder", "")}
        elif node.get("id") == "Dependencies":
            for sd in node.iter("node"):
                if sd.get("id") == "ModuleShortDesc":
                    a = attrs(sd)
                    deps.append({"uuid": a.get("UUID", "").lower(), "name": a.get("Name") or a.get("Folder", "")})
    return info, deps


class BG3(Game):
    id = "Baldur's Gate 3"
    label = "Baldur's Gate 3"
    data_like = {"mods", "public", "localization", "bin", "data"}
    root_files = (".pak", "info.json")
    install_with = "BG3 Mod Manager (or the in-game mod manager / Vortex)"
    rules = ("only mods for the current game patch (Patch 8 or newer: check the page's \"updated\" date and comments); "
             "add every requirement listed on the page (many need the Script Extender, BG3SE, which isn't on Nexus: "
             "the user installs it with BG3 Mod Manager's Tools menu); order frameworks and libraries (Mod "
             "Configuration Menu, ImpUI / ImprovedUI, Community Library) before the mods that use them; avoid two mods "
             "that edit the same class, race or item list unless one is a compatibility patch.")
    after_install = ("I've installed this modpack in BG3 Mod Manager. Please go over the load order with me (frameworks "
                     "and libraries first, then the mods that need them, then patches), check that every mod's "
                     "requirements are active, and tell me what to look for in the game's Script Extender console if "
                     "something doesn't load.")
    after_install_hint = "then let the models go over the load order with you."
    workshop = ("laid out like a BG3 mod workspace: Mods/<Folder>/meta.lsx, Public/<Folder>/..., Localization/...; "
                "the Package mod button turns it into a .pak with info.json")

    def install_steps(self):
        return ("Open BG3 Mod Manager, drag the numbered files in (they hold .pak files and info.json), move the mods to "
                "the Active list in the order of Load order.txt, then Save Order and Export Order to Game. If a mod "
                "needs the Script Extender, install it from BG3 Mod Manager's Tools menu first.")

    def inspect(self, mod):
        mods, deps, needs_se = [], [], False
        for f in mod["files"]:
            low = f.lower()
            if "scriptextender/" in low or low.endswith(".lua"):
                needs_se = True
            if low.endswith(".pak"):
                try:
                    data = mod["read"](f, 512 << 20)
                    from gamefiles.bg3 import Pak
                    pak = Pak(f, data)
                    for e in pak.files:
                        p = e["path"].replace("\\", "/").lower()
                        if p.endswith("/meta.lsx") and p.startswith("mods/"):
                            info, d = _lsx_meta(pak.extract(e).decode("utf-8", "replace"))
                            if info:
                                mods.append(info)
                                deps += d
                        if "scriptextender/" in p:
                            needs_se = True
                except Exception:
                    pass
            if low.rsplit("/", 1)[-1] == "info.json" and not mods:
                try:
                    j = json.loads(mod["read"](f, 1 << 20).decode("utf-8-sig", "replace"))
                    for m in j.get("Mods") or []:
                        mods.append({"uuid": str(m.get("UUID", "")).lower(), "name": m.get("Name", ""), "folder": m.get("Folder", "")})
                        for d in m.get("Dependencies") or []:
                            if isinstance(d, dict):
                                deps.append({"uuid": str(d.get("UUID", "")).lower(), "name": d.get("Name", "")})
                except Exception:
                    pass
        deps = [d for d in deps if d["uuid"] and (d["name"] or "").lower() not in BG3_BASE
                and not (d["name"] or "").lower().startswith(("gustav", "shared", "honour"))]
        return {"modules": mods, "deps": deps, "script_extender": needs_se,
                "loose": [f for f in mod["files"] if not f.lower().endswith((".pak", ".json", ".txt", ".md", ".png", ".jpg"))][:5]}

    def check(self, mods):
        provided = {}
        for name, i in mods:
            for m in i.get("modules") or []:
                if m["uuid"]:
                    provided[m["uuid"]] = (name, m)
        problems, notes = [], []
        for name, i in mods:
            for d in i.get("deps") or []:
                if d["uuid"] not in provided:
                    problems.append(f"{name} needs {d['name'] or d['uuid']}, which isn't in the pack.")
            if not (i.get("modules")):
                notes.append(f"{name}: no BG3 module found inside (no .pak with meta.lsx); it may be a loose-file or "
                             "tool mod that's installed by hand. Check its page.")
        if any(i.get("script_extender") for _n, i in mods):
            users = [n for n, i in mods if i.get("script_extender")]
            notes.append("These need the Script Extender (BG3SE): " + ", ".join(users[:8]) +
                         ". Install it from BG3 Mod Manager → Tools → Download & Extract the Script Extender.")
        # order: dependencies before the mods that use them, otherwise plan order
        order, seen = [], set()
        by_uuid = {u: v for u, v in provided.items()}

        def visit(name, i, depth=0):
            if name in seen or depth > 40:
                return
            seen.add(name)
            for d in i.get("deps") or []:
                if d["uuid"] in by_uuid:
                    dn = by_uuid[d["uuid"]][0]
                    visit(dn, next(ii for nn, ii in mods if nn == dn), depth + 1)
            order.append(name)
        for name, i in mods:
            visit(name, i)
        return order, problems, notes

    def load_order_file(self, order):
        return ("# Load order for BG3 Mod Manager (top loads first). Put the mods in the Active list in this order,\n"
                "# then Save Order and Export Order to Game.\n" + "".join(f"{n}\n" for n in order))


# ---------------- Cyberpunk 2077 ----------------

CP_FRAMEWORKS = {
    "red4ext": ("RED4ext", ("red4ext/red4ext.dll",)),
    "cet": ("Cyber Engine Tweaks", ("bin/x64/plugins/cyber_engine_tweaks.asi",)),
    "redscript": ("redscript", ("engine/tools/scc.exe", "engine/tools/scc_lib.dll")),
    "archivexl": ("ArchiveXL", ("red4ext/plugins/archivexl/archivexl.dll",)),
    "tweakxl": ("TweakXL", ("red4ext/plugins/tweakxl/tweakxl.dll",)),
    "codeware": ("Codeware", ("red4ext/plugins/codeware/codeware.dll",)),
}


class Cyberpunk(Game):
    id = "Cyberpunk 2077"
    label = "Cyberpunk 2077"
    data_like = {"archive", "bin", "r6", "red4ext", "engine", "mods"}
    root_files = (".archive", ".xl")
    install_with = "Vortex"
    rules = ("only mods updated for the user's game version (2.x; check the page's \"updated\" date and the "
             "requirements); frameworks first: RED4ext, Cyber Engine Tweaks, redscript, ArchiveXL, TweakXL, Codeware "
             "(add each one the mods need, and nothing that duplicates them); avoid two mods replacing the same "
             "appearance or item unless one says it's compatible.")
    after_install = ("I've installed this modpack. Please tell me what to check in-game, and which log files to send you "
                     "if something doesn't load (Cyber Engine Tweaks, RED4ext and redscript logs), and we'll fix it.")
    after_install_hint = "then let the models walk you through a quick in-game check."
    workshop = ("laid out like the game folder: archive/pc/mod/ (.archive, .xl), r6/scripts/ (.reds), r6/tweaks/, "
                "bin/x64/plugins/cyber_engine_tweaks/mods/<mod>/ for CET; or a WolvenKit project")

    def inspect(self, mod):
        files = [f.lower() for f in mod["files"]]
        provides = {k for k, (_n, sigs) in CP_FRAMEWORKS.items() if any(f.endswith(s) or f == s for f in files for s in sigs)}
        needs = set()
        for f in files:
            if f.endswith(".xl"):
                needs.add("archivexl")
            if f.endswith(".reds"):
                needs.add("redscript")
            if f.startswith("r6/tweaks/") and f.endswith((".yaml", ".yml", ".tweak")):
                needs.add("tweakxl")
            if "/cyber_engine_tweaks/mods/" in f:
                needs.add("cet")
            if f.startswith("red4ext/plugins/") and not any(f.startswith(f"red4ext/plugins/{k}/") for k in CP_FRAMEWORKS):
                needs.add("red4ext")
        if "archivexl" in provides or "tweakxl" in provides or "codeware" in provides:
            needs.add("red4ext")
        loose = [f for f in mod["files"] if "/" not in f and f.lower().endswith(".archive")]
        archives_ = [f for f in mod["files"] if f.lower().endswith(".archive")]
        wrong = [f for f in mod["files"] if f.lower().startswith("archive/pc/content/")]
        return {"provides": sorted(provides), "needs": sorted(needs - provides), "archives": archives_[:200],
                "loose_archives": loose, "wrong_place": wrong[:5]}

    def check(self, mods):
        have = set()
        for _n, i in mods:
            have |= set(i.get("provides") or [])
        problems, notes = [], []
        for name, i in mods:
            for need in i.get("needs") or []:
                if need not in have:
                    problems.append(f"{name} needs {CP_FRAMEWORKS[need][0]}, which isn't in the pack.")
            if i.get("wrong_place"):
                problems.append(f"{name} puts files in archive/pc/content (the game's own folder); mods belong in archive/pc/mod.")
        seen = {}
        for name, i in mods:
            for a in i.get("archives") or []:
                base = a.rsplit("/", 1)[-1].lower()
                if base in seen and seen[base] != name:
                    notes.append(f"{name} and {seen[base]} both ship {a.rsplit('/', 1)[-1]}; the later one replaces the earlier.")
                seen.setdefault(base, name)
        notes.append("Cyberpunk loads mod .archive files in alphabetical order (a modlist.txt in archive/pc/mod can set "
                     "the order). Vortex handles this for you.")
        return [n for n, _i in mods], problems, notes[:30]


# ---------------- The Witcher 3 ----------------

class Witcher3(Game):
    id = "The Witcher 3"
    label = "The Witcher 3 (Next-Gen 4.0+)"
    data_like = {"mods", "dlc", "bin"}
    root_files = ()
    install_with = "Vortex or The Witcher 3 Mod Manager"
    rules = ("only mods for the next-gen version (4.0+) unless the user plays 1.32; mods that change the same scripts "
             "or the same bundled files conflict: prefer mods that say they're compatible, and plan to merge the rest "
             "with Script Merger (the user runs it after installing); include Community Patch - Base when mods need it; "
             "menu mods need their XML listed in dx11filelist.txt / dx12filelist.txt.")
    after_install = ("I've installed this modpack. Please tell me what to merge with Script Merger and in what order, "
                     "check the load order (mods.settings priorities), and tell me what to check in-game.")
    after_install_hint = "then let the models walk you through Script Merger and the load order."
    workshop = ("laid out like the game folder: mods/mod<Name>/content/ (scripts/, bundles), dlc/<name>/content/, "
                "bin/config/... for menus; the Package mod button packs loose files with wcc_lite")

    def data_root(self, names):
        tops = {n.split("/")[0] for n in names}
        if {t.lower() for t in tops} & self.data_like:
            return ""
        if any(t.lower().startswith("mod") and any(n.lower().startswith(t.lower() + "/content/") for n in names) for t in tops):
            return ""                         # modFoo/content/... at the top: goes into mods/
        if len(tops) == 1:
            t = next(iter(tops))
            inner = [n[len(t) + 1:] for n in names if n.startswith(t + "/")]
            if inner:
                sub = self.data_root(inner)
                return t + ("/" + sub if sub else "")
        return ""

    def inspect(self, mod):
        mods_dirs, scripts, bundles, bad = set(), [], [], []
        for f in mod["files"]:
            parts = f.split("/")
            low = [p.lower() for p in parts]
            if low[0] == "mods" and len(parts) > 2:
                d = parts[1]
            elif low[0].startswith("mod") and len(parts) > 1 and low[1] == "content":
                d = parts[0]
            else:
                d = None
            if d:
                mods_dirs.add(d)
                if not d.lower().startswith("mod"):
                    bad.append(d)
                if f.lower().endswith(".ws"):
                    scripts.append("/".join(parts[parts.index("content") + 1:]) if "content" in low else f)
                if f.lower().endswith(".bundle"):
                    bundles.append(d)
        menus = [f for f in mod["files"] if f.lower().startswith("bin/config/r4game/user_config_matrix/pc/") and f.lower().endswith(".xml")]
        lists = [f for f in mod["files"] if f.lower().endswith(("dx11filelist.txt", "dx12filelist.txt"))]
        return {"mod_dirs": sorted(mods_dirs), "scripts": sorted(set(scripts))[:300], "bundles": bool(bundles),
                "bad_names": sorted(set(bad)), "menus": menus[:10], "filelists": bool(lists)}

    def check(self, mods):
        problems, notes = [], []
        owner = {}
        for name, i in mods:
            for b in i.get("bad_names") or []:
                problems.append(f"{name}: its folder \"{b}\" doesn't start with \"mod\", so the game won't load it.")
            for s in i.get("scripts") or []:
                owner.setdefault(s.lower(), []).append(name)
            if i.get("menus") and not i.get("filelists"):
                notes.append(f"{name} adds a menu; its XML must be listed in dx11filelist.txt / dx12filelist.txt "
                             "(check its page; some mod managers do it).")
        clash = {s: ns for s, ns in owner.items() if len(set(ns)) > 1}
        for s, ns in list(clash.items())[:20]:
            problems.append(f"{' and '.join(sorted(set(ns)))} both change {s}: merge them with Script Merger after installing.")
        if sum(1 for _n, i in mods if i.get("bundles")) > 1:
            notes.append("Several mods have bundled files; run Script Merger after installing to find and merge any "
                         "bundled conflicts too.")
        order = []
        for name, i in reversed(mods):           # later in the plan = higher priority
            order += [d for d in i.get("mod_dirs") or [] if d not in order]
        return order, problems, notes

    def load_order_file(self, order):
        out = ["; Load order (mods.settings) for this modpack: the top mod wins conflicts. Vortex / The Witcher 3 Mod",
               "; Manager write this file for you; otherwise copy it to Documents\\The Witcher 3\\mods.settings.", ""]
        for i, d in enumerate(order):
            out += [f"[{d}]", "Enabled=1", f"Priority={i}", ""]
        return "\n".join(out)


# ---------------- Mass Effect Legendary Edition ----------------

class MassEffectLE(Game):
    id = "Mass Effect Legendary Edition"
    label = "Mass Effect Legendary Edition (LE1, LE2, LE3)"
    data_like = set()
    root_files = ("moddesc.ini", ".mem")
    install_with = "ME3Tweaks Mod Manager"
    rules = ("plan for one game at a time unless the user wants all three (say which: LE1, LE2 or LE3 for each mod); "
             "prefer mods packaged for ME3Tweaks Mod Manager (they have a moddesc.ini); texture mods (.mem) are "
             "installed LAST with ME3Tweaks Mod Manager's texture installer, and after that no more content mods can be "
             "added without reinstalling the game, so settle the texture choices before building; include the "
             "Community Patch / bug-fix mods first.")
    after_install = ("I've installed this modpack with ME3Tweaks Mod Manager. Please check the order I installed things "
                     "in, remind me of anything that has to be done in ME3Tweaks Mod Manager (like the texture install "
                     "or the ASI mods), and tell me what to check in-game.")
    after_install_hint = "then let the models check the order with you."
    workshop = ("an ME3Tweaks Mod Manager mod: a DLC_MOD_<Name>/CookedPCConsole folder with moddesc.ini; the Package mod "
                "button writes the moddesc.ini")

    def data_root(self, names):
        for n in names:
            if n.lower().endswith("moddesc.ini"):
                return n[:-len("moddesc.ini")].rstrip("/")
        return super().data_root(names)

    def inspect(self, mod):
        import configparser
        info = {"m3": False, "games": [], "dlcs": [], "requires": [], "textures": False, "asi": False}
        for f in mod["files"]:
            low = f.lower()
            if low.endswith(".mem"):
                info["textures"] = True
            if low.endswith(".asi"):
                info["asi"] = True
            if low.rsplit("/", 1)[-1] == "moddesc.ini":
                cp = configparser.ConfigParser(strict=False, interpolation=None)
                try:
                    cp.read_string(mod["read"](f, 1 << 20).decode("utf-8-sig", "replace"))
                except Exception:
                    continue
                info["m3"] = True
                g = cp.get("ModInfo", "game", fallback="").strip().upper()
                if g:
                    info["games"].append(g)
                for k in ("destdirs", "sourcedirs"):
                    v = cp.get("CUSTOMDLC", k, fallback="")
                    if v:
                        info["dlcs"] = [x.strip() for x in v.split(";") if x.strip()]
                        break
                req = cp.get("ModInfo", "requireddlc", fallback="")
                info["requires"] = [x.strip().split("[")[0] for x in re.split(r"[;,]", req) if x.strip()]
        if not info["dlcs"]:
            info["dlcs"] = sorted({p for f in mod["files"] for p in f.split("/") if p.upper().startswith("DLC_MOD_")})
        return info

    def check(self, mods):
        problems, notes = [], []
        dlc_owner, games = {}, set()
        provided = set()
        for name, i in mods:
            games |= set(i.get("games") or [])
            for d in i.get("dlcs") or []:
                provided.add(d.upper())
                if d.upper() in dlc_owner and dlc_owner[d.upper()] != name:
                    problems.append(f"{name} and {dlc_owner[d.upper()]} both install {d}; only one can be used.")
                dlc_owner.setdefault(d.upper(), name)
            if not i.get("m3") and not i.get("textures"):
                notes.append(f"{name} isn't packaged for ME3Tweaks Mod Manager (no moddesc.ini); check its page for how "
                             "to install it (ME3Tweaks Mod Manager can import many such mods).")
        for name, i in mods:
            for r in i.get("requires") or []:
                if r.upper().startswith("DLC_MOD_") and r.upper() not in provided:
                    problems.append(f"{name} needs {r}, which isn't in the pack.")
        if len(games) > 1:
            notes.append("This pack has mods for " + ", ".join(sorted(games)) + "; ME3Tweaks Mod Manager installs each "
                         "into its own game automatically.")
        tex = [n for n, i in mods if i.get("textures")]
        content = [n for n, i in mods if not i.get("textures")]
        if tex:
            notes.append("Texture mods (" + ", ".join(tex) + ") go in LAST, with ME3Tweaks Mod Manager → Texture "
                         "installer. Install every content mod before that.")
        if any(i.get("asi") for _n, i in mods):
            notes.append("Some mods include ASI files: ME3Tweaks Mod Manager installs the ASI loader for you.")
        return content + tex, problems, notes

    def install_steps(self):
        return ("Open ME3Tweaks Mod Manager and drag the numbered files onto it one at a time, in order. Install "
                "texture mods (.mem) last, with its texture installer. Don't add content mods after textures.")

    def load_order_file(self, order):
        return "# Install order for ME3Tweaks Mod Manager (top first; texture mods last).\n" + "".join(f"{n}\n" for n in order)


PROFILES = {g.id: g for g in (Skyrim(), BG3(), Cyberpunk(), MassEffectLE(), Witcher3())}


def profile(game):
    return PROFILES.get(game) or PROFILES["Skyrim Special Edition"]


def present_from_files(files):
    """Names a FOMOD's file conditions can look for: full paths and file names."""
    out = set()
    for f in files:
        out.add(f.lower())
        out.add(f.rsplit("/", 1)[-1].lower())
    return out


