"""The Mod Forge (PC only): build modpacks with the models, and develop our own mods.

Modpack Builder: Claude and GPT talk the list through with you (asking only when they need you), search
Nexus Mods and keep a plan with PLAN: lines. You approve the plan once; then Amalgam downloads every file
with your Nexus Premium key, runs each FOMOD installer (filling in what follows from earlier choices and
from the other mods in the pack, and asking you only for real choices), checks masters and load order,
and leaves one ready-to-install archive per mod in <Mod Forge folder>/<game>/<modpack>/, plus the load
order and an install guide. After you install it, the models can sort with LOOT and clean with xEdit
(allowed without asking once the plan is approved) and check the setup.

Mod Workshop: each original mod is a project whose code folder is the mod's Data-layout folder in
<Mod Forge folder>/<game>/My Mods/<mod>, so the models can edit it, read game files, and package it.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import zipfile

from . import archives, fomod
from .nexus import GAME_DOMAINS, Nexus, NexusError, mod_url

GAMES = {"Skyrim Special Edition": {"label": "Skyrim Special Edition / Anniversary Edition", "plugins": True,
                                    "base": ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]}}
PLUGIN_EXT = (".esp", ".esm", ".esl")
TOOL_EXES = {"loot.exe", "sseedit.exe", "sseedit64.exe", "xedit.exe", "xedit64.exe", "tes5edit.exe"}
NEXUS_RE = re.compile(r"^\W*NEXUS\s*:\s*(.+?)\s*$")
PLAN_RE = re.compile(r"^\W*PLAN\s*:\s*(.+?)\s*$")
MAX_LINES = 6
MYMODS = "My Mods"

PACK_LINE = """
=== MOD FORGE: MODPACK BUILDER ===
This project is the modpack "{name}" for {game}, built with the user. {account}
Goal: a stable, working mod list that matches what the user wants. Ask the user (NEED USER) about what really matters (look and feel, gameplay, body/armor setup, performance on their PC, lore-friendly or not) but make sensible choices yourselves the rest of the time. One of you drafts, the other checks it (requirements, conflicts, patches, versions).
Tools: put lines just above your last line (up to {max} in all). The app runs them right away and you get the results straight after:
NEXUS: search <words> (find mods on Nexus) · NEXUS: mod <id> (description and listed requirements) · NEXUS: files <id> (the files on the page)
PLAN: add <mod id> [file <file id>] | <why> · PLAN: remove <mod id> · PLAN: move <mod id> after <mod id> (or before) · PLAN: choose <mod id> <installer option>[; <option>] (which FOMOD options to pick, e.g. "PLAN: choose 12345 3BA; Immersive Armors patch")
Rules for a stable list: look at a mod (NEXUS: mod) before adding it and add its requirements too; plan order = install order (frameworks and requirements first, then big mods, then their patches; later mods overwrite earlier ones); only Skyrim SE/AE versions; with no "file" the main file is used, so name the file when a mod has several main files (e.g. for game versions or body types); add patches for mods that conflict; don't add two mods that do the same job. Leave anything a FOMOD installer covers to the installer (say which options with PLAN: choose).
Here the Mod Forge downloads the files with the user's Nexus account once they approve the plan, so don't send them to download mods by hand. When the plan is ready, ask the user to press "Approve plan & build" in the Mod Forge. The build downloads and installs everything into ready-to-install files and reports problems (missing masters, etc.) here; fix them with PLAN lines (a changed plan needs the user's approval again).
Your own patches or tweaks go in the Patches folder of the code folder (Data layout); they're packed as the last mod.
After the user installs the pack: check it with MODS: check, sort with LOOT and clean plugins with xEdit using RUN lines (MODS: tools gives the exact commands).{tools_note}
Current plan ({count} mods, {approval}):
{plan}
{build}"""

MOD_LINE = """
=== MOD FORGE: MOD WORKSHOP ===
This project is "{name}", an original mod for {game} that you're developing with the user. The code folder is the mod itself, laid out like the game's Data folder (plugins at the top, meshes/, textures/, scripts/ and so on), so files you make there are the mod.
Read the game's own files (READ) to match its records, meshes and scripts. Keep work files (.blend, .psd, notes) in a "Source" folder. The user can check and package the mod with the Package mod button in the Code tab.
"""


def _safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip().rstrip(". ")
    return re.sub(r"\s+", " ", name)[:70] or "Untitled"


def _strip_bbcode(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"\[/?[a-zA-Z*][^\]]{0,200}\]", "", text)
    text = re.sub(r"<[^>]{0,200}>", "", text)
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _plugin_info(data):
    """(masters, is_master_flagged, is_light, form_version) from the start of a plugin file."""
    try:
        from gamefiles.tes import Plugin
        p = Plugin(data)
        return list(p.masters), bool(p.flags & 0x1), bool(p.flags & 0x200), p.header.get("version", 0)
    except Exception:
        return [], False, False, 0


class Forge:
    def __init__(self, store, workspace, app_version="1.0", post=None, game_folders=None):
        self.store = store
        self.workspace = workspace
        self.version = app_version
        self.post = post or (lambda chat_id, speaker, kind, text: store.add(chat_id, speaker, kind, text))
        self.game_folders = game_folders or (lambda: [])
        self.jobs = {}           # item id -> live build progress
        self._lock = threading.Lock()

    # ----- settings and account -----

    def root(self):
        r = self.store.get_setting("forge_root", "")
        if not r:
            docs = os.path.join(os.path.expanduser("~"), "Documents")
            r = os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "AI Amalgam Mod Forge")
        return r

    def set_root(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip()))
        os.makedirs(path, exist_ok=True)
        self.store.set_setting("forge_root", path)
        return path

    def nexus(self):
        return Nexus(self.store.get_setting("nexus_key", ""), self.version)

    def account(self):
        try:
            a = json.loads(self.store.get_setting("nexus_account", "{}") or "{}")
        except ValueError:
            a = {}
        return {"connected": bool(self.store.get_setting("nexus_key", "")), "name": a.get("name", ""),
                "premium": bool(a.get("premium")), "limits": self.nexus().limits}

    def set_key(self, key):
        key = (key or "").strip()
        if not key:
            self.store.set_setting("nexus_key", "")
            self.store.set_setting("nexus_account", "{}")
            return self.account()
        if len(key) > 400 or not re.fullmatch(r"[A-Za-z0-9+/=_\-.]+", key):
            raise NexusError("That doesn't look like a Nexus API key.")
        info = Nexus(key, self.version).validate()
        self.store.set_setting("nexus_key", key)
        self.store.set_setting("nexus_account", json.dumps(info))
        return self.account()

    # ----- modpacks and mods -----

    def _game(self, game):
        if game not in GAMES:
            raise ValueError("The Mod Forge does Skyrim Special Edition / Anniversary Edition for now.")
        return game

    def _unique(self, parent, name):
        path, n = os.path.join(parent, name), 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(parent, f"{name} ({n})")
        return path

    def new_item(self, kind, game, name):
        game = self._game(game)
        name = _safe(name)
        if kind == "pack":
            if name.lower() == MYMODS.lower():
                name += " pack"
            folder = self._unique(os.path.join(self.root(), game), name)
            os.makedirs(os.path.join(folder, "Patches"), exist_ok=True)
            with open(os.path.join(folder, ".amalgam-nosync"), "w") as f:
                f.write("Mod Forge modpack: downloaded mods stay on this PC.\n")
            title, instr = f"Modpack: {name}", f"Build the modpack \"{name}\" for {game} in the Mod Forge."
        elif kind == "mod":
            folder = self._unique(os.path.join(self.root(), game, MYMODS), name)
            os.makedirs(os.path.join(folder, "Source"), exist_ok=True)
            title, instr = f"Mod: {name}", f"Develop the original mod \"{name}\" for {game} in the Mod Forge."
        else:
            raise ValueError("kind must be pack or mod")
        pid = self.store.new_project(title)
        self.store.update_project(pid, instructions=instr, code_dir=folder)
        chat_id = self.store.new_chat(title, pid)
        item_id = self.store.forge_add(kind, game, os.path.basename(folder), pid, folder)
        self.store.forge_update(item_id, state={"stage": "planning", "chat_id": chat_id, "mods": {}})
        return self.item(item_id)

    def item(self, item_id=None, project_id=None):
        it = self.store.forge_item(item_id, project_id)
        if not it:
            return None
        it["state"].setdefault("mods", {})
        it["live"] = self.jobs.get(it["id"])
        it["chat_id"] = it["state"].get("chat_id")
        it["exists"] = os.path.isdir(it["folder"])
        return it

    def items(self):
        out = []
        for it in self.store.forge_items():
            st = it["state"]
            out.append({"id": it["id"], "kind": it["kind"], "game": it["game"], "name": it["name"], "folder": it["folder"],
                        "project_id": it["project_id"], "approved": it["approved"], "count": len(it["plan"]),
                        "stage": st.get("stage", "planning"), "running": bool(self.jobs.get(it["id"], {}).get("running")),
                        "needs_you": sum(1 for m in st.get("mods", {}).values() if m.get("status") == "needs_you"),
                        "chat_id": st.get("chat_id")})
        return out

    def remove(self, item_id):
        """Forget a modpack or mod in the Mod Forge (its folder and chats stay where they are)."""
        if self.jobs.get(item_id, {}).get("running"):
            raise ValueError("It's building right now. Stop the build first.")
        self.store.forge_delete(item_id)

    # ----- prompts for the models -----

    def prompt_for(self, project_id):
        it = self.store.forge_item(project_id=project_id) if project_id else None
        if not it:
            return ""
        if it["kind"] == "mod":
            return MOD_LINE.format(name=it["name"], game=GAMES.get(it["game"], {}).get("label", it["game"]))
        acc = self.account()
        account = (f"Nexus Mods is connected ({acc['name'] or 'account'}, {'Premium' if acc['premium'] else 'NOT Premium: downloads will fail'})."
                   if acc["connected"] else "Nexus Mods isn't connected yet: NEXUS lines won't work until the user adds their API key in the Mod Forge.")
        plan = "\n".join(self._plan_line(i + 1, m, it) for i, m in enumerate(it["plan"])) or "(empty)"
        st = it["state"]
        build = ""
        if st.get("problems") or st.get("built_at"):
            build = f"Last build ({time.strftime('%b %d %H:%M', time.localtime(st.get('built_at') or time.time()))}): " + \
                    (("problems:\n" + "\n".join("- " + p for p in st["problems"][:25])) if st.get("problems") else "no problems found.")
        need = [m for m in st.get("mods", {}).values() if m.get("status") == "needs_you"]
        if need:
            build += "\nWaiting for the user's installer choices: " + ", ".join(m.get("name", "?") for m in need)
        return PACK_LINE.format(name=it["name"], game=GAMES[it["game"]]["label"], account=account, max=MAX_LINES,
                                count=len(it["plan"]), plan=plan, build=build,
                                approval="approved by the user" if it["approved"] else "not approved yet",
                                tools_note=(" The user approved the plan, so those LOOT and xEdit commands run without asking."
                                            if it["approved"] else ""))

    @staticmethod
    def _plan_line(n, m, it):
        st = it["state"].get("mods", {}).get(str(m["mod_id"]), {})
        bits = [f"{n}. #{m['mod_id']} {m.get('name') or '?'}"]
        if m.get("file_id"):
            bits.append(f"file {m['file_id']}" + (f" ({m['file_name']})" if m.get("file_name") else ""))
        if m.get("hints"):
            bits.append("installer: " + "; ".join(m["hints"]))
        if st.get("status"):
            bits.append(f"[{st['status']}]")
        if m.get("why"):
            bits.append(f"— {m['why'][:120]}")
        return " ".join(bits)

    # ----- NEXUS: and PLAN: lines -----

    def handle_lines(self, project_id, lines, who):
        """Run the NEXUS:/PLAN: lines a model wrote. Returns the text it gets back."""
        it = self.store.forge_item(project_id=project_id)
        if not it or it["kind"] != "pack":
            return ""
        out, changed, list_changed = [], False, False
        for kind, text in lines[:MAX_LINES]:
            try:
                if kind == "NEXUS":
                    out.append(self._nexus_line(it, text))
                else:
                    msg, ch = self._plan_cmd(it, text, who)
                    changed |= bool(ch)
                    list_changed |= ch is True
                    out.append(msg)
            except NexusError as e:
                out.append(f"{kind}: {text}\n{e}")
            except Exception as e:
                out.append(f"{kind}: {text}\nCouldn't do that: {e}")
        if changed:
            was_approved = self.store.forge_item(it["id"])["approved"]
            self.store.forge_update(it["id"], plan=it["plan"], approved=was_approved and not list_changed)
            out.append(f"Plan now has {len(it['plan'])} mods." + (" The plan changed, so the user needs to approve it again."
                                                                     if was_approved and list_changed else ""))
        return "\n\n".join(o for o in out if o)

    def _nexus_line(self, it, text):
        domain = GAME_DOMAINS[it["game"]]
        verb, _, rest = text.strip().partition(" ")
        verb, rest = verb.lower(), rest.strip()
        nx = self.nexus()
        if verb == "search":
            hits = nx.search(domain, rest)
            if not hits:
                return f"NEXUS search {rest}: nothing found. Try other words."
            return f"NEXUS search {rest}:\n" + "\n".join(
                f"#{h['mod_id']} {h['name']} (v{h['version']}, by {h['author']}, {h['endorsements']} endorsements, "
                f"updated {h['updated']}): {h['summary']}" for h in hits)
        if verb in ("mod", "files"):
            mid = int(re.findall(r"\d+", rest)[0])
            if verb == "files":
                files = nx.files(domain, mid)
                if not files:
                    return f"NEXUS files {mid}: no files."
                return f"NEXUS files {mid}:\n" + "\n".join(
                    f"file {f['file_id']}: {f['name']} v{f['version']} [{f['category']}{', primary' if f['primary'] else ''}] "
                    f"{f['size'] // 1_000_000 or '<1'} MB, {time.strftime('%Y-%m-%d', time.gmtime(f['uploaded'])) if f['uploaded'] else ''}"
                    + (f" — {f['description'][:120]}" if f["description"] else "")
                    for f in files if f["category"] not in ("DELETED",))
            m = nx.mod(domain, mid)
            reqs = nx.requirements(domain, mid)
            desc = _strip_bbcode(m["description"])
            text = (f"NEXUS mod {mid}: {m['name']} v{m['version']} by {m['author']} ({m['endorsements']} endorsements, "
                    f"updated {m['updated']}){' — NOT AVAILABLE (' + m['status'] + ')' if not m['available'] else ''}\n"
                    f"{mod_url(domain, mid)}\nSummary: {m['summary']}\n")
            if reqs:
                text += "Requirements listed on the page:\n" + "\n".join(
                    f"- {'external: ' + r['name'] + ' ' + r['url'] if r['external'] else '#' + str(r['mod_id']) + ' ' + r['name']}"
                    + (f" ({r['notes']})" if r["notes"] else "") for r in reqs) + "\n"
            return text + "Description:\n" + (desc[:5000] + ("\n[... cut ...]" if len(desc) > 5000 else ""))
        return f"NEXUS: {text}\nUnknown request. Use: search <words>, mod <id>, files <id>."

    def _plan_cmd(self, it, text, who):
        plan = it["plan"]
        verb, _, rest = text.strip().partition(" ")
        verb = verb.lower()
        ids = [int(x) for x in re.findall(r"\d+", rest.split("|")[0])]
        find = lambda mid: next((m for m in plan if m["mod_id"] == mid), None)
        if verb == "add":
            head, _, why = rest.partition("|")
            nums = re.findall(r"\d+", head)
            if not nums:
                return f"PLAN: {text}\nWhich mod? Use PLAN: add <mod id>.", False
            mid = int(nums[0])
            fm = re.search(r"\bfile\s+(\d+)", head, re.I)
            fid = int(fm.group(1)) if fm else None
            old = find(mid)
            name, fname = old["name"] if old else "", ""
            try:
                info = self.nexus().mod(GAME_DOMAINS[it["game"]], mid)
                name = info["name"]
                if fid:
                    fname = next((f["name"] for f in self.nexus().files(GAME_DOMAINS[it["game"]], mid) if f["file_id"] == fid), "")
            except NexusError as e:
                if e.status == 404:
                    return f"PLAN: add {mid}\nNexus has no mod #{mid} for this game. Nothing added.", False
            if old:
                old.update({"file_id": fid or old.get("file_id"), "file_name": fname or old.get("file_name", ""),
                            "why": why.strip() or old.get("why", ""), "name": name or old["name"]})
                return f"PLAN: updated #{mid} {name}.", True
            plan.append({"mod_id": mid, "name": name or f"Mod {mid}", "file_id": fid, "file_name": fname,
                         "why": why.strip()[:300], "hints": [], "by": who})
            return f"PLAN: added #{mid} {name} (position {len(plan)}).", True
        if verb == "remove":
            m = find(ids[0]) if ids else None
            if not m:
                return f"PLAN: {text}\nThat mod isn't in the plan.", False
            plan.remove(m)
            return f"PLAN: removed #{m['mod_id']} {m['name']}.", True
        if verb == "move":
            mm = re.match(r"(\d+)\s+(after|before)\s+(\d+)", rest.strip(), re.I)
            a, b = (find(int(mm.group(1))), find(int(mm.group(3)))) if mm else (None, None)
            if not (a and b) or a is b:
                return f"PLAN: {text}\nUse PLAN: move <mod id> after <mod id>, with both in the plan.", False
            plan.remove(a)
            i = plan.index(b) + (1 if mm.group(2).lower() == "after" else 0)
            plan.insert(i, a)
            return f"PLAN: moved #{a['mod_id']} {a['name']} {mm.group(2).lower()} #{b['mod_id']} {b['name']}.", True
        if verb == "choose":
            mm = re.match(r"(\d+)\s+(.+)", rest.strip())
            m = find(int(mm.group(1))) if mm else None
            if not m:
                return f"PLAN: {text}\nUse PLAN: choose <mod id in the plan> <option>[; <option>].", False
            m["hints"] = [h.strip() for h in mm.group(2).split(";") if h.strip()][:20]
            return f"PLAN: installer options for #{m['mod_id']} {m['name']}: {'; '.join(m['hints'])}.", "hints"
        return f"PLAN: {text}\nUnknown. Use add, remove, move or choose.", False

    def set_plan(self, item_id, plan):
        """The window's edits (reorder, remove). Any change needs approval again."""
        it = self.store.forge_item(item_id)
        keep = {m["mod_id"]: m for m in it["plan"]}
        new = [keep[int(x)] for x in plan if int(x) in keep]
        if [m["mod_id"] for m in new] != [m["mod_id"] for m in it["plan"]]:
            self.store.forge_update(item_id, plan=new, approved=False)
        return self.item(item_id)

    def approve(self, item_id, ok=True):
        it = self.store.forge_item(item_id)
        if ok and not it["plan"]:
            raise ValueError("The plan is empty. Talk it through with the models first.")
        self.store.forge_update(item_id, approved=bool(ok))
        return self.item(item_id)

    def tools_ok(self, project_id, command):
        """In a modpack whose plan you approved, LOOT and xEdit run without asking."""
        it = self.store.forge_item(project_id=project_id) if project_id else None
        if not it or it["kind"] != "pack" or not it["approved"]:
            return False
        cmd = command.strip().lstrip("&").strip()
        m = re.match(r'"([^"]+)"|\'([^\']+)\'|(\S+)', cmd)
        exe = next((g for g in (m.groups() if m else ()) if g), "")
        if re.search(r"[;|`]|&&|\|\||\$\(", cmd[len(exe) + 2:]):      # nothing chained after the tool
            return False
        return os.path.basename(exe.replace("\\", "/")).lower() in TOOL_EXES

    # ----- FOMOD choices -----

    def fomod_view(self, item_id, mod_id, choices=None):
        it = self.store.forge_item(item_id)
        ms = it["state"].get("mods", {}).get(str(mod_id))
        if not ms or not ms.get("archive") or not os.path.isfile(ms["archive"]):
            raise ValueError("That mod hasn't been downloaded yet. Build the pack first.")
        f, base = self._load_fomod(ms["archive"])
        if not f:
            raise ValueError("That mod has no FOMOD installer.")
        pm = next((m for m in it["plan"] if m["mod_id"] == int(mod_id)), {})
        present = self._present(it)
        r = f.resolve(choices if choices is not None else ms.get("choices"), pm.get("hints", []), self._state_fn(present))
        img_dir = os.path.join(self.workspace, "forge", f"item-{item_id}", f"mod-{mod_id}")
        imgs = [o["image"] for s in r["steps"] for g in s["groups"] for o in g["options"] if o["image"]]
        if imgs:
            try:
                arch_names = {n.lower(): n for n in archives.names(ms["archive"])}
                want = [arch_names.get(((base + "/") if base else "").lower() + i.lower()) for i in imgs]
                want = [w for w in want if w]
                missing = [w for w in want if not os.path.isfile(os.path.join(img_dir, *w.split("/")))]
                if missing:
                    archives.extract(ms["archive"], img_dir, only=missing[:60])
                for s in r["steps"]:
                    for g in s["groups"]:
                        for o in g["options"]:
                            if o["image"]:
                                real = arch_names.get(((base + "/") if base else "").lower() + o["image"].lower())
                                o["image_path"] = os.path.join(img_dir, *real.split("/")) if real else ""
            except Exception:
                pass
        r["mod_id"] = int(mod_id)
        r["mod_name"] = ms.get("name") or pm.get("name", "")
        return r

    def fomod_set(self, item_id, mod_id, choices, done=False):
        """Save your installer choices for one mod; with done=True the build carries on with them."""
        it = self.store.forge_item(item_id)
        ms = it["state"]["mods"].get(str(mod_id))
        if not ms:
            raise ValueError("Unknown mod.")
        clean = {str(k)[:300]: [str(x)[:300] for x in (v or [])][:50] for k, v in (choices or {}).items()}
        ms["choices"] = clean
        if done:
            ms["choices_done"] = True
            if ms.get("status") == "needs_you":
                ms["status"] = "chosen"
        self.store.forge_update(item_id, state=it["state"])
        return self.fomod_view(item_id, mod_id)

    # ----- building -----

    def _downloads(self, game):
        return os.path.join(self.root(), game, ".downloads")

    def _present(self, it):
        names = {n.lower() for n in GAMES[it["game"]]["base"]}
        for ms in it["state"].get("mods", {}).values():
            names |= {p.lower() for p in ms.get("plugins", [])}
        return names

    @staticmethod
    def _state_fn(present):
        def st(name):
            n = (name or "").replace("\\", "/").lower()
            return "Active" if (n in present or n.rsplit("/", 1)[-1] in present) else "Missing"
        return st

    @staticmethod
    def _load_fomod(archive):
        names = archives.names(archive)
        cfg, base, info = fomod.find_config(names)
        if not cfg:
            return None, None
        return fomod.Fomod(archives.read(archive, cfg), base, archives.read(archive, info) if info else None), base

    @staticmethod
    def _data_root(names):
        """The folder inside an archive that maps to Data (handles a Data/ folder or a single wrapper folder)."""
        data_like = {"meshes", "textures", "scripts", "interface", "sound", "skse", "seq", "music", "strings",
                     "materials", "lodsettings", "grass", "shadersfx", "calientetools", "netscriptframework", "video"}
        tops = {n.split("/")[0] for n in names}
        if any(n.lower().endswith(PLUGIN_EXT) and "/" not in n for n in names) or {t.lower() for t in tops} & data_like:
            return ""
        if len(tops) == 1:
            t = next(iter(tops))
            inner = [n[len(t) + 1:] for n in names if n.startswith(t + "/")]
            sub = Forge._data_root(inner)
            return t + ("/" + sub if sub else "") if inner else ""
        for n in names:
            if n.lower().startswith("data/"):
                return n.split("/")[0]
        return ""

    def start_build(self, item_id):
        it = self.store.forge_item(item_id)
        if not it or it["kind"] != "pack":
            raise ValueError("Unknown modpack.")
        if not it["approved"]:
            raise ValueError("Approve the plan first.")
        acc = self.account()
        if not acc["connected"]:
            raise ValueError("Connect your Nexus account first (Nexus account, in the Mod Forge).")
        with self._lock:
            if self.jobs.get(item_id, {}).get("running"):
                return False
            self.jobs[item_id] = {"running": True, "stage": "starting", "detail": "", "done": 0, "total": len(it["plan"]),
                                  "stop": False, "bytes": 0, "size": 0}
        threading.Thread(target=self._build, args=(item_id,), daemon=True).start()
        return True

    def stop_build(self, item_id):
        j = self.jobs.get(item_id)
        if j:
            j["stop"] = True

    def _set(self, item_id, **kw):
        j = self.jobs.setdefault(item_id, {})
        j.update(kw)

    def _build(self, item_id):
        it = self.store.forge_item(item_id)
        st = it["state"]
        mods = st.setdefault("mods", {})
        problems = []
        try:
            nx = self.nexus()
            domain = GAME_DOMAINS[it["game"]]
            dl = self._downloads(it["game"])
            stop = lambda: self.jobs.get(item_id, {}).get("stop")
            # 1. download
            for i, pm in enumerate(it["plan"]):
                if stop():
                    raise NexusError("Stopped.")
                key = str(pm["mod_id"])
                ms = mods.setdefault(key, {})
                ms["name"] = pm["name"]
                self._set(item_id, stage="downloading", detail=pm["name"], done=i, bytes=0, size=0)
                try:
                    files = nx.files(domain, pm["mod_id"])
                    f = next((x for x in files if x["file_id"] == pm.get("file_id")), None) if pm.get("file_id") else nx.pick_main(files)
                    if not f:
                        raise NexusError("no main file found on the page (name one with PLAN: add <id> file <file id>)")
                    if ms.get("file_id") != f["file_id"]:
                        ms.update({"choices": ms.get("choices", {}), "choices_done": False})
                    ms.update({"file_id": f["file_id"], "file_name": f["name"], "version": f["version"]})
                    ms["archive"] = nx.download(domain, pm["mod_id"], f["file_id"], dl, f["file_name"], f["size"],
                                                progress=lambda got, tot: self._set(item_id, bytes=got, size=tot), stop=stop)
                    ms["status"] = "downloaded"
                    ms.pop("error", None)
                except NexusError as e:
                    if str(e) == "Stopped.":
                        raise
                    ms.update({"status": "error", "error": str(e)})
                    problems.append(f"{pm['name']} (#{pm['mod_id']}): couldn't download: {e}")
                self.store.forge_update(item_id, state=st)
            # 2. look inside: plugins, masters, installers
            self._set(item_id, stage="checking", detail="", done=0)
            info = {}
            for pm in it["plan"]:
                ms = mods.get(str(pm["mod_id"]), {})
                if ms.get("status") == "error":
                    continue
                try:
                    names = archives.names(ms["archive"])
                    cfg, base, _info = fomod.find_config(names)
                    ms["fomod"] = bool(cfg)
                    root = (base if cfg else self._data_root(names))
                    pre = (root + "/") if root else ""
                    if not cfg:
                        ms["plugins"] = [n[len(pre):] for n in names if n.startswith(pre) and "/" not in n[len(pre):]
                                         and n.lower().endswith(PLUGIN_EXT)]
                    info[pm["mod_id"]] = (names, cfg, base, root)
                except archives.ArchiveError as e:
                    ms.update({"status": "error", "error": str(e)})
                    problems.append(f"{pm['name']}: {e}")
            # 3. installers: two passes so patches see plugins chosen in other installers
            present = self._present(it | {"state": st})
            resolved = {}
            for _pass in range(2):
                for pm in it["plan"]:
                    ms = mods.get(str(pm["mod_id"]), {})
                    if not ms.get("fomod") or ms.get("status") == "error":
                        continue
                    f, base = self._load_fomod(ms["archive"])
                    r = f.resolve(ms.get("choices") or None, pm.get("hints", []), self._state_fn(present))
                    resolved[pm["mod_id"]] = (f, base, r)
                    plug = [d for _s, d, _isf, _p in f.install_list(r, self._state_fn(present)) if d.lower().endswith(PLUGIN_EXT)]
                    ms["plugins"] = [p for p in plug if "/" not in p]
                    present |= {p.lower() for p in ms["plugins"]}
            # 4. make the packages
            folder = it["folder"]
            os.makedirs(folder, exist_ok=True)
            for old in os.listdir(folder):
                if re.match(r"^\d{2,3} - ", old) and os.path.isfile(os.path.join(folder, old)):
                    os.remove(os.path.join(folder, old))
            work = os.path.join(self.workspace, "forge", f"item-{item_id}", "work")
            n = 0
            for pm in it["plan"]:
                if stop():
                    raise NexusError("Stopped.")
                ms = mods.get(str(pm["mod_id"]), {})
                if ms.get("status") == "error":
                    continue
                n += 1
                stem = f"{n:02d} - {_safe(pm['name'])}" + (f" {_safe(ms.get('version', ''))}" if ms.get("version") else "")
                self._set(item_id, stage="packing", detail=pm["name"], done=n - 1)
                if ms.get("fomod"):
                    f, base, r = resolved[pm["mod_id"]]
                    if r["needs_you"] and not ms.get("choices_done"):
                        ms["status"] = "needs_you"
                        ms["needs_you"] = r["needs_you"]
                        ms["out"] = ""
                        continue
                    shutil.rmtree(work, ignore_errors=True)
                    archives.extract(ms["archive"], work)
                    files, missing = fomod.materialize(f.install_list(r, self._state_fn(present)),
                                                       os.path.join(work, *base.split("/")) if base else work)
                    if missing:
                        problems.append(f"{pm['name']}: its installer points at files that aren't in the download: "
                                        + ", ".join(missing[:5]))
                    out = os.path.join(folder, stem + ".zip")
                    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                        for dest, src in sorted(files.items()):
                            z.write(src, dest)
                    masters = self._masters_from_files({d: s for d, s in files.items() if d.lower().endswith(PLUGIN_EXT) and "/" not in d})
                    shutil.rmtree(work, ignore_errors=True)
                    ms.update({"status": "ready", "out": os.path.basename(out), "needs_you": 0, "masters": masters,
                               "plugins": sorted(masters)})
                else:
                    ext = os.path.splitext(ms["archive"])[1].lower() or ".zip"
                    out = os.path.join(folder, stem + ext)
                    shutil.copyfile(ms["archive"], out)
                    names, _cfg, _base, root = info[pm["mod_id"]]
                    pre = (root + "/") if root else ""
                    masters = {}
                    for p in ms.get("plugins", []):
                        try:
                            masters[p] = _plugin_info(archives.read(ms["archive"], pre + p, limit=262144))
                        except Exception:
                            masters[p] = ([], False, False, 0)
                    ms.update({"status": "ready", "out": os.path.basename(out), "masters": masters})
                self.store.forge_update(item_id, state=st)
            # our own patches
            patches = os.path.join(folder, "Patches")
            pfiles = [os.path.join(dp, f) for dp, _d, fs in os.walk(patches) for f in fs] if os.path.isdir(patches) else []
            pmasters = {}
            if pfiles:
                n += 1
                out = os.path.join(folder, f"{n:02d} - {_safe(it['name'])} Patches.zip")
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    for full in pfiles:
                        rel = os.path.relpath(full, patches).replace("\\", "/")
                        z.write(full, rel)
                        if "/" not in rel and rel.lower().endswith(PLUGIN_EXT):
                            with open(full, "rb") as fh:
                                pmasters[rel] = _plugin_info(fh.read(262144))
            # 5. masters and load order
            order, more = self._load_order(it, mods, pmasters)
            problems += more
            waiting = [m["name"] for m in mods.values() if m.get("status") == "needs_you"]
            self._write_guide(it, mods, order, problems, waiting)
            st.update({"problems": problems, "load_order": order, "built_at": time.time(),
                       "stage": "needs_you" if waiting else ("problems" if problems else "built")})
            self.store.forge_update(item_id, state=st)
            self._report(it, st, waiting)
        except Exception as e:
            st["stage"] = "stopped" if str(e) == "Stopped." else "error"
            st["error"] = str(e)
            self.store.forge_update(item_id, state=st)
            if st["stage"] == "error" and st.get("chat_id"):
                self.post(st["chat_id"], "Mod Forge", "output", f"$ build {it['name']}\nThe build failed: {e}")
        finally:
            self._set(item_id, running=False, stage="")

    @staticmethod
    def _masters_from_files(files):
        out = {}
        for dest, src in files.items():
            try:
                with open(src, "rb") as fh:
                    out[dest] = _plugin_info(fh.read(262144))
            except OSError:
                out[dest] = ([], False, False, 0)
        return out

    def _load_order(self, it, mods, extra):
        base = GAMES[it["game"]]["base"]
        info, owner = {}, {}
        for pm in it["plan"]:
            ms = mods.get(str(pm["mod_id"]), {})
            for p, v in (ms.get("masters") or {}).items():
                info[p] = v
                owner[p.lower()] = pm["name"]
        for p, v in extra.items():
            info[p] = v
            owner[p.lower()] = "your patches"
        have = {b.lower() for b in base} | {p.lower() for p in info}
        problems = []
        for p, (masters, _m, _l, form) in info.items():
            miss = [m for m in masters if m.lower() not in have]
            for m in miss:
                hint = " (Creation Club / Anniversary content)" if m.lower().startswith("cc") else ""
                problems.append(f"{p} ({owner[p.lower()]}) needs {m}{hint}, which isn't in the pack or the base game.")
            if form and form < 44 and p.lower().endswith((".esp", ".esm")):
                problems.append(f"{p} ({owner[p.lower()]}) is a Skyrim LE (Oldrim) plugin; it needs converting for SE/AE.")
        # masters first, then plan order; master-flagged files before normal ones
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
            problems.append(f"{len(full)} full plugins: over Skyrim's limit of 254 (with the base game). Make some light (ESL) or merge them.")
        return order, problems

    def _write_guide(self, it, mods, order, problems, waiting):
        folder, domain = it["folder"], GAME_DOMAINS[it["game"]]
        with open(os.path.join(folder, "Load order.txt"), "w", encoding="utf-8") as f:
            f.write("# Load order for this modpack (plugins.txt format). Vortex and LOOT sort automatically;\n"
                    "# in Mod Organizer 2 you can match this order in the right-hand Plugins list.\n")
            for p in order:
                f.write(f"*{p}\n")
        lines = [f"# {it['name']}", "", f"Modpack for {GAMES[it['game']]['label']}, built by AI Amalgam's Mod Forge on "
                 f"{time.strftime('%Y-%m-%d %H:%M')}.", "",
                 "## Install", "Install the numbered files in order (drag them all into Vortex or Mod Organizer 2). Installer "
                 "choices are already applied, so they install without questions. Later files overwrite earlier ones.", ""]
        for i, pm in enumerate(it["plan"], 1):
            ms = mods.get(str(pm["mod_id"]), {})
            stt = {"ready": ms.get("out", ""), "needs_you": "waiting for your installer choices",
                   "error": "not downloaded: " + ms.get("error", "")}.get(ms.get("status"), ms.get("status", ""))
            lines.append(f"{i}. [{pm['name']}]({mod_url(domain, pm['mod_id'])}) {ms.get('version', '')} — {stt}")
            if ms.get("choices"):
                picked = "; ".join(f"{k.split('::')[-1]}: {', '.join(v)}" for k, v in ms["choices"].items() if v)
                if picked:
                    lines.append(f"   - Installer choices: {picked}")
            if pm.get("why"):
                lines.append(f"   - Why: {pm['why']}")
        if problems:
            lines += ["", "## Problems found", *[f"- {p}" for p in problems]]
        if waiting:
            lines += ["", "## Waiting for you", "Pick the installer options in the Mod Forge for: " + ", ".join(waiting)]
        lines += ["", "## Load order", "See Load order.txt.", ""]
        with open(os.path.join(folder, "Install guide.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _report(self, it, st, waiting):
        chat = st.get("chat_id")
        if not chat:
            return
        ready = sum(1 for m in st["mods"].values() if m.get("status") == "ready")
        text = [f"$ build {it['name']}", f"{ready} of {len(it['plan'])} mods packed into {it['folder']}."]
        if waiting:
            text.append("Waiting for the user's installer choices: " + ", ".join(waiting))
        if st["problems"]:
            text += ["Problems:"] + [f"- {p}" for p in st["problems"]]
        else:
            text.append("No missing masters or other problems found.")
        text.append(f"Load order ({len(st['load_order'])} plugins): " + ", ".join(st["load_order"][:80]))
        self.post(chat, "Mod Forge", "output", "\n".join(text))

    def status(self, item_id):
        it = self.item(item_id)
        if not it:
            return None
        st = it["state"]
        mods = []
        for pm in it["plan"]:
            ms = st["mods"].get(str(pm["mod_id"]), {})
            mods.append({"mod_id": pm["mod_id"], "name": pm["name"], "why": pm.get("why", ""), "hints": pm.get("hints", []),
                         "file_id": pm.get("file_id"), "file_name": ms.get("file_name") or pm.get("file_name", ""),
                         "version": ms.get("version", ""), "status": ms.get("status", "planned"), "fomod": ms.get("fomod", False),
                         "needs_you": ms.get("needs_you", 0), "out": ms.get("out", ""), "error": ms.get("error", ""),
                         "url": mod_url(GAME_DOMAINS[it["game"]], pm["mod_id"])})
        return {"id": it["id"], "kind": it["kind"], "name": it["name"], "game": it["game"], "folder": it["folder"],
                "exists": it["exists"], "project_id": it["project_id"], "chat_id": it["chat_id"], "approved": it["approved"],
                "stage": st.get("stage", "planning"), "problems": st.get("problems", []), "error": st.get("error", ""),
                "load_order": st.get("load_order", []), "built_at": st.get("built_at"), "mods": mods,
                "live": {k: v for k, v in (self.jobs.get(item_id) or {}).items() if k != "stop"},
                "plan_hash": hashlib.md5(json.dumps(it["plan"], sort_keys=True).encode()).hexdigest()[:10]}
