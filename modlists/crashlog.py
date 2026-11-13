"""Read Skyrim crash logs (Crash Logger SSE/AE/VR, Trainwreck, .NET Script Framework) and pull
out what matters: the exception, the modules and files on the stack, the objects involved, and
the usual causes for those clues. The causes are common patterns, not certainties."""

import os
import re
from collections import Counter

SYSTEM = {"kernelbase.dll", "ntdll.dll", "kernel32.dll", "ucrtbase.dll", "msvcp140.dll", "vcruntime140.dll",
          "vcruntime140_1.dll", "user32.dll", "win32u.dll", "gdi32.dll", "combase.dll", "rpcrt4.dll",
          "skse64_loader.exe", "crashlogger.dll", "trainwreck.dll"}
EXTS = r"(?:esp|esm|esl|nif|dds|hkx|pex|bsa|xwm|wav|fuz|tri|swf|dll|ini)"
QUOTED_FILE_RE = re.compile(r'"([^"\r\n]{1,200}?\.' + EXTS + r')"', re.I)
FILE_RE = re.compile(r"(?<![\w.])([\w'()&+\-]+(?:\.[\w'()&+\-]+)*\." + EXTS + r")\b", re.I)
FORM_RE = re.compile(r"\b(?:FormID|Form ID|formid)\s*[:=]?\s*(?:0x)?([0-9A-F]{8})\b", re.I)
MODULE_RE = re.compile(r"\b([\w.\-]+\.(?:dll|exe))\+([0-9A-Fa-f]+)")

# (pattern in the log, likely cause, what to try)
HINTS = [
    (r"hdtSMP64\.dll|hdtSSEPhysics|FSMP", "HDT-SMP / Faster SMP physics",
     "an outfit, hair or body with physics. Update FSMP, and check the armor or NPC named in the log."),
    (r"skee64\.dll", "RaceMenu / NiOverride (skee64.dll)",
     "a RaceMenu overlay, preset or body morph. Make sure RaceMenu matches your game version."),
    (r"BSResourceNiBinaryStream|NiStream|\.nif\b", "a mesh (.nif)",
     "a broken or Skyrim LE mesh. Check the .nif files named in the log; LE meshes need converting "
     "(Cathedral Assets Optimizer) for SE."),
    (r"BSDynamicTriShape|BSGeometry|BSTriShape|BSLightingShader|NiSkinInstance", "mesh/skin rendering",
     "often a body, armor or creature mesh that doesn't match the skeleton or body in use."),
    (r"BGSSaveLoadManager|BGSSaveFormBuffer|SaveLoad", "loading or saving a save game",
     "a mod was removed or changed mid-save, or the save is damaged. Try an older save or a new game."),
    (r"BSScaleform|Scaleform|GFx", "the interface (Scaleform)",
     "a UI mod (SkyUI, menus, HUD) or a .swf that doesn't match the game version."),
    (r"hkb|hkp|Havok|\.hkx\b|BShkb", "animation or physics (Havok)",
     "animations: rerun Nemesis/Pandora after adding or removing animation mods; check .hkx named in the log."),
    (r"BSXAudio2|XAudio", "audio", "a damaged or wrongly formatted sound (.xwm/.wav/.fuz)."),
    (r"nvwgf2umx|nvwgf2um\.dll|atidxx64|amdxx64|igd10", "the graphics driver",
     "graphics driver, ENB/ReShade or very high texture sizes running out of VRAM. Update the driver."),
    (r"d3d11\.dll|d3dcompiler|enbseries|enbhost|ReShade|dxgi\.dll", "ENB / ReShade / Direct3D",
     "the ENB or ReShade version for your game version, or its settings."),
    (r"tbbmalloc\.dll|EngineFixes", "memory allocation (Engine Fixes / tbbmalloc)",
     "SSE Engine Fixes setup: both parts (SKSE plugin and the part-2 DLLs in the game folder) installed for "
     "your game version."),
    (r"EXCEPTION_STACK_OVERFLOW", "a stack overflow", "an endless loop, often animations or a script."),
    (r"Actor|Character|NPC_|TESNPC", "an actor (NPC or creature)",
     "the NPC named under the possible relevant objects: their outfit, body or AI package."),
    (r"TESObjectCELL|TESObjectREFR|BGSTerrain|LoadedCellDB", "loading a location",
     "a mod that edits the cell you were entering. Note where it crashed; check mods touching that area."),
]


def parse(text):
    lines = text.splitlines()
    info = {"exception": "", "crash_at": "", "game_version": "", "stack": [], "files": [], "forms": [],
            "objects": [], "skse_plugins": [], "kind": "unknown"}
    head = "\n".join(lines[:15])
    if "PROBABLE CALL STACK" in text or "Crash Logger" in head or "CrashLoggerSSE" in head:
        info["kind"] = "Crash Logger"
    elif "Trainwreck" in head:
        info["kind"] = "Trainwreck"
    elif "NetScriptFramework" in text[:4000] or "Possible relevant objects" in text:
        info["kind"] = ".NET Script Framework"
    m = re.search(r'(?:Unhandled (?:native )?exception|EXCEPTION)\s*"?([A-Z_]+)"?\s*(?:at|occurred at)\s*(0x[0-9A-F]+)?\s*'
                  r'\(?([\w.\-]+\+[0-9A-F]+)?', text, re.I)
    if m:
        info["exception"] = m.group(1)
        info["crash_at"] = m.group(3) or m.group(2) or ""
    v = re.search(r"(?:Skyrim(?:SE|VR)?|SkyrimSE\.exe|Application)\s*(?:version)?\s*:?\s*v?(1\.\d+\.\d+(?:\.\d+)?)", text)
    if v:
        info["game_version"] = v.group(1)

    section, stack_mods = "", []
    for line in lines:
        s = line.strip()
        up = s.upper()
        if up.startswith(("PROBABLE CALL STACK", "CALL STACK", "STACK:")) or "PROBABLE CALL STACK" in up:
            section = "stack"; continue
        if up.startswith(("POSSIBLE RELEVANT OBJECTS", "REGISTERS")):
            section = "objects"; continue
        if up.startswith("STACK") and section != "stack":
            section = "rawstack"; continue
        if up.startswith(("MODULES", "SKSE PLUGINS", "PLUGINS", "SYSTEM SPECS")):
            section = up.split(":")[0].strip().lower(); continue
        if section == "stack":
            for mod, off in MODULE_RE.findall(s):
                stack_mods.append(mod)
            if len(info["stack"]) < 15 and MODULE_RE.search(s):
                info["stack"].append(s[:200])
        elif section in ("objects", "registers", "rawstack"):
            if any(k in s for k in ("Name:", "File:", "EditorID", "FormID", "Full Name", "Model")):
                if len(info["objects"]) < 40:
                    info["objects"].append(s[:220])
        elif section == "skse plugins":
            n = re.match(r"^([\w.\- ]+\.dll)", s)
            if n:
                info["skse_plugins"].append(n.group(1))
    if not stack_mods:                         # .NET Script Framework style
        stack_mods = [m for m, _ in MODULE_RE.findall(text[:20000])]
    info["stack_modules"] = Counter(m for m in stack_mods if m.lower() not in SYSTEM).most_common(8)
    skip = {"skyrimse.exe", "skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm"}
    relevant_part = text.split("MODULES:")[0] if "MODULES:" in text else text
    files = QUOTED_FILE_RE.findall(relevant_part) + FILE_RE.findall(relevant_part)
    seen = []
    for f in files:
        f = re.split(r"[\\/]", f.strip(" .'"))[-1]
        if f.lower() not in skip and f.lower() not in (x.lower() for x in seen) and len(f) < 120:
            seen.append(f)
    info["files"] = seen[:25]
    info["forms"] = list(dict.fromkeys(FORM_RE.findall(relevant_part)))[:15]
    return info


def hints(text, info):
    out = []
    # the module the crash happened in, and the stack, count most
    focus = " ".join([info.get("crash_at", ""), info.get("exception", "")] + [m for m, _ in info["stack_modules"]]
                     + info["stack"][:8] + info["objects"][:20])
    for pat, what, tip in HINTS:
        if re.search(pat, focus, re.I):
            out.append(f"{what}: {tip}")
    if not out:
        for pat, what, tip in HINTS:
            if re.search(pat, text[:30000], re.I):
                out.append(f"{what} (weaker clue): {tip}")
                if len(out) >= 3:
                    break
    return out[:5]


def report(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"Couldn't open {path}: {e}"
    info = parse(text)
    lines = [f"Crash log: {path} ({info['kind']})"]
    if info["exception"]:
        lines.append(f"Exception: {info['exception']}" + (f" at {info['crash_at']}" if info["crash_at"] else ""))
    if info["game_version"]:
        lines.append(f"Game version in the log: {info['game_version']}")
    if info["stack_modules"]:
        lines.append("Modules on the call stack (besides Windows): " +
                     ", ".join(f"{m} ×{n}" for m, n in info["stack_modules"]))
    if info["stack"]:
        lines.append("Top of the call stack:\n  " + "\n  ".join(info["stack"][:10]))
    if info["objects"]:
        lines.append("Objects involved:\n  " + "\n  ".join(info["objects"][:20]))
    if info["files"]:
        lines.append("Files and plugins named: " + ", ".join(info["files"]))
    if info["forms"]:
        lines.append("Form IDs named: " + ", ".join(info["forms"]))
    h = hints(text, info)
    if h:
        lines.append("Usual causes for these clues (patterns, not proof):\n- " + "\n- ".join(h))
    lines.append("Next: check the plugins/files named above (READ them), compare with the load-order check "
                 "(MODS: check), and ask what the user was doing when it crashed.")
    return "\n".join(lines)
