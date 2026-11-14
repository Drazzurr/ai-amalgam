"""A FOMOD installer engine: read fomod/ModuleConfig.xml, work out which options are required, recommended,
unusable or open, fill in everything that follows from earlier choices, and list the files to install.

It follows the FOMOD schema the way Mod Organizer 2 and Vortex read it:
- steps can be hidden by conditions on flags set by options picked in earlier steps;
- each option's type (Required / Recommended / Optional / CouldBeUsable / NotUsable) can depend on flags and on
  whether other plugins are present ("Active") in the load order; here, present means part of the same modpack
  or the base game;
- group types SelectExactlyOne / SelectAtMostOne / SelectAtLeastOne / SelectAll / SelectAny;
- requiredInstallFiles, alwaysInstall / installIfUsable files, and conditionalFileInstalls (checked against the
  final flags), installed in priority order (higher priority wins).
"""

import os
import re
import xml.etree.ElementTree as ET

TYPES = ("Required", "Recommended", "Optional", "CouldBeUsable", "NotUsable")


def _strip(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _kids(el, name=None):
    return [c for c in list(el) if isinstance(c.tag, str) and (name is None or _strip(c.tag) == name.lower())]


def _kid(el, name):
    k = _kids(el, name)
    return k[0] if k else None


def _attr(el, name, default=""):
    for k, v in el.attrib.items():
        if _strip(k) == name.lower():
            return v
    return default


def parse_xml(data):
    """ElementTree root from bytes that may be UTF-8 or UTF-16, with or without a lying encoding line."""
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    for enc in ("utf-16", "utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"))
        try:
            return ET.fromstring(text)
        except ET.ParseError:
            continue
    raise ValueError("The FOMOD's ModuleConfig.xml couldn't be read (broken XML).")


def _order(items, mode, key):
    mode = (mode or "Ascending").lower()
    if mode == "explicit":
        return items
    s = sorted(items, key=lambda x: key(x).lower())
    return list(reversed(s)) if mode == "descending" else s


# ---------------- conditions ----------------

def _cond(el):
    """('and'|'or', [parts]) from a <dependencies>/<moduleDependencies>/<visible> element; leaves are tuples."""
    if el is None:
        return None
    op = (_attr(el, "operator", "And") or "And").lower()
    parts = []
    for c in _kids(el):
        t = _strip(c.tag)
        if t == "filedependency":
            parts.append(("file", _attr(c, "file"), _attr(c, "state", "Active")))
        elif t == "flagdependency":
            parts.append(("flag", _attr(c, "flag"), _attr(c, "value")))
        elif t == "dependencies":
            parts.append(_cond(c))
        elif t in ("gamedependency", "fommdependency", "fosedependency", "scriptextenderdependency"):
            parts.append(("assume", t))      # game / script extender versions: assume a current setup
    return (op, parts)


def check(cond, flags, file_state):
    if cond is None:
        return True
    if cond[0] in ("and", "or"):
        res = [check(p, flags, file_state) for p in cond[1]]
        if not res:
            return True
        return all(res) if cond[0] == "and" else any(res)
    if cond[0] == "file":
        return file_state(cond[1]).lower() == (cond[2] or "Active").lower()
    if cond[0] == "flag":
        return (flags.get(cond[1].lower(), "") or "") == (cond[2] or "")
    return True


def describe(cond):
    """Short human words for a condition (shown next to options that depend on something)."""
    if cond is None:
        return ""
    if cond[0] in ("and", "or"):
        parts = [describe(p) for p in cond[1] if describe(p)]
        return f" {cond[0]} ".join(parts)
    if cond[0] == "file":
        return f"{cond[1]} {cond[2].lower()}"
    if cond[0] == "flag":
        return f"{cond[1]} = {cond[2]}"
    return ""


# ---------------- files ----------------

def _files(el):
    out = []
    if el is None:
        return out
    for c in _kids(el):
        t = _strip(c.tag)
        if t not in ("file", "folder"):
            continue
        src = _attr(c, "source").replace("\\", "/").strip("/")
        has_dest = any(_strip(k) == "destination" for k in c.attrib)
        dest = _attr(c, "destination").replace("\\", "/").strip("/") if has_dest else src
        try:
            prio = int(_attr(c, "priority", "0") or 0)
        except ValueError:
            prio = 0
        out.append({"source": src, "dest": dest, "folder": t == "folder", "priority": prio,
                    "always": _attr(c, "alwaysInstall", "false").lower() == "true",
                    "if_usable": _attr(c, "installIfUsable", "false").lower() == "true"})
    return out


class Fomod:
    def __init__(self, xml_bytes, base="", info_bytes=None):
        root = parse_xml(xml_bytes)
        self.base = base.strip("/")
        self.name = (_kid(root, "moduleName").text or "").strip() if _kid(root, "moduleName") is not None else ""
        img = _kid(root, "moduleImage")
        self.image = _attr(img, "path").replace("\\", "/") if img is not None else ""
        self.module_deps = _cond(_kid(root, "moduleDependencies"))
        self.required = _files(_kid(root, "requiredInstallFiles"))
        self.version = ""
        if info_bytes:
            try:
                info = parse_xml(info_bytes)
                self.name = self.name or (_kid(info, "Name").text or "").strip() if _kid(info, "Name") is not None else self.name
                v = _kid(info, "Version")
                self.version = (v.text or "").strip() if v is not None else ""
            except ValueError:
                pass
        self.steps = []
        steps_el = _kid(root, "installSteps")
        if steps_el is not None:
            steps = []
            for s in _kids(steps_el, "installStep"):
                groups = []
                ge = _kid(s, "optionalFileGroups")
                for g in (_kids(ge, "group") if ge is not None else []):
                    pe = _kid(g, "plugins")
                    plugins = []
                    for p in (_kids(pe, "plugin") if pe is not None else []):
                        d = _kid(p, "description")
                        im = _kid(p, "image")
                        flags = {}
                        cf = _kid(p, "conditionFlags")
                        for f in (_kids(cf, "flag") if cf is not None else []):
                            flags[_attr(f, "name")] = (f.text or "").strip()
                        td = _kid(p, "typeDescriptor")
                        default, patterns = "Optional", []
                        if td is not None:
                            simple = _kid(td, "type")
                            dt = _kid(td, "dependencyType")
                            if simple is not None:
                                default = _attr(simple, "name", "Optional")
                            elif dt is not None:
                                dflt = _kid(dt, "defaultType")
                                default = _attr(dflt, "name", "Optional") if dflt is not None else "Optional"
                                pats = _kid(dt, "patterns")
                                for pat in (_kids(pats, "pattern") if pats is not None else []):
                                    t = _kid(pat, "type")
                                    patterns.append((_cond(_kid(pat, "dependencies")), _attr(t, "name", default) if t is not None else default))
                        plugins.append({"name": _attr(p, "name"), "desc": (d.text or "").strip() if d is not None else "",
                                        "image": _attr(im, "path").replace("\\", "/") if im is not None else "",
                                        "files": _files(_kid(p, "files")), "flags": flags,
                                        "default": default if default in TYPES else "Optional", "patterns": patterns})
                    groups.append({"name": _attr(g, "name"), "type": _attr(g, "type", "SelectAny"),
                                   "plugins": _order(plugins, _attr(pe, "order") if pe is not None else "", lambda x: x["name"])})
                steps.append({"name": _attr(s, "name"), "visible": _cond(_kid(s, "visible")),
                              "groups": _order(groups, _attr(ge, "order") if ge is not None else "", lambda x: x["name"])})
            self.steps = _order(steps, _attr(steps_el, "order"), lambda x: x["name"])
        self.conditional = []
        ce = _kid(root, "conditionalFileInstalls")
        pats = _kid(ce, "patterns") if ce is not None else None
        for pat in (_kids(pats, "pattern") if pats is not None else []):
            self.conditional.append((_cond(_kid(pat, "dependencies")), _files(_kid(pat, "files"))))

    # ----- choosing -----

    @staticmethod
    def _type(plugin, flags, file_state):
        for cond, t in plugin["patterns"]:
            if check(cond, flags, file_state):
                return t if t in TYPES else "Optional"
        return plugin["default"]

    @staticmethod
    def _hinted(plugin, gname, hints):
        pn = plugin["name"].strip().lower()
        for h in hints:
            grp, _, opt = h.rpartition(">")
            grp, opt = grp.strip().lower(), opt.strip().lower()
            if grp and grp not in gname.lower():
                continue
            if opt and (opt == pn or (len(opt) >= 4 and (opt in pn or pn in opt))):
                return True
        return False

    def resolve(self, choices=None, hints=(), file_state=None):
        """Work through the installer. choices: {"<step>::<group>": [option names]} you picked;
        hints: option names the models suggested. Returns the filled-in installer for the window, plus
        `needs_you` (groups with a real decision nobody has made) and the final flags."""
        choices = choices or {}
        file_state = file_state or (lambda f: "Missing")
        flags, out, needs = {}, [], 0
        for si, step in enumerate(self.steps):
            visible = check(step["visible"], flags, file_state)
            sout = {"name": step["name"] or f"Step {si + 1}", "visible": visible, "groups": [],
                    "condition": describe(step["visible"])}
            new_flags = {}
            for gi, g in enumerate(step["groups"]):
                key = f"{step['name']}::{g['name']}"
                types = [self._type(p, flags, file_state) for p in g["plugins"]]
                usable = [i for i, t in enumerate(types) if t != "NotUsable"]
                req = [i for i in usable if types[i] == "Required"]
                rec = [i for i in usable if types[i] == "Recommended"]
                gt = (g["type"] or "SelectAny").lower()
                sel, source, open_q = set(req), "auto", False
                picked = choices.get(key)
                if gt == "selectall":
                    sel = set(usable)
                elif picked is not None:
                    names = {n.lower() for n in picked}
                    sel |= {i for i in usable if g["plugins"][i]["name"].lower() in names}
                    source = "you"
                else:
                    hinted = [i for i in usable if self._hinted(g["plugins"][i], g["name"], hints)]
                    if hinted:
                        sel |= set(hinted[:1] if gt in ("selectexactlyone", "selectatmostone") else hinted)
                        source = "suggested"
                    else:
                        sel |= set(rec[:1] if gt in ("selectexactlyone", "selectatmostone") else rec)
                    if gt in ("selectexactlyone", "selectatleastone") and not sel and usable:
                        sel = {usable[0]}
                        open_q = len(usable) > 1          # a real choice with nothing to go on
                if gt in ("selectexactlyone", "selectatmostone") and len(sel) > 1:
                    keep = [i for i in sorted(sel) if i in req] or sorted(sel)
                    sel = {keep[0]}
                if visible and open_q:
                    needs += 1
                gout = {"key": key, "name": g["name"], "type": g["type"], "needs_you": bool(visible and open_q),
                        "source": source, "options": []}
                for i, p in enumerate(g["plugins"]):
                    cond = next((describe(c) for c, t in p["patterns"] if check(c, flags, file_state)), "")
                    gout["options"].append({"name": p["name"], "desc": p["desc"][:600], "image": p["image"],
                                            "type": types[i], "selected": i in sel, "why": cond,
                                            "locked": types[i] in ("Required", "NotUsable") or gt == "selectall"})
                    if visible and i in sel:
                        for k, v in p["flags"].items():
                            new_flags[k.lower()] = v
                sout["groups"].append(gout)
            flags.update(new_flags)
            out.append(sout)
        return {"name": self.name, "steps": out, "needs_you": needs, "flags": flags,
                "module_ok": check(self.module_deps, {}, file_state)}

    def install_list(self, resolved, file_state=None):
        """Files to install for a resolved installer: [(source, dest, is_folder, priority)]."""
        file_state = file_state or (lambda f: "Missing")
        flags = resolved["flags"]
        chosen = []
        for f in self.required:
            chosen.append(f)
        for step, rs in zip(self.steps, resolved["steps"]):
            for g, rg in zip(step["groups"], rs["groups"]):
                for p, ro in zip(g["plugins"], rg["options"]):
                    for f in p["files"]:
                        if (rs["visible"] and ro["selected"]) or f["always"] or (f["if_usable"] and ro["type"] != "NotUsable"):
                            chosen.append(f)
        for cond, files in self.conditional:
            if check(cond, flags, file_state):
                chosen.extend(files)
        seen, uniq = set(), []
        for f in chosen:
            k = (f["source"].lower(), f["dest"].lower(), f["folder"])
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        uniq.sort(key=lambda f: f["priority"])         # stable: higher priority is copied later and wins
        return [(f["source"], f["dest"], f["folder"], f["priority"]) for f in uniq]


def materialize(entries, root):
    """Map the install list onto real files under `root` (the extracted folder holding fomod/).
    Returns ({dest path: source file}, [missing sources]). Source paths are matched ignoring case."""
    index = {}
    for dp, _d, fs in os.walk(root):
        for f in fs:
            full = os.path.join(dp, f)
            index[os.path.relpath(full, root).replace("\\", "/").lower()] = full
    out, missing = {}, []
    for src, dest, is_folder, _prio in entries:
        s = src.lower().strip("/")
        if is_folder:
            pre = (s + "/") if s else ""
            hits = [(k, v) for k, v in index.items() if k.startswith(pre)]
            if not hits:
                missing.append(src)
            for k, v in hits:
                real_rel = os.path.relpath(v, root).replace("\\", "/")[len(pre):] if pre else os.path.relpath(v, root).replace("\\", "/")
                out[(dest + "/" + real_rel).strip("/").lower()] = (((dest + "/") if dest else "") + real_rel, v)
        else:
            v = index.get(s)
            if not v:
                missing.append(src)
                continue
            d = dest if dest and not dest.endswith("/") else (dest + os.path.basename(v))
            out[d.lower()] = (d, v)
    return {d: v for d, v in out.values()}, missing


def find_config(names_list):
    """(path of ModuleConfig.xml, base folder, path of info.xml) inside an archive's file list, or Nones."""
    for n in names_list:
        low = n.lower()
        if low.endswith("fomod/moduleconfig.xml"):
            base = n[:-len("fomod/ModuleConfig.xml")].rstrip("/")
            info = next((m for m in names_list if m.lower() == (base + "/fomod/info.xml").lstrip("/").lower()), None)
            return n, base, info
    return None, None, None
