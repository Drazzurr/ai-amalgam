"""Coding area helpers: the project's code folder, running approved commands,
and checkpoints (a private Git history kept outside your folder) for diffs and undo.

Your own folder is never committed to: checkpoints live in a separate Git
repository under %LOCALAPPDATA%\\AIAmalgam\\checkpoints, so your project's own
.git history, branches and remotes are left alone.
"""

import os
import re
import shutil

from gitfind import find_git
import subprocess
import sys
import threading

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next",
             ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "target", "bin", "obj", ".gradle"}
EXCLUDES = ["node_modules/", "__pycache__/", ".venv/", "venv/", "dist/", "build/", ".next/", "*.pyc",
            ".pytest_cache/", ".mypy_cache/", "target/", "obj/", "*.log", ".DS_Store", "Thumbs.db"]
MAX_FILES_LISTED = 3000
MAX_FILE_VIEW = 400_000          # bytes shown in the file viewer
COMMAND_TIMEOUT = 120            # seconds per approved command
MAX_OUTPUT = 12_000              # characters of command output kept


def check_code_dir(path):
    """Return (normalized path, None) if the folder is OK to use, else (None, reason)."""
    path = (path or "").strip().strip('"')
    if not path:
        return "", None
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return None, "That folder doesn't exist."
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    drive_root = os.path.splitdrive(real)[0] + os.sep
    risky = {home, drive_root, os.path.realpath(os.sep),
             os.path.join(home, "Documents"), os.path.join(home, "Desktop"), os.path.join(home, "Downloads")}
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        if os.environ.get(env):
            risky.add(os.path.realpath(os.environ[env]))
    if real in risky:
        return None, "Pick a folder for just this project, not a whole drive, your home folder or a system folder."
    return real, None


def projects_root():
    """Where Amalgam makes code folders for you: Documents\\AI Amalgam Projects."""
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "AI Amalgam Projects")


def make_code_folder(project_name):
    """Create a new, empty folder named after the project (adding (2), (3)... if it's taken)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", project_name or "").strip().rstrip(". ") or "Project"
    name = re.sub(r"\s+", " ", name)[:60]
    root = projects_root()
    os.makedirs(root, exist_ok=True)
    path, n = os.path.join(root, name), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(root, f"{name} ({n})")
    os.makedirs(path)
    return path


def inside(root, rel):
    """Absolute path of rel inside root, or None if it would leave the folder."""
    full = os.path.realpath(os.path.join(root, rel))
    try:
        if os.path.commonpath([full, os.path.realpath(root)]) != os.path.realpath(root):
            return None
    except ValueError:
        return None
    return full


def list_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
            out.append(rel)
            if len(out) >= MAX_FILES_LISTED:
                return out, True
    return out, False


def read_file(root, rel):
    full = inside(root, rel)
    if not full or not os.path.isfile(full):
        return {"error": "File not found."}
    size = os.path.getsize(full)
    with open(full, "rb") as f:
        data = f.read(MAX_FILE_VIEW)
    if b"\x00" in data[:8000]:
        return {"binary": True, "size": size}
    return {"text": data.decode("utf-8", errors="replace"), "size": size, "cut": size > MAX_FILE_VIEW}


def _kill_tree(proc):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW, timeout=15)
        else:
            proc.kill()
    except Exception:
        pass


def shell_args(command):
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    return ["bash", "-lc", command]


def run_command(command, cwd, env=None, timeout=COMMAND_TIMEOUT, on_proc=None):
    """Run one command in the code folder. Returns (exit code or None, output, timed_out)."""
    try:
        proc = subprocess.Popen(shell_args(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    except OSError as e:
        return None, f"Couldn't start the command: {e}", False
    if on_proc:
        on_proc(proc)
    timer = threading.Timer(timeout, _kill_tree, args=(proc,))
    timer.start()
    try:
        raw = proc.stdout.read()
        proc.wait()
    finally:
        timed_out = not timer.is_alive() and proc.returncode not in (0, None)
        timer.cancel()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)   # strip terminal colors
    if len(text) > MAX_OUTPUT:
        half = MAX_OUTPUT // 2
        text = text[:half] + f"\n\n[... {len(text) - MAX_OUTPUT} characters cut ...]\n\n" + text[-half:]
    return proc.returncode, text.strip(), timed_out


class Checkpoints:
    """A private Git repository per project, pointed at your code folder as its work tree."""

    def __init__(self, data_dir):
        self.root = os.path.join(data_dir, "checkpoints")
        self.git = find_git()
        self._lock = threading.Lock()

    @property
    def available(self):
        return bool(self.git)

    def _git(self, project_id, work_tree, *args, check=True):
        git_dir = os.path.join(self.root, f"project-{project_id}.git")
        env = dict(os.environ, GIT_DIR=git_dir, GIT_WORK_TREE=work_tree,
                   GIT_AUTHOR_NAME="AI Amalgam", GIT_AUTHOR_EMAIL="amalgam@localhost",
                   GIT_COMMITTER_NAME="AI Amalgam", GIT_COMMITTER_EMAIL="amalgam@localhost")
        env.pop("GIT_INDEX_FILE", None)
        if not os.path.isdir(git_dir):
            os.makedirs(self.root, exist_ok=True)
            subprocess.run([self.git, "init", "-q"], env=env, cwd=work_tree, capture_output=True,
                           creationflags=NO_WINDOW, check=True)
            for k, v in (("core.autocrlf", "false"), ("core.quotepath", "false"), ("core.longpaths", "true"),
                         ("gc.auto", "0")):
                subprocess.run([self.git, "config", k, v], env=env, cwd=work_tree, capture_output=True,
                               creationflags=NO_WINDOW)
            os.makedirs(os.path.join(git_dir, "info"), exist_ok=True)
            with open(os.path.join(git_dir, "info", "exclude"), "w", encoding="utf-8") as f:
                f.write("\n".join(EXCLUDES) + "\n")
        r = subprocess.run([self.git, *args], env=env, cwd=work_tree, capture_output=True,
                           creationflags=NO_WINDOW, timeout=120)
        out = r.stdout.decode("utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError((r.stderr.decode("utf-8", errors="replace") or out).strip()[:500])
        return out

    def checkpoint(self, project_id, work_tree, message):
        """Save the folder's current state; returns the checkpoint id (unchanged if nothing changed)."""
        if not self.git:
            return None
        with self._lock:
            self._git(project_id, work_tree, "add", "-A", ".")
            head = self._git(project_id, work_tree, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
            if head and not self._git(project_id, work_tree, "diff", "--cached", "--name-only", "HEAD").strip():
                return head
            self._git(project_id, work_tree, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
            return self._git(project_id, work_tree, "rev-parse", "HEAD").strip()

    def changed_files(self, project_id, work_tree, before, after):
        """[{path, status, added, removed}] between two checkpoints."""
        if not (self.git and before and after) or before == after:
            return []
        with self._lock:
            status = self._git(project_id, work_tree, "diff", "--name-status", "--no-renames", before, after)
            nums = self._git(project_id, work_tree, "diff", "--numstat", "--no-renames", before, after)
        counts = {}
        for line in nums.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                counts[parts[2]] = (parts[0], parts[1])
        files = []
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                a, r = counts.get(parts[1], ("0", "0"))
                files.append({"path": parts[1], "status": {"A": "added", "D": "deleted"}.get(parts[0], "changed"),
                              "added": int(a) if a.isdigit() else 0, "removed": int(r) if r.isdigit() else 0})
        return files

    def patch(self, project_id, work_tree, before, after, max_chars=200_000):
        if not (self.git and before and after) or before == after:
            return ""
        with self._lock:
            text = self._git(project_id, work_tree, "diff", "--no-renames", "--no-color", before, after)
        return text if len(text) <= max_chars else text[:max_chars] + "\n[... diff cut ...]\n"

    def restore(self, project_id, work_tree, target):
        """Put the folder back to how it was at checkpoint `target`. Saves the current state first,
        so an undo can itself be undone. Files the models created since then are removed;
        files ignored by the checkpoints (node_modules etc.) are left alone."""
        current = self.checkpoint(project_id, work_tree, "before undo")
        with self._lock:
            added = self._git(project_id, work_tree, "diff", "--name-only", "--no-renames",
                              "--diff-filter=A", target, current).splitlines()
            for rel in added:
                full = inside(work_tree, rel)
                if full and os.path.isfile(full):
                    os.remove(full)
            self._git(project_id, work_tree, "checkout", target, "--", ".")
        return self.checkpoint(project_id, work_tree, "after undo")
