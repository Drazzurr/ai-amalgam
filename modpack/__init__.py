"""Package mods for Skyrim, Baldur's Gate 3, Cyberpunk 2077, Mass Effect Legendary Edition and
The Witcher 3: find out which game a folder is for, check it for that game's usual mistakes, do the
processing it needs (packing the game's archive format, converting files, writing the files mod
managers install from) and build a .zip that the game's usual mod manager installs.

    check(code_dir, opts)  -> report (what's in it, problems, warnings, notes, what packaging will do)
    build(code_dir, opts)  -> (path of the .zip or None, report)

opts: target ("auto" or a game key), name, version, author, description, url, plus per game:
edition (Skyrim "se"/"le"), bsa, compress, fomod, include_source (Skyrim), me_game ("LE1"/"LE2"/"LE3").
"""

import os
import time

from . import bg3, cyberpunk, masseffect, skyrim, witcher3
from .common import Stage, safe_name, write_zip

GAMES = {"skyrim": skyrim, "bg3": bg3, "cyberpunk": cyberpunk, "masseffect": masseffect, "witcher3": witcher3}
LABELS = {"skyrim": "Skyrim", "bg3": "Baldur's Gate 3", "cyberpunk": "Cyberpunk 2077",
          "masseffect": "Mass Effect Legendary Edition", "witcher3": "The Witcher 3"}


def detect(code_dir):
    """(best game key or None, {game: score})."""
    scores = {}
    for key, mod in GAMES.items():
        try:
            scores[key] = mod.detect(code_dir)
        except OSError:
            scores[key] = 0
    best = max(scores, key=scores.get)
    return (best if scores[best] > 0 else None), scores


def _target(code_dir, opts):
    t = (opts.get("target") or "auto").lower()
    if t in GAMES:
        return t, None
    if (opts.get("game") or "").lower() in ("se", "le"):      # older callers: Skyrim edition
        return "skyrim", None
    best, scores = detect(code_dir)
    return best, scores


def check(code_dir, opts):
    key, scores = _target(code_dir, opts)
    if not key:
        return {"game": None, "label": "", "errors": [
            "Couldn't tell which game this mod is for. Pick the game, or lay the files out the game's way "
            "(e.g. Skyrim: a plugin and meshes/textures; BG3: Mods/<Name>/meta.lsx; Cyberpunk: archive/pc/mod or r6; "
            "Mass Effect: a DLC_MOD_<Name> folder; Witcher 3: mods/mod<Name>/content)."],
            "warnings": [], "notes": [], "files": 0, "bytes": 0, "plugins": [], "root": None, "left_out": [],
            "optional": [], "steps": [], "requires": [], "detected": None, "games": LABELS}
    rep = GAMES[key].check(code_dir, opts)
    rep["detected"] = key if scores is not None else None
    rep["games"] = LABELS
    rep["warnings"] = rep["warnings"][:60]
    return rep


def build(code_dir, opts):
    """Check, process and package. Returns (zip path or None, report)."""
    rep = check(code_dir, opts)
    if rep["errors"]:
        return None, rep
    key = rep["game"]
    stage = Stage(code_dir)
    try:
        entries = GAMES[key].prepare(code_dir, opts, rep, stage)
        if not entries:
            rep["errors"].append("Nothing to package.")
            return None, rep
        base = opts.get("name") or (rep["plugins"][0]["name"] if rep.get("plugins") else "") or os.path.basename(
            code_dir.rstrip("/\\"))
        base = safe_name(os.path.splitext(base)[0] if base.lower().endswith((".esp", ".esm", ".esl")) else base)
        version = safe_name((opts.get("version") or "").strip()[:20], "")
        out_dir = os.path.join(code_dir, "_packages")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{base} {version}".strip() + ".zip")
        write_zip(path, entries)
    finally:
        stage.done()
    rep["path"] = path
    rep["package_bytes"] = os.path.getsize(path)
    rep["built_at"] = time.time()
    rep["files_in_package"] = len(entries)
    return path, rep


def summary(rep):
    """A short note for the chat, so the models know how packaging went."""
    game = rep.get("label") or "mod"
    if rep.get("path"):
        head = (f"Packaged {os.path.basename(rep['path'])} for {game}: {rep.get('files_in_package', rep['files'])} files, "
                f"{rep['package_bytes'] / 1e6:.1f} MB, in {os.path.dirname(rep['path'])}.")
    else:
        head = f"Mod check ({game}): " + (f"{len(rep['errors'])} problem(s) stop packaging." if rep["errors"] else
                                          f"{rep['files']} files ready to package.")
    parts = [head] + [f"Done: {s}" for s in rep.get("steps", [])] + \
        ([f"Players need: {', '.join(rep['requires'])}"] if rep.get("requires") else []) + [f"Problem: {e}" for e in rep["errors"]] + \
        [f"Warning: {w}" for w in rep["warnings"][:15]] + [f"Note: {n}" for n in rep["notes"]]
    if len(rep["warnings"]) > 15:
        parts.append(f"(and {len(rep['warnings']) - 15} more warnings)")
    return "\n".join(parts)
