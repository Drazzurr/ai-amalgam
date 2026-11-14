"""The Artisan: a third member of the chat for art and 3D, thinking with GPT (on the user's ChatGPT plan) and
making things with free models that run on this PC through ComfyUI:

- PAINT: pictures with FLUX.1 dev (text to image, or reworking an image);
- SCULPT: a textured 3D model (.glb) from a picture with TRELLIS.2 (ComfyUI's official template);
- Blender (already in the app) for cleanup, print prep, rigging and turntable renders.

ComfyUI runs in the background only while the Artisan works. Models are unloaded a few minutes after the
last job and ComfyUI is closed after ten, so the graphics card is free for games.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from .comfy import FLUX_FILE, FLUX_URL, TRELLIS_TEMPLATE, Comfy, ComfyError, api_with_image, flux_prompt, \
    template_models, trellis_prompt

PORT = 8189
UNLOAD_AFTER = 180       # seconds idle before models leave the graphics card
STOP_AFTER = 600         # seconds idle before ComfyUI closes
PAINT_RE = re.compile(r"^\W*PAINT\s*:\s*(.+?)\s*$")
SCULPT_RE = re.compile(r"^\W*SCULPT\s*:\s*(.+?)\s*$")
MAX_JOBS = 3
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
GITHUB_RELEASES = os.environ.get("AMALGAM_COMFY_RELEASES", "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest")


def tools_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AIAmalgam", "tools")


def skills_index():
    out = []
    for p in sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md"))):
        with open(p, encoding="utf-8") as f:
            first = next((ln.strip("# \n") for ln in f if ln.strip()), os.path.basename(p))
        out.append((os.path.basename(p)[:-3], first, p))
    return out


class Artisan:
    def __init__(self, store, workspace):
        self.store = store
        self.workspace = workspace
        self.proc = None
        self.last_used = 0.0
        self.loaded = False
        self.busy = False
        self.install_state = {"running": False, "step": "", "done": 0, "total": 0, "error": "", "finished": False}
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._idle_watch, daemon=True).start()

    # ----- where things are -----

    def comfy_dir(self):
        d = self.store.get_setting("artisan_comfy_dir", "")
        return d or os.path.join(tools_dir(), "ComfyUI_windows_portable")

    def own_url(self):
        """A ComfyUI the user runs themselves (e.g. ComfyUI Desktop), if they set one."""
        return (self.store.get_setting("artisan_comfy_url", "") or "").strip().rstrip("/")

    def url(self):
        return self.own_url() or f"http://127.0.0.1:{PORT}"

    def _python(self):
        d = self.comfy_dir()
        for p in (os.path.join(d, "python_embeded", "python.exe"), os.path.join(d, "python_embedded", "python.exe"),
                  os.path.join(d, ".venv", "Scripts", "python.exe"), os.path.join(d, "venv", "bin", "python")):
            if os.path.isfile(p):
                return p
        return None

    def _main(self):
        d = self.comfy_dir()
        for p in (os.path.join(d, "ComfyUI", "main.py"), os.path.join(d, "main.py")):
            if os.path.isfile(p):
                return p
        return None

    def models_dir(self):
        main = self._main()
        return os.path.join(os.path.dirname(main), "models") if main else None

    def installed(self):
        return bool(self.own_url() or (self._python() and self._main()))

    def flux_on_disk(self):
        m = self.models_dir()
        return bool(m and os.path.isfile(os.path.join(m, "checkpoints", FLUX_FILE)))

    def status(self):
        own = bool(self.own_url())
        c = Comfy(self.url())
        running = c.alive()
        st = {"installed": self.installed(), "own": own, "url": self.url(), "dir": "" if own else self.comfy_dir(),
              "running": running, "busy": self.busy, "flux": self.flux_on_disk(), "install": dict(self.install_state),
              "workflow_3d": self.store.get_setting("artisan_3d_workflow", ""), "pc": True}
        if running and own:
            try:
                st["flux"] = any("flux1-dev" in n for n in c.choices("CheckpointLoaderSimple", "ckpt_name"))
            except ComfyError:
                pass
        return st

    # ----- starting and stopping ComfyUI -----

    def ensure_running(self, say=None):
        c = Comfy(self.url())
        if c.alive():
            return c
        if self.own_url():
            raise ComfyError(f"Your ComfyUI at {self.own_url()} isn't open. Start it, or switch back to Amalgam's own "
                             "ComfyUI in Apps → Artisan.")
        py, main = self._python(), self._main()
        if not (py and main):
            raise ComfyError("ComfyUI isn't installed yet. Press Install in Apps → Artisan (one time).")
        if os.name != "nt" and not os.access(py, os.X_OK):
            os.chmod(py, 0o755)        # unzipped on Linux: give the interpreter its run permission back
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                log = open(os.path.join(self.workspace, "comfyui.log"), "ab")
                args = [py, "-s", main, "--listen", "127.0.0.1", "--port", str(PORT), "--disable-auto-launch"]
                if os.name == "nt":
                    args.append("--windows-standalone-build")
                self.proc = subprocess.Popen(args, cwd=os.path.dirname(main), stdout=log, stderr=subprocess.STDOUT,
                                             creationflags=NO_WINDOW)
        if say:
            say("starting ComfyUI")
        for _ in range(240):
            if c.alive():
                return c
            if self.proc.poll() is not None:
                raise ComfyError("ComfyUI closed while starting. The end of comfyui.log in Amalgam's data folder says why.")
            time.sleep(0.5)
        raise ComfyError("ComfyUI took too long to start.")

    def stop(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    Comfy(self.url()).free()
                except Exception:
                    pass
                from codework import _kill_tree
                _kill_tree(self.proc)
            self.proc = None
            self.loaded = False

    def _idle_watch(self):
        while not self._stop:
            time.sleep(15)
            if self.busy or not self.last_used:
                continue
            idle = time.time() - self.last_used
            if self.loaded and idle > UNLOAD_AFTER:
                Comfy(self.url()).free()        # works for the user's own ComfyUI too
                self.loaded = False
            if self.proc and idle > STOP_AFTER:
                self.stop()

    # ----- making things -----

    def paint(self, text, out_dir, width=1024, height=1024, seed=None, from_image=None, strength=0.6, say=None, stop=None):
        c = self.ensure_running(say)
        names = c.choices("CheckpointLoaderSimple", "ckpt_name")
        ckpt = next((n for n in names if os.path.basename(n) == FLUX_FILE), None) or \
            next((n for n in names if "flux1-dev" in n.lower()), None)
        if not ckpt:
            raise ComfyError("FLUX.1 dev isn't downloaded yet. Press Install in Apps → Artisan to get it (about 17 GB).")
        img = c.upload_image(from_image) if from_image else None
        width, height = (max(256, min(2048, int(v) // 16 * 16)) for v in (width, height))
        prompt, seed = flux_prompt(ckpt, text, width, height, seed, image_name=img, strength=strength)
        self._mark(True)
        try:
            if say:
                say("painting")
            outs = c.run(prompt, timeout=1200, stop=stop)
            files = c.fetch_outputs(outs, out_dir, f"artisan-{int(time.time())}")
        finally:
            self._mark(False)
        return [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))], seed

    def sculpt(self, image, out_dir, say=None, stop=None):
        c = self.ensure_running(say)
        name = c.upload_image(image)
        own = self.store.get_setting("artisan_3d_workflow", "")
        if own and os.path.isfile(own):
            with open(own, encoding="utf-8") as f:
                wf = json.load(f)
            prompt = api_with_image(wf, name) if "nodes" not in wf else trellis_prompt(wf, c.object_info(), name)
        else:
            try:
                tpl = c.template(TRELLIS_TEMPLATE)
            except ComfyError:
                raise ComfyError("This ComfyUI doesn't have the TRELLIS.2 3D template. Update ComfyUI (Apps → Artisan → "
                                 "Update), it needs version 0.34 or newer.") from None
            prompt = trellis_prompt(tpl, c.object_info(), name)
        self._mark(True)
        try:
            if say:
                say("sculpting a 3D model")
            outs = c.run(prompt, timeout=2400, stop=stop)
            files = c.fetch_outputs(outs, out_dir, f"artisan-3d-{int(time.time())}")
        finally:
            self._mark(False)
        return files

    def _mark(self, busy):
        self.busy = busy
        self.last_used = time.time()
        if busy:
            self.loaded = True

    # ----- the Artisan's lines in the chat -----

    def handle(self, kind, text, out_dir, attachments, recent_images, say=None, stop=None):
        """Run one PAINT:/SCULPT: line. Returns (message text, [image paths], [other files])."""
        parts = [p.strip() for p in text.split("|")]
        main, opts = parts[0], {}
        for p in parts[1:]:
            k, _, v = p.partition(" ")
            opts[k.lower()] = v.strip()

        def pick_image(ref):
            ref = (ref or "").strip().strip('"')
            if not ref or ref.lower() in ("last", "latest", "previous"):
                return recent_images[-1] if recent_images else None
            if os.path.isfile(ref):
                return ref
            for p in list(attachments) + list(recent_images):
                if os.path.basename(p).lower() == os.path.basename(ref).lower():
                    return p
            cand = os.path.join(self.workspace, ref)
            return cand if os.path.isfile(cand) else None

        if kind == "PAINT":
            w, h = 1024, 1024
            m = re.match(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", opts.get("size", ""))
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            src = None
            if "from" in opts:
                src = pick_image(opts["from"])
                if not src:
                    return f"PAINT: couldn't find the picture \"{opts['from']}\" to rework.", [], []
            try:
                strength = float(opts.get("strength", 0.6))
            except ValueError:
                strength = 0.6
            seed = int(opts["seed"]) if opts.get("seed", "").isdigit() else None
            imgs, seed = self.paint(main, out_dir, w, h, seed, src, strength, say, stop)
            return (f"PAINT ({w}×{h}, seed {seed}{', reworking ' + os.path.basename(src) if src else ''}): "
                    f"{len(imgs)} picture(s).", imgs, [])
        src = pick_image(main)
        if not src:
            return ("SCULPT: which picture? Use SCULPT: last (the newest picture in the chat) or SCULPT: <file name>. "
                    "Best results: one subject, plain background, front three-quarter view, whole object visible."), [], []
        files = self.sculpt(src, out_dir, say, stop)
        meshes = [f for f in files if f.lower().endswith((".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx"))]
        imgs = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        if not meshes:
            return "SCULPT finished but ComfyUI didn't save a 3D file. Check the 3D workflow in Apps → Artisan.", imgs, []
        return (f"SCULPT from {os.path.basename(src)}: 3D model saved at {meshes[0]}. Open it in Blender (File → "
                f"Import → glTF) or use it with the Blender tools.", imgs, meshes)

    # ----- one-time setup -----

    def start_install(self, parts=("comfy", "flux", "trellis")):
        with self._lock:
            if self.install_state["running"]:
                return False
            self.install_state = {"running": True, "step": "starting", "done": 0, "total": 0, "error": "", "finished": False}
        threading.Thread(target=self._install, args=(tuple(parts),), daemon=True).start()
        return True

    def _say(self, step, done=0, total=0):
        self.install_state.update(step=step, done=done, total=total)

    def _download(self, url, dest, label):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "AI-Amalgam", **({"Range": f"bytes={have}-"} if have else {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            with open(part, "ab" if have else "wb") as f:
                got = have
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    self._say(f"Downloading {label}", got, total)
        os.replace(part, dest)

    def _install(self, parts):
        try:
            if "comfy" in parts and not self.own_url() and not (self._python() and self._main()):
                self._say("Finding the latest ComfyUI")
                with urllib.request.urlopen(urllib.request.Request(GITHUB_RELEASES, headers={"User-Agent": "AI-Amalgam"}),
                                            timeout=60) as r:
                    rel = json.loads(r.read())
                asset = next((a for a in rel.get("assets", []) if re.search(r"windows_portable_nvidia.*\.7z$", a["name"], re.I)), None) \
                    or next((a for a in rel.get("assets", []) if "portable" in a["name"].lower() and a["name"].endswith((".7z", ".zip"))), None)
                if not asset:
                    raise RuntimeError("Couldn't find the ComfyUI download for Windows on GitHub.")
                target = self.comfy_dir()
                os.makedirs(os.path.dirname(target), exist_ok=True)
                arc = os.path.join(os.path.dirname(target), asset["name"])      # same drive as the install
                if not os.path.isfile(arc):
                    self._download(asset["browser_download_url"], arc, f"ComfyUI {rel.get('tag_name', '')}")
                from forge import archives
                if archives.kind(arc) == "7z":           # by its contents, not its name
                    self._say("Getting 7-Zip's tool to unpack it")
                    archives.ensure_7z()
                self._say("Unpacking ComfyUI (a few minutes)")
                unpack = target + ".unpack"
                shutil.rmtree(unpack, ignore_errors=True)
                archives.extract(arc, unpack)
                found = glob.glob(os.path.join(unpack, "*", "ComfyUI", "main.py")) + glob.glob(os.path.join(unpack, "ComfyUI", "main.py"))
                if found:
                    inner = os.path.dirname(os.path.dirname(found[0]))
                    os.makedirs(target, exist_ok=True)
                    for name in os.listdir(inner):
                        shutil.move(os.path.join(inner, name), os.path.join(target, name))
                shutil.rmtree(unpack, ignore_errors=True)
                if not (self._python() and self._main()):
                    raise RuntimeError("ComfyUI unpacked, but its files aren't where Amalgam expected.")
                try:
                    os.remove(arc)
                except OSError:
                    pass
            if "flux" in parts and not self.own_url() and not self.flux_on_disk():
                self._download(FLUX_URL, os.path.join(self.models_dir(), "checkpoints", FLUX_FILE), "FLUX.1 dev (17 GB)")
            if "trellis" in parts and not self.own_url():
                self._say("Starting ComfyUI to find the 3D model files")
                c = self.ensure_running()
                try:
                    tpl = c.template(TRELLIS_TEMPLATE)
                except ComfyError:
                    tpl = None
                files = template_models(tpl) if tpl else []
                if not files:
                    self.install_state["note"] = ("ComfyUI didn't list the TRELLIS.2 files. Open ComfyUI once (Apps → Artisan → "
                                                  "Open ComfyUI), load Templates → 3D → Pixal3D/Trellis.2, and let it download "
                                                  "the missing models.")
                for i, m in enumerate(files):
                    dest = os.path.join(self.models_dir(), *m["directory"].split("/"), m["name"])
                    if not os.path.isfile(dest):
                        self._download(m["url"], dest, f"3D model files ({i + 1} of {len(files)}: {m['name']})")
            self._say("Done")
            self.install_state["finished"] = True
        except Exception as e:
            self.install_state["error"] = f"{type(e).__name__}: {e}" if not isinstance(e, (RuntimeError, ComfyError)) else str(e)
        finally:
            self.install_state["running"] = False

    def move_to(self, folder):
        """Use (or move ComfyUI and its models to) another folder, e.g. on a bigger drive."""
        if not os.path.isabs(folder):
            raise ValueError("Use a full folder path, like D:\\AI Amalgam Tools\\ComfyUI.")
        old = self.comfy_dir()
        if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(folder)):
            return
        new_has = os.path.isfile(os.path.join(folder, "ComfyUI", "main.py"))
        old_has = bool(self._python() and self._main())
        if new_has or not old_has:          # a ComfyUI already there, or nothing to move: just use the new place
            os.makedirs(folder, exist_ok=True)
            self.store.set_setting("artisan_comfy_dir", folder)
            return
        if os.path.isdir(folder) and os.listdir(folder):
            raise ValueError("To move ComfyUI there, pick an empty (or new) folder.")
        with self._lock:
            if self.install_state["running"] or self.busy:
                raise ValueError("The Artisan is busy. Try again when it's done.")
            self.install_state = {"running": True, "step": "Moving ComfyUI and its models", "done": 0, "total": 0,
                                  "error": "", "finished": False}
        threading.Thread(target=self._move, args=(old, folder), daemon=True).start()

    def _move(self, old, new):
        try:
            self.stop()
            total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(old) for f in fs)
            done = 0
            os.makedirs(new, exist_ok=True)
            for dp, ds, fs in os.walk(old):         # copy everything first, so nothing is lost if it fails half way
                rel = os.path.relpath(dp, old)
                os.makedirs(os.path.join(new, rel), exist_ok=True)
                for f in fs:
                    shutil.copy2(os.path.join(dp, f), os.path.join(new, rel, f))
                    done += os.path.getsize(os.path.join(dp, f))
                    self._say("Moving ComfyUI and its models", done, total)
            self.store.set_setting("artisan_comfy_dir", new)
            shutil.rmtree(old, ignore_errors=True)
            self._say("Moved")
            self.install_state["finished"] = True
        except Exception as e:
            self.install_state["error"] = f"Couldn't move it ({e}). It's still in the old place and still works."
        finally:
            self.install_state["running"] = False

    def update_comfy(self):
        """Run ComfyUI's own updater (portable install)."""
        d = self.comfy_dir()
        bat = next((p for p in (os.path.join(d, "update", "update_comfyui.bat"), os.path.join(d, "update", "update_comfyui.sh"))
                    if os.path.isfile(p)), None)
        if not bat:
            raise ComfyError("ComfyUI's updater wasn't found in its folder.")
        self.stop()
        r = subprocess.run([bat] if os.name == "nt" else ["sh", bat], cwd=os.path.dirname(bat), capture_output=True,
                           timeout=1800, creationflags=NO_WINDOW, input=b"\n")
        return (r.stdout + r.stderr).decode("utf-8", "replace")[-1500:]

    def set_own(self, url="", folder="", workflow=""):
        if url is not None:
            url = url.strip().rstrip("/")
            if url and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", url):
                raise ValueError("Use the address of ComfyUI on this PC, like http://127.0.0.1:8000.")
            self.store.set_setting("artisan_comfy_url", url)
        if folder is not None:
            folder = folder.strip().strip('"').rstrip("\\/")
            if folder:
                self.move_to(folder)
            else:
                self.store.set_setting("artisan_comfy_dir", "")
        if workflow is not None:
            workflow = workflow.strip()
            if workflow:
                with open(workflow, encoding="utf-8") as f:
                    json.load(f)
            self.store.set_setting("artisan_3d_workflow", workflow)
        return self.status()


def copy_skills_note():
    return "\n".join(f"- {name}: {title} (open {path})" for name, title, path in skills_index())

