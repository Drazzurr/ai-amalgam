"""What every game's packager shares: the report, walking a folder (without work files), running
helper tools, and writing the final archive."""

import os
import re
import shutil
import subprocess
import time
import zipfile

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SKIP_DIRS = {".git", "_packages", "node_modules", "__pycache__", ".vscode", ".idea", ".venv", "venv"}
# work files: never part of a release
WORK_EXT = (".blend", ".blend1", ".blend2", ".psd", ".xcf", ".kra", ".spp", ".sbs", ".sbsar", ".ztl", ".max", ".ma",
            ".mb", ".fbx", ".obj", ".dae", ".gltf", ".glb", ".stl", ".py", ".pyc", ".md", ".log", ".bak", ".tmp",
            ".ps1", ".bat", ".sh", ".zip", ".7z", ".rar", ".gitignore", ".gitattributes", ".ds_store", ".cpmodproj")


def new_report(game, label):
    return {"game": game, "label": label, "errors": [], "warnings": [], "notes": [], "files": 0, "bytes": 0,
            "plugins": [], "root": None, "left_out": [], "optional": [], "existing_fomod": False, "steps": [],
            "requires": []}


def safe_name(name, fallback="mod"):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name or "").strip().rstrip(". ") or fallback


def walk(root, skip_dirs=(), keep=None, work_ext=WORK_EXT):
    """(files as (relative path with /, full path), work files left out) under root.
    keep(rel) can veto a file (returns False to leave it out)."""
    files, left = [], []
    skip = {d.lower() for d in SKIP_DIRS | set(skip_dirs)}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in skip and not d.startswith("."))
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if name.lower().endswith(work_ext) or name.startswith(".") or (keep and not keep(rel)):
                left.append(rel)
            else:
                files.append((rel, os.path.join(dirpath, name)))
    return files, left


def total_size(files):
    total = 0
    for _r, f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return total


def case_clashes(rels, rep):
    seen = {}
    for r in rels:
        low = r.lower()
        if low in seen and seen[low] != r:
            rep["errors"].append(f"Two files differ only in capital letters: {seen[low]} and {r} (Windows can't hold both).")
        seen[low] = r


def long_paths(rels, rep, limit=200):
    for r in rels:
        if len(r) > limit:
            rep["warnings"].append(f"{r[:60]}…: a very long path; some tools fail past 260 characters.")
            break


def run_tool(args, timeout=1800, cwd=None):
    """Run a helper program; returns (ok, last lines of its output)."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, f"{os.path.basename(args[0])} wasn't found."
    except subprocess.TimeoutExpired:
        return False, f"{os.path.basename(args[0])} took too long and was stopped."
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    return r.returncode == 0, out[-1500:]


class Stage:
    """A scratch folder for files made while packaging (packed archives, converted files)."""

    def __init__(self, code_dir):
        self.path = os.path.join(code_dir, "_packages", f".build-{int(time.time() * 1000)}")
        os.makedirs(self.path, exist_ok=True)

    def file(self, rel):
        p = os.path.join(self.path, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def done(self):
        shutil.rmtree(self.path, ignore_errors=True)


def write_zip(path, entries, compress=True):
    """entries: [(name inside the archive, file path or bytes)]. Written to .part first, then renamed."""
    tmp = path + ".part"
    seen = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED, compresslevel=6) as z:
        for arc, src in entries:
            arc = arc.replace("\\", "/").lstrip("/")
            if arc.lower() in seen:
                continue
            seen.add(arc.lower())
            if isinstance(src, (bytes, bytearray)):
                z.writestr(arc, src)
            else:
                # already-packed game archives don't shrink: store them as they are (much faster)
                ctype = zipfile.ZIP_STORED if src.lower().endswith((".bsa", ".pak", ".archive", ".bundle", ".cache",
                                                                   ".sfar", ".tfc", ".bik", ".dds", ".png", ".jpg")) \
                    else None
                z.write(src, arc, compress_type=ctype)
    os.replace(tmp, path)
    return path


def version_parts(version, n=4):
    nums = [int(x) for x in re.findall(r"\d+", version or "")][:n]
    return nums + [0] * (n - len(nums))
