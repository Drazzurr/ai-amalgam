"""The orchestrator: decides who speaks next, builds each model's prompt,
and pauses for you when a model asks a question."""

import re
import threading
import time

# Each turn re-sends the conversation. Above this size, the oldest middle
# messages are left out (the first message - your goal - is always kept).
MAX_TRANSCRIPT_CHARS = 60000

SIGNAL_RE = re.compile(r"^\W*(NEXT|DONE|NEED USER)\b\W*:?\s*(.*)$", re.IGNORECASE)
MENTION_RE = re.compile(r"@(claude|gpt|chatgpt|codex|all)\b", re.IGNORECASE)

PROMPT_TEMPLATE = """You are {name}, one of several AI participants in a group discussion run by the AI Amalgam app.
Participants: you ({name}), {others}, and the user (the human who leads and makes the decisions).

How this works:
- The transcript below shows everything said so far. Each message is tagged with its speaker.
- Write ONLY your next message to the group. Talk to the other model(s) directly: build on good ideas, challenge weak ones, and move the work forward. Don't repeat points that are already agreed.
- Keep it focused: aim for under 250 words unless the task needs more (for example a draft, a plan or code).
- Don't make decisions that belong to the user (preferences, budget, scope, taste). Ask instead.
{tools}{doc}
The last line of your message must be exactly one of these:
NEXT
DONE
NEED USER: <one clear question>
Use NEXT if the discussion should continue, DONE if the goal is fully met and you are happy with the result, and NEED USER when you need a decision, approval or clarification that only the user can give.

=== TRANSCRIPT ===
{transcript}
=== END OF TRANSCRIPT ===

Now write your message as {name}."""

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

DOC_LINE = """
Project document for this chat: {link}
This is the shared document the group is working on.
"""

EDITS_ON_EDITOR = """
Document editing is ON, and you are the one who applies edits. Rules:
1. First agree on the change in the discussion.
2. Then end your message with NEED USER showing the exact change: which document, where, and the new text.
3. Apply it only if the user's most recent message clearly approves that change. Then say exactly what you changed.
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
            question = m.group(2).strip().strip("*_`").strip() if signal == "NEED USER" else ""
            body = "\n".join(lines[:-1]).rstrip()
            return body or "(nothing to add)", signal, question
    return text.strip(), "NEXT", ""


class Orchestrator:
    def __init__(self, store, runners):
        self.store = store
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
        self.resting = {}   # model key -> time it can rejoin (after hitting its plan limit)
        self._thread = None

    # ----- public actions (called from the web UI) -----

    def new_chat(self, project_id=None):
        self.stop()
        with self._lock:
            self.chat_id = None
            self.project_id = project_id
            project = self.store.project(project_id)
            self.pending_chat_settings = {"doc_link": project["doc_link"] if project else "",
                                          "allow_edits": False}
            self.status = "idle"

    def open_chat(self, chat_id):
        self.stop()
        with self._lock:
            self.chat_id = chat_id
            self.project_id = self.store.chat_project(chat_id)
            self.status = "idle"

    def settings(self, max_rounds=None, enabled=None, speeds=None):
        with self._lock:
            if max_rounds is not None:
                self.max_rounds = max(1, min(20, int(max_rounds)))
                self.store.set_setting("max_rounds", self.max_rounds)
            if speeds:
                for key, sp in speeds.items():
                    if key in self.runners and sp in ("fast", "balanced", "best"):
                        self.runners[key].speed = sp
                        self.store.set_setting(f"speed_{key}", sp)
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
                self.pending_files = []
                text += "\n\n" + "\n".join(f"📎 {n}" for n in names)
            self.store.add(self.chat_id, "You", "user", text)
            if self.status == "running":
                # The models will see it on their next turn.
                return
            self.status = "running"
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, args=(text,), daemon=True)
            self._thread.start()

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
        for r in self.runners.values():
            r.cancel()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)

    def state(self):
        return {
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "status": self.status,
            "speaker": self.speaker,
            "live": self.live if self.speaker else None,
            "resting": {self.runners[k].name: until for k, until in self.resting.items()},
            "max_rounds": self.max_rounds,
            "chat_settings": self.chat_settings(),
            "pending_files": [f["name"] for f in self.pending_files],
            "attachments": self.store.attachments(self.chat_id) if self.chat_id else [],
            "doc_edits": self.store.doc_edits(self.chat_id, self.project_id) if self.chat_id or self.project_id else [],
            "condensed": self.store.summary(self.chat_id)[1] > 0 if self.chat_id else False,
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
                    result = self._turn(key)
                    if result == "error":
                        return
                    asked = asked or result == "question"
                if asked:
                    self._pause_for_user()
                return

            done_streak = 0
            for _round in range(1, self.max_rounds + 1):
                asked = False
                for key in self._active():
                    if self._stop.is_set():
                        self._note("Stopped. Send a message to continue.")
                        return
                    if key not in self._active():
                        continue
                    result = self._turn(key)
                    if result == "error":
                        return
                    if result == "rested":
                        continue
                    if result == "question":
                        asked = True  # let the others have their say, then pause
                    done_streak = done_streak + 1 if result == "DONE" else 0
                    if not asked and done_streak >= len(self._active()):
                        self._note("Discussion finished: every model agrees the goal is met. "
                                   "Send a message to keep going or start a new chat.")
                        return
                if asked:
                    self._pause_for_user()
                    return
            self._note(f"Round limit reached ({self.max_rounds} rounds). "
                       "Send a message (even just \"continue\") to keep going.")
        finally:
            with self._lock:
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

    def _turn(self, key):
        try:
            self._maybe_condense()
        except Exception as e:
            print("Condensing error:", e)
        runner = self.runners[key]
        self.live = {"activity": "starting", "text": "", "rev": self.live["rev"] + 1}
        self.speaker = runner.name
        prompt = self._build_prompt(key)
        allow_edits = bool(self.chat_settings().get("allow_edits"))

        def on_event(activity=None, text=None):
            live = dict(self.live)
            if activity:
                live["activity"] = activity
            if text is not None:
                live["text"] = text
            live["rev"] += 1
            self.live = live

        try:
            reply = runner.reply(prompt, allow_edits=allow_edits, on_event=on_event)
        except Exception as e:  # RunnerError or anything unexpected
            others = [k for k in self._active() if k != key]
            if self._stop.is_set():
                self._note("Stopped. Send a message to continue.")
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
                self._note(f"{runner.name} couldn't reply, so the discussion is paused.\n\n{e}\n\n"
                           "If this mentions a usage limit, wait for it to reset, then send \"continue\".")
            return "error"
        finally:
            self.speaker = None
            self.turn_seq += 1
        if self._stop.is_set():
            self._note("Stopped. Send a message to continue.")
            return "error"
        for edit in getattr(runner, "last_edits", []) or []:
            self.store.add_doc_edit(self.chat_id, self.project_id, edit["tool"], edit["doc_id"], edit["summary"])
            self._note(f"{runner.name} {edit['summary'][0].lower() + edit['summary'][1:]}")
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
        if remembered:
            saved = self.store.add_memory(self.project_id, remembered)
            if saved:
                self._note("Saved to project memory: " + "; ".join(saved))
        if signal == "NEED USER":
            self.store.add(self.chat_id, runner.name, "question",
                           question or "(asked for your input)")
            return "question"
        return signal

    def _build_prompt(self, key):
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
        doc += DOC_LINE.format(link=link) if link else ""
        if settings.get("allow_edits") and editor:
            doc += EDITS_ON_EDITOR if runner.can_edit_docs else EDITS_ON_OTHER.format(editor=editor)
        elif tools:
            doc += EDITS_OFF

        files = self.store.attachments(self.chat_id) if self.chat_id else []
        if files:
            doc += ("\nFiles the user attached (in your working folder; open them with your file-reading tool "
                    "when they're relevant, and describe what matters for the others):\n"
                    + "\n".join(f"- {f['path']}" for f in files) + "\n")

        summary, upto = self.store.summary(self.chat_id) if self.chat_id else ("", 0)
        lines = [f"[Summary of the earlier part of this conversation]:\n{summary}"] if summary else []
        for m in self.store.messages(self.chat_id):
            if m["id"] <= upto:
                continue
            if m["kind"] == "system":
                continue
            if m["kind"] == "question":
                lines.append(f"[{m['speaker']} asked the user]: {m['text']}")
            else:
                lines.append(f"[{m['speaker']}]: {m['text']}")
        return PROMPT_TEMPLATE.format(name=name, others=others_text, tools=tools, doc=doc,
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
