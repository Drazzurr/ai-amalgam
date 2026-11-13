"""The job queue: tasks Amalgam works through on its own, one at a time, each in its own chat.

Add jobs ("balance these 20 items", "/lore-check chapter 3"), press Start, and walk away. Each job
starts a new chat (in a project, if you picked one) that runs in the background. When it finishes,
asks you something, or fails, the queue notes it and moves on to the next job. A job that hit a
plan limit is retried automatically an hour later. While a job runs, the PC is kept from going to
sleep. Jobs wait while you're in the middle of a discussion yourself, so they never compete with
you for your plan.
"""

import sys
import threading
import time

from orchestrator import LIMIT_RE

JOB_NOTE = ("\n\n(Queued job, running while I'm away: work it through without me. Make sensible "
            "assumptions and list them, and only ask me something if you're truly blocked.)")
RETRY_AFTER_LIMIT = 3600
STATUSES = ("queued", "running", "done", "needs you", "failed", "stopped")


class JobQueue:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.orch = None          # the orchestrator working on the running job
        self._lock = threading.RLock()
        self._awake = False
        self.notify = None        # set by the app: shows a Windows notification
        # jobs that were running when Amalgam closed: pick them up again (their chat is resumed)
        for j in self.store.jobs():
            if j["status"] == "running":
                self.store.update_job(j["id"], status="queued", note="Amalgam restarted while this ran; it continues.")

    # ----- settings -----

    @property
    def running(self):
        return self.store.get_setting("queue_running", "0") == "1"

    def set_running(self, on):
        self.store.set_setting("queue_running", "1" if on else "0")
        if on:
            self.store.set_setting("queue_paused_until", "0")

    @property
    def rounds(self):
        try:
            return max(2, min(20, int(self.store.get_setting("queue_rounds", "6"))))
        except ValueError:
            return 6

    def paused_until(self):
        try:
            return float(self.store.get_setting("queue_paused_until", "0") or 0)
        except ValueError:
            return 0.0

    def view(self):
        jobs = self.store.jobs()
        return {"jobs": jobs, "running": self.running, "paused_until": self.paused_until(), "rounds": self.rounds,
                "waiting_for_you": self.app.any_running() and not any(j["status"] == "running" for j in jobs)}

    def summary(self):
        """Small version for every status check (the sidebar badge)."""
        jobs = self.store.jobs()
        return {"running": self.running, "open": sum(1 for j in jobs if j["status"] in ("queued", "running")),
                "needs_you": sum(1 for j in jobs if j["status"] == "needs you")}

    # ----- the loop -----

    def loop(self, quit_event):
        while not quit_event.wait(2):
            try:
                self.step()
            except Exception as e:
                import traceback
                traceback.print_exc()
                print("Job queue step failed:", e)
            self._keep_awake(self.app.any_running())
        self._keep_awake(False)

    def step(self):
        with self._lock:
            jobs = self.store.jobs()
            current = next((j for j in jobs if j["status"] == "running"), None)
            if current:
                return self._check(current)
            if not self.running or time.time() < self.paused_until():
                return None
            if self.app.any_running():
                return None          # you (or a background chat) are mid-discussion: wait
            for j in jobs:
                if j["status"] == "queued" and not self._code_busy(j):
                    return self._start(j)
            return None

    def _code_busy(self, job):
        from codework import check_code_dir
        p = self.store.project(job["project_id"]) if job["project_id"] else None
        if not p or not p.get("code_dir"):
            return False
        path, _err = check_code_dir(p["code_dir"])
        return bool(path) and any(o.status == "running" and o.code_dir() == path for o in self.app.all_orchs())

    def _start(self, job):
        app = self.app
        o = app._make_orch()
        o.max_rounds = self.rounds
        if job["chat_id"] and self.store.chat_title(job["chat_id"]) is not None:
            o.open_chat(job["chat_id"])         # a retry: carry on in the same chat
            o.question_open = False
            if not o.resume():
                self.store.update_job(job["id"], status="failed", note="Couldn't continue its chat.")
                return "failed"
        else:
            o.new_chat(job["project_id"])
            o.send(job["text"] + JOB_NOTE)
        if o.chat_id is None:
            self.store.update_job(job["id"], status="failed", note="Couldn't start a chat for it.")
            return "failed"
        with app._switch_lock:
            app.background[o.chat_id] = o
        self.orch = o
        self.store.update_job(job["id"], status="running", chat_id=o.chat_id, started=time.time(), note="")
        print(f"Job {job['id']} started in chat {o.chat_id}")
        return "started"

    def _check(self, job):
        o = self.orch
        if o is not None and o.chat_id == job["chat_id"] and o.status == "running":
            return "running"
        if o is not None and o.chat_id == job["chat_id"]:
            what = self.app._outcome(o)
            last_note = self._last_note(job["chat_id"])
        else:   # its orchestrator is gone (restart) or now shows another chat: read the chat itself
            what, last_note = self._outcome_from_chat(job["chat_id"])
        self.orch = None
        if what == "error" and LIMIT_RE.search(last_note or ""):
            until = time.time() + RETRY_AFTER_LIMIT
            self.store.set_setting("queue_paused_until", str(until))
            self.store.update_job(job["id"], status="queued",
                                  note="Hit a plan limit; the queue tries again at " +
                                       time.strftime("%I:%M %p", time.localtime(until)).lstrip("0") + ".")
            return "limit"
        status, note = {"done": ("done", ""), "question": ("needs you", "Has a question for you."),
                        "commands": ("needs you", "Wants to run a command."),
                        "error": ("failed", (last_note or "A model couldn't reply.")[:300])}.get(
            what, ("stopped", "Stopped."))
        self.store.update_job(job["id"], status=status, note=note, finished=time.time())
        print(f"Job {job['id']} ended: {status}")
        remaining = [j for j in self.store.jobs() if j["status"] == "queued"]
        if status != "stopped" and not remaining and self.running and self.store.get_setting("notify", "1") == "1":
            jobs = self.store.jobs()
            done = sum(1 for j in jobs if j["status"] == "done")
            need = sum(1 for j in jobs if j["status"] == "needs you")
            if self.notify:
                self.notify("AI Amalgam: job queue finished", f"{done} done" + (f", {need} need you" if need else "") + ".")
        return status

    def _last_note(self, chat_id):
        for m in reversed(self.store.messages(chat_id)[-6:]):
            if m["kind"] == "system":
                return m["text"]
        return ""

    def _outcome_from_chat(self, chat_id):
        msgs = [m for m in self.store.messages(chat_id)[-8:]]
        last = next((m for m in reversed(msgs) if m["kind"] != "system"), None)
        note = next((m["text"] for m in reversed(msgs) if m["kind"] == "system"), "")
        if last and last["kind"] == "question":
            return "question", note
        if last and last["kind"] == "command":
            return "commands", note
        if "couldn't reply" in note or LIMIT_RE.search(note):
            return "error", note
        if note.startswith("Stopped"):
            return None, note
        return "done", note

    # ----- your actions -----

    def add(self, text, project_id=None):
        text = (text or "").strip()
        if not text:
            return None
        return self.store.add_job(text[:20000], project_id)

    def stop(self, job_id):
        with self._lock:
            j = self.store.job(job_id)
            if j and j["status"] == "running" and self.orch is not None:
                self.orch.stop()
                return True
        return False

    def retry(self, job_id):
        j = self.store.job(job_id)
        if j and j["status"] in ("failed", "stopped", "needs you", "done"):
            self.store.update_job(job_id, status="queued", note="", finished=0)
            self.store.set_setting("queue_paused_until", "0")
            return True
        return False

    # ----- keep the PC awake while a job (or any discussion) runs -----

    def _keep_awake(self, on):
        if on == self._awake or sys.platform != "win32":
            self._awake = on
            return
        self._awake = on
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        except Exception as e:
            print("Couldn't change the sleep setting:", e)

