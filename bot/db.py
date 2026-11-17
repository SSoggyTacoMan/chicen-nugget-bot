"""Tiny SQLite persistence layer.

Everything is stored locally in `data/bot.db`. Each operation opens its own
short-lived connection, which keeps the async handlers simple and avoids
sharing a connection across threads/event-loop turns.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    joined_at TEXT,
    left_at TEXT,
    joins INTEGER NOT NULL DEFAULT 0,
    leaves INTEGER NOT NULL DEFAULT 0,
    welcomed_at TEXT
);

CREATE TABLE IF NOT EXISTS member_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    channel_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_member_events ON member_events (kind, created_at);

CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    admin_note TEXT,
    admin_channel TEXT,
    admin_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_status ON access_requests (status, created_at);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    next_run REAL NOT NULL,
    recurrence TEXT,
    kind TEXT NOT NULL DEFAULT 'reminder',
    created_at TEXT NOT NULL,
    last_run REAL,
    done INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (done, next_run);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    role TEXT NOT NULL,
    user_id TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation, id);

CREATE TABLE IF NOT EXISTS memory_vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    conversation TEXT NOT NULL,
    role TEXT NOT NULL,
    user_id TEXT,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (message_id, model)
);
CREATE INDEX IF NOT EXISTS idx_memory_model ON memory_vectors (model, id);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_ts TEXT,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    option_index INTEGER NOT NULL,
    voted_at TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);

CREATE TABLE IF NOT EXISTS bot_threads (
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'bot',
    PRIMARY KEY (channel_id, thread_ts)
);

CREATE TABLE IF NOT EXISTS dm_chats (
    user_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, thread_ts)
);
CREATE INDEX IF NOT EXISTS idx_dm_chats_user ON dm_chats (user_id, archived, last_used_at);

CREATE TABLE IF NOT EXISTS dm_secret_threads (
    user_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (user_id, thread_ts)
);
CREATE INDEX IF NOT EXISTS idx_dm_secret_user ON dm_secret_threads (user_id, expires_at);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_day ON ai_usage (day, kind);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (user_id, done);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _rank_with_numpy(
    rows: list[dict[str, Any]],
    vector: list[float],
    query_norm: float,
    limit: int,
) -> list[dict[str, Any]] | None:
    """Vectorised cosine ranking. Returns None when numpy is unavailable."""
    try:
        import numpy as np
    except ImportError:
        return None

    dimensions_expected = len(vector)
    matrix: list[Any] = []
    kept: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("dimensions") or 0) != dimensions_expected:
            continue
        raw = row.get("vector")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != dimensions_expected * 4:
            continue
        matrix.append(np.frombuffer(raw, dtype=np.float32))
        kept.append(row)
    if not matrix:
        return []

    array = np.vstack(matrix)
    query = np.asarray(vector, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1)
    denominator = norms * query_norm
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(denominator > 0, (array @ query) / denominator, 0.0)
    order = np.argsort(-scores)[: max(1, limit)]
    return [
        {
            "message_id": kept[index].get("message_id"),
            "conversation": kept[index].get("conversation"),
            "role": kept[index].get("role"),
            "user_id": kept[index].get("user_id"),
            "text": kept[index].get("text"),
            "created_at": kept[index].get("created_at"),
            "score": float(scores[index]),
        }
        for index in order
    ]


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards so user text matches literally (use with ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()

    # ------------------------------------------------------------------ core
    def _connect(self) -> sqlite3.Connection:
        """Return this thread's long-lived connection.

        Opening a fresh SQLite connection (and re-applying PRAGMAs) for every
        query was measurable overhead: an agent turn performs dozens of small
        reads and a handful of writes. One connection per thread keeps the hot
        path cheap while staying safe for the synchronous call sites.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL is crash-safe for the database and much faster than the
        # default FULL sync, which fsynced on every single insert.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        try:
            Path(self.path).chmod(0o600)
        except OSError:
            pass
        return conn

    def close(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.conn = None

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            conn = self._connect()
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(access_requests)").fetchall()
            }
            if "admin_note" not in columns:
                conn.execute("ALTER TABLE access_requests ADD COLUMN admin_note TEXT")

            usage_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(ai_usage)").fetchall()
            }
            if "user_id" not in usage_columns:
                # Older databases predate per-user spend tracking. Keep the
                # column here so the index below can be created after.
                conn.execute(
                    "ALTER TABLE ai_usage ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage (day, user_id)"
            )

            thread_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(bot_threads)").fetchall()
            }
            if "owner" not in thread_columns:
                conn.execute(
                    "ALTER TABLE bot_threads ADD COLUMN owner TEXT NOT NULL DEFAULT 'bot'"
                )
            conn.commit()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return int(cur.lastrowid or 0)

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write and return the number of affected rows."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return max(0, int(cur.rowcount))

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------- kv
    def kv_get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def integrity_check(self) -> str:
        row = self._one("PRAGMA integrity_check")
        return str((row or {}).get("integrity_check") or "unknown")

    def kv_set(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def kv_del(self, key: str) -> None:
        self._write("DELETE FROM kv WHERE key = ?", (key,))

    def kv_prefix(self, prefix: str) -> list[tuple[str, str]]:
        rows = self._all(
            "SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (f"{_like_escape(prefix)}%",),
        )
        return [(r["key"], r["value"]) for r in rows]

    # -------------------------------------------------------------- members
    def record_join(
        self, user_id: str, username: str | None = None, channel_id: str | None = None
    ) -> None:
        now = iso()
        self._write(
            """
            INSERT INTO members (user_id, username, joined_at, joins)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, members.username),
                joined_at = excluded.joined_at,
                left_at = NULL,
                joins = members.joins + 1
            """,
            (user_id, username, now),
        )
        self._write(
            "INSERT INTO member_events (user_id, kind, channel_id, created_at) VALUES (?, 'join', ?, ?)",
            (user_id, channel_id, now),
        )

    def record_leave(self, user_id: str, channel_id: str | None = None) -> None:
        now = iso()
        self._write(
            """
            INSERT INTO members (user_id, left_at, leaves)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                left_at = excluded.left_at,
                leaves = members.leaves + 1
            """,
            (user_id, now),
        )
        self._write(
            "INSERT INTO member_events (user_id, kind, channel_id, created_at) VALUES (?, 'leave', ?, ?)",
            (user_id, channel_id, now),
        )

    def was_welcomed(self, user_id: str) -> bool:
        row = self._one("SELECT welcomed_at FROM members WHERE user_id = ?", (user_id,))
        return bool(row and row.get("welcomed_at"))

    def mark_welcomed(self, user_id: str) -> None:
        self._write(
            "UPDATE members SET welcomed_at = ? WHERE user_id = ?",
            (iso(), user_id),
        )

    def member(self, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM members WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------- access requests
    def create_access_request(self, user_id: str, reason: str) -> int:
        return self._write(
            "INSERT INTO access_requests (user_id, reason, created_at) VALUES (?, ?, ?)",
            (user_id, reason, iso()),
        )

    def access_request(self, request_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM access_requests WHERE id = ?", (request_id,))

    def pending_access_for_user(self, user_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM access_requests WHERE user_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    def last_access_for_user(self, user_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM access_requests WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    def pending_access(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM access_requests WHERE status = 'pending' ORDER BY created_at"
        )

    def decide_access(
        self, request_id: int, status: str, decided_by: str, note: str | None = None
    ) -> None:
        self._write(
            "UPDATE access_requests SET status = ?, decided_at = ?, decided_by = ?, "
            "admin_note = ? WHERE id = ?",
            (status, iso(), decided_by, note, request_id),
        )

    def set_access_admin_message(self, request_id: int, channel: str, ts: str) -> None:
        self._write(
            "UPDATE access_requests SET admin_channel = ?, admin_ts = ? WHERE id = ?",
            (channel, ts, request_id),
        )

    def access_counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM access_requests GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    # -------------------------------------------------------------- reminders
    def active_reminder_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM reminders WHERE done = 0")
        return int(row["n"]) if row else 0

    def add_reminder(
        self,
        user_id: str,
        target: str,
        text: str,
        next_run: datetime,
        recurrence: str | None,
        kind: str = "reminder",
    ) -> int:
        return self._write(
            "INSERT INTO reminders (user_id, target, text, next_run, recurrence, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, target, text, next_run.timestamp(), recurrence, kind, iso()),
        )

    def due_reminders(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        return self._all(
            "SELECT * FROM reminders WHERE done = 0 AND next_run <= ? ORDER BY next_run",
            (now.timestamp(),),
        )

    def advance_reminder(self, reminder_id: int, next_run: datetime | None) -> None:
        if next_run is None:
            self._write(
                "UPDATE reminders SET done = 1, last_run = ? WHERE id = ?",
                (utcnow().timestamp(), reminder_id),
            )
        else:
            self._write(
                "UPDATE reminders SET next_run = ?, last_run = ? WHERE id = ?",
                (next_run.timestamp(), utcnow().timestamp(), reminder_id),
            )

    def finish_reminder(self, reminder_id: int, error: str | None = None) -> None:
        self._write(
            "UPDATE reminders SET done = 1, last_run = ?, error = ? WHERE id = ?",
            (utcnow().timestamp(), error, reminder_id),
        )

    def reminder(self, reminder_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))

    def list_reminders(self, user_id: str | None = None) -> list[dict[str, Any]]:
        if user_id:
            return self._all(
                "SELECT * FROM reminders WHERE done = 0 AND user_id = ? ORDER BY next_run",
                (user_id,),
            )
        return self._all("SELECT * FROM reminders WHERE done = 0 ORDER BY next_run")

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE reminders SET done = 1 WHERE id = ? AND done = 0",
                (reminder_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def cancel_reminders_by_kind(self, kind: str, user_id: str | None = None) -> int:
        with self._write_lock:
            conn = self._connect()
            query = "UPDATE reminders SET done = 1 WHERE done = 0 AND kind = ?"
            params: list[Any] = [kind]
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount

    def find_recurring(self, kind: str, user_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM reminders WHERE done = 0 AND kind = ? AND user_id = ? LIMIT 1",
            (kind, user_id),
        )

    # --------------------------------------------------------------- messages
    def add_message(self, conversation: str, role: str, user_id: str | None, text: str) -> int:
        return self._write(
            "INSERT INTO messages (conversation, role, user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation, role, user_id, text, iso()),
        )

    def history(self, conversation: str, limit: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT role, user_id, text FROM messages WHERE conversation = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation, max(1, limit)),
        )
        rows.reverse()
        return rows

    def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{_like_escape(query)}%"
        return self._all(
            "SELECT conversation, role, user_id, text, created_at FROM messages "
            "WHERE text LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            (like, max(1, limit)),
        )

    def clear_history(self, conversation: str) -> int:
        self._execute(
            "DELETE FROM memory_vectors WHERE conversation = ?", (conversation,)
        )
        return self._execute(
            "DELETE FROM messages WHERE conversation = ?", (conversation,)
        )

    def clear_history_prefix(self, prefix: str) -> int:
        pattern = _like_escape(prefix) + "%"
        self._execute(
            "DELETE FROM memory_vectors WHERE conversation LIKE ? ESCAPE '\\'",
            (pattern,),
        )
        return self._execute(
            "DELETE FROM messages WHERE conversation LIKE ? ESCAPE '\\'",
            (pattern,),
        )

    def clear_all_history(self) -> int:
        self._execute("DELETE FROM memory_vectors")
        return self._execute("DELETE FROM messages")

    def prune_history(self, days: int) -> dict[str, int]:
        if days <= 0:
            return {"messages": 0, "vectors": 0, "usage": 0}
        cutoff = iso(utcnow() - timedelta(days=days))
        messages = self._execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        vectors = self._execute(
            "DELETE FROM memory_vectors WHERE created_at < ? "
            "OR message_id NOT IN (SELECT id FROM messages)",
            (cutoff,),
        )
        usage = self._execute("DELETE FROM ai_usage WHERE created_at < ?", (cutoff,))
        return {"messages": messages, "vectors": vectors, "usage": usage}

    def vacuum(self) -> None:
        with self._write_lock:
            conn = self._connect()
            conn.execute("VACUUM")
            conn.commit()

    def add_memory_vector(
        self,
        message_id: int,
        conversation: str,
        role: str,
        user_id: str | None,
        text: str,
        model: str,
        vector: list[float],
    ) -> int:
        if not vector:
            return 0
        blob = struct.pack(f"{len(vector)}f", *[float(value) for value in vector])
        return self._write(
            "INSERT OR IGNORE INTO memory_vectors "
            "(message_id, conversation, role, user_id, text, model, dimensions, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                conversation,
                role,
                user_id,
                text,
                model,
                len(vector),
                sqlite3.Binary(blob),
                iso(),
            ),
        )

    def search_memory_vectors(
        self, model: str, vector: list[float], limit: int = 8
    ) -> list[dict[str, Any]]:
        if not vector:
            return []
        rows = self._all(
            "SELECT message_id, conversation, role, user_id, text, dimensions, vector, created_at "
            "FROM memory_vectors WHERE model = ? ORDER BY id DESC LIMIT 5000",
            (model,),
        )
        query_norm = sum(value * value for value in vector) ** 0.5
        if query_norm <= 0:
            return []

        if rows:
            result = _rank_with_numpy(rows, vector, query_norm, limit)
            if result is not None:
                return result

        scored: list[dict[str, Any]] = []
        for row in rows:
            dimensions = int(row.get("dimensions") or 0)
            if dimensions != len(vector):
                continue
            raw = row.get("vector")
            if not isinstance(raw, (bytes, bytearray)):
                continue
            try:
                stored = struct.unpack(f"{dimensions}f", raw)
            except struct.error:
                continue
            norm = sum(value * value for value in stored) ** 0.5
            if norm <= 0:
                continue
            dot = sum(a * b for a, b in zip(vector, stored))
            scored.append(
                {
                    "message_id": row.get("message_id"),
                    "conversation": row.get("conversation"),
                    "role": row.get("role"),
                    "user_id": row.get("user_id"),
                    "text": row.get("text"),
                    "created_at": row.get("created_at"),
                    "score": dot / (query_norm * norm),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(1, limit)]

    def messages_without_vectors(
        self, model: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self._all(
            "SELECT m.id, m.conversation, m.role, m.user_id, m.text, m.created_at "
            "FROM messages m LEFT JOIN memory_vectors v "
            "ON v.message_id = m.id AND v.model = ? "
            "WHERE v.id IS NULL ORDER BY m.id DESC LIMIT ?",
            (model, max(1, limit)),
        )

    def count_memory_vectors(self, model: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM memory_vectors WHERE model = ?", (model,)
        )
        return int((row or {}).get("n") or 0)

    # ------------------------------------------------------------------ notes
    def add_note(self, user_id: str, text: str) -> int:
        return self._write(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, iso()),
        )

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        return self._one(
            "SELECT id, user_id, text, created_at FROM notes WHERE id = ?",
            (note_id,),
        )

    def user_notes(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, text, created_at FROM notes WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, limit)),
        )

    def delete_note(self, note_id: int, user_id: str | None = None) -> bool:
        if user_id:
            count = self._execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        else:
            count = self._execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return count > 0

    def delete_notes_matching(self, query: str, user_id: str | None = None) -> int:
        like = f"%{_like_escape(query)}%"
        if user_id:
            return self._execute(
                "DELETE FROM notes WHERE user_id = ? AND text LIKE ? ESCAPE '\\'",
                (user_id, like),
            )
        return self._execute("DELETE FROM notes WHERE text LIKE ? ESCAPE '\\'", (like,))

    def search_notes(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{_like_escape(query)}%"
        return self._all(
            "SELECT id, text, created_at FROM notes WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?",
            (like, max(1, limit)),
        )

    def recent_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, text, created_at FROM notes ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        )

    # ------------------------------------------------------------------ tasks
    def add_task(self, user_id: str, title: str) -> int:
        return self._write(
            "INSERT INTO tasks (user_id, title, done, created_at) VALUES (?, ?, 0, ?)",
            (user_id, title.strip(), iso()),
        )

    def list_tasks(self, user_id: str, show_all: bool = False, limit: int = 30) -> list[dict[str, Any]]:
        if show_all:
            return self._all(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY done ASC, id DESC LIMIT ?",
                (user_id, max(1, limit)),
            )
        return self._all(
            "SELECT * FROM tasks WHERE user_id = ? AND done = 0 ORDER BY id ASC LIMIT ?",
            (user_id, max(1, limit)),
        )

    def complete_task(self, task_id: int, user_id: str | None = None) -> bool:
        if user_id:
            count = self._execute(
                "UPDATE tasks SET done = 1, completed_at = ? WHERE id = ? AND user_id = ?",
                (iso(), task_id, user_id),
            )
        else:
            count = self._execute(
                "UPDATE tasks SET done = 1, completed_at = ? WHERE id = ?",
                (iso(), task_id),
            )
        return count > 0

    def uncomplete_task(self, task_id: int, user_id: str | None = None) -> bool:
        if user_id:
            count = self._execute(
                "UPDATE tasks SET done = 0, completed_at = NULL WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
        else:
            count = self._execute(
                "UPDATE tasks SET done = 0, completed_at = NULL WHERE id = ?",
                (task_id,),
            )
        return count > 0

    def delete_task(self, task_id: int, user_id: str | None = None) -> bool:
        if user_id:
            count = self._execute(
                "DELETE FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
        else:
            count = self._execute(
                "DELETE FROM tasks WHERE id = ?",
                (task_id,),
            )
        return count > 0

    def clear_completed_tasks(self, user_id: str) -> int:
        return self._execute(
            "DELETE FROM tasks WHERE user_id = ? AND done = 1",
            (user_id,),
        )

    # ------------------------------------------------------------------ polls
    def create_poll(
        self, poll_id: str, channel_id: str, question: str, options: list[str], creator: str
    ) -> None:
        self._write(
            "INSERT INTO polls (id, channel_id, question, options, creator, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (poll_id, channel_id, question, json.dumps(options), creator, iso()),
        )

    def set_poll_message(self, poll_id: str, ts: str) -> None:
        self._write("UPDATE polls SET message_ts = ? WHERE id = ?", (ts, poll_id))

    def poll(self, poll_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM polls WHERE id = ?", (poll_id,))
        if row:
            row["options"] = json.loads(row["options"])
        return row

    def polls_for_channel(self, channel_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM polls WHERE channel_id = ? ORDER BY created_at DESC LIMIT 20",
            (channel_id,),
        )
        for row in rows:
            row["options"] = json.loads(row["options"])
        return rows

    def close_poll(self, poll_id: str) -> None:
        self._write("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))

    def vote_poll(self, poll_id: str, user_id: str, option_index: int) -> None:
        self._write(
            "INSERT INTO poll_votes (poll_id, user_id, option_index, voted_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(poll_id, user_id) DO UPDATE SET "
            "option_index = excluded.option_index, voted_at = excluded.voted_at",
            (poll_id, user_id, option_index, iso()),
        )

    def unvote_poll(self, poll_id: str, user_id: str) -> None:
        self._write("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))

    def poll_counts(self, poll_id: str) -> tuple[list[int], int]:
        poll = self.poll(poll_id)
        if not poll:
            return [], 0
        counts = [0] * len(poll["options"])
        rows = self._all(
            "SELECT option_index, COUNT(*) AS n FROM poll_votes WHERE poll_id = ? "
            "GROUP BY option_index",
            (poll_id,),
        )
        for row in rows:
            idx = row["option_index"]
            if 0 <= idx < len(counts):
                counts[idx] = row["n"]
        return counts, sum(counts)

    def user_poll_vote(self, poll_id: str, user_id: str) -> int | None:
        row = self._one(
            "SELECT option_index FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        )
        return row["option_index"] if row else None

    # ---------------------------------------------------------------- ai usage
    def record_ai_usage(
        self,
        *,
        day: str,
        kind: str,
        model: str,
        user_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._write(
            "INSERT INTO ai_usage (day, created_at, kind, model, user_id, prompt_tokens, "
            "completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                iso(),
                kind,
                model,
                user_id or "",
                max(0, int(prompt_tokens)),
                max(0, int(completion_tokens)),
                max(0.0, float(cost_usd)),
            ),
        )

    def ai_spend(self, day: str, user_id: str | None = None) -> float:
        if user_id is None:
            row = self._one(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage WHERE day = ?",
                (day,),
            )
        else:
            row = self._one(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage "
                "WHERE day = ? AND user_id = ?",
                (day, user_id),
            )
        return float(row["total"]) if row else 0.0

    def ai_calls(self, day: str, user_id: str | None = None) -> int:
        if user_id is None:
            row = self._one("SELECT COUNT(*) AS n FROM ai_usage WHERE day = ?", (day,))
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM ai_usage WHERE day = ? AND user_id = ?",
                (day, user_id),
            )
        return int(row["n"]) if row else 0

    def ai_usage(self, day: str, user_id: str | None = None) -> list[dict[str, Any]]:
        where = "day = ?" if user_id is None else "day = ? AND user_id = ?"
        params: tuple[Any, ...] = (day,) if user_id is None else (day, user_id)
        return self._all(
            "SELECT kind, model, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            f"FROM ai_usage WHERE {where} GROUP BY kind, model ORDER BY cost_usd DESC",
            params,
        )

    def ai_usage_by_user(self, day: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._all(
            "SELECT user_id, COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM ai_usage WHERE day = ? AND user_id != '' "
            "GROUP BY user_id ORDER BY cost_usd DESC LIMIT ?",
            (day, max(1, limit)),
        )

    # ---------------------------------------------------------------- threads
    def mark_thread(self, channel_id: str, thread_ts: str, owner: str = "bot") -> None:
        """Record an identity that answered here.

        A thread can belong to both identities: the bot app and boi may reply in
        the same thread, and whichever spoke last must not evict the other.
        """
        self._write(
            "INSERT INTO bot_threads (channel_id, thread_ts, created_at, owner) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel_id, thread_ts) DO UPDATE SET owner = "
            "CASE WHEN instr(',' || bot_threads.owner || ',', ',' || excluded.owner || ',') > 0 "
            "THEN bot_threads.owner ELSE bot_threads.owner || ',' || excluded.owner END",
            (channel_id, thread_ts, iso(), owner),
        )

    def thread_owners(self, channel_id: str, thread_ts: str) -> set[str]:
        row = self._one(
            "SELECT owner FROM bot_threads WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        raw = str(row["owner"]) if row and row.get("owner") else ""
        return {part for part in raw.split(",") if part}

    def is_thread(self, channel_id: str, thread_ts: str, owner: str | None = None) -> bool:
        owners = self.thread_owners(channel_id, thread_ts)
        if owner:
            return owner in owners
        return bool(owners)

    def thread_owner(self, channel_id: str, thread_ts: str) -> str | None:
        owners = self.thread_owners(channel_id, thread_ts)
        if not owners:
            return None
        return ",".join(sorted(owners))

    # --------------------------------------------------------------- dm chats
    def add_dm_chat(self, user_id: str, thread_ts: str, title: str = "") -> None:
        now = iso()
        self._write(
            "INSERT INTO dm_chats (user_id, thread_ts, title, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, thread_ts) DO UPDATE SET "
            "last_used_at = excluded.last_used_at",
            (user_id, thread_ts, title, now, now),
        )

    def dm_chat(self, user_id: str, thread_ts: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM dm_chats WHERE user_id = ? AND thread_ts = ?",
            (user_id, thread_ts),
        )

    def dm_chats(self, user_id: str, limit: int = 25) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM dm_chats WHERE user_id = ? AND archived = 0 "
            "ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT ?",
            (user_id, max(1, limit)),
        )

    def rename_dm_chat(self, user_id: str, thread_ts: str, title: str) -> bool:
        return (
            self._execute(
                "UPDATE dm_chats SET title = ? WHERE user_id = ? AND thread_ts = ?",
                (title, user_id, thread_ts),
            )
            > 0
        )

    def archive_dm_chat(self, user_id: str, thread_ts: str) -> bool:
        return (
            self._execute(
                "UPDATE dm_chats SET archived = 1 WHERE user_id = ? AND thread_ts = ?",
                (user_id, thread_ts),
            )
            > 0
        )

    def touch_dm_chat(self, user_id: str, thread_ts: str) -> None:
        self._write(
            "UPDATE dm_chats SET last_used_at = ? WHERE user_id = ? AND thread_ts = ?",
            (iso(), user_id, thread_ts),
        )

    def count_dm_chats(self, user_id: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM dm_chats WHERE user_id = ? AND archived = 0",
            (user_id,),
        )
        return int((row or {}).get("n") or 0)

    def prune_dm_chats(self, user_id: str, keep: int = 50) -> int:
        """Drop the oldest chats past the per-user cap."""
        keep = max(1, keep)
        rows = self._all(
            "SELECT thread_ts FROM dm_chats WHERE user_id = ? AND archived = 0 "
            "ORDER BY COALESCE(last_used_at, created_at) DESC",
            (user_id,),
        )
        doomed = [row["thread_ts"] for row in rows[keep:]]
        for thread_ts in doomed:
            self._execute(
                "DELETE FROM dm_chats WHERE user_id = ? AND thread_ts = ?",
                (user_id, thread_ts),
            )
        return len(doomed)

    def prune_old_dm_chats(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = iso(utcnow() - timedelta(days=days))
        return self._execute(
            "DELETE FROM dm_chats WHERE COALESCE(last_used_at, created_at) < ?",
            (cutoff,),
        )

    def active_dm_chat(self, user_id: str) -> str | None:
        return self.kv_get(f"dm_active:{user_id}")

    def set_active_dm_chat(self, user_id: str, thread_ts: str | None) -> None:
        key = f"dm_active:{user_id}"
        if thread_ts:
            self.kv_set(key, thread_ts)
        else:
            self.kv_del(key)

    # ----------------------------------------------------------- secret chats
    def mark_secret_thread(
        self, user_id: str, thread_ts: str, expires_at: str | None = None
    ) -> None:
        self._write(
            "INSERT INTO dm_secret_threads (user_id, thread_ts, created_at, expires_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, thread_ts) DO UPDATE SET "
            "expires_at = excluded.expires_at",
            (user_id, thread_ts, iso(), expires_at),
        )

    def is_secret_thread(self, user_id: str, thread_ts: str) -> bool:
        # A thread that was ever secret stays secret until it is explicitly
        # unmarked. Expiry only drops the in-memory turns; if an old thread
        # became normal again, a late reply would start being saved.
        row = self._one(
            "SELECT 1 AS ok FROM dm_secret_threads WHERE user_id = ? AND thread_ts = ?",
            (user_id, thread_ts),
        )
        return bool(row)

    def unmark_secret_thread(self, user_id: str, thread_ts: str) -> None:
        self._execute(
            "DELETE FROM dm_secret_threads WHERE user_id = ? AND thread_ts = ?",
            (user_id, thread_ts),
        )

    def secret_threads(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM dm_secret_threads WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, limit)),
        )

    def prune_secret_threads(self, days: int = 0) -> int:
        if days <= 0:
            return 0
        cutoff = iso(utcnow() - timedelta(days=days))
        return self._execute(
            "DELETE FROM dm_secret_threads WHERE created_at < ?",
            (cutoff,),
        )

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, int]:
        since_24h = iso(utcnow() - timedelta(hours=24))
        since_7d = iso(utcnow() - timedelta(days=7))
        one = lambda sql, params=(): int((self._one(sql, params) or {"n": 0})["n"])  # noqa: E731
        return {
            "members": one("SELECT COUNT(*) AS n FROM members"),
            "joins_24h": one("SELECT COUNT(*) AS n FROM member_events WHERE kind = 'join' AND created_at >= ?", (since_24h,)),
            "joins_7d": one("SELECT COUNT(*) AS n FROM member_events WHERE kind = 'join' AND created_at >= ?", (since_7d,)),
            "leaves_24h": one("SELECT COUNT(*) AS n FROM member_events WHERE kind = 'leave' AND created_at >= ?", (since_24h,)),
            "pending_access": one("SELECT COUNT(*) AS n FROM access_requests WHERE status = 'pending'"),
            "approved_access": one("SELECT COUNT(*) AS n FROM access_requests WHERE status = 'approved'"),
            "denied_access": one("SELECT COUNT(*) AS n FROM access_requests WHERE status = 'denied'"),
            "open_polls": one("SELECT COUNT(*) AS n FROM polls WHERE closed = 0"),
            "reminders": one("SELECT COUNT(*) AS n FROM reminders WHERE done = 0"),
            "messages": one("SELECT COUNT(*) AS n FROM messages"),
            "notes": one("SELECT COUNT(*) AS n FROM notes"),
            "tasks": one("SELECT COUNT(*) AS n FROM tasks WHERE done = 0"),
        }
