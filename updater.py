"""Check GitHub for a newer AI Amalgam and install it, keeping your chats, settings and files.

Updates come from the public repo github.com/Drazzurr/ai-amalgam (main branch).
Your data is never touched: amalgam.db (chats, projects, settings), workspace/ (attachments)
and amalgam.log stay as they are. The replaced program files are backed up first.
"""

import io
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

REPO = "Drazzurr/ai-amalgam"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
KEEP = {"amalgam.db", "amalgam.db-journal", "amalgam.log", "amalgam.log.1", "workspace", "__pycache__", ".git"}
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def current_version():
    try:
        with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def _parse(v):
    parts = []
    for p in str(v).strip().lstrip("v").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "AI-Amalgam-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check():
    """Returns {"current", "latest", "available"} or {"error"}."""
    cur = current_version()
    try:
        latest = _get(VERSION_URL + f"?t={int(time.time())}", timeout=8).decode("utf-8").strip()
    except Exception as e:
        msg = "Couldn't reach GitHub. Check the internet connection and try again."
        if os.path.isfile(os.path.expanduser("~/AIAmalgam/phone.json")):
            msg = ("Couldn't reach GitHub. If your phone just switched between Wi-Fi and mobile data, close the "
                   "Terminal app fully and open it again, then try again.")
        return {"current": cur, "error": msg}
    return {"current": cur, "latest": latest, "available": _parse(latest) > _parse(cur)}


def install():
    """Download the newest version and copy it over the program files. Returns the new version."""
    data = _get(ZIP_URL, timeout=60)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if not names:
        raise RuntimeError("The download was empty.")
    top = names[0].split("/")[0] + "/"          # e.g. "ai-amalgam-main/"
    if top + "app.py" not in names:
        raise RuntimeError("The download doesn't look like AI Amalgam.")

    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    backup = os.path.join(base, "AIAmalgam", "backups", f"{current_version()}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(backup, exist_ok=True)
    added = []

    for name in names:
        rel = name[len(top):]
        if not rel or name.endswith("/"):
            continue
        if rel.split("/")[0] in KEEP:
            continue
        dest = os.path.join(APP_DIR, *rel.split("/"))
        new = zf.read(name)
        if os.path.exists(dest):
            try:
                with open(dest, "rb") as f:
                    if f.read() == new:
                        continue                    # unchanged: nothing to replace or back up
            except OSError:
                pass
            b = os.path.join(backup, *rel.split("/"))   # keep a copy of what we replace
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dest, b)
        else:
            added.append(rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as out:
            out.write(new)
    with open(os.path.join(backup, ".added-files"), "w", encoding="utf-8") as f:
        f.write("\n".join(added))
    prune_backups()
    return current_version()


def prune_backups(keep=5):
    """Old update backups pile up (one per update): keep the newest few."""
    root = _backups_root()
    try:
        dirs = sorted((os.path.join(root, d) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
                      key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)


def _backups_root():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AIAmalgam", "backups")


def last_backup():
    """(folder, version) of the most recent update backup that can be restored, or (None, None)."""
    root = _backups_root()
    try:
        dirs = [os.path.join(root, d) for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d)) and not d.endswith("-restored")]
    except OSError:
        return None, None
    # newer backups hold only the files an update changed, plus the list of files it added
    dirs = [d for d in dirs if os.path.isfile(os.path.join(d, ".added-files")) or
            os.path.isfile(os.path.join(d, "app.py"))]
    if not dirs:
        return None, None
    newest = max(dirs, key=os.path.getmtime)
    return newest, os.path.basename(newest).split("-")[0]


def rollback():
    """Put back the program files from before the last update. Your chats and settings are untouched."""
    folder, version = last_backup()
    if not folder:
        raise RuntimeError("There's no earlier version saved to go back to.")
    try:
        with open(os.path.join(folder, ".added-files"), encoding="utf-8") as f:
            added = [l for l in f.read().splitlines() if l.strip()]
    except OSError:
        added = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            if name == ".added-files":
                continue
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, folder)
            dest = os.path.join(APP_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
    for rel in added:   # files that the update added and the old version didn't have
        if rel.split("/")[0] in KEEP:
            continue
        path = os.path.join(APP_DIR, *rel.split("/"))
        try:
            os.remove(path)
        except OSError:
            pass
    os.rename(folder, folder + "-restored")
    return version


def restart_later(args, delay=3):
    """Start the app again a few seconds after this one has closed."""
    cmd = [sys.executable, "-c",
           f"import time, subprocess; time.sleep({delay}); "
           f"subprocess.Popen({[sys.executable, os.path.join(APP_DIR, 'app.py')] + list(args)!r}, "
           f"cwd={APP_DIR!r}, creationflags={NO_WINDOW})"]
    subprocess.Popen(cmd, cwd=APP_DIR, creationflags=NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
