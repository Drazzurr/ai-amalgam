"""Keep chats, projects, settings and code folders the same on your PC and your phone.

Everything goes through a PRIVATE GitHub repository of yours (for example Drazzurr/amalgam-sync),
using a key (fine-grained token) that can only touch that one repository.

How a sync works (it never loses anything):
  1. Download the latest copy from GitHub.
  2. Merge it with this device:
       - messages are only ever added (each has an id that is the same on every device),
         so both devices' messages are kept, in time order;
       - deleted chats / messages / projects are remembered, so a delete on one device
         deletes on the other too;
       - titles, project instructions and settings: the most recent change wins;
       - project memory: lines from both devices are kept;
       - code files: a file changed on only one device takes that change; a file changed on
         BOTH devices keeps the other device's version and saves yours next to it as
         "name (conflict from PC).ext", so nothing is overwritten.
  3. Upload the merged copy. If the other device uploaded in the meantime, start again at 1.

The key is stored only on this device (sync/key next to Amalgam's other data), never in the repository.
"""

import base64
import hashlib
import json
import os
import re
import shutil

from gitfind import find_git
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
FORMAT = 1
MAX_SYNC_FILE = 25_000_000          # bytes; bigger files stay on their own device
CODE_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next",
                  ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "target", "obj", ".gradle"}
CODE_SKIP_EXT = (".pyc", ".log")
# settings that follow you between devices (others, like notifications, are per device)
GLOBAL_SETTINGS = ("max_rounds", "check_questions", "speed_claude", "speed_codex", "skills_off")
PROJECT_SETTINGS = ("speed_claude_project_{}", "speed_codex_project_{}", "skills_off_project_{}",
                    "allowed_cmds_project_{}", "allow_edits_project_{}")
MAX_CODE_FOLDER = 300_000_000      # bytes; bigger code folders aren't synced (GitHub isn't for huge assets)
AUTO_EVERY = 120                    # seconds between automatic syncs while nothing is running
AUTO_EVERY_AWAY = 600               # ...and while you're not using AI Amalgam (saves the phone's battery)


def _sha(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(".") or "file"
    return name[:120]


class SyncError(Exception):
    pass


class Sync:
    def __init__(self, store, data_dir, workspace, device_name):
        self.store = store
        self.root = os.path.join(data_dir, "sync")
        self.repo_dir = os.path.join(self.root, "repo")
        self.base_dir = os.path.join(self.root, "base")       # last-synced code file hashes, per project
        self.key_file = os.path.join(self.root, "key")
        self.workspace = workspace
        self.device = device_name
        self.git = find_git()
        self._lock = threading.Lock()
        self.state = {"state": "off", "last_ok": 0, "error": "", "busy": False, "changed": 0,
                      "conflicts": [], "notes": [], "who": ""}
        self.rev = 0                  # bumps whenever a sync changed something here (the UI reloads)
        self.busy_check = lambda: False   # set by the app: True while the models are working
        self.away_check = lambda: False   # set by the app: True while nobody is looking at AI Amalgam
        self.busy_ids = lambda: (None, None)   # set by the app: (chat id, project id) being worked on
        self.on_imported = None       # set by the app: called with the set of local chat ids changed
        self.on_network_error = None  # set by the app (phone): check and repair the connection
        self._wake = threading.Event()
        self._hashes = {}             # path -> (size, mtime, sha1): unchanged files aren't re-read
        os.makedirs(self.root, exist_ok=True)
        if self.configured():
            self.state["state"] = "idle"
            self.state["who"] = self.store.get_setting("sync_repo", "")

    # ----- setup -----

    def configured(self):
        return bool(self.store.get_setting("sync_repo") and os.path.isfile(self.key_file))

    def _key(self):
        try:
            with open(self.key_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _api(self, key, path):
        req = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json",
            "User-Agent": "AI-Amalgam-sync", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, {}, dict(e.headers or {})
        except Exception as e:
            raise SyncError(f"Couldn't reach GitHub ({e.__class__.__name__}). Check the internet connection.")

    def connect(self, key, repo_name="amalgam-sync"):
        """Check the key, find the private repo, and save both. Returns "owner/name"."""
        key = (key or "").strip()
        repo_name = (repo_name or "amalgam-sync").strip().split("/")[-1] or "amalgam-sync"
        if not re.fullmatch(r"(github_pat_|ghp_|gho_)[A-Za-z0-9_]{20,}", key):
            raise SyncError("That doesn't look like a GitHub key. It starts with github_pat_ — copy the whole thing.")
        code, me, _ = self._api(key, "/user")
        if code == 401:
            raise SyncError("GitHub didn't accept that key. Make a new one with the \"Get my key\" button.")
        if code != 200 or not me.get("login"):
            raise SyncError(f"GitHub answered {code} when checking the key. Try again in a minute.")
        full = f"{me['login']}/{repo_name}"
        code, repo, _ = self._api(key, f"/repos/{full}")
        if code == 404:
            raise SyncError(f"The key can't see {full}. Make the sync space first, and when making the key choose "
                            f"\"Only select repositories\" → {repo_name}.")
        if code != 200:
            raise SyncError(f"GitHub answered {code} when opening {full}.")
        if not repo.get("private"):
            raise SyncError(f"{full} is PUBLIC, so anyone could read your chats. Make it private on GitHub "
                            "(Settings → Danger zone → Change visibility), then try again.")
        perms = repo.get("permissions") or {}
        if perms and not perms.get("push"):
            raise SyncError("The key can read the sync space but not save to it. Give it Contents: Read and write.")
        os.makedirs(self.root, exist_ok=True)
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass
        if self.store.get_setting("sync_repo") != full:
            shutil.rmtree(self.repo_dir, ignore_errors=True)
            shutil.rmtree(self.base_dir, ignore_errors=True)
        self.store.set_setting("sync_repo", full)
        self.state.update(state="idle", error="", who=full)
        self.request()
        return full

    def disconnect(self):
        try:
            os.remove(self.key_file)
        except OSError:
            pass
        self.store.set_setting("sync_repo", "")
        shutil.rmtree(self.repo_dir, ignore_errors=True)
        shutil.rmtree(self.base_dir, ignore_errors=True)
        self.state.update(state="off", error="", who="", last_ok=0)

    def view(self):
        return dict(self.state, device=self.device, rev=self.rev, configured=self.configured())

    # ----- background loop -----

    def request(self):
        """Sync soon (after a reply finishes, when a window comes back, or from the Sync now button)."""
        self._wake.set()

    def loop(self, quit_event):
        last = 0
        while not quit_event.is_set():
            woke = self._wake.wait(5)
            if quit_event.is_set():
                return
            every = AUTO_EVERY_AWAY if self.away_check() else AUTO_EVERY
            due = woke or time.time() - last > every
            if not due or not self.configured() or self.busy_check():
                continue
            self._wake.clear()
            last = time.time()
            self.sync_once()

    def sync_once(self):
        if not self.configured():
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self.state.update(busy=True, state="syncing")
        try:
            for attempt in range(4):
                if self._run_once():
                    break
                time.sleep(1 + attempt * 2)     # the other device uploaded meanwhile: merge again
            else:
                raise SyncError("The other device kept uploading at the same moment. Will try again shortly.")
            self.state.update(state="idle", error="", last_ok=time.time())
            return True
        except SyncError as e:
            self.state.update(state="error", error=str(e))
            if self.on_network_error and any(w in str(e) for w in ("internet", "reach", "too long")):
                self.on_network_error()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.state.update(state="error", error=f"Sync hit a problem: {e}"[:300])
        finally:
            self.state["busy"] = False
            self._lock.release()
        return False

    # ----- git plumbing -----

    def _git_cmd(self, *args, check=True, timeout=180):
        if not self.git:
            self.git = find_git()       # installed since the app started?
        if not self.git:
            raise SyncError("Git isn't installed on this device, and sync needs it. "
                            "On the PC, open PowerShell and run: winget install Git.Git  "
                            "(then press Sync now). On the phone: run 'amalgam repair'.")
        auth = base64.b64encode(f"x-access-token:{self._key()}".encode()).decode()
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="AI Amalgam",
                   GIT_AUTHOR_EMAIL="amalgam@localhost", GIT_COMMITTER_NAME="AI Amalgam",
                   GIT_COMMITTER_EMAIL="amalgam@localhost")
        cmd = [self.git, "-c", f"http.extraHeader=Authorization: Basic {auth}", "-c", "core.autocrlf=false",
               "-c", "core.quotepath=false", "-c", "core.longpaths=true", *args]
        try:
            r = subprocess.run(cmd, cwd=self.repo_dir, env=env, capture_output=True, timeout=timeout,
                               creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise SyncError("GitHub is taking too long to answer. Will try again shortly.")
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        if check and r.returncode != 0:
            low = out.lower()
            if "could not resolve host" in low or "unable to access" in low and "resolve" in low:
                raise SyncError("No internet connection right now. Will try again shortly.")
            if "403" in low or "401" in low or "authentication failed" in low or "permission" in low:
                raise SyncError("GitHub refused the key. It may have been deleted or changed. "
                                "Make a new one with \"Get my key\" and connect again.")
            raise SyncError("Git: " + out.strip().splitlines()[-1][:250] if out.strip() else "Git failed.")
        return r.returncode, out

    def _prepare(self):
        """Make the local copy match what's on GitHub right now. Returns True if GitHub has anything."""
        repo = self.store.get_setting("sync_repo")
        url = repo if repo.startswith(("file://", "/")) else f"https://github.com/{repo}.git"   # paths: tests
        if not os.path.isdir(os.path.join(self.repo_dir, ".git")):
            shutil.rmtree(self.repo_dir, ignore_errors=True)
            os.makedirs(self.repo_dir, exist_ok=True)
            self._git_cmd("init", "-q")
            self._git_cmd("remote", "add", "origin", url)
        else:
            self._git_cmd("remote", "set-url", "origin", url)
        code, out = self._git_cmd("ls-remote", "--heads", "origin", "main", check=True)
        if "refs/heads/main" not in out:
            # brand-new, empty sync space: start from an empty folder
            self._git_cmd("checkout", "-q", "-B", "main", check=False)
            for name in os.listdir(self.repo_dir):
                if name != ".git":
                    p = os.path.join(self.repo_dir, name)
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            return False
        self._git_cmd("fetch", "-q", "--depth", "1", "origin", "main")
        self._git_cmd("checkout", "-q", "-B", "main", "FETCH_HEAD")
        self._git_cmd("reset", "-q", "--hard", "FETCH_HEAD")
        self._git_cmd("clean", "-q", "-fdx")
        return True

    def _run_once(self):
        """One fetch → merge → upload. Returns False if the upload was beaten by the other device."""
        self._pending_bases, self._pending_mem = {}, {}
        had_remote = self._prepare()
        fmt = _read_json(os.path.join(self.repo_dir, "format.json"), {})
        if fmt.get("format", FORMAT) > FORMAT:
            raise SyncError("The other device has a newer AI Amalgam. Update this one to keep syncing.")
        self.state["conflicts"], self.state["notes"] = [], []
        changed_here = self._merge()
        _write_json(os.path.join(self.repo_dir, "format.json"), {"format": FORMAT})
        self._git_cmd("add", "-A", ".")
        code, out = self._git_cmd("status", "--porcelain")
        if out.strip():
            self._git_cmd("commit", "-q", "-m", f"Sync from {self.device} {time.strftime('%Y-%m-%d %H:%M')}")
            code, out = self._git_cmd("push", "-q", "origin", "HEAD:main", check=False)
            if code != 0:
                low = out.lower()
                if "rejected" in low or "fetch first" in low or "non-fast-forward" in low:
                    return False
                if "403" in low or "401" in low or "denied" in low:
                    raise SyncError("GitHub refused to save with this key. It needs Contents: Read and write "
                                    "on the sync space.")
                raise SyncError("Couldn't upload: " + (out.strip().splitlines() or ["unknown error"])[-1][:200])
        self.state["changed"] = changed_here
        if changed_here:
            self.rev += 1
        self._commit_bases()
        return True

    # ----- merging -----

    def _merge(self):
        """Merge the GitHub copy (in repo_dir) and this device; write the result to both.
        Returns how many things changed on this device."""
        R = self.repo_dir
        changed = 0
        tomb_path = os.path.join(R, "tombstones.json")
        remote_tombs = _read_json(tomb_path, {})
        local_tombs = self.store.sync_tombstones()
        tombs = dict(remote_tombs)
        for uid, t in local_tombs.items():
            if uid not in tombs or t["at"] > tombs[uid]["at"]:
                tombs[uid] = t
        changed += self.store.sync_apply_tombstones(tombs)

        # projects first (chats refer to them)
        proj_dir = os.path.join(R, "projects")
        remote_projects = {}
        if os.path.isdir(proj_dir):
            for n in os.listdir(proj_dir):
                if n.endswith(".json"):
                    d = _read_json(os.path.join(proj_dir, n), None)
                    if d and d.get("uid") and d["uid"] not in tombs:
                        remote_projects[d["uid"]] = d
                    elif d and d.get("uid") in tombs:
                        os.remove(os.path.join(proj_dir, n))
        local_projects = self.store.sync_projects()
        for uid in set(remote_projects) | set(local_projects):
            if uid in tombs:
                continue
            r, l = remote_projects.get(uid), local_projects.get(uid)
            merged = self._merge_project(r, l)
            if merged != l:
                self.store.sync_put_project(merged)
                changed += 1
            out = {k: v for k, v in merged.items() if k not in ("id", "code_dir")}
            if r != out:
                _write_json(os.path.join(proj_dir, uid + ".json"), out)

        # chats
        chat_dir = os.path.join(R, "chats")
        remote_chats = {}
        if os.path.isdir(chat_dir):
            for n in os.listdir(chat_dir):
                if n.endswith(".json"):
                    uid = n[:-5]
                    if uid in tombs:
                        os.remove(os.path.join(chat_dir, n))
                        shutil.rmtree(os.path.join(R, "files", uid), ignore_errors=True)
                    else:
                        remote_chats[uid] = os.path.join(chat_dir, n)
        local_chats = self.store.sync_chat_index()      # uid -> (local id, updated, edited)
        busy_chat = self.store.uid_of("chats", self.busy_ids()[0]) if self.busy_check() else None
        touched = set()
        for uid in set(remote_chats) | set(local_chats):
            if uid in tombs:
                continue
            r = _read_json(remote_chats[uid], None) if uid in remote_chats else None
            l = self.store.sync_chat(uid, self.workspace) if uid in local_chats else None
            merged, local_needs = self._merge_chat(r, l, tombs)
            # the files that go with the chat (attachments, pictures)
            self._export_files(uid, merged, l)
            if local_needs and uid != busy_chat:
                cid = self.store.sync_put_chat(merged, self._import_file(uid))
                touched.add(cid)
                changed += 1
            clean = self._strip_local(merged)
            if r != clean:
                _write_json(os.path.join(chat_dir, uid + ".json"), clean)

        # settings
        changed += self._merge_settings(tombs)

        # code folders
        changed += self._merge_code(tombs)

        _write_json(tomb_path, tombs)
        if touched and self.on_imported:
            try:
                self.on_imported(touched)
            except Exception as e:
                print("Sync: refresh after import failed:", e)
        return changed

    def _merge_project(self, r, l):
        if not r:
            return l
        if not l:
            return dict(r, id=None, code_dir="")
        newer = r if r.get("edited", 0) > l.get("edited", 0) else l
        m = dict(newer)
        m["id"], m["code_dir"] = l.get("id"), l.get("code_dir", "")
        m["memory"] = self._merge_memory(r["uid"], r.get("memory") or "", l.get("memory") or "",
                                         first=newer is r)
        m["created"] = min(r.get("created", 0) or 0, l.get("created", 0) or 0) or l.get("created", 0)
        return m

    def _merge_memory(self, uid, rmem, lmem, first):
        """Lines added on either device are kept; a line removed on either device stays removed."""
        base = set(_read_json(os.path.join(self.base_dir, f"mem-{uid}.json"), []))
        rl = [x for x in rmem.splitlines() if x.strip()]
        ll = [x for x in lmem.splitlines() if x.strip()]
        order = (rl + ll) if first else (ll + rl)
        rs, ls = set(rl), set(ll)
        out = []
        for x in order:
            if x in out:
                continue
            if x in base and (x not in rs or x not in ls):
                continue
            out.append(x)
        self._pending_mem = getattr(self, "_pending_mem", {})
        self._pending_mem[uid] = out
        return "\n".join(out)

    def _merge_chat(self, r, l, tombs):
        """Returns (merged chat dict, does this device need updating)."""
        if r and not l:
            r = dict(r)
            r["messages"] = [m for m in r.get("messages", []) if m["uid"] not in tombs]
            r["attachments"] = [a for a in r.get("attachments", []) if a["uid"] not in tombs]
            return r, True
        if l and not r:
            return l, False
        meta_src = r if r.get("edited", 0) > l.get("edited", 0) else l
        m = {k: v for k, v in meta_src.items() if k not in ("messages", "attachments")}
        msgs = {x["uid"]: x for x in r.get("messages", []) if x["uid"] not in tombs}
        for x in l.get("messages", []):
            msgs.setdefault(x["uid"], x)
        m["messages"] = sorted(msgs.values(), key=lambda x: (x["created"], x["uid"]))
        atts = {a["uid"]: a for a in r.get("attachments", []) if a["uid"] not in tombs}
        for a in l.get("attachments", []):
            atts.setdefault(a["uid"], a)
        m["attachments"] = sorted(atts.values(), key=lambda a: (a.get("created", 0), a["uid"]))
        m["updated"] = max(r.get("updated", 0), l.get("updated", 0))
        m["summary"], m["summary_upto_uid"] = l.get("summary", ""), l.get("summary_upto_uid", "")
        local_uids = [x["uid"] for x in l.get("messages", [])]
        merged_uids = [x["uid"] for x in m["messages"]]
        need = (merged_uids != local_uids or meta_src is r and any(
            m.get(k) != l.get(k) for k in ("title", "project_uid", "doc_link", "allow_edits", "models_off"))
            or {a["uid"] for a in m["attachments"]} != {a["uid"] for a in l.get("attachments", [])})
        return m, need

    @staticmethod
    def _strip_local(chat):
        """What goes to GitHub: no device paths, no condensed summary (each device makes its own)."""
        out = {k: v for k, v in chat.items() if k not in ("id", "summary", "summary_upto_uid")}
        out["messages"] = [{k: v for k, v in x.items() if k not in ("id", "local_path")} for x in chat["messages"]]
        out["attachments"] = [{k: v for k, v in a.items() if k not in ("id", "local_path")}
                              for a in chat["attachments"]]
        return out

    def _export_files(self, chat_uid, merged, local):
        """Copy this device's attachments and pictures for the chat into the sync copy."""
        if not local:
            return
        folder = os.path.join(self.repo_dir, "files", chat_uid)
        local_atts = {a["uid"]: a for a in local.get("attachments", [])}
        for a in merged["attachments"]:
            la = local_atts.get(a["uid"])
            if la and la.get("local_path") and not a.get("file"):
                src = la["local_path"]
                if os.path.isfile(src) and os.path.getsize(src) <= MAX_SYNC_FILE:
                    a["file"] = f"{a['uid'][:8]}-{_safe_name(a['name'])}"
                    la["file"] = a["file"]
            if a.get("file") and la and la.get("local_path") and os.path.isfile(la["local_path"]):
                dest = os.path.join(folder, a["file"])
                if not os.path.isfile(dest):
                    os.makedirs(folder, exist_ok=True)
                    shutil.copy2(la["local_path"], dest)
        local_msgs = {x["uid"]: x for x in local.get("messages", [])}
        for x in merged["messages"]:
            lx = local_msgs.get(x["uid"])
            if x["kind"] != "image" or not lx or not lx.get("local_path"):
                continue
            src = lx["local_path"]
            if not x.get("file") and os.path.isfile(src) and os.path.getsize(src) <= MAX_SYNC_FILE:
                x["file"] = f"img-{x['uid'][:8]}-{_safe_name(os.path.basename(src))}"
            if x.get("file") and os.path.isfile(src):
                dest = os.path.join(folder, x["file"])
                if not os.path.isfile(dest):
                    os.makedirs(folder, exist_ok=True)
                    shutil.copy2(src, dest)

    def _import_file(self, chat_uid):
        """Returns a function that copies a synced file into this device's workspace."""
        def put(file_name, chat_local_id):
            src = os.path.join(self.repo_dir, "files", chat_uid, file_name)
            if not os.path.isfile(src):
                return None
            rel = f"attachments/chat-{chat_local_id}/{file_name}"
            dest = os.path.join(self.workspace, *rel.split("/"))
            if not os.path.isfile(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
            return rel
        return put

    def _merge_settings(self, tombs):
        path = os.path.join(self.repo_dir, "settings.json")
        remote = _read_json(path, {})
        local = self.store.sync_settings(GLOBAL_SETTINGS, PROJECT_SETTINGS)
        merged, changed = dict(remote), 0
        for key, lv in local.items():
            rv = remote.get(key)
            if key.split(":")[0] in tombs:
                merged.pop(key, None)
                continue
            if not rv or lv["at"] > rv["at"]:
                merged[key] = lv
        for key, rv in remote.items():
            if key.split(":")[0] in tombs:
                merged.pop(key, None)
                continue
            lv = local.get(key)
            if not lv or rv["at"] > lv["at"]:
                if self.store.sync_put_setting(key, rv["value"], rv["at"], PROJECT_SETTINGS):
                    changed += 1
        if merged != remote:
            _write_json(path, merged)
        return changed

    # ----- code folders -----

    def _code_files(self, root):
        out = {}
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in CODE_SKIP_DIRS and not d.startswith(".amalgam")]
            for n in files:
                if n.endswith(CODE_SKIP_EXT) or n in ("Thumbs.db", ".DS_Store"):
                    continue
                full = os.path.join(dirpath, n)
                try:
                    if os.path.islink(full) or os.path.getsize(full) > MAX_SYNC_FILE:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                out[rel] = full
        return out

    def _merge_code(self, tombs):
        changed = 0
        code_root = os.path.join(self.repo_dir, "code")
        self._pending_bases = {}
        remote_uids = set(os.listdir(code_root)) if os.path.isdir(code_root) else set()
        projects = self.store.sync_projects()
        busy_project = self.store.uid_of("projects", self.busy_ids()[1]) if self.busy_check() else None
        for uid in remote_uids | {u for u, p in projects.items() if p.get("code_dir")}:
            if uid in tombs or uid not in projects:
                if uid in tombs:
                    shutil.rmtree(os.path.join(code_root, uid), ignore_errors=True)
                continue
            if uid == busy_project:
                continue      # files are changing right now; next time
            proj = projects[uid]
            local_dir = proj.get("code_dir")
            if not local_dir or not os.path.isdir(local_dir):
                if uid not in remote_uids:
                    continue
                # the other device has code for this project: give it a folder here
                from codework import make_code_folder
                local_dir = make_code_folder(proj["name"])
                self.store.update_project(proj["id"], code_dir=local_dir)
                changed += 1
            rdir = os.path.join(code_root, uid)
            os.makedirs(rdir, exist_ok=True)
            base = _read_json(os.path.join(self.base_dir, uid + ".json"), {})
            L = self._code_files(local_dir)
            total = sum(os.path.getsize(f) for f in L.values() if os.path.exists(f))
            if total > MAX_CODE_FOLDER:
                self.state["notes"].append(f"{proj['name']}: code folder is {total // 1_000_000} MB, too big to sync "
                                           f"(limit {MAX_CODE_FOLDER // 1_000_000} MB). Chats still sync.")
                continue
            Rf = self._code_files(rdir)
            Lh = {p: self._quick_sha(f) for p, f in L.items()}
            Rh = {p: self._quick_sha(f) for p, f in Rf.items()}
            new_base = {}
            for p in set(Lh) | set(Rh) | set(base):
                lh, rh, bh = Lh.get(p), Rh.get(p), base.get(p)
                lpath = os.path.join(local_dir, *p.split("/"))
                rpath = os.path.join(rdir, *p.split("/"))
                if lh == rh:
                    if lh:
                        new_base[p] = lh
                    continue
                if lh == bh:              # changed (or deleted) only on the other device: take it
                    if rh:
                        os.makedirs(os.path.dirname(lpath), exist_ok=True)
                        shutil.copy2(rpath, lpath)
                        new_base[p] = rh
                    else:
                        try:
                            os.remove(lpath)
                        except OSError:
                            pass
                    changed += 1
                elif rh == bh:            # changed (or deleted) only here: send it
                    if lh:
                        os.makedirs(os.path.dirname(rpath), exist_ok=True)
                        shutil.copy2(lpath, rpath)
                        new_base[p] = lh
                    else:
                        try:
                            os.remove(rpath)
                        except OSError:
                            pass
                else:                     # changed on both: keep theirs, save ours next to it
                    if lh and rh:
                        stem, ext = os.path.splitext(p)
                        cp = f"{stem} (conflict from {self.device} {time.strftime('%b %d %H.%M')}){ext}"
                        cpath = os.path.join(local_dir, *cp.split("/"))
                        shutil.copy2(lpath, cpath)
                        shutil.copy2(cpath, os.path.join(rdir, *cp.split("/")))
                        shutil.copy2(rpath, lpath)
                        new_base[p] = rh
                        new_base[cp] = lh
                        self.state["conflicts"].append(f"{proj['name']}: {p}")
                    elif lh:              # deleted there, changed here: keep the change
                        os.makedirs(os.path.dirname(rpath), exist_ok=True)
                        shutil.copy2(lpath, rpath)
                        new_base[p] = lh
                    else:                 # deleted here, changed there: bring it back
                        os.makedirs(os.path.dirname(lpath), exist_ok=True)
                        shutil.copy2(rpath, lpath)
                        new_base[p] = rh
                    changed += 1
            self._pending_bases[uid] = new_base
        return changed

    def _quick_sha(self, path):
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
        hit = self._hashes.get(path)
        if hit and hit[0] == key:
            return hit[1]
        h = _sha(path)
        self._hashes[path] = (key, h)
        return h

    def _commit_bases(self):
        """Only after a successful upload do we remember what was synced."""
        for uid, base in getattr(self, "_pending_bases", {}).items():
            _write_json(os.path.join(self.base_dir, uid + ".json"), base)
        for uid, lines in getattr(self, "_pending_mem", {}).items():
            _write_json(os.path.join(self.base_dir, f"mem-{uid}.json"), lines)
        self._pending_bases, self._pending_mem = {}, {}
