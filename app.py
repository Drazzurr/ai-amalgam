"""AI Amalgam - Phase 1: a Claude + GPT discussion relay.

Normal start:  double-click the "AI Amalgam" desktop shortcut (no console window).
Options:       --demo     stand-in models, to try the app without using your plans
               --browser  open in your normal web browser instead of an app window
               --console  keep printing to the console (for troubleshooting)
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(APP_DIR, "ui", "index.html")
ICON_FILE = os.path.join(APP_DIR, "amalgam.ico")
WORKSPACE = os.path.join(APP_DIR, "workspace")
DB_FILE = os.path.join(APP_DIR, "amalgam.db")
LOG_FILE = os.path.join(APP_DIR, "amalgam.log")
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AIAmalgam")
LOCK_FILE = os.path.join(DATA_DIR, "running.json")
WINDOW_PROFILE = os.path.join(DATA_DIR, "window")
OLD_WINDOW_PROFILE = os.path.join(DATA_DIR, "window-profile")  # v1 profile; Edge synced into it

BYE_GRACE = 15     # seconds after a window closes before the app stops (a reload reconnects sooner)
MAX_UPLOAD = 25_000_000  # bytes per attached file
IDLE_LIMIT = 150     # seconds without the window checking in before the app closes itself
NEVER_OPENED = 180   # seconds to wait for the first window to connect
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------- small helpers ----------

def setup_logging(console):
    """Started without a console (pythonw), print() has nowhere to go: use a log file."""
    if console and sys.stdout is not None:
        return
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    print(f"\n--- AI Amalgam started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def show_error(message):
    print("ERROR:", message)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "AI Amalgam", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.__stderr__ or sys.stderr)


def find_app_browser():
    """Edge ships with Windows; Chrome also works. Both can open a page as an app window."""
    candidates = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def install_skills():
    """Copy the app's skills (for example world-bible-keeper) into Claude Code's and Codex's
    personal skills folders, so both models follow the same playbooks. Only our own skill
    folders are touched, and only when the bundled copy has changed."""
    src_root = os.path.join(APP_DIR, "skills")
    if not os.path.isdir(src_root):
        return
    home = os.path.expanduser("~")
    for dest_root in (os.path.join(home, ".claude", "skills"), os.path.join(home, ".codex", "skills")):
        for name in os.listdir(src_root):
            src = os.path.join(src_root, name, "SKILL.md")
            if not os.path.isfile(src):
                continue
            dest = os.path.join(dest_root, name, "SKILL.md")
            try:
                with open(src, "rb") as f:
                    new = f.read()
                old = None
                if os.path.isfile(dest):
                    with open(dest, "rb") as f:
                        old = f.read()
                if old != new:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(new)
                    print("Installed skill", name, "to", dest_root)
            except OSError as e:
                print("Couldn't install skill", name, "to", dest_root, ":", e)


def toast(title, body):
    """A Windows notification (bottom-right pop-up with sound). Silently does nothing elsewhere."""
    if sys.platform != "win32":
        print("Notify:", title, "-", body)
        return
    import base64
    from xml.sax.saxutils import escape
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(body)}</text></binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml(@'\n" + xml + "\n'@);"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)")
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                          base64.b64encode(script.encode("utf-16-le")).decode()],
                         creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print("Notification failed:", e)


def list_skills():
    """Skills Claude Code can use: the ones that come with AI Amalgam plus any you added yourself."""
    found = {}
    roots = [(os.path.join(APP_DIR, "skills"), "AI Amalgam"),
             (os.path.join(os.path.expanduser("~"), ".claude", "skills"), "yours")]
    for root, source in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name, "SKILL.md")
            if name in found or not os.path.isfile(path):
                continue
            desc = ""
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(3000)
                m = re.search(r"^description:\s*(.+)$", head, re.M)
                desc = m.group(1).strip().strip('"') if m else ""
            except OSError:
                pass
            found[name] = {"name": name, "description": desc[:220], "source": source}
    return list(found.values())


def pick_folder():
    """Show Windows' folder picker (runs on the app's side, so it can return a real path)."""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose the code folder for this project", mustexist=True)
        root.destroy()
        return os.path.normpath(path) if path else ""
    except Exception as e:
        print("Folder picker failed:", e)
        return None


def open_window(url, force_browser=False):
    """Open the UI. Returns the window process (or None if we can't track it)."""
    if not force_browser:
        exe = find_app_browser()
        if exe:
            os.makedirs(WINDOW_PROFILE, exist_ok=True)
            # InPrivate/incognito: no sign-in, no sync prompt, no first-run pages, no restored tabs.
            private = "--inprivate" if "edge" in os.path.basename(exe).lower() else "--incognito"
            args = [exe, private, f"--app={url}", f"--user-data-dir={WINDOW_PROFILE}",
                    "--no-first-run", "--no-default-browser-check", "--disable-sync",
                    "--disable-session-crashed-bubble", "--window-size=1320,880"]
            try:
                return subprocess.Popen(args, creationflags=NO_WINDOW,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                print("Couldn't open the app window:", e)
    webbrowser.open(url)
    return None


def existing_instance():
    """If AI Amalgam is already running, return its address so we can just open another window."""
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            info = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping",
                                     headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{info['port']}/#t={info['token']}"
    except Exception:
        pass
    return None


# ---------- the app ----------

class App:
    def __init__(self, args):
        from orchestrator import Orchestrator
        from runners import billing_vars_present, make_runners
        from storage import Store

        self.args = args
        self.args_list = []   # start arguments to reuse after an update (the port is picked fresh)
        skip = False
        for a in sys.argv[1:]:
            if skip:
                skip = False
            elif a == "--port":
                skip = True
            elif not a.startswith("--port="):
                self.args_list.append(a)
        os.makedirs(WORKSPACE, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
        self.store = Store(DB_FILE)
        from codework import Checkpoints
        self.orch = Orchestrator(self.store, make_runners(WORKSPACE, demo=args.demo), Checkpoints(DATA_DIR))
        self.token = secrets.token_urlsafe(24)
        self.billing_vars = billing_vars_present()
        self.quit_event = threading.Event()
        self.started = time.time()
        self.last_seen = None      # last time the window polled
        self.bye_at = None         # when a window said it was closing
        self.window = None
        self.connections = {"checking": False, "checked_at": None, "models": {}}
        import updater
        self.updater = updater
        self.update = {"current": updater.current_version(), "state": "idle"}
        self.usage = {"checked_at": 0, "models": {}}
        self._usage_busy = False
        self._last_status = 0
        self._usage_again = False
        self.focused_at = 0        # last time the window said it was in front
        import apps
        from runners import clean_env
        self.apps = apps
        self.blender_setup = apps.BlenderSetup(clean_env)
        self.orch.blender_path = apps.find_blender()[0]
        self.games = {"detected": {}, "state": "idle", "log": []}
        self._load_game_folders()
        self.server = ThreadingHTTPServer(("127.0.0.1", args.port), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/#t={self.token}"

    def _notifier(self):
        """Pop up a Windows notification when the models need you or finish, if you're not looking."""
        prev = self.orch.status
        while not self.quit_event.wait(1):
            st = self.orch.status
            if prev == "running" and st != "running" and self.store.get_setting("notify", "1") == "1" \
                    and time.time() - self.focused_at > 3:
                o = self.orch
                if o.pending_commands:
                    toast("AI Amalgam: approve a command?", f"{o.pending_commands['speaker']} wants to run: "
                          + "; ".join(o.pending_commands["commands"])[:120])
                elif st == "waiting":
                    toast("AI Amalgam needs you", "A question is waiting for your answer.")
                elif o.hint == "error":
                    toast("AI Amalgam paused", "A model couldn't reply. Open the app to retry.")
                elif o.hint in ("done", "limit"):
                    toast("AI Amalgam", "The discussion finished." if o.hint == "done"
                          else "The models reached the round limit.")
            prev = st

    def quit(self):
        self.quit_event.set()

    def check_update(self):
        if self.update.get("state") in ("checking", "installing"):
            return
        self.update = dict(self.update, state="checking")

        def work():
            info = self.updater.check()
            self.update = dict(info, state="idle", checked_at=time.time())
            print("Update check:", info)

        threading.Thread(target=work, daemon=True).start()

    def rollback(self):
        if self.update.get("state") in ("installing", "restarting"):
            return
        self.update = dict(self.update, state="installing")

        def work():
            try:
                old = self.updater.rollback()
                print("Went back to version", old)
                self.update = dict(self.update, state="restarting", installed=old)
                self.updater.restart_later(self.args_list)
                time.sleep(1.5)
                self.quit()
            except Exception as e:
                self.update = dict(self.update, state="idle", error=f"Couldn't go back: {e}")

        threading.Thread(target=work, daemon=True).start()

    def install_update(self):
        if self.update.get("state") == "installing":
            return
        self.update = dict(self.update, state="installing")

        def work():
            try:
                new = self.updater.install()
                print("Installed version", new)
                self.update = dict(self.update, state="restarting", installed=new)
                self.updater.restart_later(self.args_list)
                time.sleep(1.5)
                self.quit()
            except Exception as e:
                print("Update failed:", e)
                self.update = dict(self.update, state="idle", error=f"Update failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def refresh_usage(self, force=False):
        """Re-read plan usage at most every 20 s (cheap: a few local files)."""
        if self._usage_busy:
            self._usage_again = self._usage_again or force   # run once more when the current check ends
            return
        if not force and time.time() - self.usage["checked_at"] < 20:
            return
        self._usage_busy = True

        def work():
            try:
                now = time.time()
                models = {}
                for key in self.orch.order:
                    r = self.orch.runners[key]
                    try:
                        info = r.usage() or {}
                    except Exception as e:
                        info = {"error": str(e)}
                    info["turns_5h"] = self.store.count_turns(r.name, now - 5 * 3600)
                    info["turns_7d"] = self.store.count_turns(r.name, now - 7 * 86400)
                    models[key] = info
                self.usage = {"checked_at": now, "models": models}
            finally:
                self._usage_busy = False
                if self._usage_again:
                    self._usage_again = False
                    self.refresh_usage(force=True)

        threading.Thread(target=work, daemon=True).start()

    def _load_game_folders(self, detect=True):
        import gamefiles.tools as gt
        from gamefiles import oodle
        try:
            user = json.loads(self.store.get_setting("game_folders_user", "[]"))
        except ValueError:
            user = []

        def work():
            if detect:
                try:
                    self.games["detected"] = gt.find_games()
                except Exception as e:
                    print("Game detection failed:", e)
            folders = list(dict.fromkeys(list(self.games["detected"].values()) + user))
            self.orch.game_folders = folders
            oodle.add_search_dirs(folders)
        self.games["user"] = user
        threading.Thread(target=work, daemon=True).start()

    def update_clis(self):
        """Update Claude Code (and Codex) to their latest versions; fixes come out often."""
        if getattr(self, "cli_update", {}).get("state") == "running":
            return
        self.cli_update = {"state": "running", "log": []}

        def work():
            from runners import clean_env
            for name, cmd in (("Claude Code", ["claude", "update"]),):
                exe = shutil.which(cmd[0])
                if not exe:
                    self.cli_update["log"].append(f"{name}: not found")
                    continue
                try:
                    r = subprocess.run([exe] + cmd[1:], capture_output=True, timeout=600, env=clean_env(),
                                       creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
                    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
                    self.cli_update["log"].append(f"{name}: " + (out[-1] if out else "done"))
                except Exception as e:
                    self.cli_update["log"].append(f"{name}: {e}")
            self.cli_update["state"] = "done"
            self.refresh_connections()
        threading.Thread(target=work, daemon=True).start()

    def install_game_helpers(self):
        if self.games["state"] == "running":
            return
        import gamefiles.tools as gt
        self.games.update(state="running", log=[])

        def work():
            ok = gt.install(lambda t: (print("Game helpers:", t), self.games["log"].append(t)))
            self.games["state"] = "done" if ok else "failed"
        threading.Thread(target=work, daemon=True).start()

    def apps_view(self):
        path, ver = self.apps.find_blender() if not self.orch.blender_path else (self.orch.blender_path, None)
        if ver is None and path:
            m = re.search(r"Blender\s*([\d.]+)", path)
            ver = m.group(1) if m else ""
        conn = {}
        for key, group in (self.connections.get("models") or {}).items():
            conn[key] = any("blender" in it["name"].lower() for it in group.get("items", []))
        setup = self.blender_setup
        if setup.state == "done" and getattr(self, "refresh_connections_later", False):
            self.refresh_connections_later = False
            self.refresh_connections()
        import gamefiles.tools as gt
        games = {"detected": self.games["detected"], "user": self.games.get("user", []),
                 "helpers": gt.installed(), "state": self.games["state"], "log": self.games["log"][-14:]}
        return {"games": games, "blender": {"path": path, "version": ver, "listening": self.apps.blender_listening(0.2),
                            "claude": conn.get("claude", False), "gpt": conn.get("gpt", False),
                            "on": self.orch.blender_on, "setup": {"state": setup.state, "log": setup.log[-12:]}}}

    def connections_view(self):
        """Connections, corrected by what actually worked in chats: a connector a model used
        successfully shows as working even if the quick start-up health check failed."""
        view = dict(self.connections)
        models = {}
        for key, group in (self.connections.get("models") or {}).items():
            verified = getattr(self.orch.runners.get(key), "verified_connectors", set())
            items = []
            for it in group.get("items", []):
                it = dict(it)
                if it["name"].lower() in verified:
                    it["status"], it["verified"] = "connected", True
                elif it["status"] == "failed":
                    it["status"] = "check failed"   # the quick check failed; it may still work
                items.append(it)
            models[key] = dict(group, items=items)
        view["models"] = models
        return view

    def refresh_connections(self):
        """Ask each model's tool which connectors/servers it has (runs in the background)."""
        if self.connections["checking"]:
            return
        self.connections["checking"] = True

        def work():
            models = {}
            self.cli_versions = {}
            for key in self.orch.order:
                runner = self.orch.runners[key]
                try:
                    from runners import run_command as rc
                    self.cli_versions[key] = (rc(runner.exe, ["--version"]) or "").strip().splitlines()[0][:60]
                except Exception:
                    pass
                try:
                    models[key] = {"items": runner.list_connections(), "error": None}
                except Exception as e:
                    models[key] = {"items": [], "error": str(e)}
                print(f"Connections for {runner.name}:", models[key])
            self.connections.update(models=models, checked_at=time.time(), checking=False)
            names = {it["name"] for it in (models.get("claude") or {}).get("items", []) if "blender" in it["name"].lower()}
            self.orch.blender_servers = sorted(names | {"blender"})

        threading.Thread(target=work, daemon=True).start()

    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, data, code=200):
                body = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path, ctype):
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token_ok(self, url):
                sent = self.headers.get("X-Token") or parse_qs(url.query).get("t", [""])[0]
                return secrets.compare_digest(sent, app.token)

            def _upload(self, url, length):
                """Save a file you attach; it goes with your next message."""
                if length > MAX_UPLOAD:
                    self.rfile.read(length)
                    return self._json({"error": f"That file is over {MAX_UPLOAD // 1_000_000} MB."}, 413)
                name = os.path.basename(parse_qs(url.query).get("name", ["file"])[0]).strip() or "file"
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]
                inbox = os.path.join(WORKSPACE, "attachments", "inbox")
                os.makedirs(inbox, exist_ok=True)
                path = os.path.join(inbox, secrets.token_hex(4) + "_" + name)
                remaining = length
                with open(path, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                app.orch.pending_files.append({"name": name, "abs": path, "size": length - remaining})
                return self._json({"ok": True})

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in ("/", "/index.html"):
                    return self._file(UI_FILE, "text/html; charset=utf-8")
                if url.path == "/favicon.ico" and os.path.isfile(ICON_FILE):
                    return self._file(ICON_FILE, "image/x-icon")
                if not self._token_ok(url):
                    return self._json({"error": "forbidden"}, 403)
                if url.path == "/api/ping":
                    return self._json({"ok": True})
                if url.path == "/api/state":
                    app.last_seen = time.time()
                    q = parse_qs(url.query)
                    if q.get("focused", ["0"])[0] == "1":
                        app.focused_at = time.time()
                    after = int(q.get("after", ["0"])[0] or 0)
                    state = app.orch.state()
                    msgs = app.store.messages(state["chat_id"]) if state["chat_id"] else []
                    state["messages"] = [m for m in msgs if m["id"] > after]
                    if q.get("lite", ["0"])[0] == "1":   # quick refresh while models talk
                        return self._json(state)
                    state["chats"] = app.store.chats()
                    state["projects"] = app.store.projects()
                    state["demo"] = app.args.demo
                    state["billing_vars"] = app.billing_vars
                    state["connections"] = app.connections_view()
                    app.refresh_usage(force=app.orch.turn_seq != app._last_status)
                    app._last_status = app.orch.turn_seq
                    state["usage"] = app.usage
                    state["update"] = dict(app.update, rollback=app.updater.last_backup()[1])
                    state["notify"] = app.store.get_setting("notify", "1") == "1"
                    state["check_questions"] = app.orch.check_questions
                    state["skills"] = list_skills()
                    state["skills_off"] = app.orch.skills_off()
                    state["apps"] = app.apps_view()
                    state["cli"] = {"versions": getattr(app, "cli_versions", {}),
                                    "update": getattr(app, "cli_update", {"state": "idle", "log": []})}
                    try:
                        state["ui_collapsed"] = json.loads(app.store.get_setting("ui_collapsed", "null"))
                    except ValueError:
                        state["ui_collapsed"] = None
                    return self._json(state)
                if url.path == "/api/img":
                    raw = parse_qs(url.query).get("path", [""])[0]
                    p = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(WORKSPACE, raw))
                    roots = [WORKSPACE] + list(app.orch.game_folders) + [app.orch.code_dir() or WORKSPACE]

                    def under(r):
                        try:
                            r = os.path.realpath(r)
                            return os.path.commonpath([p, r]) == r
                        except ValueError:
                            return False
                    if not any(under(r) for r in roots) or not os.path.isfile(p) or \
                            not p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                        return self._json({"error": "not found"}, 404)
                    with open(p, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    ctype = {"jpg": "jpeg", "jpeg": "jpeg"}.get(p.rsplit(".", 1)[-1].lower(), p.rsplit(".", 1)[-1].lower())
                    self.send_header("Content-Type", f"image/{ctype}")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if url.path == "/api/search":
                    return self._json({"results": app.store.search(parse_qs(url.query).get("q", [""])[0])})
                if url.path.startswith("/api/code/"):
                    return self._code_get(url)
                self._json({"error": "not found"}, 404)

            def _code_get(self, url):
                import codework
                q = parse_qs(url.query)
                code_dir = app.orch.code_dir()
                if not code_dir:
                    return self._json({"error": "This project has no code folder."}, 400)
                if url.path == "/api/code/files":
                    files, cut = codework.list_files(code_dir)
                    return self._json({"dir": code_dir, "files": files, "cut": cut})
                if url.path == "/api/code/file":
                    return self._json(codework.read_file(code_dir, q.get("path", [""])[0]))
                if url.path == "/api/code/changes":
                    return self._json({"changes": app.store.code_changes(app.orch.project_id)})
                if url.path == "/api/code/diff":
                    ch = app.store.code_change(int(q.get("id", ["0"])[0] or 0))
                    if not ch or ch["project_id"] != app.orch.project_id:
                        return self._json({"error": "not found"}, 404)
                    try:
                        patch = app.orch.cp.patch(ch["project_id"], code_dir, ch["before"], ch["after"])
                    except Exception as e:
                        return self._json({"error": str(e)}, 500)
                    return self._json({"patch": patch})
                return self._json({"error": "not found"}, 404)

            def do_POST(self):
                url = urlparse(self.path)
                if not self._token_ok(url):
                    return self._json({"error": "forbidden"}, 403)
                length = int(self.headers.get("Content-Length", 0) or 0)
                if url.path == "/api/upload":
                    return self._upload(url, length)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    data = {}
                path = url.path
                if path == "/api/send":
                    app.orch.send(str(data.get("text", "")))
                elif path == "/api/stop":
                    threading.Thread(target=app.orch.stop, daemon=True).start()
                elif path == "/api/new":
                    pid = data.get("project_id")
                    app.orch.new_chat(int(pid) if pid else None)
                elif path == "/api/project/new":
                    pid = app.store.new_project(str(data.get("name", "")))
                    app.orch.new_chat(pid)
                    return self._json({"ok": True, "project_id": pid})
                elif path == "/api/project/update":
                    roles = data.get("roles")
                    code_dir = data.get("code_dir")
                    if code_dir is not None:
                        from codework import check_code_dir
                        code_dir, err = check_code_dir(str(code_dir))
                        if err:
                            return self._json({"error": err}, 400)
                        app.store.update_project(int(data["project_id"]), code_dir=code_dir)
                    app.store.update_project(int(data["project_id"]), data.get("name"),
                                             data.get("instructions"), data.get("doc_link"),
                                             data.get("memory"), roles if isinstance(roles, dict) else None)
                elif path == "/api/project/make-code-folder":
                    from codework import make_code_folder
                    pid = int(data["project_id"])
                    proj = app.store.project(pid)
                    if not proj:
                        return self._json({"error": "Project not found."}, 404)
                    try:
                        folder = make_code_folder(proj["name"])
                    except OSError as e:
                        return self._json({"error": f"Couldn't create the folder: {e}"}, 500)
                    app.store.update_project(pid, code_dir=folder)
                    return self._json({"ok": True, "path": folder})
                elif path == "/api/code/open-folder":
                    folder = app.orch.code_dir()
                    if folder and sys.platform == "win32":
                        os.startfile(folder)
                    elif folder:
                        webbrowser.open("file://" + folder)
                elif path == "/api/pick-folder":
                    folder = pick_folder()
                    if folder is None:
                        return self._json({"error": "Couldn't open the folder picker. Paste the folder path instead."}, 500)
                    return self._json({"ok": True, "path": folder})
                elif path == "/api/approve":
                    if not app.orch.approve_commands(int(data.get("msg_id", 0)), bool(data.get("ok")),
                                                     bool(data.get("always"))):
                        return self._json({"error": "That request isn't waiting any more."}, 409)
                elif path == "/api/code/run":
                    rel = str(data.get("path", ""))
                    if rel.lower().endswith((".html", ".htm")):
                        from codework import inside
                        code_dir = app.orch.code_dir()
                        full = inside(code_dir, rel) if code_dir else None
                        if not full or not os.path.isfile(full):
                            return self._json({"error": "File not found."}, 404)
                        webbrowser.open("file:///" + full.replace("\\", "/").lstrip("/"))
                    else:
                        err = app.orch.run_file(rel)
                        if err:
                            return self._json({"error": err}, 400)
                elif path == "/api/code/undo":
                    err = app.orch.undo_change(int(data.get("id", 0)))
                    if err:
                        return self._json({"error": err}, 400)
                elif path == "/api/unattach":
                    i = int(data.get("index", -1))
                    if 0 <= i < len(app.orch.pending_files):
                        f = app.orch.pending_files.pop(i)
                        try:
                            os.remove(f["abs"])
                        except OSError:
                            pass
                elif path == "/api/chat/rename":
                    app.store.rename_chat(int(data["chat_id"]), str(data.get("title", "")))
                elif path == "/api/chat/delete":
                    cid = int(data["chat_id"])
                    if app.orch.chat_id == cid:
                        app.orch.new_chat(app.orch.project_id)
                    app.store.delete_chat(cid)
                    shutil.rmtree(os.path.join(WORKSPACE, "attachments", f"chat-{cid}"), ignore_errors=True)
                    shutil.rmtree(os.path.join(WORKSPACE, "extracted", f"chat-{cid}"), ignore_errors=True)
                elif path == "/api/chat/move":
                    pid = data.get("project_id")
                    app.store.move_chat(int(data["chat_id"]), int(pid) if pid else None)
                    if app.orch.chat_id == int(data["chat_id"]):
                        app.orch.project_id = int(pid) if pid else None
                elif path == "/api/project/delete":
                    pid = int(data["project_id"])
                    app.store.delete_project(pid)
                    if app.orch.project_id == pid:
                        app.orch.project_id = None
                elif path == "/api/open":
                    app.orch.open_chat(int(data["chat_id"]))
                elif path == "/api/settings":
                    app.orch.settings(data.get("max_rounds"), data.get("enabled"), data.get("speeds"),
                                      data.get("check_questions"), data.get("notify"))
                elif path == "/api/skills/toggle":
                    app.orch.set_skill(str(data.get("name", "")), bool(data.get("on")))
                elif path == "/api/ui":
                    if isinstance(data.get("collapsed"), list):
                        app.store.set_setting("ui_collapsed", json.dumps([str(x) for x in data["collapsed"]][:20]))
                elif path == "/api/allowed-commands":
                    if isinstance(data.get("commands"), list):
                        app.orch.set_allowed_commands([str(c) for c in data["commands"]])
                elif path == "/api/apps/blender/setup":
                    app.blender_setup.start()
                    app.refresh_connections_later = True
                elif path == "/api/apps/blender/toggle":
                    app.orch.blender_on = bool(data.get("on"))
                    app.store.set_setting("blender_on", "1" if app.orch.blender_on else "0")
                elif path == "/api/games/add":
                    folder = str(data.get("path") or "") or (pick_folder() or "")
                    if folder and os.path.isdir(folder):
                        user = list(dict.fromkeys(app.games.get("user", []) + [os.path.normpath(folder)]))
                        app.store.set_setting("game_folders_user", json.dumps(user))
                        app._load_game_folders(detect=False)
                    elif folder:
                        return self._json({"error": "That folder doesn't exist."}, 400)
                elif path == "/api/games/remove":
                    user = [u for u in app.games.get("user", []) if u != data.get("path")]
                    app.store.set_setting("game_folders_user", json.dumps(user))
                    app._load_game_folders(detect=False)
                elif path == "/api/reveal":
                    p = os.path.realpath(str(data.get("path", "")))
                    root = os.path.realpath(WORKSPACE)
                    if os.path.commonpath([p, root]) == root and os.path.exists(p):
                        if sys.platform == "win32":
                            if data.get("folder"):
                                subprocess.Popen(["explorer", "/select,", p])
                            else:
                                os.startfile(p)
                        else:
                            webbrowser.open("file://" + (os.path.dirname(p) if data.get("folder") else p))
                elif path == "/api/update-cli":
                    app.update_clis()
                elif path == "/api/games/install-helpers":
                    app.install_game_helpers()
                elif path == "/api/apps/blender/recheck":
                    app.orch.blender_path = app.apps.find_blender()[0]
                    app.refresh_connections()
                elif path == "/api/resume":
                    app.orch.resume()
                elif path == "/api/regenerate":
                    err = app.orch.regenerate()
                    if err:
                        return self._json({"error": err}, 400)
                elif path == "/api/edit-last":
                    err = app.orch.edit_last(str(data.get("text", "")).strip())
                    if err:
                        return self._json({"error": err}, 400)
                elif path == "/api/open-link":
                    link = str(data.get("url", ""))
                    if re.match(r"https?://", link):
                        webbrowser.open(link)
                elif path == "/api/update/rollback":
                    app.rollback()
                elif path == "/api/update/check":
                    app.check_update()
                elif path == "/api/update/install":
                    app.install_update()
                elif path == "/api/open-url":
                    url = str(data.get("url", ""))
                    if url in ("https://claude.ai/settings/usage", "https://chatgpt.com/settings/usage") or \
                            re.fullmatch(r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]+/edit", url):
                        webbrowser.open(url)  # your normal browser, where you're signed in
                elif path == "/api/refresh-connections":
                    app.refresh_connections()
                elif path == "/api/chat-settings":
                    link = data.get("doc_link")
                    app.orch.set_chat_settings(str(link) if link is not None else None,
                                               data.get("allow_edits"))
                elif path == "/api/quit":
                    self._json({"ok": True})
                    app.quit()
                    return
                elif path == "/api/bye":
                    app.bye_at = time.time()
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})

        return Handler

    def _watchdog(self, track_window):
        """Close the app when its window is gone (closed, or not checking in any more)."""
        opened_at = time.time()
        busy_until, told_at = 0, 0
        while not self.quit_event.wait(2):
            now = time.time()
            gone = (self.bye_at and now - self.bye_at > BYE_GRACE and (self.last_seen or 0) < self.bye_at) or \
                   (self.last_seen is not None and now - self.last_seen > IDLE_LIMIT)
            # Models still working: keep going in the background, even with the window closed.
            if self.orch.status == "running":
                busy_until = now + 6
                if gone and told_at < (self.last_seen or 0) + 1:
                    told_at = now
                    toast("AI Amalgam is still working",
                          "You'll get a notification when the models finish or need you. "
                          "Open the app again from the desktop shortcut to watch.")
                continue
            if now < busy_until:      # let the "finished" notification go out first
                continue
            # A window said goodbye and no window has checked in since: it was closed.
            if self.bye_at and now - self.bye_at > BYE_GRACE and (self.last_seen or 0) < self.bye_at:
                print("Window closed.")
                return self.quit()
            if self.last_seen is None:
                if now - self.started > NEVER_OPENED:
                    print("No window connected; closing.")
                    return self.quit()
            elif now - self.last_seen > IDLE_LIMIT:
                print("Window stopped checking in; closing.")
                return self.quit()

    def run(self):
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._notifier, daemon=True).start()
        shutil.rmtree(OLD_WINDOW_PROFILE, ignore_errors=True)
        self.refresh_connections()
        if not self.args.demo:
            install_skills()
            threading.Timer(6, self.check_update).start()
        print("AI Amalgam is running at", self.url)
        if self.billing_vars:
            print("Ignoring", ", ".join(self.billing_vars), "so you stay on your subscriptions.")

        try:
            if self.args.no_open:
                self.quit_event.wait()
                return
            if not self.args.browser:
                try:
                    import webview  # optional: pip install pywebview
                except ImportError:
                    webview = None
                if webview:
                    threading.Thread(target=self._watchdog, args=(False,), daemon=True).start()
                    w = webview.create_window("AI Amalgam", self.url, width=1320, height=880)
                    threading.Thread(target=lambda: (self.quit_event.wait(), w.destroy()),
                                     daemon=True).start()
                    webview.start()
                    return
            self.window = open_window(self.url, force_browser=self.args.browser)
            threading.Thread(target=self._watchdog, args=(False,), daemon=True).start()
            self.quit_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        print("Shutting down.")
        try:
            self.orch.stop()
        except Exception:
            pass
        if self.window and self.window.poll() is None:
            try:
                self.window.terminate()
            except OSError:
                pass
        self.server.shutdown()
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                if json.load(f).get("token") == self.token:
                    os.remove(LOCK_FILE)
        except (OSError, ValueError):
            pass


def main():
    parser = argparse.ArgumentParser(description="AI Amalgam - Claude + GPT discussion relay")
    parser.add_argument("--demo", action="store_true", help="use stand-in models")
    parser.add_argument("--browser", action="store_true", help="open in your normal web browser")
    parser.add_argument("--console", action="store_true", help="print to the console")
    parser.add_argument("--port", type=int, default=0, help="local port (default: any free port)")
    parser.add_argument("--no-open", action="store_true", help="don't open any window")
    args = parser.parse_args()
    setup_logging(args.console)

    if not args.demo and not args.no_open:
        url = existing_instance()
        if url:
            print("Already running; opening another window.")
            open_window(url, force_browser=args.browser)
            return 0
    try:
        App(args).run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        show_error(f"AI Amalgam couldn't start:\n\n{e}\n\nDetails are in amalgam.log in the app folder.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
