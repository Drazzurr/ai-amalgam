"""Saved chats in one local SQLite file (amalgam.db next to the app)."""

import json
import re
import sqlite3
import threading
import time


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
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}
        if "doc_link" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN doc_link TEXT NOT NULL DEFAULT ''")
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
        self._db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                doc_link TEXT NOT NULL DEFAULT '',
                memory TEXT NOT NULL DEFAULT '',
                roles TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL,
                updated REAL NOT NULL
            )""")
        self._db.commit()

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
            self._db.commit()

    def chat_settings(self, chat_id):
        with self._lock:
            row = self._db.execute("SELECT doc_link, allow_edits FROM chats WHERE id = ?",
                                   (chat_id,)).fetchone()
        return {"doc_link": row[0], "allow_edits": bool(row[1])} if row else None

    def set_chat_settings(self, chat_id, doc_link=None, allow_edits=None):
        with self._lock:
            if doc_link is not None:
                self._db.execute("UPDATE chats SET doc_link = ? WHERE id = ?", (doc_link.strip(), chat_id))
            if allow_edits is not None:
                self._db.execute("UPDATE chats SET allow_edits = ? WHERE id = ?", (int(bool(allow_edits)), chat_id))
            self._db.commit()

    def new_chat(self, title, project_id=None):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO chats (title, created, updated, project_id) VALUES (?, ?, ?, ?)",
                (title[:80] or "New chat", now, now, project_id),
            )
            self._db.commit()
            return cur.lastrowid

    def add(self, chat_id, speaker, kind, text):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (chat_id, speaker, kind, text, created) VALUES (?, ?, ?, ?, ?)",
                (chat_id, speaker, kind, text, now),
            )
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            self._db.execute("UPDATE projects SET updated = ? WHERE id = (SELECT project_id FROM chats WHERE id = ?)",
                             (now, chat_id))
            self._db.commit()
            return {"id": cur.lastrowid, "speaker": speaker, "kind": kind, "text": text, "created": now}

    def messages(self, chat_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, speaker, kind, text, created FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        return [dict(zip(("id", "speaker", "kind", "text", "created"), r)) for r in rows]

    def count_turns(self, speaker, since):
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE speaker = ? AND kind = 'model' AND created >= ?",
                (speaker, since)).fetchone()
        return row[0]

    def chats(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, updated, project_id FROM chats ORDER BY updated DESC LIMIT 300"
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
                "INSERT INTO projects (name, created, updated) VALUES (?, ?, ?)",
                (name.strip()[:80] or "New project", now, now))
            self._db.commit()
            return cur.lastrowid

    def projects(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, instructions, doc_link, memory, roles FROM projects ORDER BY updated DESC").fetchall()
        return [self._proj(r) for r in rows]

    @staticmethod
    def _proj(r):
        d = dict(zip(("id", "name", "instructions", "doc_link", "memory", "roles"), r))
        try:
            d["roles"] = json.loads(d["roles"] or "{}")
        except ValueError:
            d["roles"] = {}
        return d

    def project(self, project_id):
        if project_id is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT id, name, instructions, doc_link, memory, roles FROM projects WHERE id = ?",
                                   (project_id,)).fetchone()
        return self._proj(row) if row else None

    def update_project(self, project_id, name=None, instructions=None, doc_link=None, memory=None, roles=None):
        with self._lock:
            if roles is not None:
                self._db.execute("UPDATE projects SET roles = ?, updated = ? WHERE id = ?",
                                 (json.dumps({k: str(v).strip()[:300] for k, v in roles.items()}), time.time(), project_id))
            for col, val in (("name", name), ("instructions", instructions), ("doc_link", doc_link), ("memory", memory)):
                if val is not None:
                    v = val.strip()[:80] if col == "name" else val.strip()
                    if col == "name" and not v:
                        continue
                    self._db.execute(f"UPDATE projects SET {col} = ?, updated = ? WHERE id = ?",
                                     (v, time.time(), project_id))
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
                self._db.execute("UPDATE projects SET memory = ? WHERE id = ?",
                                 ((existing.rstrip() + "\n" + add).strip(), project_id))
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

    # ----- attachments -----

    def add_attachment(self, chat_id, name, path, size):
        with self._lock:
            self._db.execute("INSERT INTO attachments (chat_id, name, path, size, created) VALUES (?, ?, ?, ?, ?)",
                             (chat_id, name, path, size, time.time()))
            self._db.commit()

    def attachments(self, chat_id):
        with self._lock:
            rows = self._db.execute("SELECT id, name, path, size FROM attachments WHERE chat_id = ? ORDER BY id",
                                    (chat_id,)).fetchall()
        return [dict(zip(("id", "name", "path", "size"), r)) for r in rows]

    # ----- chats: rename / delete / move -----

    def rename_chat(self, chat_id, title):
        title = title.strip()[:80]
        if title:
            with self._lock:
                self._db.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
                self._db.commit()

    def delete_chat(self, chat_id):
        with self._lock:
            self._db.execute("DELETE FROM attachments WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._db.commit()

    def move_chat(self, chat_id, project_id):
        with self._lock:
            self._db.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))
            self._db.commit()

    def delete_project(self, project_id):
        """Removes the project; its chats are kept and move to the main chat list."""
        with self._lock:
            self._db.execute("UPDATE chats SET project_id = NULL WHERE project_id = ?", (project_id,))
            self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._db.commit()
