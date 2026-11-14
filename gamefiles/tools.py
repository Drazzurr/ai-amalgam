"""Helper programs and libraries for the game-file readers, installed on request into
%LOCALAPPDATA%\\AIAmalgam\\tools, and detection of your game install folders."""

import csv
import glob
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TOOLS = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AIAmalgam", "tools")

GAMES = {
    "Skyrim Special Edition": ["Skyrim Special Edition", "Skyrim Anniversary Edition", "Skyrim"],
    "Baldur's Gate 3": ["Baldurs Gate 3", "Baldur's Gate 3"],
    "Cyberpunk 2077": ["Cyberpunk 2077"],
    "The Witcher 3": ["The Witcher 3", "The Witcher 3 Wild Hunt", "The Witcher 3 Wild Hunt GOTY"],
    "Mass Effect Legendary Edition": ["Mass Effect Legendary Edition"],
    "Mass Effect (original)": ["Mass Effect", "Mass Effect 2", "Mass Effect 3"],
    "Dragon Age: Origins": ["Dragon Age Ultimate Edition", "Dragon Age Origins", "Dragon Age Origins - Ultimate Edition"],
    "Dragon Age II": ["Dragon Age II", "Dragon Age 2"],
    "Dragon Age: Inquisition": ["Dragon Age Inquisition"],
    "Dragon Age: The Veilguard": ["Dragon Age The Veilguard"],
}


def _steam_libraries():
    roots = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            roots.append(os.path.join(base, "Steam"))
    libs = set()
    for root in roots:
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            libs.add(root)
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libs.add(m.group(1).replace("\\\\", "\\"))
    for drive in "CDEFGH":
        for name in ("SteamLibrary", "Steam", "Games\\SteamLibrary"):
            p = f"{drive}:\\{name}"
            if os.path.isdir(os.path.join(p, "steamapps")):
                libs.add(p)
    return [os.path.join(l, "steamapps", "common") for l in libs]


def find_games():
    """{game: folder} for games found in Steam, GOG and EA default locations."""
    if os.name != "nt":
        return {}
    bases = _steam_libraries()
    for drive in "CDE":
        bases += [f"{drive}:\\GOG Games", f"{drive}:\\Program Files (x86)\\GOG Galaxy\\Games",
                  f"{drive}:\\Program Files\\EA Games", f"{drive}:\\Program Files (x86)\\Origin Games",
                  f"{drive}:\\Games"]
    found = {}
    for game, names in GAMES.items():
        for base in bases:
            for n in names:
                p = os.path.join(base, n)
                if os.path.isdir(p) and game not in found:
                    found[game] = p
    return found


# ---------------- installs ----------------

def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "AI-Amalgam", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _install_release(repo, pattern, dest, say):
    info = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))
    assets = [a for a in info.get("assets", []) if re.search(pattern, a["name"], re.I)]
    if not assets:
        raise RuntimeError(f"no matching download in {repo}'s latest release")
    asset = assets[0]
    say(f"Downloading {asset['name']} ({asset['size'] // 1_000_000} MB)…")
    blob = _get(asset["browser_download_url"], timeout=600)
    os.makedirs(dest, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(blob)).extractall(dest)
    with open(os.path.join(dest, "VERSION.txt"), "w") as f:
        f.write(info.get("tag_name", ""))
    say(f"Installed {asset['name']}.")


def install(say=print):
    """Install everything the readers can use. Each part is optional; failures are reported, not fatal."""
    ok = True
    say("Installing Python libraries (lz4, zstandard, python-lzo, py7zr)…")
    for pkg in ("lz4", "zstandard", "python-lzo", "py7zr"):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", pkg],
                           capture_output=True, creationflags=NO_WINDOW)
        say(f"  {pkg}: {'ok' if r.returncode == 0 else 'skipped (' + r.stderr.decode(errors='replace').strip()[-120:] + ')'}")
    for name, repo, pattern, folder in (
            ("LSLib (Baldur's Gate 3 .lsf/.gr2)", "Norbyte/lslib", r"^ExportTool.*\.zip$", "lslib"),
            ("WolvenKit CLI (Cyberpunk 2077)", "WolvenKit/WolvenKit", r"(console|cli).*\.zip$", "wolvenkit")):
        try:
            say(f"{name}:")
            _install_release(repo, pattern, os.path.join(TOOLS, folder), say)
        except Exception as e:
            ok = False
            say(f"  couldn't install: {e}")
    return ok


def installed():
    def exists(folder, names):
        root = os.path.join(TOOLS, folder)
        for dirpath, _d, files in os.walk(root):
            if any(f.lower() in names for f in files):
                return True
        return False
    libs = {}
    for mod in ("lz4", "zstandard", "lzo"):
        try:
            __import__(mod)
            libs[mod] = True
        except ImportError:
            libs[mod] = False
    return {"lslib": exists("lslib", {"divine.exe"}),
            "wolvenkit": exists("wolvenkit", {"wolvenkit.cli.exe", "cp77tools.exe"}), **libs}


# ---------------- Cyberpunk helpers ----------------

def _wolvenkit_exe():
    for dirpath, _d, files in os.walk(os.path.join(TOOLS, "wolvenkit")):
        for f in files:
            if f.lower() in ("wolvenkit.cli.exe", "cp77tools.exe"):
                return os.path.join(dirpath, f)
    return None


def wolvenkit_json(path, out_dir):
    exe = _wolvenkit_exe()
    if not exe:
        return "Converting to JSON needs WolvenKit. Click 'Install helpers' (Apps card → Game files) first."
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run([exe, "convert", "serialize", path, "-o", out_dir], capture_output=True, timeout=300,
                       creationflags=NO_WINDOW)
    hits = sorted(glob.glob(os.path.join(out_dir, "**", os.path.basename(path) + ".json"), recursive=True))
    if not hits:
        return "WolvenKit couldn't convert it:\n" + (r.stdout + r.stderr).decode(errors="replace")[-800:]
    with open(hits[-1], encoding="utf-8", errors="replace") as f:
        text = f.read()
    return f"Converted to {hits[-1]}\n\n" + (text if len(text) < 60000 else text[:60000] + "\n[... cut ...]")


_names = None


def cp77_names():
    """Path names for Cyberpunk archive hashes, from WolvenKit's hash list when installed."""
    global _names
    if _names is not None:
        return _names
    _names = {}
    for dirpath, _d, files in os.walk(os.path.join(TOOLS, "wolvenkit")):
        for f in files:
            if f.lower().startswith("archivehashes") and f.lower().endswith((".csv", ".zip")):
                p = os.path.join(dirpath, f)
                try:
                    if p.endswith(".zip"):
                        z = zipfile.ZipFile(p)
                        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
                    else:
                        text = open(p, encoding="utf-8", errors="replace").read()
                    for row in csv.reader(io.StringIO(text)):
                        if len(row) >= 2 and row[1].strip().isdigit():
                            _names[int(row[1])] = row[0]
                except Exception:
                    pass
    return _names
