"""Model runners: each one sends a prompt to a model and returns its reply.

Claude and GPT run through the official command-line tools, signed in with
your own subscriptions (Claude Pro, ChatGPT Plus). No API keys are used.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

# Environment variables that would switch the tools from your subscription
# to pay-per-use API billing. They are removed before every call.
BILLING_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
]

# Tools Claude may use without asking: web lookups and READ-ONLY access to the Google
# Drive / Docs connectors on your Claude account. Nothing here can change or delete files.
CLAUDE_READ_ONLY_TOOLS = [
    "Read", "Glob", "Grep",   # files you attach (saved in the app's workspace folder)
    "Skill",                  # playbooks like world-bible-keeper (installed by the app)
    "WebSearch",
    "WebFetch",
    # Whole Drive and Docs connectors (so renamed or new read tools still work). Their edit
    # tools are blocked separately unless "Allow doc edits" is on, and trash/share always are.
    "mcp__claude_ai_Google_Drive",
    "mcp__claude_ai_Google_Docs",
]

# Added only when "Allow doc edits" is on for the chat.
CLAUDE_DOC_EDIT_TOOLS = [
    "mcp__claude_ai_Google_Docs__update_doc",
    "mcp__claude_ai_Google_Drive__update_file",
    "mcp__claude_ai_Google_Drive__create_file",
    "mcp__claude_ai_Google_Drive__copy_file",
    "mcp__claude_ai_Google_Drive__add_comment_reply",
    "mcp__claude_ai_Google_Drive__resolve_comment_thread",
]

# Never allowed, even with doc edits on.
CLAUDE_BLOCKED_TOOLS = [
    "Bash", "PowerShell", "Edit", "Write", "NotebookEdit",
    "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Google_Drive__share_file",
    "mcp__claude_ai_Google_Drive__request_access",
]

# Coding area: Claude may edit files, but only inside the project's code folder (Claude Code's
# acceptEdits mode only auto-approves edits in the folder it runs in). Shell commands stay blocked:
# the models ask with RUN: lines and the app runs them after you approve.
CLAUDE_CODE_BLOCKED_TOOLS = [
    "Bash", "PowerShell",
    "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Google_Drive__share_file",
    "mcp__claude_ai_Google_Drive__request_access",
]

TURN_TIMEOUT_SECONDS = 600  # give up on a single reply after 10 minutes

# Windows: don't flash a console window for each call.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class RunnerError(Exception):
    """A model could not reply (not installed, not signed in, limit hit...)."""


def clean_env():
    env = dict(os.environ)
    for var in BILLING_VARS:
        env.pop(var, None)
    return env


def billing_vars_present():
    return [v for v in BILLING_VARS if os.environ.get(v)]


def to_epoch(v):
    """Unix seconds from a number (s or ms) or an ISO date string."""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def read_codex_rate_limits(max_files=12):
    """Find the newest rate-limit snapshot Codex wrote to its session logs.

    Lines look like {"timestamp": "...", "type": "event_msg", "payload": {"type": "token_count",
    "rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": ...},
    "secondary": {...}}}}"""
    root = os.path.join(codex_home(), "sessions")
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith(".jsonl"):
                p = os.path.join(dirpath, n)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    for _mtime, path in sorted(files, reverse=True)[:max_files]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            if "rate_limits" not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
            rl = payload.get("rate_limits") or (payload.get("msg") or {}).get("rate_limits")
            if not isinstance(rl, dict) or not (rl.get("primary") or rl.get("secondary")):
                continue
            stamp = to_epoch(ev.get("timestamp")) if ev.get("timestamp") else _mtime
            out = {"as_of": stamp}
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not isinstance(w, dict):
                    continue
                pct = w.get("used_percent", w.get("usedPercent"))
                resets = w.get("resets_at", w.get("resetsAt"))
                if resets is not None:
                    resets = to_epoch(resets)
                elif w.get("resets_in_seconds") is not None and stamp:
                    resets = stamp + float(w["resets_in_seconds"])
                mins = w.get("window_minutes", w.get("windowMinutes"))
                if resets and resets < time.time():
                    pct, stale = 0.0, True   # that window has reset since this snapshot
                else:
                    stale = False
                out[key] = {"used_percent": pct, "resets_at": resets, "window_minutes": mins, "reset_since": stale}
            return out
    return None


def run_command(exe, args, timeout=90):
    """Run a short helper command (not a model turn) and return its text output."""
    path = shutil.which(exe)
    if not path:
        raise RunnerError(f"'{exe}' isn't installed or isn't on PATH.")
    try:
        done = subprocess.run([path] + args, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, env=clean_env(), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RunnerError("took too long to answer")
    out = done.stdout.decode("utf-8", errors="replace") + "\n" + done.stderr.decode("utf-8", errors="replace")
    return out.strip()


def _status_word(text):
    t = text.lower()
    if "fail" in t or "error" in t or "✗" in text:
        return "failed"
    if "auth" in t or "sign in" in t or "login" in t or "⚠" in text:
        return "needs sign-in"
    if "connected" in t or "✓" in text or "enabled" in t or "ready" in t:
        return "connected"
    if "disabled" in t:
        return "disabled"
    return "unknown"


def parse_claude_mcp_list(text):
    """Lines look like:  'claude.ai Google Drive: https://... - ✓ Connected'"""
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(.+?):\s+(.*)\s+-\s+(.+)$", line)
        if not m or line.lower().startswith(("checking", "error")):
            continue
        name, status = m.group(1).strip(), m.group(3).strip()
        source = "Claude account" if name.lower().startswith("claude.ai ") else "Claude Code"
        if source == "Claude account":
            name = name[len("claude.ai "):].strip()
        items.append({"name": name, "status": _status_word(status), "source": source})
    return items


def parse_codex_mcp_list(text):
    """`codex mcp list` prints a table with a header row (Name ... Status ...), or a 'no servers' note."""
    if "no mcp servers" in text.lower():
        return []
    items = []
    lines = [l for l in text.splitlines() if l.strip()]
    header_seen = False
    for line in lines:
        cells = line.split()
        if not header_seen:
            if cells and cells[0].lower() == "name":
                header_seen = True
            continue
        if set(line.strip()) <= set("-─ "):
            continue
        items.append({"name": cells[0], "status": _status_word(line), "source": "Codex"})
    return items


SPEEDS = ("fast", "balanced", "best")

# How each speed setting is passed to the tools. "Balanced" leaves each tool on its own default.
CLAUDE_SPEED_ARGS = {"fast": ["--model", "haiku"], "balanced": [], "best": ["--model", "opus"]}
CODEX_SPEED_ARGS = {"fast": ["-c", 'model_reasoning_effort="low"'], "balanced": [],
                    "best": ["-c", 'model_reasoning_effort="high"']}

SIGNAL_TAIL_RE = re.compile(r"\n?\W*(NEXT|DONE|AGREE|HOLD\b.*|NEED USER\b.*)\W*$", re.IGNORECASE)


CHOICE_LINE_RE = re.compile(r"^\W*CHOICE\s*:.*(?:\n|$)", re.IGNORECASE | re.MULTILINE)


def save_doc_copy(workdir, file_id, text):
    """Keep a local copy of a Google Doc a model just read, for turns when Drive doesn't load."""
    folder = os.path.join(workdir, "docs")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "", file_id)[:80]
    with open(os.path.join(folder, safe + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)


def doc_copy(workdir, file_id):
    """(path, age in seconds) of the saved copy of a doc, or (None, None)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", file_id or "")[:80]
    path = os.path.join(workdir, "docs", safe + ".txt")
    if safe and os.path.isfile(path):
        return path, time.time() - os.path.getmtime(path)
    return None, None


def find_new_images(since, dest_dir, prefix):
    """Images Codex generated during this turn. Codex keeps them in its session log (as base64)
    and, in newer versions, as files under ~/.codex; collect both and save them as files."""
    import base64
    saved, seen = [], set()
    roots = [os.path.join(codex_home(), "sessions"), os.path.join(codex_home(), "generated_images"),
             os.path.join(codex_home(), "images")]
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    if os.path.getmtime(p) < since - 2:
                        continue
                except OSError:
                    continue
                low = n.lower()
                if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    os.makedirs(dest_dir, exist_ok=True)
                    out = os.path.join(dest_dir, f"{prefix}-{len(saved) + 1}{os.path.splitext(low)[1]}")
                    shutil.copyfile(p, out)
                    saved.append(out)
                elif low.endswith(".jsonl"):
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                    except OSError:
                        continue
                    for line in lines:
                        if "image" not in line or len(line) < 4000:
                            continue
                        if '"input_image"' in line or '"role":"user"' in line or '"role": "user"' in line:
                            continue        # a picture the user attached, not one GPT made
                        try:
                            ev = json.loads(line)
                            ts = ev.get("timestamp")
                            if ts and to_epoch(ts) and to_epoch(ts) < since - 2:
                                continue
                        except ValueError:
                            pass
                        for m in re.finditer(r'(?:data:image/(png|jpeg|webp);base64,)?((?:iVBORw0KGgo|/9j/|UklGR)[A-Za-z0-9+/=]{4000,})', line):
                            blob = m.group(2)
                            if blob[:64] in seen:
                                continue
                            seen.add(blob[:64])
                            try:
                                raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
                            except Exception:
                                continue
                            ext = ".png" if raw[:4] == b"\x89PNG" else ".jpg" if raw[:2] == b"\xff\xd8" else ".webp"
                            os.makedirs(dest_dir, exist_ok=True)
                            out = os.path.join(dest_dir, f"{prefix}-{len(saved) + 1}{ext}")
                            with open(out, "wb") as f:
                                f.write(raw)
                            saved.append(out)
    # the same picture can show up both as a file and in the log: keep one of each
    import hashlib
    unique, hashes = [], set()
    for p in saved:
        with open(p, "rb") as f:
            h = hashlib.sha1(f.read()).hexdigest()
        if h in hashes:
            os.remove(p)
        else:
            hashes.add(h)
            unique.append(p)
    return unique


def visible_text(text):
    """Text for the live preview: hide the NEXT/DONE/NEED USER control line at the end,
    including a half-typed one like 'NE'."""
    text = SIGNAL_TAIL_RE.sub("", text.rstrip()).rstrip()
    text = CHOICE_LINE_RE.sub("", text).rstrip()
    head, _, last = text.rpartition("\n")
    tail = last.strip().strip("*_`").upper()
    if tail and any(sig.startswith(tail) or tail.startswith("CHOICE") for sig in ("NEXT", "DONE", "NEED USER", "CHOICE", "AGREE", "HOLD")):
        return head.rstrip()
    return text


def tool_activity(name):
    """Turn a tool name into a short status like 'searching your Drive'."""
    n = (name or "").lower()
    if "search_files" in n or "list_recent" in n or "list_shared" in n:
        return "searching your Drive"
    if "update_doc" in n or "update_file" in n:
        return "editing the doc"
    if "create_file" in n or "copy_file" in n:
        return "creating a file in Drive"
    if "comment" in n:
        return "writing a comment"
    if "read_doc" in n or "read_file" in n or "download_file" in n or "metadata" in n:
        return "reading a doc"
    if "drive" in n or "docs" in n or "google" in n:
        return "using Google Drive"
    if "blender" in n:
        return "screenshotting Blender" if "screenshot" in n else "working in Blender"
    if "websearch" in n or "web_search" in n:
        return "searching the web"
    if n in ("edit", "multiedit", "write", "notebookedit"):
        return "editing code"
    if n in ("read", "glob", "grep"):
        return "reading code"
    if "webfetch" in n or "fetch" in n:
        return "reading a web page"
    return "using a tool"


DOC_ID_RE = re.compile(r"(?:/d/|^)([A-Za-z0-9_-]{25,})")


def describe_edit(tool, inp):
    """Summarise an edit tool call for the doc-edit log: (tool, doc id, short description)."""
    short = tool.split("__")[-1] if tool else "edit"
    doc_id, texts = "", []

    def walk(x, key=""):
        nonlocal doc_id
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str):
            k = key.lower()
            m = DOC_ID_RE.search(x)
            if not doc_id and m and ("id" in k or "url" in k or "doc" in k or "file" in k or "/d/" in x):
                doc_id = m.group(1)
            elif len(x) > 2 and "id" not in k:
                texts.append(x.strip())

    walk(inp)
    what = {"update_doc": "Edited doc", "update_file": "Updated file", "create_file": "Created file",
            "copy_file": "Copied file", "add_comment_reply": "Replied to a comment"}.get(short, short)
    detail = " ".join(texts)
    detail = (detail[:160] + "…") if len(detail) > 160 else detail
    return {"tool": short, "doc_id": doc_id, "summary": f"{what}: {detail}" if detail else what}


class Runner:
    name = "Model"
    key = "model"
    exe = None
    can_use_drive = False   # can read Google Drive / Docs
    can_edit_docs = False   # is the one who applies approved doc edits
    speed_args = {}

    def __init__(self, workdir):
        self.workdir = workdir
        self._proc = None
        self._stopped = False
        self.last_output = ""
        self.speed = "balanced"
        self._speed_ok = True   # set False if the tool rejects the speed option
        self._lock = threading.Lock()
        self.code_dir = None   # set by the orchestrator for chats in a project with a code folder

    def available(self):
        return shutil.which(self.exe) is not None if self.exe else True

    def status(self):
        return "ready" if self.available() else f"'{self.exe}' not found - see README step 2"

    def cancel(self):
        with self._lock:
            self._stopped = True
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    def _env(self):
        env = clean_env()
        if self.key == "claude":
            # Give the claude.ai connectors (Drive, Docs) time to connect before Claude starts.
            env.setdefault("MCP_TIMEOUT", "30000")
        return env

    def _speed(self):
        sp = getattr(self, "_speed_override", None) or self.speed
        return list(self.speed_args.get(sp, [])) if self._speed_ok else []

    def _run(self, args, prompt, on_line=None):
        """Run the tool, feed the prompt on stdin, and hand each stdout line to on_line as it arrives."""
        path = shutil.which(self.exe)
        if not path:
            raise RunnerError(f"{self.name}: the '{self.exe}' command isn't installed or isn't on PATH.")
        with self._lock:
            self._stopped = False
            self._proc = subprocess.Popen(
                [path] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.code_dir or self.workdir, env=self._env(), creationflags=NO_WINDOW)
            proc = self._proc
        err_chunks, out_lines = [], []
        reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(TURN_TIMEOUT_SECONDS, proc.kill)
        timer.start()
        timed_out = False
        try:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                out_lines.append(line)
                if on_line:
                    try:
                        on_line(line.rstrip("\r\n"))
                    except Exception as e:  # never let the preview break a turn
                        print("preview error:", e)
            proc.wait()
            timed_out = not timer.is_alive() and proc.returncode != 0 and not self._stopped
        finally:
            timer.cancel()
            reader.join(timeout=5)
            with self._lock:
                self._proc = None
        out_text = "".join(out_lines).strip()
        err_text = b"".join(c for c in err_chunks if c).decode("utf-8", errors="replace").strip()
        self.last_output = out_text
        if self._stopped:
            raise RunnerError(f"{self.name} was stopped.")
        if timed_out:
            raise RunnerError(f"{self.name} took longer than {TURN_TIMEOUT_SECONDS // 60} minutes.")
        if proc.returncode != 0:
            detail = (err_text or out_text)[-800:]
            raise RunnerError(f"{self.name} returned an error (code {proc.returncode}):\n{detail}")
        return out_text, err_text

    def reply(self, prompt, allow_edits=False, on_event=None, speed=None):
        """on_event(activity=..., text=...) is called with live progress while the model works.
        speed overrides the model's speed setting for this one call (used for quick summaries)."""
        self._speed_override = speed
        self.last_edits = []
        try:
            return self._reply(prompt, allow_edits, on_event)
        except RunnerError as e:
            # If the speed option itself was rejected (e.g. an older tool version), retry without it.
            msg = str(e).lower()
            if self._speed() and not self._stopped and any(w in msg for w in
                    ("unknown option", "unexpected argument", "unrecognized", "invalid value",
                     "unknown model", "model not found", "not supported", "invalid model")):
                print(f"{self.name}: speed option rejected, retrying without it:", e)
                self._speed_ok = False
                return self._reply(prompt, allow_edits, on_event)
            raise
        finally:
            self._speed_override = None

    def _reply(self, prompt, allow_edits, on_event):
        raise NotImplementedError

    def list_connections(self):
        return []

    def usage(self):
        """Plan usage info for the Usage card (None if not available)."""
        return None


class ClaudeRunner(Runner):
    """Claude via Claude Code in print mode: `claude -p`, prompt on stdin, streamed JSON out."""

    name = "Claude"
    key = "claude"
    exe = "claude"
    can_use_drive = True
    can_edit_docs = True
    speed_args = CLAUDE_SPEED_ARGS

    def list_connections(self):
        return parse_claude_mcp_list(run_command(self.exe, ["mcp", "list"]))

    def _reply(self, prompt, allow_edits, on_event):
        allowed = CLAUDE_READ_ONLY_TOOLS + (CLAUDE_DOC_EDIT_TOOLS if allow_edits else []) + \
            list(getattr(self, "extra_allowed", []) or [])
        blocked = (CLAUDE_CODE_BLOCKED_TOOLS if self.code_dir else CLAUDE_BLOCKED_TOOLS) + \
            ([] if allow_edits else CLAUDE_DOC_EDIT_TOOLS)
        self.diag = {"servers": {}, "denied": [], "tool_errors": []}
        args = [
            "-p",
            # stream-json: live text as Claude writes, tool activity, and rate-limit status.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            # Script mode can't show permission prompts, so the tools it may use are pre-approved.
            "--allowedTools", ",".join(allowed),
            # No shell commands or local file edits, and never delete or share Drive files.
            "--disallowedTools", ",".join(blocked),
        ] + (["--permission-mode", "acceptEdits"] if self.code_dir else []) + self._speed()
        live = {"text": ""}
        used = set()

        def note_tool(name):
            m = re.match(r"mcp__claude_ai_(.+?)__", name or "")
            if m:
                used.add(m.group(1).replace("_", " ").lower())

        def on_line(line):
            self._diagnose(line)
            if '"assistant"' in line and '"tool_use"' in line:
                try:
                    ev = json.loads(line)
                    for block in (ev.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and \
                                any(t in (block.get("name") or "") for t in CLAUDE_DOC_EDIT_TOOLS):
                            self.last_edits.append(describe_edit(block.get("name"), block.get("input") or {}))
                except ValueError:
                    pass
            if '"tool_use"' in line or '"mcp_tool_use"' in line:
                for name in re.findall(r'"name"\s*:\s*"(mcp__claude_ai_[^"]+)"', line):
                    note_tool(name)
            if not on_event or not line.startswith("{"):
                return
            try:
                ev = json.loads(line)
            except ValueError:
                return
            kind = ev.get("type")
            if kind == "stream_event":
                se = ev.get("event") or {}
                st = se.get("type")
                if st == "message_start":
                    live["text"] = ""
                elif st == "content_block_start":
                    block = se.get("content_block") or {}
                    if block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                        on_event(activity=tool_activity(block.get("name")))
                    elif block.get("type") == "thinking":
                        on_event(activity="thinking")
                elif st == "content_block_delta":
                    d = se.get("delta") or {}
                    if d.get("type") == "text_delta":
                        live["text"] += d.get("text", "")
                        on_event(activity="writing", text=visible_text(live["text"]))
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                        on_event(activity=tool_activity(block.get("name")))
            elif kind == "user":
                on_event(activity="thinking")
            elif kind == "system" and ev.get("subtype") == "init":
                on_event(activity="starting")

        try:
            out, _ = self._run(args, prompt, on_line)
        except RunnerError as e:
            text, failed = self._digest(self.last_output)
            if failed and text:
                raise RunnerError(f"Claude returned an error:\n{text[-800:]}")
            raise e
        text, failed = self._digest(out)
        if failed:
            raise RunnerError(f"Claude returned an error:\n{(text or out)[-800:]}")
        if not text:
            raise RunnerError("Claude returned an empty reply.")
        if used:
            self.verified_connectors = getattr(self, "verified_connectors", set()) | used
        return text

    def _diagnose(self, line):
        """Note which connectors loaded, which tools were refused, and connector errors, so the app
        can say why Claude couldn't open Drive instead of leaving it to guess."""
        if not line.startswith("{") or not any(k in line for k in ('"init"', '"permission_denials"', '"is_error"',
                                                                   '"tool_use"', '"tool_result"')):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        d = self.diag
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    d.setdefault("uses", {})[block.get("id")] = (block.get("name", ""), block.get("input") or {})
        if ev.get("type") == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    name, inp = d.get("uses", {}).get(block.get("tool_use_id"), ("", {}))
                    if re.search(r"Google_Docs__read_doc|Google_Drive__read_file_content", name):
                        c = block.get("content")
                        text = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                        fid = inp.get("documentId") or inp.get("fileId")
                        if fid and len(text) > 300:
                            save_doc_copy(self.workdir, fid, text)
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            for srv in ev.get("mcp_servers") or []:
                if isinstance(srv, dict) and srv.get("name"):
                    d["servers"][srv["name"]] = srv.get("status", "?")
            print("Claude connectors this turn:", d["servers"])
        elif ev.get("type") == "result":
            for den in ev.get("permission_denials") or []:
                name = den.get("tool_name") if isinstance(den, dict) else str(den)
                if name and name not in d["denied"]:
                    d["denied"].append(name)
        elif ev.get("type") == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    c = block.get("content")
                    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                    if text:
                        d["tool_errors"].append(text.strip()[:300])
        if d["denied"] or d["tool_errors"]:
            print("Claude tool problems:", d["denied"], d["tool_errors"][-3:])

    def _digest(self, raw):
        """Read Claude Code's stream-json lines: returns (final text, is_error) and records rate limits."""
        result, failed, chunks = None, False, []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = str(ev.get("type", ""))
            if "rate_limit" in kind:
                self._note_rate_limit(ev)
            elif kind == "result":
                result = ev.get("result")
                failed = bool(ev.get("is_error")) or str(ev.get("subtype", "success")) != "success"
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunks.append(block.get("text", ""))
        if result is None:
            result = chunks[-1] if chunks else ""
            if not chunks and raw and not raw.lstrip().startswith("{"):
                result = raw  # plain-text output from an older Claude Code
        return (result or "").strip(), failed

    def _note_rate_limit(self, ev):
        found = {}

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    lk = k.lower()
                    if lk == "status" and isinstance(v, str):
                        found.setdefault("status", v)
                    elif lk in ("resetsat", "resets_at") and v:
                        found.setdefault("resets_at", v)
                    elif lk in ("ratelimittype", "rate_limit_type") and v:
                        found.setdefault("window", v)
                    elif lk in ("utilization", "used_percentage", "usedpercent", "used_percent") and v is not None:
                        found.setdefault("used_percent", v)
                    else:
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(ev)
        if found:
            found["seen_at"] = time.time()
            if "resets_at" in found:
                found["resets_at"] = to_epoch(found["resets_at"])
            if isinstance(found.get("used_percent"), (int, float)) and found["used_percent"] <= 1:
                found["used_percent"] = found["used_percent"] * 100  # reported as a 0-1 fraction
            # Claude has a 5-hour session window and a weekly window; keep the latest of each.
            if not hasattr(self, "rate_windows"):
                self.rate_windows = {}
            self.rate_windows[str(found.get("window") or "session")] = found
            self.rate_limit = found
            print("Claude rate limit:", found)

    def usage(self):
        windows = dict(getattr(self, "rate_windows", {}) or {})
        info = dict(getattr(self, "rate_limit", None) or {})
        # overall status = the most severe window
        rank = lambda st: 2 if "reject" in st or "exceed" in st else 1 if "warn" in st else 0
        if windows:
            worst = max(windows.values(), key=lambda w: rank(str(w.get("status", "")).lower()))
            info["status"] = worst.get("status")
            soonest = [w["resets_at"] for w in windows.values() if w.get("resets_at") and
                       rank(str(w.get("status", "")).lower()) > 0]
            info["resets_at"] = min(soonest) if soonest else info.get("resets_at")
        order = lambda k: 0 if "five" in k or "session" in k or "hour" in k else 1
        info["windows"] = [dict(w, window=k) for k, w in sorted(windows.items(), key=lambda kv: order(kv[0]))]
        return {"kind": "status", **info, "link": "https://claude.ai/settings/usage"}


class CodexRunner(Runner):
    """GPT via Codex CLI: `codex exec --json -`, prompt on stdin, read-only sandbox."""

    name = "GPT"
    key = "gpt"
    exe = "codex"
    can_use_drive = True    # through the Google Drive app on your ChatGPT account
    speed_args = CODEX_SPEED_ARGS

    def list_connections(self):
        items = parse_codex_mcp_list(run_command(self.exe, ["mcp", "list"]))
        # ChatGPT apps live in your ChatGPT account, so `codex mcp list` can't show them.
        # Google Drive was confirmed working from inside AI Amalgam on 2026-11-07.
        return [{"name": "Google Drive", "status": "connected", "source": "ChatGPT app"}] + items

    def usage(self):
        info = read_codex_rate_limits()
        return {"kind": "percent", **(info or {}), "link": "https://chatgpt.com/settings/usage"}

    image_flag = ["-c", "features.image_generation=true"]

    def _maybe_save_doc(self, item):
        """When GPT reads a Google Doc through its Drive app, keep a local copy too."""
        try:
            args = item.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            ref = " ".join(str(v) for v in (args.values() if isinstance(args, dict) else []))
            m = re.search(r"([A-Za-z0-9_-]{25,})", ref)
            res = item.get("result") or {}
            parts = res.get("content") if isinstance(res, dict) else res
            text = parts if isinstance(parts, str) else "\n".join(
                p.get("text", "") for p in (parts or []) if isinstance(p, dict))
            if m and len(text) > 300 and re.search(r"fetch|read|get|open", str(item.get("tool", "")), re.I):
                save_doc_copy(self.workdir, m.group(1), text)
        except Exception:
            pass

    def _reply(self, prompt, allow_edits, on_event):
        self.last_images = []
        started = time.time()
        fd, out_file = tempfile.mkstemp(prefix="amalgam_gpt_", suffix=".txt")
        os.close(fd)
        errors = []

        def on_line(line):
            if not line.startswith("{"):
                return
            try:
                ev = json.loads(line)
            except ValueError:
                return
            kind = ev.get("type", "")
            if kind in ("error", "turn.failed"):
                msg = ev.get("message") or (ev.get("error") or {}).get("message")
                if msg:
                    errors.append(str(msg))
            if not on_event:
                return
            item = ev.get("item") or {}
            itype = item.get("type") or item.get("item_type") or ""
            if kind == "turn.started":
                on_event(activity="thinking")
            elif kind in ("item.started", "item.updated", "item.completed"):
                if itype == "reasoning":
                    on_event(activity="thinking")
                elif itype in ("mcp_tool_call", "tool_call", "function_call"):
                    on_event(activity=tool_activity(f"{item.get('server', '')} {item.get('tool', '') or item.get('name', '')}"))
                    if kind == "item.completed":
                        self._maybe_save_doc(item)
                elif itype == "web_search":
                    on_event(activity="searching the web")
                elif itype == "command_execution":
                    on_event(activity="running a check")
                elif itype in ("agent_message", "assistant_message") and item.get("text"):
                    on_event(activity="writing", text=visible_text(item["text"]))

        try:
            args = [
                "exec",
                "--json",                       # live progress events, one per line
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                # (no --ephemeral: Codex's session log holds the plan usage shown in the Usage card)
                "--output-last-message", out_file,
            ] + [f"--image={p}" for p in getattr(self, "images", []) if os.path.isfile(p)] + self._speed() + \
                self.image_flag + ["-"]
            try:
                out, _ = self._run(args, prompt, on_line)
            except RunnerError as e:
                if self.image_flag and not self._stopped and "image_generation" in str(e) + " ".join(errors):
                    print("Codex doesn't know the image_generation feature; turning it off.")
                    CodexRunner.image_flag = []
                    return self._reply(prompt, allow_edits, on_event)
                if errors and not self._stopped:
                    raise RunnerError(f"GPT returned an error:\n{errors[-1][-800:]}")
                raise e
            try:
                dest = getattr(self, "image_dir", None) or os.path.join(self.workdir, "images")
                self.last_images = find_new_images(started, dest, time.strftime("gpt-%Y%m%d-%H%M%S"))
            except Exception as e:
                print("Couldn't collect generated images:", e)
            try:
                with open(out_file, encoding="utf-8", errors="replace") as f:
                    text = f.read().strip()
            except OSError:
                text = ""
            if not text:
                # fall back to the last agent message in the event stream
                for line in reversed(out.splitlines()):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    item = ev.get("item") or {}
                    if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                        text = item["text"].strip()
                        break
            if not text:
                raise RunnerError("GPT returned an empty reply." + (f"\n{errors[-1]}" if errors else ""))
            return text
        finally:
            try:
                os.remove(out_file)
            except OSError:
                pass


class DemoRunner(Runner):
    """Stand-in model for trying the app without the real tools (--demo)."""

    exe = None

    def __init__(self, workdir, name, key):
        super().__init__(workdir)
        self.name = name
        self.key = key
        self.turns = 0
        self._cancelled = threading.Event()

    def status(self):
        return "demo stand-in"

    def list_connections(self):
        if self.key == "claude":
            return [{"name": "Google Drive", "status": "connected", "source": "Claude account"},
                    {"name": "Google Docs", "status": "connected", "source": "Claude account"},
                    {"name": "Gmail", "status": "connected", "source": "Claude account"},
                    {"name": "Google Calendar", "status": "needs sign-in", "source": "Claude account"}]
        return [{"name": "Google Drive", "status": "connected", "source": "ChatGPT app"}]

    def usage(self):
        now = time.time()
        if self.key == "gpt":
            return {"kind": "percent", "as_of": now, "link": "https://chatgpt.com/settings/usage",
                    "primary": {"used_percent": 12.0 + self.turns, "resets_at": now + 3 * 3600, "window_minutes": 300},
                    "secondary": {"used_percent": 4.0, "resets_at": now + 6 * 86400, "window_minutes": 10080}}
        return {"kind": "status", "status": "allowed", "window": "five_hour", "resets_at": now + 2 * 3600,
                "seen_at": now, "link": "https://claude.ai/settings/usage"}

    def cancel(self):
        self._cancelled.set()

    def _reply(self, prompt, allow_edits, on_event):
        self._cancelled.clear()
        self.turns += 1
        lines = [
            f"({self.name}, demo turn {self.turns}, {self.speed} speed) I've read the conversation so far.",
            "Here is my take:\n- break the goal into **small steps**\n- test each one\n  - then check the doc\n\n"
            "| Step | Owner |\n|---|---|\n| Plan | Claude |\n| Review | GPT |\n\n```python\nprint('hi')\n```",
        ]
        if self.code_dir and self.key == "claude" and "[Command result" not in prompt:
            with open(os.path.join(self.code_dir, "hello.py"), "w", encoding="utf-8") as f:
                f.write('print("Hello from AI Amalgam")\n')
            lines = [f"({self.name}, demo) I created hello.py. Let's run it to check.",
                     "RUN: python3 hello.py" if sys.platform != "win32" else "RUN: python hello.py",
                     "NEXT"]
        elif self.code_dir and "[Command result" in prompt:
            lines = [f"({self.name}, demo) The script ran fine. Looks good to me.", "DONE"]
        elif "CHECK" in prompt and "QUESTION ===" in prompt:
            lines = [f"({self.name}, demo check) I think we can draft a bit more before asking.",
                     "HOLD: we can still draft one more section first"]
        elif "YOUR QUESTION WAS HELD" in prompt:
            lines = [f"({self.name}, demo) Fair point, but I still think we need this now.",
                     "CHOICE: GPT's way - ask now", "CHOICE: Claude's way - draft more first",
                     "NEED USER: Should we decide speed vs polish now, or draft more first?"]
        elif self.turns == 2 and self.key == "gpt":
            lines += ["CHOICE: Speed first", "CHOICE: Polish first", "CHOICE: A bit of both",
                      "NEED USER: Should we aim for speed or for polish first?"]
        elif self.turns >= 4:
            lines.append("DONE")
        else:
            lines.append("NEXT")
        self.last_images = []
        if self.key == "gpt" and "draw" in prompt.lower() and getattr(self, "image_dir", None):
            try:
                from PIL import Image, ImageDraw
                os.makedirs(self.image_dir, exist_ok=True)
                im = Image.new("RGB", (512, 512), (40, 44, 60))
                d = ImageDraw.Draw(im)
                d.ellipse((96, 96, 416, 416), fill=(200, 160, 80))
                d.text((180, 240), "demo image", fill=(20, 20, 20))
                path = os.path.join(self.image_dir, f"gpt-demo-{self.turns}.png")
                im.save(path)
                self.last_images = [path]
                lines.insert(0, "(GPT, demo) Here's a quick sketch.")
            except Exception:
                pass
        if "TITLE: <" in prompt:
            lines.insert(-1, "TITLE: Demo planning session")
        full = "\n".join(lines)
        if allow_edits and self.key == "claude" and self.turns == 1:
            self.last_edits = [describe_edit("mcp__claude_ai_Google_Docs__update_doc",
                                             {"document_id": "1CrBAFuo12Bznhkj9sTx-m11v5IXCA_GdnEhMh-PA8Ok",
                                              "text": "Added a demo line under Overview."})]
        steps = [("starting", 3), ("searching your Drive", 6), ("reading a doc", 5)]
        for activity, ticks in steps:
            if on_event:
                on_event(activity=activity)
            for _ in range(ticks):
                if self._cancelled.wait(0.1):
                    raise RunnerError(f"{self.name} was stopped.")
        words = full.split(" ")
        for i in range(1, len(words) + 1):
            if self._cancelled.wait(0.04):
                raise RunnerError(f"{self.name} was stopped.")
            if on_event:
                on_event(activity="writing", text=visible_text(" ".join(words[:i])))
        return full


def make_runners(workdir, demo=False):
    if demo:
        return [DemoRunner(workdir, "Claude", "claude"), DemoRunner(workdir, "GPT", "gpt")]
    return [ClaudeRunner(workdir), CodexRunner(workdir)]
