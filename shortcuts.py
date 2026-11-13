"""Slash shortcuts: type /balance, /lore-check ... at the start of a message.

Your message is saved as you typed it. When the models read the chat, the shortcut's instructions
are added right after it, so they know exactly what /balance means. You can add your own shortcuts
(Info → Shortcuts); they follow you between devices with sync.
"""

import json
import re

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
SETTING = "shortcuts_custom"

BUILTIN = [
    {"name": "balance", "description": "Balance-check a monster, item, spell, class or mod content",
     "only": "",
     "instructions": "Use the rules-and-balance skill. Balance-check what the user gave. Use the rules system they "
                     "named; if they didn't name one, assume D&D 5e and say so. Compare it with official content of "
                     "the same level or tier and show the key numbers side by side. Flag what's over- or under-tuned "
                     "and why. Finish with a corrected version that's ready to use. Each of you checks it on your "
                     "own first, then agree on one final version."},
    {"name": "lore-check", "description": "Check text against the World Bible for contradictions",
     "only": "",
     "instructions": "Use the world-bible-keeper skill. Check what the user gave against the World Bible, the "
                     "project document and project memory. Look for contradictions, timeline or geography problems, "
                     "names spelled differently, and open Q-numbered decisions it depends on. Quote the exact "
                     "passages that conflict. List the issues from most to least serious, then suggest fixes. "
                     "Don't edit any document."},
    {"name": "style-match", "description": "Study an art style and write a prompt that reproduces it",
     "only": "",
     "instructions": "Use the art-director skill. Study the style of the images or references the user gave "
                     "(attached files or ones they name): palette, line work, shading, lighting, composition, "
                     "texture and recurring motifs. Write a short style guide and one ready-to-use image prompt "
                     "that reproduces the style. GPT: generate one sample image with that prompt so the group can "
                     "compare it with the references."},
    {"name": "write-like", "description": "Write something in the style of a work or author",
     "only": "",
     "instructions": "Use the writing-style-analyst skill. First, briefly analyse the style of the work or author "
                     "the user named: voice, sentence rhythm, word choice, structure, themes and clichés to avoid. "
                     "Then write what they asked for in that style, and say which choices make it match."},
    {"name": "study-model", "description": "Inspect a 3D model file for a mod, print or animation",
     "only": "",
     "instructions": "Use the 3d-model-study skill. Inspect the 3D model file(s) the user attached or named: "
                     "structure, mesh and vertex counts, materials and textures, rig and animation, and scale. List "
                     "any problems for the use they stated (game mod, 3D print or animation), then give concrete "
                     "next steps."},
    {"name": "read-game", "description": "Look inside a game file or archive",
     "only": "",
     "instructions": "Use the game-files skill. Read the game file or archive the user named with READ lines and "
                     "report what's inside that matters for their goal. Go deeper (list, extract, records) where it "
                     "helps."},
    {"name": "brainstorm", "description": "Both models propose ideas, then pick the best together",
     "only": "",
     "instructions": "Each of you independently proposes 5 distinct ideas for what the user asked, with no overlap "
                     "with your teammate's. Then pick the 3 strongest together and explain why they beat the rest."},
]


def custom(store):
    try:
        items = json.loads(store.get_setting(SETTING, "[]") or "[]")
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and NAME_RE.match(str(it.get("name", ""))) and str(it.get("instructions", "")).strip():
            out.append({"name": it["name"], "description": str(it.get("description", ""))[:120],
                        "instructions": str(it["instructions"])[:4000],
                        "only": it.get("only") if it.get("only") in ("claude", "gpt") else "", "custom": True})
    return out


def all_shortcuts(store):
    """Your own shortcuts first (they win over a built-in with the same name), then the built-in ones."""
    mine = custom(store)
    names = {s["name"] for s in mine}
    return mine + [dict(s, custom=False) for s in BUILTIN if s["name"] not in names]


def save_custom(store, items):
    """Validate and save your shortcuts. Returns an error message or None."""
    clean, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip().lstrip("/").lower()
        if not NAME_RE.match(name):
            return f"“{name or '(empty)'}” can't be a shortcut name: use letters, numbers and dashes (up to 24)."
        if name in seen:
            return f"There are two shortcuts named /{name}."
        if not str(it.get("instructions", "")).strip():
            return f"/{name} needs instructions."
        seen.add(name)
        clean.append({"name": name, "description": str(it.get("description", "")).strip()[:120],
                      "instructions": str(it.get("instructions", "")).strip()[:4000],
                      "only": it.get("only") if it.get("only") in ("claude", "gpt") else ""})
    store.set_setting(SETTING, json.dumps(clean[:50]))
    return None


def match(text, store):
    """The shortcut a message starts with (/name ...), or None."""
    m = re.match(r"^\s*/([a-z0-9][a-z0-9-]*)(?=\s|$)", text or "", re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).lower()
    return next((s for s in all_shortcuts(store) if s["name"] == name), None)
