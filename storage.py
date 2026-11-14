"""Saved chats in one local SQLite file (amalgam.db next to the app)."""

import json
import re
import sqlite3
import threading
import time
import uuid


def new_uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL REFERENCES chats(id),
                speaker TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                created REAL NOT NULL
            );
            """
        )
        mcols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
        if "device" not in mcols:   # which device a message was sent from ("phone|Pixel", "pc|Desk")
            self._db.execute("ALTER TABLE messages ADD COLUMN device TEXT NOT NULL DEFAULT ''")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}
        if "doc_link" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN doc_link TEXT NOT NULL DEFAULT ''")
        if "models_off" not in cols:   # models switched off in this chat, comma-separated keys
            self._db.execute("ALTER TABLE chats ADD COLUMN models_off TEXT NOT NULL DEFAULT ''")
        if "allow_edits" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN allow_edits INTEGER NOT NULL DEFAULT 0")
        if "project_id" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN project_id INTEGER")
        if "summary" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            self._db.execute("ALTER TABLE chats ADD COLUMN summary_upto INTEGER NOT NULL DEFAULT 0")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS doc_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, project_id INTEGER,
                created REAL NOT NULL, tool TEXT NOT NULL, doc_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                name TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL);
        """)
        pcols = {r[1] for r in self._db.execute("PRAGMA table_info(projects)")}
        if pcols and "memory" not in pcols:
            self._db.execute("ALTER TABLE projects ADD COLUMN memory TEXT NOT NULL DEFAULT ''")
        if pcols and "roles" not in pcols:
            self._db.execute("ALTER TABLE projects ADD COLUMN roles TEXT NOT NULL DEFAULT '{}'")
        if pcols and "code_dir" not in pcols:
            self._db.execute("ALTER TABLE projects ADD COLUMN code_dir TEXT NOT NULL DEFAULT ''")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS code_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, project_id INTEGER,
                created REAL NOT NULL, speaker TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL,
                files TEXT NOT NULL DEFAULT '[]', undone INTEGER NOT NULL DEFAULT 0)""")
        self._db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                doc_link TEXT NOT NULL DEFAULT '',
                memory TEXT NOT NULL DEFAULT '',
                roles TEXT NOT NULL DEFAULT '{}',
                code_dir TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                updated REAL NOT NULL
            )""")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, project_id INTEGER,
                status TEXT NOT NULL DEFAULT 'queued', chat_id INTEGER, position REAL NOT NULL,
                created REAL NOT NULL, started REAL NOT NULL DEFAULT 0, finished REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '')""")
        # Mod Forge: modpacks and our own mods (PC only; never synced: they point at big local folders)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS forge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, game TEXT NOT NULL, name TEXT NOT NULL,
                project_id INTEGER, folder TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT '{}', approved INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL)""")
        self._migrate_sync()
        self._setup_search()
        self._db.commit()

    # ----- full-text search index -----

    def _setup_search(self):
        """A word index of all messages (SQLite FTS5), kept up to date by triggers. Built once from the
        existing history (a couple of seconds even for a huge one). Without FTS5 in this Python's
        SQLite, search simply keeps using the plain scan."""
        db = self._db
        try:
            had = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', "
                       "content_rowid='id', tokenize='unicode61 remove_diacritics 2')")
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
                    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
            """)
            if not had:
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
            self._fts = True
        except sqlite3.OperationalError as e:
            print("Search index not available here (plain search instead):", e)
            for t in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
                try:
                    db.execute(f"DROP TRIGGER IF EXISTS {t}")
                except sqlite3.Error:
                    pass
            self._fts = False

    def _fts_ready(self):
        return getattr(self, "_fts", False)

    def _migrate_sync(self):
        """Ids that are the same on every device (for syncing), plus edit times and deletion records."""
        for table in ("chats", "projects", "messages", "attachments"):
            cols = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if "uid" not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
            if table in ("chats", "projects") and "edited" not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN edited REAL NOT NULL DEFAULT 0")
            ids = [r[0] for r in self._db.execute(f"SELECT id FROM {table} WHERE uid IS NULL OR uid = ''")]
            for i in ids:
                self._db.execute(f"UPDATE {table} SET uid = ? WHERE id = ?", (new_uid(), i))
            self._db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_uid ON {table}(uid)")
        self._db.execute("UPDATE chats SET edited = created WHERE edited = 0")
        self._db.execute("UPDATE projects SET edited = created WHERE edited = 0")
        self._db.execute("CREATE TABLE IF NOT EXISTS tombstones (uid TEXT PRIMARY KEY, kind TEXT NOT NULL, at REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS setting_times (key TEXT PRIMARY KEY, at REAL NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id)")

    def _tomb(self, kind, uids):
        now = time.time()
        for u in uids:
            if u:
                self._db.execute("INSERT OR REPLACE INTO tombstones (uid, kind, at) VALUES (?, ?, ?)", (u, kind, now))

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self._lock:
            old = self._db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            self._db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
            if not old or old[0] != str(value):
                self._db.execute("INSERT OR REPLACE INTO setting_times (key, at) VALUES (?, ?)", (key, time.time()))
            self._db.commit()

    def chat_settings(self, chat_id):
        with self._lock:
            row = self._db.execute("SELECT doc_link, allow_edits, models_off FROM chats WHERE id = ?",
                                   (chat_id,)).fetchone()
        return ({"doc_link": row[0], "allow_edits": bool(row[1]),
                 "models_off": [k for k in (row[2] or "").split(",") if k]} if row else None)

    def set_chat_settings(self, chat_id, doc_link=None, allow_edits=None, models_off=None):
        with self._lock:
            if models_off is not None:
                val = ",".join(sorted(set(models_off)))
                self._db.execute("UPDATE chats SET models_off = ?, edited = ? WHERE id = ? AND models_off != ?",
                                 (val, time.time(), chat_id, val))
            if doc_link is not None:
                self._db.execute("UPDATE chats SET doc_link = ?, edited = ? WHERE id = ? AND doc_link != ?",
                                 (doc_link.strip(), time.time(), chat_id, doc_link.strip()))
            if allow_edits is not None:
                self._db.execute("UPDATE chats SET allow_edits = ?, edited = ? WHERE id = ? AND allow_edits != ?",
                                 (int(bool(allow_edits)), time.time(), chat_id, int(bool(allow_edits))))
            self._db.commit()

    def new_chat(self, title, project_id=None):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO chats (title, created, updated, project_id, uid, edited) VALUES (?, ?, ?, ?, ?, ?)",
                (title[:80] or "New chat", now, now, project_id, new_uid(), now),
            )
            self._db.commit()
            return cur.lastrowid

    def add(self, chat_id, speaker, kind, text, device=""):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (chat_id, speaker, kind, text, created, uid, device) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, speaker, kind, text, now, new_uid(), device or ""),
            )
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            self._db.execute("UPDATE projects SET updated = ? WHERE id = (SELECT project_id FROM chats WHERE id = ?)",
                             (now, chat_id))
            self._db.commit()
            return {"id": cur.lastrowid, "speaker": speaker, "kind": kind, "text": text, "created": now,
                    "device": device or ""}

    def messages(self, chat_id, after=0):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, speaker, kind, text, created, device FROM messages WHERE chat_id = ? AND id > ? ORDER BY id",
                (chat_id, after),
            ).fetchall()
        return [dict(zip(("id", "speaker", "kind", "text", "created", "device"), r)) for r in rows]

    def messages_tail(self, chat_id, limit, before=None):
        """The newest `limit` messages (before message id `before`, if given), oldest first,
        plus how many older ones are left. Long chats open with just these."""
        cols = ("id", "speaker", "kind", "text", "created", "device")
        with self._lock:
            cond, args = ("AND id < ?", [before]) if before else ("", [])
            rows = self._db.execute(f"SELECT id, speaker, kind, text, created, device FROM messages "
                                    f"WHERE chat_id = ? {cond} ORDER BY id DESC LIMIT ?",
                                    [chat_id] + args + [limit]).fetchall()
            rows.reverse()
            left = self._db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND id < ?",
                                    (chat_id, rows[0][0])).fetchone()[0] if rows else 0
        return [dict(zip(cols, r)) for r in rows], left

    def last_user_id(self, chat_id):
        """Id of the newest message you wrote in a chat (0 if none)."""
        with self._lock:
            row = self._db.execute("SELECT MAX(id) FROM messages WHERE chat_id = ? AND kind = 'user'",
                                   (chat_id,)).fetchone()
        return row[0] or 0

    def count_turns(self, speaker, since):
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE speaker = ? AND kind = 'model' AND created >= ?",
                (speaker, since)).fetchone()
        return row[0]

    def chats(self):
        with self._lock:
            rows = self._db.execute(
                # the 300 most recent, plus every chat in a project (so older project chats don't vanish)
                "SELECT id, title, updated, project_id FROM chats WHERE project_id IS NOT NULL "
                "OR id IN (SELECT id FROM chats ORDER BY updated DESC LIMIT 300) ORDER BY updated DESC"
            ).fetchall()
        return [dict(zip(("id", "title", "updated", "project_id"), r)) for r in rows]

    def chat_project(self, chat_id):
        with self._lock:
            row = self._db.execute("SELECT project_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    # ----- projects -----

    def new_project(self, name):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO projects (name, created, updated, uid, edited) VALUES (?, ?, ?, ?, ?)",
                (name.strip()[:80] or "New project", now, now, new_uid(), now))
            self._db.commit()
            return cur.lastrowid

    def projects(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, instructions, doc_link, memory, roles, code_dir FROM projects ORDER BY updated DESC").fetchall()
        return [self._proj(r) for r in rows]

    @staticmethod
    def _proj(r):
        d = dict(zip(("id", "name", "instructions", "doc_link", "memory", "roles", "code_dir"), r))
        try:
            d["roles"] = json.loads(d["roles"] or "{}")
        except ValueError:
            d["roles"] = {}
        return d

    def project(self, project_id):
        if project_id is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT id, name, instructions, doc_link, memory, roles, code_dir FROM projects WHERE id = ?",
                                   (project_id,)).fetchone()
        return self._proj(row) if row else None

    def update_project(self, project_id, name=None, instructions=None, doc_link=None, memory=None, roles=None,
                       code_dir=None):
        with self._lock:
            if roles is not None:
                self._db.execute("UPDATE projects SET roles = ?, updated = ?, edited = ? WHERE id = ? AND roles != ?",
                                 (json.dumps({k: str(v).strip()[:300] for k, v in roles.items()}), time.time(),
                                  time.time(), project_id,
                                  json.dumps({k: str(v).strip()[:300] for k, v in roles.items()})))
            for col, val in (("name", name), ("instructions", instructions), ("doc_link", doc_link), ("memory", memory),
                             ("code_dir", code_dir)):
                if val is not None:
                    v = val.strip()[:80] if col == "name" else val.strip()
                    if col == "name" and not v:
                        continue
                    # code_dir is this device's own folder, so it isn't a synced edit
                    edited = ", edited = ?" if col != "code_dir" else ""
                    args = (v, time.time()) + ((time.time(),) if edited else ()) + (project_id, v)
                    self._db.execute(f"UPDATE projects SET {col} = ?, updated = ?{edited} WHERE id = ? AND {col} != ?",
                                     args)
            self._db.commit()

    def add_memory(self, project_id, lines):
        """Append remembered facts (one per line) to a project's memory, skipping duplicates."""
        with self._lock:
            row = self._db.execute("SELECT memory FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not row:
                return []
            existing = row[0] or ""
            norm = lambda t: re.sub(r"\s*\([A-Z][a-z]{2} \d{1,2}\)\s*$", "", t.strip().lstrip("-").strip()).rstrip(".").lower()
            have = {norm(l) for l in existing.splitlines()}
            new = []
            for l in lines:
                if norm(l) and norm(l) not in have:
                    have.add(norm(l))
                    new.append(l)
            if new:
                stamp = time.strftime("%b %d")
                add = "\n".join(f"- {l} ({stamp})" for l in new)
                self._db.execute("UPDATE projects SET memory = ?, edited = ? WHERE id = ?",
                                 ((existing.rstrip() + "\n" + add).strip(), time.time(), project_id))
                self._db.commit()
            return new

    # ----- condensed history -----

    def summary(self, chat_id):
        with self._lock:
            row = self._db.execute("SELECT summary, summary_upto FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return (row[0], row[1]) if row else ("", 0)

    def set_summary(self, chat_id, text, upto):
        with self._lock:
            self._db.execute("UPDATE chats SET summary = ?, summary_upto = ? WHERE id = ?", (text, upto, chat_id))
            self._db.commit()

    # ----- doc edits -----

    def add_doc_edit(self, chat_id, project_id, tool, doc_id, summary):
        with self._lock:
            self._db.execute("INSERT INTO doc_edits (chat_id, project_id, created, tool, doc_id, summary) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (chat_id, project_id, time.time(), tool, doc_id, summary[:300]))
            self._db.commit()

    def doc_edits(self, chat_id=None, project_id=None, limit=30):
        with self._lock:
            if project_id:
                rows = self._db.execute("SELECT id, chat_id, created, tool, doc_id, summary FROM doc_edits "
                                        "WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT id, chat_id, created, tool, doc_id, summary FROM doc_edits "
                                        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(zip(("id", "chat_id", "created", "tool", "doc_id", "summary"), r)) for r in rows]

    # ----- code changes (one row per model turn that changed files) -----

    def add_code_change(self, chat_id, project_id, speaker, before, after, files):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO code_changes (chat_id, project_id, created, speaker, before, after, files) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, project_id, time.time(), speaker, before, after, json.dumps(files)))
            self._db.commit()
            return cur.lastrowid

    def code_changes(self, project_id, limit=40):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, chat_id, created, speaker, before, after, files, undone FROM code_changes "
                "WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(("id", "chat_id", "created", "speaker", "before", "after", "files", "undone"), r))
            d["files"] = json.loads(d["files"] or "[]")
            out.append(d)
        return out

    def code_change(self, change_id):
        with self._lock:
            row = self._db.execute("SELECT id, project_id, before, after FROM code_changes WHERE id = ?",
                                   (change_id,)).fetchone()
        return dict(zip(("id", "project_id", "before", "after"), row)) if row else None

    def mark_undone(self, project_id, from_id):
        with self._lock:
            self._db.execute("UPDATE code_changes SET undone = 1 WHERE project_id = ? AND id >= ?",
                             (project_id, from_id))
            self._db.commit()

    # ----- attachments -----

    def add_attachment(self, chat_id, name, path, size):
        with self._lock:
            self._db.execute("INSERT INTO attachments (chat_id, name, path, size, created, uid) VALUES (?, ?, ?, ?, ?, ?)",
                             (chat_id, name, path, size, time.time(), new_uid()))
            self._db.commit()

    def attachments(self, chat_id):
        with self._lock:
            rows = self._db.execute("SELECT id, name, path, size FROM attachments WHERE chat_id = ? ORDER BY id",
                                    (chat_id,)).fetchall()
        return [dict(zip(("id", "name", "path", "size"), r)) for r in rows]

    # ----- job queue (this device only) -----

    JOB_COLS = ("id", "text", "project_id", "status", "chat_id", "position", "created", "started", "finished", "note")

    def add_job(self, text, project_id=None):
        now = time.time()
        with self._lock:
            top = self._db.execute("SELECT COALESCE(MAX(position), 0) FROM jobs").fetchone()[0]
            cur = self._db.execute("INSERT INTO jobs (text, project_id, position, created) VALUES (?, ?, ?, ?)",
                                   (text, project_id, top + 1, now))
            self._db.commit()
            return cur.lastrowid

    # ----- Mod Forge -----

    FORGE_COLS = ("id", "kind", "game", "name", "project_id", "folder", "plan", "state", "approved", "created", "updated")

    def _forge_row(self, r):
        d = dict(zip(self.FORGE_COLS, r))
        for k, empty in (("plan", []), ("state", {})):
            try:
                d[k] = json.loads(d[k] or "null") or empty
            except ValueError:
                d[k] = empty
        d["approved"] = bool(d["approved"])
        return d

    def forge_add(self, kind, game, name, project_id, folder):
        now = time.time()
        with self._lock:
            cur = self._db.execute("INSERT INTO forge_items (kind, game, name, project_id, folder, created, updated) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, game, name, project_id, folder, now, now))
            self._db.commit()
            return cur.lastrowid

    def forge_items(self, kind=None):
        with self._lock:
            q = f"SELECT {', '.join(self.FORGE_COLS)} FROM forge_items" + (" WHERE kind = ?" if kind else "") + " ORDER BY updated DESC"
            rows = self._db.execute(q, (kind,) if kind else ()).fetchall()
        return [self._forge_row(r) for r in rows]

    def forge_item(self, item_id=None, project_id=None):
        with self._lock:
            if item_id is not None:
                row = self._db.execute(f"SELECT {', '.join(self.FORGE_COLS)} FROM forge_items WHERE id = ?", (item_id,)).fetchone()
            else:
                row = self._db.execute(f"SELECT {', '.join(self.FORGE_COLS)} FROM forge_items WHERE project_id = ?",
                                       (project_id,)).fetchone()
        return self._forge_row(row) if row else None

    def forge_update(self, item_id, **fields):
        fields = {k: (json.dumps(v) if k in ("plan", "state") else int(v) if k == "approved" else v)
                  for k, v in fields.items() if k in self.FORGE_COLS and k not in ("id", "created")}
        if not fields:
            return
        fields["updated"] = time.time()
        with self._lock:
            self._db.execute(f"UPDATE forge_items SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                             (*fields.values(), item_id))
            self._db.commit()

    def forge_delete(self, item_id):
        with self._lock:
            self._db.execute("DELETE FROM forge_items WHERE id = ?", (item_id,))
            self._db.commit()

    def jobs(self):
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(self.JOB_COLS)} FROM jobs ORDER BY position, id").fetchall()
        return [dict(zip(self.JOB_COLS, r)) for r in rows]

    def job(self, job_id):
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self.JOB_COLS)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(zip(self.JOB_COLS, row)) if row else None

    def update_job(self, job_id, **fields):
        fields = {k: v for k, v in fields.items() if k in self.JOB_COLS and k != "id"}
        if not fields:
            return
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                             list(fields.values()) + [job_id])
            self._db.commit()

    def delete_job(self, job_id):
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id = ? AND status != 'running'", (job_id,))
            self._db.commit()

    def clear_finished_jobs(self):
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE status IN ('done', 'stopped', 'failed')")
            self._db.commit()

    def move_job(self, job_id, direction):
        """Swap a queued job with the queued job above (direction -1) or below (+1) it."""
        with self._lock:
            rows = self._db.execute("SELECT id, position FROM jobs WHERE status = 'queued' ORDER BY position, id").fetchall()
            ids = [r[0] for r in rows]
            if job_id not in ids:
                return
            i = ids.index(job_id)
            j = i + (1 if direction > 0 else -1)
            if 0 <= j < len(ids):
                (a, pa), (b, pb) = rows[i], rows[j]
                if pa == pb:
                    pb = pa + (1 if j > i else -1)
                self._db.execute("UPDATE jobs SET position = ? WHERE id = ?", (pb, a))
                self._db.execute("UPDATE jobs SET position = ? WHERE id = ?", (pa, b))
                self._db.commit()

    # ----- chats: rename / delete / move -----

    def rename_chat(self, chat_id, title):
        title = title.strip()[:80]
        if title:
            with self._lock:
                self._db.execute("UPDATE chats SET title = ?, edited = ? WHERE id = ?", (title, time.time(), chat_id))
                self._db.commit()

    def chat_title(self, chat_id):
        with self._lock:
            row = self._db.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def delete_messages(self, chat_id, after_id, from_id=None):
        """Delete a chat's messages with id > after_id (used by Regenerate and Edit)."""
        with self._lock:
            self._tomb("message", [r[0] for r in self._db.execute(
                "SELECT uid FROM messages WHERE chat_id = ? AND id > ?", (chat_id, after_id))])
            self._db.execute("DELETE FROM messages WHERE chat_id = ? AND id > ?", (chat_id, after_id))
            self._db.execute("UPDATE chats SET summary = '', summary_upto = 0 WHERE id = ? AND summary_upto > ?",
                             (chat_id, after_id))
            self._db.commit()

    def search(self, query, limit=40):
        """Chats matching your words, best matches first: chats whose title matches, then chats whose
        messages contain all the words (any order; word starts count, so "tav" finds "tavern").
        Falls back to a plain text scan when the index can't answer (or isn't built yet)."""
        query = query.strip()
        words = re.findall(r"\w+", query)
        if not query:
            return []
        with self._lock:
            ready = self._fts_ready()
        if ready and words:
            try:
                return self._search_index(query, words, limit) or self._search_scan(query, limit)
            except sqlite3.Error as e:
                print("Search index error, using a plain scan:", e)
        return self._search_scan(query, limit)

    @staticmethod
    def _snippet(text, words_or_query):
        low = text.lower()
        terms = words_or_query if isinstance(words_or_query, list) else [words_or_query]
        i = min((low.find(t.lower()) for t in terms if low.find(t.lower()) >= 0), default=0)
        return ("…" if i > 40 else "") + text[max(0, i - 40): i + 100].replace("\n", " ") + "…"

    def _search_index(self, query, words, limit):
        match = " ".join('"' + w.replace('"', '') + '"*' for w in words)
        like = "%" + re.sub(r"([\\%_])", r"\\\1", query) + "%"
        with self._lock:
            hits = self._db.execute(
                "SELECT m.chat_id, MIN(f.score) AS best FROM (SELECT rowid, rank AS score FROM messages_fts "
                "WHERE messages_fts MATCH ?) f JOIN messages m ON m.id = f.rowid "
                "GROUP BY m.chat_id ORDER BY best LIMIT ?", (match, limit)).fetchall()
            titled = self._db.execute("SELECT id FROM chats WHERE title LIKE ? ESCAPE '\\' "
                                      "ORDER BY updated DESC LIMIT ?", (like, limit)).fetchall()
            order = [r[0] for r in titled] + [h[0] for h in hits if h[0] not in {t[0] for t in titled}]
            info = {}
            for cid in order[:limit]:
                row = self._db.execute("SELECT title, project_id, updated FROM chats WHERE id = ?", (cid,)).fetchone()
                if not row:
                    continue
                best = self._db.execute(
                    "SELECT m.text FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                    "WHERE messages_fts MATCH ? AND m.chat_id = ? ORDER BY m.id DESC LIMIT 1", (match, cid)).fetchone()
                info[cid] = (row, best[0] if best else "")
        out = []
        for cid in order[:limit]:
            if cid not in info:
                continue
            (title, pid, updated), text = info[cid]
            out.append({"id": cid, "title": title, "project_id": pid, "updated": updated,
                        "snippet": self._snippet(text, words) if text else ""})
        return out

    def _search_scan(self, query, limit=40):
        """Plain scan: chats whose title or messages contain the exact text (case-insensitive), newest first."""
        # % and _ are wildcards in LIKE: search for them as ordinary characters
        q = "%" + re.sub(r"([\\%_])", r"\\\1", query.strip()) + "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.title, c.project_id, c.updated, "
                "(SELECT m.text FROM messages m WHERE m.chat_id = c.id AND m.text LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT 1) "
                "FROM chats c WHERE c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.id "
                "AND m.text LIKE ? ESCAPE '\\') ORDER BY c.updated DESC LIMIT ?", (q, q, q, limit)).fetchall()
        return [{"id": cid, "title": title, "project_id": pid, "updated": updated,
                 "snippet": self._snippet(text, query.strip()) if text else ""}
                for cid, title, pid, updated, text in rows]

    def delete_chat(self, chat_id):
        with self._lock:
            self._tomb("chat", [r[0] for r in self._db.execute("SELECT uid FROM chats WHERE id = ?", (chat_id,))])
            self._forget_chat(chat_id)
            self._db.commit()

    def _forget_chat(self, chat_id):
        """Remove a chat and what hangs off it. Its file changes stay in the project's history
        (the Code tab can still undo them); they just no longer point at the chat."""
        self._db.execute("DELETE FROM attachments WHERE chat_id = ?", (chat_id,))
        self._db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self._db.execute("DELETE FROM doc_edits WHERE chat_id = ?", (chat_id,))
        self._db.execute("UPDATE code_changes SET chat_id = NULL WHERE chat_id = ?", (chat_id,))
        self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def move_chat(self, chat_id, project_id):
        with self._lock:
            self._db.execute("UPDATE chats SET project_id = ?, edited = ? WHERE id = ?", (project_id, time.time(), chat_id))
            self._db.commit()

    def delete_project(self, project_id):
        """Removes the project; its chats are kept and move to the main chat list."""
        with self._lock:
            self._tomb("project", [r[0] for r in self._db.execute("SELECT uid FROM projects WHERE id = ?", (project_id,))])
            self._db.execute("UPDATE chats SET project_id = NULL, edited = ? WHERE project_id = ?", (time.time(), project_id))
            self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._db.commit()

    # ----- sync (see sync.py) -----

    def uid_of(self, table, row_id):
        if row_id is None:
            return None
        with self._lock:
            row = self._db.execute(f"SELECT uid FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return row[0] if row else None

    def sync_tombstones(self):
        with self._lock:
            rows = self._db.execute("SELECT uid, kind, at FROM tombstones").fetchall()
        return {u: {"kind": k, "at": a} for u, k, a in rows}

    def sync_apply_tombstones(self, tombs):
        """Delete here whatever was deleted on another device. Returns how many things went."""
        gone = 0
        with self._lock:
            for uid, t in tombs.items():
                self._db.execute("INSERT OR IGNORE INTO tombstones (uid, kind, at) VALUES (?, ?, ?)",
                                 (uid, t["kind"], t["at"]))
                if t["kind"] == "message":
                    gone += self._db.execute("DELETE FROM messages WHERE uid = ?", (uid,)).rowcount
                elif t["kind"] == "chat":
                    row = self._db.execute("SELECT id FROM chats WHERE uid = ?", (uid,)).fetchone()
                    if row:
                        self._forget_chat(row[0])
                        gone += 1
                elif t["kind"] == "project":
                    row = self._db.execute("SELECT id FROM projects WHERE uid = ?", (uid,)).fetchone()
                    if row:
                        self._db.execute("UPDATE chats SET project_id = NULL WHERE project_id = ?", (row[0],))
                        self._db.execute("DELETE FROM projects WHERE id = ?", (row[0],))
                        gone += 1
                elif t["kind"] == "attachment":
                    gone += self._db.execute("DELETE FROM attachments WHERE uid = ?", (uid,)).rowcount
            self._db.commit()
        return gone

    def sync_projects(self):
        with self._lock:
            rows = self._db.execute("SELECT id, uid, name, instructions, doc_link, memory, roles, code_dir, "
                                    "created, edited FROM projects").fetchall()
        out = {}
        for r in rows:
            d = dict(zip(("id", "uid", "name", "instructions", "doc_link", "memory", "roles", "code_dir",
                          "created", "edited"), r))
            try:
                d["roles"] = json.loads(d["roles"] or "{}")
            except ValueError:
                d["roles"] = {}
            out[d["uid"]] = d
        return out

    def sync_put_project(self, m):
        with self._lock:
            row = self._db.execute("SELECT id FROM projects WHERE uid = ?", (m["uid"],)).fetchone()
            vals = (m.get("name") or "Project", m.get("instructions") or "", m.get("doc_link") or "",
                    m.get("memory") or "", json.dumps(m.get("roles") or {}), m.get("edited") or time.time())
            if row:
                self._db.execute("UPDATE projects SET name = ?, instructions = ?, doc_link = ?, memory = ?, roles = ?, "
                                 "edited = ? WHERE id = ?", vals + (row[0],))
            else:
                self._db.execute("INSERT INTO projects (name, instructions, doc_link, memory, roles, edited, uid, "
                                 "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 vals + (m["uid"], m.get("created") or time.time(), m.get("edited") or time.time()))
            self._db.commit()

    def sync_chat_index(self):
        with self._lock:
            rows = self._db.execute("SELECT uid, id FROM chats").fetchall()
        return {u: i for u, i in rows}

    def sync_chat_fingerprints(self):
        """uid -> a short text that changes whenever anything sync carries changes in that chat
        (messages added or removed, title, project, doc link, settings, attachments)."""
        with self._lock:
            chats = self._db.execute("SELECT id, uid, edited, updated, title, project_id, doc_link, allow_edits, "
                                     "models_off FROM chats").fetchall()
            msgs = {cid: (n, mx) for cid, n, mx in self._db.execute(
                "SELECT chat_id, COUNT(*), MAX(id) FROM messages GROUP BY chat_id")}
            atts = {cid: (n, mx) for cid, n, mx in self._db.execute(
                "SELECT chat_id, COUNT(*), MAX(id) FROM attachments GROUP BY chat_id")}
            puids = dict(self._db.execute("SELECT id, uid FROM projects"))
        out = {}
        for cid, uid, edited, updated, title, pid, link, edits, off in chats:
            out[uid] = repr((edited, updated, title, puids.get(pid), link, edits, off,
                             msgs.get(cid, (0, 0)), atts.get(cid, (0, 0))))
        return out

    def sync_chat(self, uid, workspace):
        import os
        with self._lock:
            c = self._db.execute(
                "SELECT c.id, c.uid, c.title, p.uid, c.doc_link, c.allow_edits, c.created, c.updated, c.edited, "
                "c.summary, c.models_off FROM chats c LEFT JOIN projects p ON p.id = c.project_id WHERE c.uid = ?", (uid,)).fetchone()
            if not c:
                return None
            msgs = self._db.execute("SELECT uid, speaker, kind, text, created, device FROM messages WHERE chat_id = ? "
                                    "ORDER BY id", (c[0],)).fetchall()
            atts = self._db.execute("SELECT uid, name, path, size, created FROM attachments WHERE chat_id = ? "
                                    "ORDER BY id", (c[0],)).fetchall()
        d = dict(zip(("id", "uid", "title", "project_uid", "doc_link", "allow_edits", "created", "updated",
                      "edited", "summary", "models_off"), c))
        d["allow_edits"] = bool(d["allow_edits"])

        def local(path):
            if not path:
                return None
            return path if os.path.isabs(path) else os.path.join(workspace, *path.replace("\\", "/").split("/"))
        d["messages"] = []
        for u, sp, k, t, cr, dev in msgs:
            x = {"uid": u, "speaker": sp, "kind": k, "text": t, "created": cr}
            if dev:
                x["device"] = dev
            if k == "image":
                x["local_path"] = local(t.strip())
            d["messages"].append(x)
        d["attachments"] = [{"uid": u, "name": n, "size": sz, "created": cr, "local_path": local(pa)}
                            for u, n, pa, sz, cr in atts]
        return d

    def sync_put_chat(self, m, put_file):
        """Bring a merged chat into this device. Messages keep time order; returns the local chat id."""
        with self._lock:
            pid = None
            if m.get("project_uid"):
                row = self._db.execute("SELECT id FROM projects WHERE uid = ?", (m["project_uid"],)).fetchone()
                pid = row[0] if row else None
            row = self._db.execute("SELECT id, edited FROM chats WHERE uid = ?", (m["uid"],)).fetchone()
            if row:
                cid = row[0]
                if (m.get("edited") or 0) > (row[1] or 0):
                    self._db.execute("UPDATE chats SET title = ?, project_id = ?, doc_link = ?, allow_edits = ?, "
                                     "models_off = ?, edited = ? WHERE id = ?",
                                     (m.get("title") or "Chat", pid, m.get("doc_link") or "",
                                      int(bool(m.get("allow_edits"))), m.get("models_off") or "", m["edited"], cid))
            else:
                cid = self._db.execute(
                    "INSERT INTO chats (title, created, updated, project_id, uid, edited, doc_link, allow_edits, "
                    "models_off) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m.get("title") or "Chat", m.get("created") or time.time(), m.get("updated") or time.time(), pid,
                     m["uid"], m.get("edited") or time.time(), m.get("doc_link") or "",
                     int(bool(m.get("allow_edits"))), m.get("models_off") or "")).lastrowid
            self._db.execute("UPDATE chats SET updated = MAX(updated, ?) WHERE id = ?", (m.get("updated") or 0, cid))
            have = {u: (i, cr) for u, i, cr in self._db.execute(
                "SELECT uid, id, created FROM messages WHERE chat_id = ?", (cid,))}
            new = [x for x in m["messages"] if x["uid"] not in have]
            latest = max((cr for _i, cr in have.values()), default=0)

            def text_of(x):
                if x["kind"] == "image" and x.get("file"):
                    rel = put_file(x["file"], cid)
                    return rel or x["text"]
                return x["text"]
            if new and min(x["created"] for x in new) < latest:
                # some arrived from the other device in between ours: rewrite in time order
                keep = {u: t for u, t in self._db.execute("SELECT uid, text FROM messages WHERE chat_id = ?", (cid,))}
                self._db.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
                for x in m["messages"]:
                    t = keep[x["uid"]] if x["uid"] in keep else text_of(x)
                    self._db.execute("INSERT INTO messages (chat_id, speaker, kind, text, created, uid, device) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, x["speaker"], x["kind"], t, x["created"],
                                                                      x["uid"], x.get("device", "")))
                self._db.execute("UPDATE chats SET summary = '', summary_upto = 0 WHERE id = ?", (cid,))
            else:
                for x in new:
                    self._db.execute("INSERT INTO messages (chat_id, speaker, kind, text, created, uid, device) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (cid, x["speaker"], x["kind"], text_of(x), x["created"], x["uid"],
                                      x.get("device", "")))
            have_att = {u for (u,) in self._db.execute("SELECT uid FROM attachments WHERE chat_id = ?", (cid,))}
            for a in m.get("attachments", []):
                if a["uid"] in have_att:
                    continue
                rel = put_file(a["file"], cid) if a.get("file") else None
                if rel:
                    self._db.execute("INSERT INTO attachments (chat_id, name, path, size, created, uid) "
                                     "VALUES (?, ?, ?, ?, ?, ?)",
                                     (cid, a["name"], rel, a.get("size") or 0, a.get("created") or time.time(), a["uid"]))
            self._db.commit()
        return cid

    def sync_settings(self, global_keys, project_templates):
        with self._lock:
            vals = dict(self._db.execute("SELECT key, value FROM app_settings"))
            times = dict(self._db.execute("SELECT key, at FROM setting_times"))
            projects = dict(self._db.execute("SELECT id, uid FROM projects"))
        out = {}
        for k in global_keys:
            if k in vals:
                out[k] = {"value": vals[k], "at": times.get(k, 0)}
        for pid, puid in projects.items():
            for tpl in project_templates:
                k = tpl.format(pid)
                if k in vals:
                    out[f"{puid}:{tpl}"] = {"value": vals[k], "at": times.get(k, 0)}
        return out

    def sync_put_setting(self, key, value, at, project_templates):
        if ":" in key:
            puid, tpl = key.split(":", 1)
            if tpl not in project_templates:
                return False
            with self._lock:
                row = self._db.execute("SELECT id FROM projects WHERE uid = ?", (puid,)).fetchone()
            if not row:
                return False
            key = tpl.format(row[0])
        with self._lock:
            old = self._db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if old and old[0] == str(value):
                self._db.execute("INSERT OR REPLACE INTO setting_times (key, at) VALUES (?, ?)", (key, at))
                self._db.commit()
                return False
            self._db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
            self._db.execute("INSERT OR REPLACE INTO setting_times (key, at) VALUES (?, ?)", (key, at))
            self._db.commit()
        return True
