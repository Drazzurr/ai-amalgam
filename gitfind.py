"""Find Git, even when it isn't on PATH (common on Windows: Git for Windows, Claude Code's Git Bash,
GitHub Desktop's built-in copy)."""
import glob
import os
import shutil
import sys

_cached = None


def find_git():
    global _cached
    if _cached and os.path.isfile(_cached):
        return _cached
    found = shutil.which("git")
    if not found and sys.platform == "win32":
        candidates = []
        bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")     # Claude Code's setting points at bash.exe
        if bash:
            root = os.path.dirname(os.path.dirname(bash))          # ...\Git\bin\bash.exe -> ...\Git
            candidates += [os.path.join(root, "cmd", "git.exe"), os.path.join(root, "bin", "git.exe")]
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("ProgramW6432", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                     os.environ.get("USERPROFILE", "") + r"\scoop\apps\git\current\.."):
            if base:
                candidates += [os.path.join(base, "Git", "cmd", "git.exe"), os.path.join(base, "Git", "bin", "git.exe")]
        candidates += [os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims", "git.exe")]
        candidates += sorted(glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "GitHubDesktop", "app-*",
                                                    "resources", "app", "git", "cmd", "git.exe")), reverse=True)
        found = next((c for c in candidates if c and os.path.isfile(c)), None)
    _cached = found
    return found
