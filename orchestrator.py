"""The orchestrator: decides who speaks next, builds each model's prompt,
and pauses for you when a model asks a question."""

import os
import re
import sys
import threading
import time

from codework import COMMAND_TIMEOUT, check_code_dir, run_command

# Each turn re-sends the conversation. Above this size, the oldest middle
# messages are left out (the first message - your goal - is always kept).
MAX_TRANSCRIPT_CHARS = 60000

SIGNAL_RE = re.compile(r"^\W*(NEXT|DONE|NEED USER|AGREE|HOLD)\b\W*:?\s*(.*)$", re.IGNORECASE)
MENTION_RE = re.compile(r"@(claude|gpt|chatgpt|codex|all)\b", re.IGNORECASE)

PROMPT_TEMPLATE = """You are {name}, one of several AI participants in a group discussion run by the AI Amalgam app.
Participants: you ({name}), {others}, and the user (the human who leads and makes the decisions).

How this works:
- The transcript below shows everything said so far. Each message is tagged with its speaker.
- Write ONLY your next message to the group. Talk to the other model(s) directly and move the work forward. Don't repeat points that are already agreed.
- You are teammates working for the user, not debaters. Build on each other's work. When a teammate makes a fair point, say so plainly ("good catch, I'll use that") and adopt it. If you disagree, say so once with your reason; don't reopen a point that's been settled. If you still disagree after one exchange, stop arguing and put both options to the user.
- Correct real problems (wrong facts, contradictions with the user's decisions or documents, missed requirements), but don't nitpick wording or taste.
- Keep it focused: aim for under 250 words unless the task needs more (for example a draft, a plan or code).
- Don't make decisions that belong to the user (preferences, budget, scope, taste). Ask instead, but collect your questions: do all the work you can first, then ask them together. If the user said how often they want to be asked (for example "keep going until you need me"), that wins.
{tools}{doc}
The last line of your message must be exactly one of these:
NEXT
DONE
NEED USER: <one clear question>
Use NEXT if the discussion should continue, DONE if the goal is fully met and you are happy with the result, and NEED USER when you need a decision, approval or clarification that only the user can give.
When your NEED USER question has a few clear answers, list them just above that last line, one per line, as "CHOICE: <short answer>" (2 to 5 choices, each under 15 words, e.g. "CHOICE: Option A - rewrite each section"). The app shows them to the user as buttons; the user can still type their own answer.

=== TRANSCRIPT ===
{transcript}
=== END OF TRANSCRIPT ===
{extra}
Now write your message as {name}."""

REVIEW_EXTRA = """
=== THIS TURN: CHECK {asker}'S QUESTION ===
{asker} wants to pause the discussion and ask the user:
{question}{choices}

Before it goes to the user, you check it as {asker}'s teammate. Write a short message (under 120 words): add anything the user should know before answering, if anything. Don't restart other debates here.
Then your last line must be exactly one of these (instead of NEXT/DONE):
AGREE
HOLD: <one-sentence reason>
NEED USER: <your own question>
Use AGREE if the question is needed now and fairly framed. Use HOLD only for a real problem: it's premature (the group can still make progress first, or the user said to keep working), the user already answered it, or the choices leave out an important option or are slanted. Use NEED USER if you agree and also need to ask the user something yourself (add CHOICE lines for it as usual).
"""

RECONSIDER_EXTRA = """
=== THIS TURN: YOUR QUESTION WAS HELD ===
You wanted to ask the user:
{question}
Your teammate{s} suggested holding it:
{holds}

Take their point seriously. Do one of these:
- Drop or postpone the question: say so in a sentence, then carry on with the work and end with NEXT.
- Ask a better question: fix it and end with NEED USER (with CHOICE lines).
- Ask it anyway: only if you still disagree. Say why in one sentence, and give CHOICE lines that fairly cover both your view and {others}'s view (for example "CHOICE: {me}'s way - ..." and "CHOICE: {other1}'s way - ...") so the user can settle it with one click. End with NEED USER.
"""

TOOLS_WITH_DRIVE = """
Your tools: you can read the user's Google Drive and Google Docs{web}.{peers_note} Don't assume the others have read the same parts you did: quote or summarise what you rely on.
"""

TOOLS_WITHOUT_DRIVE = """
Your tools: you cannot open Google Drive or Google Docs yourself. {drive_peer} can, and will share the relevant parts in the transcript; ask when you need something looked up.
"""

PROJECT_LINE = """
This chat is part of the user's project "{name}".{instructions}{roles}{memory}
Project memory: when the user confirms a decision or a new fact worth keeping for future chats in this project, add a line "REMEMBER: <one short sentence>" just above your last line. Only for things the user actually approved, and never repeat something already in project memory. The app saves these lines for every future chat in the project.
"""

LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|hit your limit|reached your limit|quota|too many requests|try again (later|at|in)",
                      re.IGNORECASE)
CHOICE_RE = re.compile(r"^\W*CHOICE\s*:\s*(.+?)\s*$", re.IGNORECASE)
MAX_CHOICES = 6
OPTION_RE = re.compile(r"\b(Option\s+[A-Z1-9])\b\s*(?:\(([^()]{1,160})\)|[:\-\u2013\u2014]\s*([^,;?()]{1,120}?)(?=\s*(?:,|;|\?|\bor\b|$)))?",
                       re.IGNORECASE)
YES_NO_RE = re.compile(r"^\s*(should|shall|do|does|did|can|could|would|will|is|are|may|approve|ok|okay|happy)\b", re.IGNORECASE)
REMEMBER_RE = re.compile(r"^\W*REMEMBER\s*:\s*(.+?)\s*$", re.IGNORECASE)
MEMORY_CHARS = 6000

# Condensing: once older messages (beyond the most recent ones) add up to this many characters,
# they're folded into a short running summary so each turn re-sends far less text.
CONDENSE_AT = 12000
KEEP_RECENT = 10

CONDENSE_PROMPT = """You are summarising part of a group discussion between a user and AI models, so the discussion can continue with less text.
Write an updated summary of under 350 words in plain bullet points. Keep: the user's goal, every decision the user confirmed, open questions, key facts or quotes from documents, and who proposed what. Drop chit-chat and repetition. Write only the summary.

{existing}=== MESSAGES TO FOLD IN ===
{messages}
=== END ===

Last line must be exactly: DONE"""

CODE_LINE = """
=== CODING ===
This project has a code folder on the user's PC: {path}
You are working inside it (it is your current folder). {role}
- To run anything (tests, scripts, installs, git, builds), put each command on its own line just above your last line, as: RUN: <command>
  The app shows them to the user, who approves or denies them; then everyone sees the output. Commands run in {shell} in the code folder, with a {timeout}-second limit each. Don't run servers, watchers or anything interactive; tell the user how to start those instead. At most 3 commands per message. Prefer read-only checks before anything that changes the system, and never run anything destructive (deleting folders, git push, force-resets) unless the user asked for exactly that.
- Never read or change files outside the code folder.
- Work as a team: the editor makes changes in small, reviewable steps and says what changed and why; the reviewer reads the changed files and the diff, points out real bugs or risks, and suggests concrete fixes. Test changes by asking to run them.
{changes}"""

BLENDER_LINE = """
=== BLENDER ===
The user's Blender is connected to you through your blender tools{status}. {who}
- Work in small steps and check the result with a viewport screenshot.
- Save the .blend file before big changes, and never delete or overwrite the user's files on disk unless they asked.
- Say what you changed in the scene.
"""
BLENDER_WHO_EDITOR = "You make the changes: inspect the scene, take screenshots, and run Blender Python (full access)."
BLENDER_WHO_REVIEWER = ("{editor} makes the changes in Blender. You inspect (scene info, screenshots) and review; "
                        "don't change the scene unless the user asks you directly.")
BLENDER_HEADLESS = ("Blender is installed at {path}. For batch jobs or renders without the open Blender, ask to run it "
                    "in the background: RUN: & \"{path}\" --background --python <script.py> (put scripts in the code folder).\n")

CODE_ROLE_EDITOR = "You are the editor: change files directly with your file edit and write tools."
CODE_ROLE_REVIEWER = ("You are the reviewer: you can read files and run read-only checks yourself, but you can't change "
                      "files. {editor} makes the changes; when you want one, say exactly which file and what to change.")
CODE_ROLE_NO_EDITOR = ("Nobody can edit files right now (the editor, Claude, is unavailable). Read and review, and "
                       "describe exact changes (file and code) for the user to apply or for later.")

TITLE_RE = re.compile(r"^\W*TITLE\s*:\s*(.+?)\s*$", re.IGNORECASE)
TITLE_LINE = ("\nThis is the first reply in a new chat: add one line \"TITLE: <a 3 to 6 word title for this chat>\" "
              "just above your last line.\n")
READ_RE = re.compile(r"^\W*READ\s*:\s*(.+?)\s*$", re.IGNORECASE)
MAX_READS = 4
GAMEFILES_LINE = """
=== GAME FILES ===
You can read game files directly (archives, plugins, packages, meshes, textures, talk tables, scripts), without the user exporting anything. Put a line READ: <full path> :: <what> just above your last line (up to 4 lines). The app runs it right away, read-only, and you get the result straight after.
<what> can be: nothing (overview), list <text>, extract <path inside the archive>, record <EditorID or FormID>, exports <text>, export <object>, find <text>, get <string id>, json, hex <offset> <length>, strings.
Extracted files and converted images (.png) go to {out}; READ those paths next, and open .png files with your file reader to see them.
Folders you may read: {folders}. Never change files in game folders. The game-files skill has details for each game.
"""
RUN_RE = re.compile(r"^\W*RUN\s*:\s*(.+?)\s*$", re.IGNORECASE)
MAX_RUNS = 3

DOC_LINE = """
Project document for this chat: {link}
This is the shared document the group is working on.{copy}
"""
DOC_COPY = ("\nIf your Google Drive/Docs tools aren't available this turn, don't stop: read the copy of this document "
            "saved {age} ago at {path} (it may miss very recent edits; say so if that matters). Edits still need the "
            "Docs tools.")
IMAGE_LINE = """
You can create images with your image generation tool (the user's ChatGPT plan covers it). When the user or the group asks for art, generate it rather than only describing it; the app shows your images in the chat and saves them, and the other models can look at them. Say in one line what you made.
"""

EDITS_ON_EDITOR = """
Document editing is ON, and you are the one who applies edits. Rules:
1. First agree on the change in the discussion.
2. Batch your edits: draft as many agreed changes as you sensibly can, then ask for approval once for the whole batch. List each change in your message (which document, where, the new text) and end with NEED USER. Don't stop to ask after every small change.
3. Apply only the changes the user's most recent message clearly approves (they may approve some and not others). Then say exactly what you changed.
4. Change only what was approved. Never delete whole sections, rename, move, share or trash files.
"""

EDITS_ON_OTHER = """
Document editing is ON, but only {editor} applies edits (after the user approves). Do NOT edit documents yourself, even if a tool lets you. Help shape the exact wording of proposed changes.
"""

EDITS_OFF = """
Document editing is OFF: read documents but never change them. If the group agrees on a change, write out the exact new text so the user can apply it, and mention they can switch on "Allow doc edits" in the app.
"""


def parse_signal(text):
    """Split a reply into (body, signal, question). Missing signal = NEXT."""
    lines = text.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        m = SIGNAL_RE.match(lines[-1].strip().strip("*_`"))
        if m:
            signal = m.group(1).upper()
            question = m.group(2).strip().strip("*_`").strip() if signal in ("NEED USER", "HOLD") else ""
            body = "\n".join(lines[:-1]).rstrip()
            return body or "(nothing to add)", signal, question
    return text.strip(), "NEXT", ""


def split_choices(text):
    """Pull 'CHOICE: ...' lines out of a reply. Returns (text without them, [choices])."""
    keep, choices = [], []
    for line in text.splitlines():
        m = CHOICE_RE.match(line)
        if m:
            c = m.group(1).strip().strip("*_`").strip()
            if c and c not in choices:
                choices.append(c[:200])
        else:
            keep.append(line)
    return "\n".join(keep), choices[:MAX_CHOICES]


def guess_choices(question):
    """Backup when a model forgot CHOICE lines: find 'Option A (...) or Option B (...)',
    or offer Yes / No for a plain yes-or-no question."""
    found, seen = [], set()
    for m in OPTION_RE.finditer(question):
        label = re.sub(r"\s+", " ", m.group(1)).title()
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        detail = (m.group(2) or m.group(3) or "").strip()
        found.append(f"{label} - {detail}" if detail else label)
    if len(found) >= 2:
        return found[:MAX_CHOICES]
    q = question.strip()
    if YES_NO_RE.match(q) and " or " not in q.lower() and q.endswith("?"):
        return ["Yes", "No"]
    return []


def split_runs(text):
    """Pull 'RUN: command' lines out of a reply. Returns (text without them, [commands])."""
    keep, runs = [], []
    for line in text.splitlines():
        m = RUN_RE.match(line)
        if m:
            cmd = m.group(1).strip().strip("`").strip()
            if cmd and cmd not in runs:
                runs.append(cmd[:500])
        else:
            keep.append(line)
    return "\n".join(keep), runs[:MAX_RUNS]


class Orchestrator:
    def __init__(self, store, runners, checkpoints=None):
        self.store = store
        self.cp = checkpoints          # codework.Checkpoints, for diffs and undo in code folders
        self.pending_commands = None   # {"msg_id", "speaker", "commands"} waiting for your approval
        self.question_open = False     # a question is waiting for your answer
        self.hint = None               # why the discussion stopped: limit | error | stopped | done
        self._cmd_proc = None
        self.runners = {r.key: r for r in runners}
        self.order = [r.key for r in runners]
        self.enabled = {r.key: True for r in runners}
        self.max_rounds = 4
        self.live = {"activity": "", "text": "", "rev": 0}   # what the current speaker is doing right now
        # remembered between sessions
        try:
            self.max_rounds = max(1, min(20, int(store.get_setting("max_rounds", 4))))
        except (TypeError, ValueError):
            pass
        for r in runners:
            sp = store.get_setting(f"speed_{r.key}", "balanced")
            r.speed = sp if sp in ("fast", "balanced", "best") else "balanced"
        self.chat_id = None
        self.project_id = None   # project of the open chat, or where the next new chat goes
        self.pending_chat_settings = {"doc_link": "", "allow_edits": False}  # before the chat exists
        self.status = "idle"  # idle | running | waiting
        self.speaker = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.pending_files = []   # uploaded files waiting to go with your next message
        self.turn_seq = 0   # bumps after every finished turn (the app refreshes usage on change)
        self.resting = {}
        self.last_question = None   # (speaker, question, choices) waiting for a teammate's check
        self.last_hold = ""   # model key -> time it can rejoin (after hitting its plan limit)
        self._thread = None
        self.check_questions = store.get_setting("check_questions", "1") == "1"
        self.blender_on = store.get_setting("blender_on", "0") == "1"
        self.blender_servers = ["blender"]   # connector names to allow (the app adds any found with "blender")
        self.blender_path = None
        self.game_folders = []      # set by the app: detected game installs + folders you added
        self.workspace = runners[0].workdir if runners else "."

    # ----- public actions (called from the web UI) -----

    def new_chat(self, project_id=None):
        self.stop()
        with self._lock:
            self.chat_id = None
            self.project_id = project_id
            self.hint = None
            self.pending_commands = None
            self.question_open = False
            self._apply_speeds()
            project = self.store.project(project_id)
            # new chats in a project start with the project's last "Allow doc edits" choice
            self.pending_chat_settings = {"doc_link": project["doc_link"] if project else "",
                                          "allow_edits": bool(project) and
                                          self.store.get_setting(f"allow_edits_project_{project_id}") == "1"}
            self.status = "idle"

    def open_chat(self, chat_id):
        self.stop()
        with self._lock:
            self.chat_id = chat_id
            self.project_id = self.store.chat_project(chat_id)
            self.status = "idle"
            self.hint = None
            self.pending_commands = None
            self.question_open = False
            self._apply_speeds()
            # pick up where the chat left off: an unanswered question or command request
            for m in reversed(self.store.messages(chat_id)):
                if m["kind"] == "system":
                    continue
                if m["kind"] == "command":
                    self.pending_commands = {"msg_id": m["id"], "speaker": m["speaker"],
                                             "commands": m["text"].splitlines()}
                    self.status = "waiting"
                elif m["kind"] == "question":
                    self.question_open = True
                    self.status = "waiting"
                    continue   # a command request may sit just before it
                break

    def _apply_speeds(self):
        """Use the open project's speed for each model if it has one, else your general choice."""
        for key, r in self.runners.items():
            sp = None
            if self.project_id:
                sp = self.store.get_setting(f"speed_{key}_project_{self.project_id}")
            sp = sp or self.store.get_setting(f"speed_{key}", "balanced")
            r.speed = sp if sp in ("fast", "balanced", "best") else "balanced"

    def settings(self, max_rounds=None, enabled=None, speeds=None, check_questions=None, notify=None):
        with self._lock:
            if check_questions is not None:
                self.check_questions = bool(check_questions)
                self.store.set_setting("check_questions", "1" if check_questions else "0")
            if notify is not None:
                self.store.set_setting("notify", "1" if notify else "0")
            if max_rounds is not None:
                self.max_rounds = max(1, min(20, int(max_rounds)))
                self.store.set_setting("max_rounds", self.max_rounds)
            if speeds:
                for key, sp in speeds.items():
                    if key in self.runners and sp in ("fast", "balanced", "best"):
                        self.runners[key].speed = sp
                        # in a project, the speed is remembered for that project only
                        name = f"speed_{key}_project_{self.project_id}" if self.project_id else f"speed_{key}"
                        self.store.set_setting(name, sp)
            if enabled:
                for key, on in enabled.items():
                    if key in self.enabled:
                        self.enabled[key] = bool(on)

    def chat_settings(self):
        if self.chat_id is None:
            return dict(self.pending_chat_settings)
        return self.store.chat_settings(self.chat_id) or dict(self.pending_chat_settings)

    def set_chat_settings(self, doc_link=None, allow_edits=None):
        with self._lock:
            if allow_edits is not None and self.project_id:
                self.store.set_setting(f"allow_edits_project_{self.project_id}", "1" if allow_edits else "0")
            if self.chat_id is None:
                if doc_link is not None:
                    self.pending_chat_settings["doc_link"] = doc_link.strip()
                if allow_edits is not None:
                    self.pending_chat_settings["allow_edits"] = bool(allow_edits)
            else:
                self.store.set_chat_settings(self.chat_id, doc_link, allow_edits)

    def send(self, text):
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self.chat_id is None:
                self.chat_id = self.store.new_chat(text.splitlines()[0], self.project_id)
                self.store.set_chat_settings(self.chat_id, **self.pending_chat_settings)
            if self.pending_files:
                names = []
                for f in self.pending_files:
                    rel = self._file_into_chat(f)
                    self.store.add_attachment(self.chat_id, f["name"], rel, f["size"])
                    names.append(f["name"])
                    if f["name"].lower().endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
                        unpacked, note = self._unpack(rel)
                        for name, path, size in unpacked:
                            self.store.add_attachment(self.chat_id, name, path, size)
                        if note:
                            names[-1] += f" ({note})"
                self.pending_files = []
                text += "\n\n" + "\n".join(f"📎 {n}" for n in names)
            if self.pending_commands:
                self.store.add(self.chat_id, "Amalgam", "system",
                               "You replied instead of approving, so those commands were skipped.")
                self.pending_commands = None
            self.question_open = False
            self.hint = None
            self.refresh_doc_copy()
            self._unpack_old_archives()
            self.store.add(self.chat_id, "You", "user", text)
            if self.status == "running":
                # The models will see it on their next turn.
                return
            self._start_run(text)

    def _start_run(self, text):
        """Start the discussion loop (called with the lock held)."""
        self.status = "running"
        self._stop.clear()
        self._run_no = getattr(self, "_run_no", 0) + 1
        self._thread = threading.Thread(target=self._loop, args=(text,), daemon=True)
        self._thread.start()

    def refresh_doc_copy(self):
        """If the project doc is shared by link, keep a fresh local copy without needing any connector."""
        link = (self.chat_settings() or {}).get("doc_link") or ""
        m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]{20,})", link)
        if not m:
            return
        from runners import doc_copy, save_doc_copy
        _p, age = doc_copy(self.workspace, m.group(1))
        if age is not None and age < 900:
            return

        def work():
            import urllib.request
            try:
                url = f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "AI-Amalgam"}),
                                            timeout=20) as r:
                    ctype = r.headers.get("Content-Type", "")
                    body = r.read()
                if "text/plain" in ctype and len(body) > 100:
                    save_doc_copy(self.workspace, m.group(1), body.decode("utf-8", "replace"))
            except Exception:
                pass   # not shared by link (or offline): the models' own reads keep the copy instead
        threading.Thread(target=work, daemon=True).start()

    def resume(self):
        """Continue (or retry) the discussion without a new message from you."""
        with self._lock:
            if self.chat_id is None or self.status == "running":
                return False
            self.hint = None
            self._start_run("")
        return True

    def _last_user_id(self):
        for m in reversed(self.store.messages(self.chat_id)):
            if m["kind"] == "user":
                return m["id"]
        return None

    def regenerate(self):
        """Throw away everything after your last message and let the models answer it again."""
        if self.chat_id is None or self.status == "running":
            return "Wait for the models to finish, or press Stop first."
        last = self._last_user_id()
        if last is None:
            return "There's nothing to regenerate yet."
        self.store.delete_messages(self.chat_id, after_id=last)
        self.pending_commands = None
        self.question_open = False
        self.resume()
        return None

    def edit_last(self, text):
        """Replace your last message (and everything after it) with an edited version."""
        if self.chat_id is None or self.status == "running":
            return "Wait for the models to finish, or press Stop first."
        last = self._last_user_id()
        if last is None:
            return "There's no message to edit."
        self.store.delete_messages(self.chat_id, after_id=last - 1, from_id=last)
        self.pending_commands = None
        self.question_open = False
        self.send(text)
        return None

    def _unpack_old_archives(self):
        """Archives attached before Amalgam could unzip them: unzip them now."""
        if not self.chat_id:
            return
        for att in self.store.attachments(self.chat_id):
            name = att["name"].lower()
            if not name.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
                continue
            src = os.path.join(self.workspace, att["path"])
            done = os.path.join(os.path.dirname(src), os.path.splitext(os.path.basename(src))[0] + " (unzipped)")
            if os.path.isfile(src) and not os.path.isdir(done):
                unpacked, note = self._unpack(att["path"])
                for n, path, size in unpacked:
                    self.store.add_attachment(self.chat_id, n, path, size)
                if note:
                    self._note(f"{att['name']}: {note}, so the models can open each file.")

    def _unpack(self, rel, max_files=400, max_bytes=2 * 1024 ** 3):
        """Unzip an attached archive next to it so the models can open each file."""
        import tarfile, zipfile
        src = os.path.join(self.workspace, rel)
        base = os.path.splitext(os.path.basename(src))[0]
        dest = os.path.join(os.path.dirname(src), base + " (unzipped)")
        out, total = [], 0
        try:
            if zipfile.is_zipfile(src):
                z = zipfile.ZipFile(src)
                members = [(i.filename, i.file_size, lambda i=i: z.open(i)) for i in z.infolist() if not i.is_dir()]
            else:
                t = tarfile.open(src)
                members = [(m.name, m.size, lambda m=m: t.extractfile(m)) for m in t.getmembers() if m.isfile()]
        except Exception as e:
            return [], f"couldn't unzip: {e}"
        skipped = 0
        for name, size, opener in members:
            parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
            if not parts or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
                continue
            if len(out) >= max_files or total + size > max_bytes:
                skipped += 1
                continue
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with opener() as fsrc, open(target, "wb") as fdst:
                import shutil
                shutil.copyfileobj(fsrc, fdst)
            total += size
            out.append(("/".join(parts), os.path.relpath(target, self.workspace).replace("\\", "/"), size))
        note = f"unzipped {len(out)} files" + (f", skipped {skipped} (too many or too big)" if skipped else "")
        return out, note

    def _file_into_chat(self, f):
        """Move an uploaded file from the inbox into this chat's attachments folder."""
        import os, shutil
        root = os.path.dirname(os.path.dirname(f["abs"]))          # .../workspace/attachments
        folder = os.path.join(root, f"chat-{self.chat_id}")
        os.makedirs(folder, exist_ok=True)
        base, ext = os.path.splitext(f["name"])
        dest, n = os.path.join(folder, f["name"]), 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(folder, f"{base} ({n}){ext}")
        shutil.move(f["abs"], dest)
        return f"attachments/chat-{self.chat_id}/{os.path.basename(dest)}"

    def stop(self):
        self._stop.set()
        proc = self._cmd_proc
        if proc and proc.poll() is None:
            from codework import _kill_tree
            _kill_tree(proc)
        for r in self.runners.values():
            r.cancel()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
            if t.is_alive():
                # a tool is still shutting down; don't keep the chat stuck on "running" because of it
                with self._lock:
                    if self.status == "running":
                        self.status = "idle"
                        self.speaker = None
                    self._run_no = getattr(self, "_run_no", 0) + 1   # the old run can't change anything now

    def state(self):
        return {
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "status": self.status,
            "hint": self.hint if self.status != "running" else None,
            "speaker": self.speaker,
            "live": self.live if self.speaker else None,
            "resting": {self.runners[k].name: until for k, until in self.resting.items()},
            "max_rounds": self.max_rounds,
            "chat_settings": self.chat_settings(),
            "pending_files": [f["name"] for f in self.pending_files],
            "pending_paths": [os.path.relpath(f["abs"], self.workspace).replace("\\", "/") for f in self.pending_files],
            "attachments": self.store.attachments(self.chat_id) if self.chat_id else [],
            "doc_edits": self.store.doc_edits(self.chat_id, self.project_id) if self.chat_id or self.project_id else [],
            "condensed": self.store.summary(self.chat_id)[1] > 0 if self.chat_id else False,
            "code": {"dir": self.code_dir(), "checkpoints": bool(self.cp and self.cp.available),
                     "allowed": self.allowed_commands(),
                     "pending_command": self.pending_commands["msg_id"] if self.pending_commands else None},
            "models": [
                {"key": k, "name": self.runners[k].name, "enabled": self.enabled[k],
                 "status": self.runners[k].status(), "speed": self.runners[k].speed}
                for k in self.order
            ],
        }

    # ----- the discussion loop -----

    def _active(self):
        now = time.time()
        for key, until in list(self.resting.items()):
            if until <= now:
                del self.resting[key]
                if self.chat_id:
                    self._note(f"{self.runners[key].name}'s plan limit has reset, so it's back in the discussion.")
        return [k for k in self.order if self.enabled[k] and k not in self.resting]

    def _note(self, text):
        self.store.add(self.chat_id, "Amalgam", "system", text)

    def _pause_for_user(self):
        with self._lock:
            self.status = "waiting"

    def _loop(self, trigger_text):
        my_run = getattr(self, "_run_no", 0)
        try:
            mentioned = {m.lower() for m in MENTION_RE.findall(trigger_text)}
            mentioned = {"gpt" if m in ("chatgpt", "codex") else m for m in mentioned}
            if not self._active():
                self._note("No models are switched on. Turn one on in the right panel.")
                return
            if mentioned and "all" not in mentioned:
                asked = False
                for key in [k for k in self.order if k in mentioned]:
                    if self._stop.is_set():
                        return
                    if key in self.resting and key not in self._active():
                        self._note(f"{self.runners[key].name} is resting until its plan limit resets "
                                   f"({time.strftime('%I:%M %p', time.localtime(self.resting[key])).lstrip('0')}).")
                        continue
                    result = self._turn_reading(key)
                    if result == "error":
                        return
                    if result == "command":
                        if self._auto_run() and not self.last_question:
                            continue
                        if self.last_question:
                            self._publish(self.last_question)
                        if self.pending_commands or self.last_question:
                            self._pause_for_user()
                        return
                    if result == "question":
                        self._publish(self.last_question)   # you asked this model directly: no check
                        asked = True
                if asked:
                    self._pause_for_user()
                return

            done_streak = 0
            for _round in range(1, self.max_rounds + 1):
                asked = False
                for key in self._active():
                    if self._stop.is_set():
                        self._note("Stopped. Send a message, or press Continue, to keep going."); self.hint = "stopped"
                        return
                    if key not in self._active():
                        continue
                    result = self._turn_reading(key)
                    if result == "error":
                        return
                    if result == "rested":
                        continue
                    if result == "command":   # wait for you to approve or deny the commands
                        if self._auto_run() and not self.last_question:
                            done_streak = 0
                            continue      # always-allowed commands ran; keep going
                        if self.last_question:
                            self._publish(self.last_question)
                        if self.pending_commands or self.last_question:
                            self._pause_for_user()
                        return
                    if result == "question":
                        outcome = self._ask_user(key)
                        if outcome == "error":
                            return
                        if outcome == "asked":
                            self._pause_for_user()
                            return
                        done_streak = 0
                        continue
                    done_streak = done_streak + 1 if result == "DONE" else 0
                    if not asked and done_streak >= len(self._active()):
                        self.hint = "done"
                        self._note("Discussion finished: every model agrees the goal is met. "
                                   "Send a message to keep going or start a new chat.")
                        return
                if asked:
                    self._pause_for_user()
                    return
            self.hint = "limit"
            self._note(f"Round limit reached ({self.max_rounds} rounds). Press Continue, or send a message, to keep going.")
        finally:
            with self._lock:
                if getattr(self, "_run_no", 0) == my_run:
                    if self.status == "running":
                        self.status = "idle"
                    self.speaker = None

    def _maybe_condense(self):
        msgs = [m for m in self.store.messages(self.chat_id) if m["kind"] != "system"]
        summary, upto = self.store.summary(self.chat_id)
        if len(msgs) <= KEEP_RECENT:
            return
        older = [m for m in msgs[:-KEEP_RECENT] if m["id"] > upto]
        if sum(len(m["text"]) for m in older) < CONDENSE_AT:
            return
        # use whichever model is free, at Fast speed; GPT first since its limits are usually roomier
        order = sorted(self._active(), key=lambda k: 0 if k == "gpt" else 1)
        if not order:
            return
        runner = self.runners[order[0]]
        self.speaker = runner.name
        self.live = {"activity": "condensing earlier messages", "text": "", "rev": self.live["rev"] + 1}
        lines = [f"[{m['speaker']}]: {m['text']}" for m in older]
        prompt = CONDENSE_PROMPT.format(
            existing=f"=== CURRENT SUMMARY ===\n{summary}\n\n" if summary else "",
            messages="\n\n".join(lines)[:60000])
        try:
            out = runner.reply(prompt, speed="fast")
        except Exception as e:
            print("Condensing skipped:", e)
            return
        finally:
            self.speaker = None
        body, _sig, _q = parse_signal(out)
        if body and len(body) > 40:
            self.store.set_summary(self.chat_id, body, older[-1]["id"])
            self._note(f"Condensed {len(older)} earlier messages into a short summary to save usage. "
                       "The full chat is still here for you.")

    def _turn(self, key, extra=""):
        try:
            self._maybe_condense()
        except Exception as e:
            print("Condensing error:", e)
        runner = self.runners[key]
        self.live = {"activity": "starting", "text": "", "rev": self.live["rev"] + 1}
        self.speaker = runner.name
        code_dir = self.code_dir()
        runner.code_dir = code_dir
        runner.image_dir = os.path.join(self.workspace, "images", f"chat-{self.chat_id}")
        from apps import tool_prefix
        runner.extra_allowed = [tool_prefix(n) for n in self.blender_servers] if self.blender_on else []
        runner.images = [os.path.join(runner.workdir, a["path"]) for a in
                         (self.store.attachments(self.chat_id) if self.chat_id else [])
                         if a["path"].lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))][-12:]
        before = self._checkpoint(code_dir, f"before {runner.name}'s turn")
        self.last_question = None
        prompt = self._build_prompt(key, extra)
        allow_edits = bool(self.chat_settings().get("allow_edits"))

        def on_event(activity=None, text=None):
            live = dict(self.live)
            if activity:
                live["activity"] = activity
            if text is not None:
                live["text"] = text
            live["rev"] += 1
            self.live = live

        reply_ok = False
        try:
            reply = runner.reply(prompt, allow_edits=allow_edits, on_event=on_event)
            reply_ok = True
        except Exception as e:  # RunnerError or anything unexpected
            others = [k for k in self._active() if k != key]
            if self._stop.is_set():
                self._note("Stopped. Send a message, or press Continue, to keep going."); self.hint = "stopped"
            elif LIMIT_RE.search(str(e)) and others:
                # Plan limit hit: carry on without this model and bring it back after the reset.
                until = None
                try:
                    u = runner.usage() or {}
                    until = u.get("resets_at") or min((w.get("resets_at") for w in (u.get("primary"), u.get("secondary"))
                                                       if isinstance(w, dict) and w.get("resets_at")), default=None)
                except Exception:
                    pass
                if not until or until < time.time():
                    until = time.time() + 3600
                self.resting[key] = until
                back = time.strftime("%I:%M %p", time.localtime(until)).lstrip("0")
                self._note(f"{runner.name} has hit its plan limit, so the discussion carries on with "
                           f"{', '.join(self.runners[k].name for k in others)}. {runner.name} rejoins "
                           f"automatically after {back}.")
                return "rested"
            else:
                self.hint = "error"
                self._note(f"{runner.name} couldn't reply, so the discussion is paused.\n\n{e}\n\n"
                           "Press Retry to try again. If this mentions a usage limit, wait for it to reset first.")
            return "error"
        finally:
            self.speaker = None
            self.turn_seq += 1
            runner.code_dir = None
            if not reply_ok:   # failed or stopped turn: still log any files it changed
                self._record_change(code_dir, before, runner.name)
        if self._stop.is_set():
            self._note("Stopped. Send a message, or press Continue, to keep going."); self.hint = "stopped"
            self._record_change(code_dir, before, runner.name)
            return "error"
        self._report_tool_problems(runner)
        for edit in getattr(runner, "last_edits", []) or []:
            self.store.add_doc_edit(self.chat_id, self.project_id, edit["tool"], edit["doc_id"], edit["summary"])
            self._note(f"{runner.name} {edit['summary'][0].lower() + edit['summary'][1:]}")
        reply, choices = split_choices(reply)
        reply, runs = split_runs(reply) if code_dir else (reply, [])
        reads, kept_r = [], []
        for line in reply.splitlines():
            m = READ_RE.match(line)
            if m and len(reads) < MAX_READS:
                reads.append(m.group(1).strip().strip("`"))
            elif not m:
                kept_r.append(line)
        reply = "\n".join(kept_r)
        kept, title = [], None
        for line in reply.splitlines():
            m = TITLE_RE.match(line)
            if m:
                title = title or m.group(1).strip().strip('"*_`').strip()
            else:
                kept.append(line)
        reply = "\n".join(kept)
        if title and self._needs_title():
            self.store.rename_chat(self.chat_id, title[:60])
        body, signal, question = parse_signal(reply)
        keep, remembered = [], []
        for line in body.splitlines():
            m = REMEMBER_RE.match(line)
            if m and self.project_id:
                remembered.append(m.group(1).strip().strip("*_").strip())
            else:
                keep.append(line)
        body = "\n".join(keep).strip() or body
        self.store.add(self.chat_id, runner.name, "model", body)
        for img in getattr(runner, "last_images", []) or []:
            self.store.add(self.chat_id, runner.name, "image", img)
        self._record_change(code_dir, before, runner.name)
        if remembered:
            saved = self.store.add_memory(self.project_id, remembered)
            if saved:
                self._note("Saved to project memory: " + "; ".join(saved))
        for r in reads:
            self._read_file(r, runner.name)
        if signal == "NEED USER":
            question = question or "(asked for your input)"
            # held back until a teammate has checked it (see _ask_user)
            self.last_question = (runner.name, question, choices or guess_choices(question))
            if not runs:
                return "question"
        if runs:
            msg = self.store.add(self.chat_id, runner.name, "command", "\n".join(runs))
            self.pending_commands = {"msg_id": msg["id"], "speaker": runner.name, "commands": runs}
            return "command"
        if signal == "HOLD":
            self.last_hold = question or "they think it's too early to ask"
        if reads and signal not in ("DONE", "AGREE", "HOLD"):
            return "read"          # let the same model look at what it read
        return signal

    # ----- game files -----

    def game_roots(self):
        """Folders the models may READ from: game folders, Amalgam's workspace and the code folder."""
        roots = [g for g in self.game_folders if g] + [self.workspace]
        cd = self.code_dir()
        if cd:
            roots.append(cd)
        return roots

    def extract_dir(self):
        return os.path.join(self.workspace, "extracted", f"chat-{self.chat_id}")

    def _read_file(self, request, who):
        import gamefiles
        path, _, query = request.partition("::")
        path = path.strip().strip('"').strip("'")
        query = query.strip()
        full = os.path.realpath(os.path.expanduser(path))
        ok = False
        for root in self.game_roots():
            try:
                r = os.path.realpath(root)
                if os.path.commonpath([full, r]) == r:
                    ok = True
                    break
            except ValueError:
                continue
        if not ok:
            text = ("Not allowed: that file isn't inside a game folder, the code folder or Amalgam's workspace. "
                    "Ask the user to add its folder under Game files in the Apps card.")
        else:
            self.speaker = "Reader"
            self.live = {"activity": f"reading {os.path.basename(full)}", "text": "", "rev": self.live["rev"] + 1}
            try:
                text = gamefiles.read(full, query, self.extract_dir())
            except Exception as e:
                text = f"Couldn't read it: {e}"
            finally:
                self.speaker = None
        if len(text) > 30000:
            text = text[:30000] + "\n[... cut; ask for something narrower, e.g. list <text> ...]"
        self.store.add(self.chat_id, "Reader", "output", f"$ read {path}" + (f" :: {query}" if query else "") + f"\n[for {who}]\n{text}")

    def _turn_reading(self, key):
        """A turn, plus up to three follow-ups when the model asked to read files."""
        result = self._turn(key)
        hops = 0
        while result == "read" and hops < 3 and not self._stop.is_set():
            hops += 1
            result = self._turn(key)
        return "NEXT" if result == "read" else result

    def _report_tool_problems(self, runner):
        """Tell you (in plain words) when Claude's connectors didn't load or a tool was refused."""
        d = getattr(runner, "diag", None)
        if not d:
            return
        drive_like = {k: v for k, v in d["servers"].items() if re.search(r"drive|docs", k, re.I)}
        doc_on = bool(self.chat_settings().get("doc_link"))
        if d["servers"] and not drive_like and doc_on and runner.key == "claude":
            self._note(f"{runner.name}'s Google Drive tools didn't load this turn. This is a known Claude Code issue "
                       "in background mode; Amalgam gave it the saved copy of the doc if there is one. If it keeps "
                       "happening, press 'Update Claude Code' in the Connections card.")
        down = {k.replace("claude.ai ", ""): v for k, v in drive_like.items() if str(v).lower() not in ("connected", "ok")}
        if down:
            what = ", ".join(f"{k} ({v})" for k, v in down.items())
            fix = ("sign in again: claude.ai → Settings → Connectors" if any("auth" in str(v).lower() for v in down.values())
                   else "it may be a temporary hiccup; if it keeps happening, press Refresh in Connections")
            self._note(f"{runner.name}'s connectors didn't load this turn: {what}. To fix: {fix}.")
        denied = [t for t in d["denied"] if "Bash" not in t and "PowerShell" not in t]
        edit_denied = [t for t in denied if any(e.split("__")[-1] in t for e in
                       ("update_doc", "update_file", "create_file", "copy_file", "add_comment_reply"))]
        if edit_denied and not self.chat_settings().get("allow_edits"):
            self._note(f"{runner.name} tried to edit a doc, but Allow doc edits is off for this chat. "
                       "Turn it on in the right panel if you want it to make approved edits.")
        others = [t for t in denied if t not in edit_denied]
        if others:
            self._note(f"{runner.name} was blocked from using: {', '.join(sorted(set(others)))[:300]}.")

    def skills_off(self):
        key = f"skills_off_project_{self.project_id}" if self.project_id else "skills_off"
        try:
            import json
            return list(json.loads(self.store.get_setting(key, "[]")))
        except ValueError:
            return []

    def set_skill(self, name, on):
        import json
        off = set(self.skills_off())
        (off.discard if on else off.add)(name)
        key = f"skills_off_project_{self.project_id}" if self.project_id else "skills_off"
        self.store.set_setting(key, json.dumps(sorted(off)))

    def _needs_title(self):
        """True while a chat still has its automatic title (your first line) and no model reply yet."""
        if not self.chat_id:
            return False
        msgs = self.store.messages(self.chat_id)
        if any(m["kind"] == "model" for m in msgs):
            return False
        first = next((m for m in msgs if m["kind"] == "user"), None)
        return bool(first) and self.store.chat_title(self.chat_id) == (first["text"].splitlines()[0][:80] or "New chat")

    # ----- coding area -----

    def code_dir(self):
        project = self.store.project(self.project_id)
        if not project or not project.get("code_dir"):
            return None
        path, err = check_code_dir(project["code_dir"])
        return path if path and not err else None

    def _checkpoint(self, code_dir, message):
        if not (code_dir and self.cp and self.cp.available):
            return None
        try:
            return self.cp.checkpoint(self.project_id, code_dir, message)
        except Exception as e:
            print("Checkpoint error:", e)
            return None

    def _record_change(self, code_dir, before, who):
        """After a turn or command: save a checkpoint and log which files changed."""
        if not before:
            return
        after = self._checkpoint(code_dir, f"after {who}")
        if not after or after == before:
            return
        try:
            files = self.cp.changed_files(self.project_id, code_dir, before, after)
        except Exception as e:
            print("Diff error:", e)
            return
        if not files:
            return
        self.store.add_code_change(self.chat_id, self.project_id, who, before, after, files)
        names = ", ".join(f["path"] for f in files[:6]) + (f" and {len(files) - 6} more" if len(files) > 6 else "")
        plus, minus = sum(f["added"] for f in files), sum(f["removed"] for f in files)
        self._note(f"{who} changed {len(files)} file{'s' if len(files) != 1 else ''}: {names} (+{plus} −{minus}). "
                   "See the Code tab for the changes or to undo them.")

    def allowed_commands(self):
        import json
        if not self.project_id:
            return []
        try:
            return list(json.loads(self.store.get_setting(f"allowed_cmds_project_{self.project_id}", "[]")))
        except ValueError:
            return []

    def set_allowed_commands(self, commands):
        import json
        if self.project_id:
            self.store.set_setting(f"allowed_cmds_project_{self.project_id}",
                                   json.dumps(sorted(set(c for c in commands if c))[:100]))

    def _auto_run(self):
        """Run the pending commands right away if you marked every one of them 'always allow'."""
        pc, code_dir = self.pending_commands, self.code_dir()
        allowed = set(self.allowed_commands())
        if not (pc and code_dir and allowed and all(c in allowed for c in pc["commands"])):
            return False
        self.pending_commands = None
        for cmd in pc["commands"]:
            if self._stop.is_set():
                break
            self._run_one(cmd, code_dir, "Terminal")
        self.turn_seq += 1
        return True

    def approve_commands(self, msg_id, ok, always=False):
        """You clicked Approve, Always allow, or Deny on a command request."""
        with self._lock:
            pc = self.pending_commands
            if not pc or pc["msg_id"] != msg_id or self.status == "running":
                return False
            if ok and always:
                self.set_allowed_commands(self.allowed_commands() + pc["commands"])
            self.pending_commands = None
            self.status = "running"
            self._stop.clear()
            self._thread = threading.Thread(target=self._commands_then_continue, args=(pc, ok), daemon=True)
            self._thread.start()
        return True

    def _run_one(self, command, code_dir, who):
        from runners import clean_env
        self.speaker = "Terminal"
        self.live = {"activity": f"running {command[:60]}", "text": "", "rev": self.live["rev"] + 1}
        before = self._checkpoint(code_dir, "before command")
        try:
            code, out, timed_out = run_command(command, code_dir, env=clean_env(),
                                               on_proc=lambda p: setattr(self, "_cmd_proc", p))
        finally:
            self._cmd_proc = None
            self.speaker = None
        status = "timed out" if timed_out else ("stopped" if self._stop.is_set() else f"exit code {code}")
        self.store.add(self.chat_id, who, "output", f"$ {command}\n[{status}]\n{out or '(no output)'}")
        self._record_change(code_dir, before, "Commands")
        return code == 0 and not timed_out

    def _commands_then_continue(self, pc, ok):
        try:
            code_dir = self.code_dir()
            if not ok:
                self.store.add(self.chat_id, "You", "output",
                               "Denied: " + "; ".join(pc["commands"]) + "\n(The user didn't allow these commands.)")
            elif not code_dir:
                self._note("The project's code folder is missing, so the commands didn't run.")
            else:
                for cmd in pc["commands"]:
                    if self._stop.is_set():
                        break
                    self._run_one(cmd, code_dir, "Terminal")
            self.turn_seq += 1
            if self._stop.is_set():
                self._note("Stopped. Send a message, or press Continue, to keep going."); self.hint = "stopped"
                return
            if self.question_open:      # a question is still waiting for you
                self._pause_for_user()
                return
            self._loop("")
        finally:
            with self._lock:
                if getattr(self, "_run_no", 0) == my_run:
                    if self.status == "running":
                        self.status = "idle"
                    self.speaker = None

    def run_file(self, rel):
        """You pressed Run on a file in the Code tab: run it now (your click is the approval)."""
        from codework import inside
        code_dir = self.code_dir()
        full = inside(code_dir, rel) if code_dir else None
        if not full or not os.path.isfile(full):
            return "File not found."
        ext = os.path.splitext(full)[1].lower()
        py = "python" if sys.platform == "win32" else "python3"
        commands = {".py": f'{py} "{rel}"', ".js": f'node "{rel}"', ".mjs": f'node "{rel}"',
                    ".ps1": f'& ".\\{rel}"' if sys.platform == "win32" else None,
                    ".bat": f'cmd /c "{rel}"' if sys.platform == "win32" else None,
                    ".sh": f'bash "{rel}"'}
        command = commands.get(ext)
        if not command:
            return "Amalgam can't run that kind of file."
        with self._lock:
            if self.status == "running":
                return "Wait for the models to finish, or press Stop first."
            if self.chat_id is None:
                self.chat_id = self.store.new_chat(f"Run {os.path.basename(rel)}", self.project_id)
                self.store.set_chat_settings(self.chat_id, **self.pending_chat_settings)
            self.status = "running"
            self._stop.clear()

        def work():
            try:
                self._run_one(command, code_dir, "You ran")
                self.turn_seq += 1
            finally:
                with self._lock:
                    if self.status == "running":
                        self.status = "waiting" if (self.question_open or self.pending_commands) else "idle"
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return None

    def undo_change(self, change_id):
        change = self.store.code_change(change_id)
        code_dir = self.code_dir()
        if not change or change["project_id"] != self.project_id or not code_dir or not self.cp:
            return "Can't find that change for this project."
        if self.status == "running":
            return "Wait for the models to finish, or press Stop first."
        try:
            self.cp.restore(self.project_id, code_dir, change["before"])
        except Exception as e:
            return f"Undo failed: {e}"
        self.store.mark_undone(self.project_id, change_id)
        if self.chat_id:
            self._note("You undid file changes back to before change #%d. The models will see the files as they are now." % change_id)
        return None

    def _publish(self, q):
        speaker, question, choices = q
        self.question_open = True
        self.store.add(self.chat_id, speaker, "question",
                       question + "".join(f"\nCHOICE: {c}" for c in choices))

    def _ask_user(self, asker):
        """A model wants to ask you something. Its teammates check the question first; if one
        holds it, the asker reconsiders once. Returns 'asked', 'continue' or 'error'."""
        q = self.last_question
        reviewers = [k for k in self._active() if k != asker]
        if not reviewers or not self.check_questions:
            self._publish(q)
            return "asked"
        names = " and ".join(self.runners[k].name for k in reviewers)
        self._note(f"{q[0]} wants to ask you something; {names} is checking it first.")
        choices_txt = "".join(f"\nCHOICE: {c}" for c in q[2])
        holds, extra_qs = [], []
        for key in reviewers:
            if self._stop.is_set():
                return "error"
            self.last_hold = ""
            result = self._turn(key, REVIEW_EXTRA.format(asker=q[0], question=q[1], choices=choices_txt))
            if result == "error":
                return "error"
            if result == "HOLD":
                holds.append((self.runners[key].name, self.last_hold))
            elif result == "question":
                extra_qs.append(self.last_question)
            # AGREE, NEXT, DONE, or resting: the question goes ahead
        if not holds:
            for x in [q] + extra_qs:
                self._publish(x)
            return "asked"
        for who, why in holds:
            self._note(f"{who} suggested holding {q[0]}'s question: {why}")
        if self._stop.is_set():
            return "error"
        others = [w for w, _ in holds]
        result = self._turn(asker, RECONSIDER_EXTRA.format(
            question=q[1] + choices_txt, s="s" if len(holds) > 1 else "",
            holds="\n".join(f"- {w}: {why}" for w, why in holds),
            others=" and ".join(others), me=q[0], other1=others[0]))
        if result == "error":
            return "error"
        if result == "question":
            extra_qs.insert(0, self.last_question)
        elif result != "rested":
            self._note(f"{q[0]} held off on the question for now.")
        for x in extra_qs:
            self._publish(x)
        return "asked" if extra_qs else "continue"

    def _build_prompt(self, key, extra=""):
        runner = self.runners[key]
        name = runner.name
        others = [self.runners[k].name for k in self._active() if k != key]
        others_text = ", ".join(others) if others else "no other models right now"
        active = [self.runners[k] for k in self._active()]
        drive_peers = [r.name for r in active if r is not runner and r.can_use_drive]
        editor = next((r.name for r in active if r.can_edit_docs), None)

        if runner.can_use_drive:
            peers_note = (f" {', '.join(drive_peers)} can read them too." if drive_peers
                          else " The others can't, so share what matters.")
            web = " and search the web" if runner.key == "claude" else ""
            tools = TOOLS_WITH_DRIVE.format(peers_note=peers_note, web=web)
        elif drive_peers:
            tools = TOOLS_WITHOUT_DRIVE.format(drive_peer=drive_peers[0])
        else:
            tools = ""

        settings = self.chat_settings()
        project = self.store.project(self.project_id)
        link = settings.get("doc_link") or (project["doc_link"] if project else "")
        doc = ""
        if project:
            instr = project["instructions"].strip()
            roles = project.get("roles") or {}
            mine = (roles.get(key) or "").strip()
            role_txt = f"\nYour role in this project: {mine}" if mine else ""
            others_roles = [f"{self.runners[k].name}: {roles[k].strip()}" for k in self._active()
                            if k != key and (roles.get(k) or "").strip()]
            if others_roles:
                role_txt += "\nOther roles: " + "; ".join(others_roles)
            if role_txt:
                role_txt += "\nStay in your role: bring that perspective rather than simply agreeing."
            mem = (project.get("memory") or "").strip()
            if len(mem) > MEMORY_CHARS:
                mem = "...\n" + mem[-MEMORY_CHARS:]
            mem_txt = (f"\nProject memory (decisions and facts already agreed with the user; treat them as settled "
                       f"unless the user changes them):\n{mem}") if mem else ""
            doc += PROJECT_LINE.format(name=project["name"], roles=role_txt, memory=mem_txt,
                                       instructions=f"\nProject instructions from the user (always follow these):\n{instr}" if instr else "")
        copy = ""
        if link:
            from runners import doc_copy
            m = re.search(r"/d/([A-Za-z0-9_-]{20,})", link)
            path, age = doc_copy(self.workspace, m.group(1)) if m else (None, None)
            if path:
                mins = int(age // 60)
                copy = DOC_COPY.format(path=path, age=f"{mins} minutes" if mins < 120 else f"{mins // 60} hours")
        doc += DOC_LINE.format(link=link, copy=copy) if link else ""
        if key == "gpt":
            doc += IMAGE_LINE
        if settings.get("allow_edits") and editor:
            doc += EDITS_ON_EDITOR if runner.can_edit_docs else EDITS_ON_OTHER.format(editor=editor)
        elif tools:
            doc += EDITS_OFF

        code_dir = self.code_dir()
        if self.blender_on:
            from apps import blender_listening
            claude_on = "claude" in self._active()
            who = BLENDER_WHO_EDITOR if key == "claude" or not claude_on else \
                BLENDER_WHO_REVIEWER.format(editor=self.runners["claude"].name)
            status = "" if blender_listening() else (
                ", but Blender isn't open with the add-on's server running right now. If you need it, ask the user to "
                "open Blender and click Connect in the BlenderMCP tab (press N in the 3D view)")
            doc += BLENDER_LINE.format(status=status, who=who)
        if self.game_folders:
            doc += GAMEFILES_LINE.format(out=self.extract_dir() if self.chat_id else os.path.join(self.workspace, "extracted"),
                                         folders="; ".join(self.game_folders[:12]))
        if code_dir and self.blender_path:
            doc += BLENDER_HEADLESS.format(path=self.blender_path)
        if code_dir:
            claude_on = "claude" in self._active()
            if key == "claude":
                role = CODE_ROLE_EDITOR
            elif claude_on:
                role = CODE_ROLE_REVIEWER.format(editor=self.runners["claude"].name)
            else:
                role = CODE_ROLE_NO_EDITOR
            changes = ""
            recent = [c for c in self.store.code_changes(self.project_id, 1) if not c["undone"]]
            if recent and recent[0]["chat_id"] == self.chat_id and self.cp:
                c = recent[0]
                try:
                    patch = self.cp.patch(self.project_id, code_dir, c["before"], c["after"], max_chars=8000)
                except Exception:
                    patch = ""
                if patch:
                    changes = f"Latest file changes, by {c['speaker']}:\n```diff\n{patch}\n```\n"
            doc += CODE_LINE.format(path=code_dir, role=role, changes=changes, timeout=COMMAND_TIMEOUT,
                                    shell="Windows PowerShell" if sys.platform == "win32" else "bash")

        doc += ("\nTo show the user a picture (an attachment, an extracted texture, a generated image), put it in your "
                "message as Markdown: ![short caption](<full or workspace-relative path>). The app displays it. "
                "Don't just give bare file paths.\n")
        files = self.store.attachments(self.chat_id) if self.chat_id else []
        if files:
            doc += ("\nFiles the user attached (in your working folder; open them with your file-reading tool "
                    "when they're relevant, and describe what matters for the others):\n"
                    + "\n".join(f"- {os.path.join(runner.workdir, f['path']) if code_dir else f['path']}" for f in files) + "\n")

        summary, upto = self.store.summary(self.chat_id) if self.chat_id else ("", 0)
        lines = [f"[Summary of the earlier part of this conversation]:\n{summary}"] if summary else []
        for m in self.store.messages(self.chat_id):
            if m["id"] <= upto:
                continue
            if m["kind"] == "system":
                continue
            if m["kind"] == "question":
                lines.append(f"[{m['speaker']} asked the user]: {m['text']}")
            elif m["kind"] == "image":
                lines.append(f"[{m['speaker']} made an image, saved at {m['text']}; open that file to look at it]")
            elif m["kind"] == "command":
                lines.append(f"[{m['speaker']} asked to run]: " + "; ".join(m["text"].splitlines()))
            elif m["kind"] == "output":
                cap = 16000 if m["speaker"] == "Reader" else 4000
                out = m["text"] if len(m["text"]) <= cap else m["text"][:cap // 2] + "\n[...]\n" + m["text"][-cap // 2:]
                lines.append(f"[{'File read' if m['speaker'] == 'Reader' else 'Command'} result]:\n{out}")
            else:
                lines.append(f"[{m['speaker']}]: {m['text']}")
        if self._needs_title():
            extra = TITLE_LINE + extra
        off = self.skills_off()
        if off:
            doc += ("\nThe user switched these skills off here, so don't use them: " + ", ".join(off) + "\n")
        return PROMPT_TEMPLATE.format(name=name, others=others_text, tools=tools, doc=doc, extra=extra,
                                      transcript=self._fit(lines))

    @staticmethod
    def _fit(lines):
        text = "\n\n".join(lines)
        if len(text) <= MAX_TRANSCRIPT_CHARS or len(lines) < 3:
            return text
        first, rest = lines[0], lines[1:]
        kept = []
        size = len(first) + 200
        for line in reversed(rest):
            if size + len(line) > MAX_TRANSCRIPT_CHARS:
                break
            kept.append(line)
            size += len(line) + 2
        kept.reverse()
        skipped = len(rest) - len(kept)
        return "\n\n".join([first, f"[... {skipped} older messages left out to save space ...]"] + kept)
