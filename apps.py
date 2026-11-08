"""Other programs on your PC that the models can use. For now: Blender.

Two ways in:
- Live control: the "Blender MCP" add-on runs a small server inside your open Blender
  (localhost:9876), and the models talk to it through the blender-mcp connector. They can
  look at the scene, take viewport screenshots, and run Blender Python (full access).
- Headless: in a project with a code folder, the models can ask to run
  blender --background --python script.py (with your approval), for batch jobs and renders.
"""

import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BLENDER_PORT = 9876
SERVER_NAME = "blender"                       # name the connector is registered under
SERVER_CMD = ["uvx", "blender-mcp"]           # community "MCP for Blender" (works with Blender 3.0+)


def find_blender():
    """(path to blender.exe, version text) or (None, None)."""
    found = shutil.which("blender")
    candidates = [found] if found else []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                candidates += glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
        for steam in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam", r"D:\SteamLibrary", r"E:\SteamLibrary"):
            candidates.append(os.path.join(steam, "steamapps", "common", "Blender", "blender.exe"))
    else:
        candidates += ["/usr/bin/blender", "/snap/bin/blender", "/Applications/Blender.app/Contents/MacOS/Blender"]
    best = None
    for path in candidates:
        if path and os.path.isfile(path):
            m = re.search(r"Blender\s*([\d.]+)", path)
            ver = m.group(1) if m else ""
            key = tuple(int(x) for x in ver.split(".") if x.isdigit()) if ver else (0,)
            if best is None or key > best[2]:
                best = (path, ver, key)
    return (best[0], best[1]) if best else (None, None)


def blender_listening(timeout=0.4):
    """True if the add-on's server is running in an open Blender."""
    try:
        with socket.create_connection(("127.0.0.1", BLENDER_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def tool_prefix(server_name):
    """Claude Code names a server's tools mcp__<name>__<tool>, with odd characters made '_'."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server_name)


class BlenderSetup:
    """One-click setup, run in the background; progress lines go to .log for the UI."""

    def __init__(self, clean_env):
        self.clean_env = clean_env
        self.state = "idle"      # idle | running | done | failed
        self.log = []

    def _say(self, text):
        print("Blender setup:", text)
        self.log.append(text)

    def _run(self, args, timeout=300):
        exe = shutil.which(args[0]) or args[0]
        try:
            r = subprocess.run([exe] + args[1:], capture_output=True, timeout=timeout,
                               env=self._env(), creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
            out = (r.stdout + b"\n" + r.stderr).decode("utf-8", errors="replace").strip()
            return r.returncode, out
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, str(e)

    def _env(self):
        env = self.clean_env()
        # uv installs to ~/.local/bin (and older versions to ~/.cargo/bin); make sure we can find it
        extra = [os.path.join(os.path.expanduser("~"), ".local", "bin"),
                 os.path.join(os.path.expanduser("~"), ".cargo", "bin")]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        return env

    def _which(self, name):
        return shutil.which(name, path=self._env()["PATH"])

    def start(self):
        if self.state == "running":
            return
        self.state, self.log = "running", []
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            path, ver = find_blender()
            self._say(f"Blender found: {path} ({ver or 'version unknown'})" if path
                      else "Blender wasn't found in the usual places. Live control still works if Blender is installed elsewhere.")
            # 1. uv (runs the connector)
            if not self._which("uvx"):
                self._say("Installing uv (the small tool that runs the Blender connector)…")
                if sys.platform == "win32":
                    code, out = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "ByPass", "-Command",
                                           "irm https://astral.sh/uv/install.ps1 | iex"])
                else:
                    code, out = self._run(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"])
                if not self._which("uvx"):
                    raise RuntimeError("Couldn't install uv automatically:\n" + out[-600:])
            self._say("uv is ready.")
            uvx = self._which("uvx")
            # 2. the Blender add-on
            self._say("Copying the Blender MCP add-on into Blender's add-ons folder…")
            code, out = self._run([uvx, "blender-mcp", "install-addon"])
            self._say(out[-500:] if out else "(no output)")
            addon_ok = code == 0
            # 3. register the connector with Claude Code and Codex
            cmd = [uvx, "blender-mcp"]
            for tool, add in (("claude", ["mcp", "add", "--scope", "user", "-e", "DISABLE_TELEMETRY=true",
                                          SERVER_NAME, "--"] + cmd),
                              ("codex", ["mcp", "add", "--env", "DISABLE_TELEMETRY=true", SERVER_NAME, "--"] + cmd)):
                if not shutil.which(tool):
                    self._say(f"{tool} not found; skipped.")
                    continue
                code, out = self._run([tool] + add, timeout=60)
                if code == 0:
                    self._say(f"Connected Blender to {'Claude' if tool == 'claude' else 'GPT'}.")
                elif "already exists" in out.lower():
                    self._say(f"{'Claude' if tool == 'claude' else 'GPT'} already had the Blender connector.")
                else:
                    self._say(f"Couldn't add the connector to {tool}: {out[-300:]}")
            self._say("Last step, in Blender: Edit → Preferences → Add-ons, search \"MCP\", tick "
                      "\"Interface: Blender MCP\"" + ("" if addon_ok else " (if it isn't listed, click Install… and pick the "
                      "addon.py file from github.com/ahujasid/blender-mcp)") + ". Then in the 3D view press N, open the "
                      "BlenderMCP tab and click Connect / Start server. Amalgam shows \"Blender is open with the add-on running\" when it works.")
            self.state = "done"
        except Exception as e:
            self._say(f"Setup stopped: {e}")
            self.state = "failed"
