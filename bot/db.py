"""Tiny SQLite persistence layer.

Everything is stored locally in `data/bot.db`. Each operation opens its own
short-lived connection, which keeps the async handlers simple and avoids
sharing a connection across threads/event-loop turns.
"""

from __future__ import annotations

import json
import sqlite3
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
    PRIMARY KEY (channel_id, thread_ts)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_day ON ai_usage (day, kind);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ core
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(access_requests)").fetchall()
                }
                if "admin_note" not in columns:
                    conn.execute("ALTER TABLE access_requests ADD COLUMN admin_note TEXT")
                conn.commit()
            finally:
                conn.close()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, tuple(params))
                conn.commit()
                return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write and return the number of affected rows."""
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, tuple(params))
                conn.commit()
                return max(0, int(cur.rowcount))
            finally:
                conn.close()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        finally:
            conn.close()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------- kv
    def kv_get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

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
        conn = self._connect()
        try:
            with self._write_lock:
                cur = conn.execute(
                    "UPDATE reminders SET done = 1 WHERE id = ? AND done = 0",
                    (reminder_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            conn.close()

    def find_recurring(self, kind: str, user_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM reminders WHERE done = 0 AND kind = ? AND user_id = ? LIMIT 1",
            (kind, user_id),
        )

    # --------------------------------------------------------------- messages
    def add_message(self, conversation: str, role: str, user_id: str | None, text: str) -> None:
        self._write(
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
        like = f"%{query}%"
        return self._all(
            "SELECT conversation, role, user_id, text, created_at FROM messages "
            "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (like, max(1, limit)),
        )

    def clear_history(self, conversation: str) -> int:
        return self._execute("DELETE FROM messages WHERE conversation = ?", (conversation,))

    def clear_history_prefix(self, prefix: str) -> int:
        return self._execute(
            "DELETE FROM messages WHERE conversation LIKE ?",
            (prefix + "%",),
        )

    # ------------------------------------------------------------------ notes
    def add_note(self, user_id: str, text: str) -> int:
        return self._write(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, iso()),
        )

    def search_notes(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query}%"
        return self._all(
            "SELECT id, text, created_at FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (like, max(1, limit)),
        )

    def recent_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, text, created_at FROM notes ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
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
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._write(
            "INSERT INTO ai_usage (day, created_at, kind, model, prompt_tokens, "
            "completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                iso(),
                kind,
                model,
                max(0, int(prompt_tokens)),
                max(0, int(completion_tokens)),
                max(0.0, float(cost_usd)),
            ),
        )

    def ai_spend(self, day: str) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage WHERE day = ?",
            (day,),
        )
        return float(row["total"]) if row else 0.0

    def ai_calls(self, day: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM ai_usage WHERE day = ?", (day,))
        return int(row["n"]) if row else 0

    def ai_usage(self, day: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT kind, model, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM ai_usage WHERE day = ? GROUP BY kind, model ORDER BY cost_usd DESC",
            (day,),
        )

    # ---------------------------------------------------------------- threads
    def mark_thread(self, channel_id: str, thread_ts: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO bot_threads (channel_id, thread_ts, created_at) VALUES (?, ?, ?)",
            (channel_id, thread_ts, iso()),
        )

    def is_thread(self, channel_id: str, thread_ts: str) -> bool:
        row = self._one(
            "SELECT 1 AS ok FROM bot_threads WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        return bool(row)

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
        }
