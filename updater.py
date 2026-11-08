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
KEEP = {"amalgam.db", "amalgam.db-journal", "amalgam.log", "workspace", "__pycache__", ".git"}
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
        return {"current": cur, "error": f"Couldn't reach GitHub ({e.__class__.__name__})."}
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

    for name in names:
        rel = name[len(top):]
        if not rel or name.endswith("/"):
            continue
        if rel.split("/")[0] in KEEP:
            continue
        dest = os.path.join(APP_DIR, *rel.split("/"))
        if os.path.exists(dest):                    # keep a copy of what we replace
            b = os.path.join(backup, *rel.split("/"))
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dest, b)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(name) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    return current_version()


def restart_later(args, delay=3):
    """Start the app again a few seconds after this one has closed."""
    cmd = [sys.executable, "-c",
           f"import time, subprocess; time.sleep({delay}); "
           f"subprocess.Popen({[sys.executable, os.path.join(APP_DIR, 'app.py')] + list(args)!r}, "
           f"cwd={APP_DIR!r}, creationflags={NO_WINDOW})"]
    subprocess.Popen(cmd, cwd=APP_DIR, creationflags=NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
