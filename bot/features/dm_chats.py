"""Managed DM chats: one thread per conversation, plus in-memory secret chats.

Slack gives a DM one endless timeline. This module gives each DM conversation
its own thread by replying under the message that started it, keeps a small
index so ``/nugget chats`` can list them, and routes thread replies back to the
right conversation.

Secret chats never touch sqlite for message content. Only a marker row
``(user_id, thread_ts, expires_at)`` is stored, so a restart cannot silently
turn a secret thread into a saved one. Turns live in process memory and die
with the process.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.db import iso, utcnow
from bot.runtime import runtime
from bot.slack_api import error_code, message_text, post, post_ephemeral

logger = logging.getLogger(__name__)

SECRET_ROOT = (
    "🔒 *secret chat*\n"
    "nothing in this thread is saved to my database, and I won't remember it "
    "later. reply here so even your own messages stay out of the main DM list."
)
SECRET_PREVIEW = "🔒 secret chat"
SECRET_REPLY_PREVIEW = "🔒 secret chat reply"
SECRET_SYSTEM = (
    "PRIVATE MODE: this conversation is not stored and will not be remembered. "
    "Do not claim you will remember it later. You may search the web or fetch a "
    "URL if that helps, but do not post, save, schedule, email, store or upload "
    "anything."
)

# Continuation heuristics live here so they are easy to tune and test.
_NEW_CHAT_RE = re.compile(
    r"^\s*(?:new|another|fresh|start\s+(?:a\s+|an\s+)?(?:new|another|fresh))\s+"
    r"(?:chat|thread|topic|conversation)\b[:,]?\s*",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"^\s*(?:continue|same\s+chat|go\s+on|keep\s+going|more)\b[:,]?\s*",
    re.IGNORECASE,
)
_CONTINUATION_CUE = re.compile(
    r"^\s*(?:and|also|but|so|then|what\s+about|what\s+if|why|how|hmm+|wait|"
    r"ok(?:ay)?|yes|yeah|no|nah|thanks|thank\s+you|ty|lol|lmao|haha|nice|cool)\b",
    re.IGNORECASE,
)

_SAME_CHAT_QUESTION = {
    "same_chat": {
        "type": "noul",
        "instructions": (
            "Is the latest message continuing the same conversation as the recent "
            "ones, or starting a new unrelated topic?"
        ),
    }
}


@dataclass
class SecretChat:
    """One in-memory secret conversation. Never serialized."""

    user_id: str
    thread_ts: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    bot_messages: list[str] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)

    def history(self, limit: int) -> list[dict[str, Any]]:
        return [dict(turn) for turn in self.turns[-max(1, limit) :]]

    def add(self, role: str, user_id: str | None, text: str) -> None:
        self.turns.append({"role": role, "user_id": user_id, "text": text})
        if len(self.turns) > 80:
            del self.turns[:-80]
        self.last_used = time.monotonic()

    def note_bot_message(self, ts: str | None) -> None:
        if ts:
            self.bot_messages.append(str(ts))

    def clear(self) -> None:
        self.turns.clear()
        self.bot_messages.clear()
        self.last_used = time.monotonic()


@dataclass
class DMRoute:
    """Where one incoming DM message goes."""

    conversation: str
    thread_ts: str | None
    text: str
    secret: SecretChat | None = None
    created: bool = False
    redirected: bool = False
    chat_ts: str | None = None
    title: str = ""


_secret: dict[tuple[str, str], SecretChat] = {}
_last_message: dict[str, float] = {}
_nudged: set[tuple[str, str]] = set()


# --------------------------------------------------------------------- store
def _ttl_seconds() -> int:
    return max(60, int(getattr(runtime.settings, "secret_chat_ttl_minutes", 120)) * 60)


def secret_chat(
    user_id: str, thread_ts: str, *, create: bool = False
) -> SecretChat | None:
    key = (user_id, thread_ts)
    chat = _secret.get(key)
    if chat is None and create:
        prune_secret()
        _enforce_user_cap(user_id)
        chat = _secret[key] = SecretChat(user_id, thread_ts)
    return chat


def clear_secret(user_id: str, thread_ts: str) -> int:
    chat = _secret.pop((user_id, thread_ts), None)
    return len(chat.turns) if chat else 0


def clear_all_secret(user_id: str) -> int:
    cleared = 0
    for key in [key for key in _secret if key[0] == user_id]:
        cleared += len(_secret.pop(key).turns)
    return cleared


def prune_secret(*, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    cutoff = now - _ttl_seconds()
    for key, chat in list(_secret.items()):
        if chat.last_used < cutoff:
            _secret.pop(key, None)
    if len(_secret) > 200:
        keep = sorted(_secret.items(), key=lambda item: item[1].last_used)[-200:]
        _secret.clear()
        _secret.update(keep)


def _enforce_user_cap(user_id: str) -> None:
    cap = max(1, int(getattr(runtime.settings, "secret_chat_max_per_user", 3)))
    mine = [(key, chat) for key, chat in _secret.items() if key[0] == user_id]
    if len(mine) < cap:
        return
    for key, _chat in sorted(mine, key=lambda item: item[1].last_used)[: len(mine) - cap + 1]:
        _secret.pop(key, None)


def should_nudge(user_id: str, thread_ts: str) -> bool:
    """Only tell someone their reply moved into a thread once per chat."""
    key = (user_id, thread_ts)
    if key in _nudged:
        return False
    _nudged.add(key)
    return True


# ------------------------------------------------------------------- helpers
def _title(text: str, limit: int = 60) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0]
    return (cut or cleaned[:limit]).rstrip(".,;:") + "…"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ago(value: str | None) -> str:
    stamp = _parse_iso(value)
    if stamp is None:
        return "?"
    seconds = max(0, int((utcnow() - stamp).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def split_new_chat(text: str) -> tuple[bool, str]:
    """Return (explicit_new_chat, text_with_prefix_removed)."""
    match = _NEW_CHAT_RE.match(text or "")
    if not match:
        return False, (text or "").strip()
    rest = (text or "")[match.end() :].strip()
    return True, rest


async def _same_chat_with_jev(text: str, recent: list[str]) -> bool:
    gate = runtime.gate
    if gate is None:
        return False
    state = "\n".join(
        ["channel: a direct message", "recent messages:"]
        + [f"- {line}" for line in recent[-4:]]
        + [f"latest message: {text.strip()}"]
    )
    try:
        result = await gate.jev.ask(state=state, questions=_SAME_CHAT_QUESTION)
    except Exception:  # noqa: BLE001 - classification is best-effort
        return False
    if result is None:
        return False
    if gate.budget is not None:
        gate.budget.record(
            kind="jev",
            model=result.model or getattr(gate.settings, "ai_gate_model", "jev"),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    answer = result.answers.get("same_chat")
    return bool(answer and answer.probability >= 0.6)


# ------------------------------------------------------------------- routing
async def _should_continue(
    db,
    user_id: str,
    text: str,
    chat: dict[str, Any],
    *,
    previous_message: float,
) -> bool:
    now = time.monotonic()
    if previous_message and now - previous_message < 2.0:
        # Two quick messages are almost always one thought.
        return True
    window = max(1, int(getattr(runtime.settings, "dm_thread_minutes", 5))) * 60
    last = _parse_iso(chat.get("last_used_at") or chat.get("created_at"))
    if last is None or (utcnow() - last).total_seconds() > window:
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    if _CONTINUE_RE.match(cleaned):
        return True
    words = len(cleaned.split())
    if words <= 3:
        return True
    if words <= 12 and _CONTINUATION_CUE.match(cleaned):
        return True
    thread_ts = str(chat.get("thread_ts") or "")
    if thread_ts:
        recent_rows = db.history(f"dm:{user_id}:{thread_ts}", 2)
        if (
            recent_rows
            and recent_rows[-1].get("role") == "assistant"
            and (recent_rows[-1].get("text") or "").rstrip().endswith("?")
            and words <= 25
        ):
            return True
    if getattr(runtime.settings, "dm_thread_classifier", False):
        recent = [
            f"{'you' if row.get('role') == 'assistant' else 'them'}: {(row.get('text') or '')[:160]}"
            for row in db.history(f"dm:{user_id}:{thread_ts}", 6)
        ]
        return await _same_chat_with_jev(cleaned, recent)
    return False


def _thread_route(db, user_id: str, text: str, thread_ts: str) -> DMRoute:
    prefix = f"dm:{user_id}"
    if db.is_secret_thread(user_id, thread_ts):
        chat = secret_chat(user_id, thread_ts, create=True)
        db.set_active_dm_chat(user_id, thread_ts)
        return DMRoute(
            conversation=f"{prefix}:{thread_ts}",
            thread_ts=thread_ts,
            text=text,
            secret=chat,
            chat_ts=thread_ts,
        )
    db.add_dm_chat(user_id, thread_ts, title=_title(text))
    db.touch_dm_chat(user_id, thread_ts)
    db.set_active_dm_chat(user_id, thread_ts)
    return DMRoute(
        conversation=f"{prefix}:{thread_ts}",
        thread_ts=thread_ts,
        text=text,
        chat_ts=thread_ts,
    )


def _new_normal_route(db, user_id: str, text: str, ts: str) -> DMRoute:
    title = _title(text)
    db.add_dm_chat(user_id, ts, title=title)
    db.set_active_dm_chat(user_id, ts)
    db.prune_dm_chats(user_id, int(getattr(runtime.settings, "dm_chat_max", 50)))
    return DMRoute(
        conversation=f"dm:{user_id}:{ts}",
        thread_ts=ts,
        text=text,
        created=True,
        chat_ts=ts,
        title=title,
    )


async def route_dm(event: dict[str, Any]) -> DMRoute | None:
    """Route one DM message to its chat, thread and history key."""
    db = runtime.db
    if db is None:
        return None
    user_id = event.get("user") or ""
    channel = event.get("channel") or ""
    ts = event.get("ts") or ""
    text = message_text(event).strip()
    thread_ts = event.get("thread_ts") or None
    if not user_id or not channel or not ts:
        return None

    now = time.monotonic()
    previous = _last_message.get(user_id, 0.0)
    _last_message[user_id] = now
    if len(_last_message) > 500:
        cutoff = now - 3600
        for key, stamp in list(_last_message.items()):
            if stamp < cutoff:
                _last_message.pop(key, None)

    # Turning DM threads off must never turn an old secret thread into a saved
    # one. Normal chats fall back to the legacy single timeline; secret ones
    # keep their own in-memory conversation.
    if not getattr(runtime.settings, "dm_chats", True):
        if thread_ts and thread_ts != ts:
            # Legacy handles normal threads; only known secret threads stay here.
            if not db.is_secret_thread(user_id, thread_ts):
                return None
            candidate = thread_ts
        else:
            active = db.active_dm_chat(user_id)
            candidate = (
                active
                if active and db.is_secret_thread(user_id, active)
                else None
            )
        if not candidate:
            return None
        chat = secret_chat(user_id, candidate, create=True)
        return DMRoute(
            conversation=f"dm:{user_id}:{candidate}",
            thread_ts=candidate,
            text=text,
            secret=chat,
            redirected=not (thread_ts and thread_ts != ts),
            chat_ts=candidate,
        )

    # A reply inside a thread always belongs to that thread.
    if thread_ts and thread_ts != ts:
        return _thread_route(db, user_id, text, thread_ts)

    # An explicit "new chat" wins over the continuation window.
    explicit, cleaned = split_new_chat(text)
    if explicit:
        return _new_normal_route(db, user_id, cleaned or text, ts)

    active = db.active_dm_chat(user_id)
    if active:
        if db.is_secret_thread(user_id, active):
            # Once secret, stay secret until /nugget end. Never silently resume
            # saving just because the continuation window passed.
            chat = secret_chat(user_id, active, create=True)
            return DMRoute(
                conversation=f"dm:{user_id}:{active}",
                thread_ts=active,
                text=text,
                secret=chat,
                redirected=active != ts,
                chat_ts=active,
            )
        chat = db.dm_chat(user_id, active)
        if chat and await _should_continue(
            db, user_id, text, chat, previous_message=previous
        ):
            db.touch_dm_chat(user_id, active)
            return DMRoute(
                conversation=f"dm:{user_id}:{active}",
                thread_ts=active,
                text=text,
                redirected=True,
                chat_ts=active,
                title=str(chat.get("title") or ""),
            )

    return _new_normal_route(db, user_id, text, ts)


def chat_for_thread(
    user_id: str, thread_ts: str | None
) -> tuple[str, SecretChat | None] | None:
    """Resolve a known DM thread for reactions and shortcuts."""
    db = runtime.db
    if db is None or not thread_ts:
        return None
    if db.is_secret_thread(user_id, thread_ts):
        return (
            f"dm:{user_id}:{thread_ts}",
            secret_chat(user_id, thread_ts, create=True),
        )
    if db.dm_chat(user_id, thread_ts):
        return f"dm:{user_id}:{thread_ts}", None
    return None


def secret_active(user_id: str) -> SecretChat | None:
    db = runtime.db
    if db is None:
        return None
    thread_ts = db.active_dm_chat(user_id)
    if thread_ts and db.is_secret_thread(user_id, thread_ts):
        return secret_chat(user_id, thread_ts, create=True)
    return None


# ------------------------------------------------------------------ commands
async def _dm_only(client, command, what: str = "this") -> bool:
    channel = command.get("channel_id") or ""
    user = command.get("user_id") or ""
    if channel.startswith("D"):
        return True
    await post_ephemeral(client, channel, user, f"{what} works in my DMs, not channels.")
    return False


async def _list_chats(client, channel: str, user: str) -> None:
    db = runtime.db
    rows = db.dm_chats(user, 15) if db else []
    active = db.active_dm_chat(user) if db else None
    lines = ["*your dm chats*"]
    if secret_active(user) is not None:
        lines.append("• 🔒 secret chat - active, not saved")
    for index, row in enumerate(rows, 1):
        title = row.get("title") or "(untitled)"
        mark = " ← active" if row.get("thread_ts") == active else ""
        lines.append(f"• {index}. {title}{mark} - {_ago(row.get('last_used_at') or row.get('created_at'))}")
    if not rows:
        lines.append("no saved chats yet. send me a DM and I'll start one.")
    lines.append("")
    lines.append("`/nugget chat new [title]` · `/nugget chat use <n>` · `/nugget chat rename <title>` · `/nugget chat close`")
    lines.append("`/nugget secret` starts an unsaved chat.")
    await post_ephemeral(client, channel, user, "\n".join(lines))


async def cmd_chats(ack, command, client) -> None:
    await ack()
    if not await _dm_only(client, command, "chat history"):
        return
    await _list_chats(client, command["channel_id"], command["user_id"])


async def cmd_chat(ack, command, client) -> None:
    await ack()
    if not await _dm_only(client, command):
        return
    channel = command["channel_id"]
    user = command["user_id"]
    sub, _, rest = (command.get("text") or "").strip().partition(" ")
    sub = sub.lower()
    rest = rest.strip()
    if sub in {"", "list"}:
        await _list_chats(client, channel, user)
        return
    if sub in {"new", "start"}:
        await start_new_chat(client, channel, user, rest)
        return
    if sub in {"use", "switch", "resume"}:
        await _use_chat(client, channel, user, rest)
        return
    if sub in {"rename", "title"}:
        await _rename_chat(client, channel, user, rest)
        return
    if sub in {"close", "archive"}:
        await _close_chat(client, channel, user)
        return
    if sub in {"secret", "incognito"}:
        await start_secret_chat(client, channel, user, rest)
        return
    await post_ephemeral(
        client,
        channel,
        user,
        "usage: `/nugget chat [new|use <n>|rename <title>|close|list]`",
    )


async def cmd_newchat(ack, command, client) -> None:
    await ack()
    if not await _dm_only(client, command):
        return
    await start_new_chat(
        client,
        command["channel_id"],
        command["user_id"],
        (command.get("text") or "").strip(),
    )


async def start_new_chat(client, channel: str, user: str, title: str = "") -> str | None:
    db = runtime.db
    if db is None:
        return None
    title = _title(title) or "new chat"
    response = await post(
        client,
        channel,
        f"💬 *{title}*\nreply in this thread. `/nugget chat new` starts another one.",
    )
    root = str((response or {}).get("ts") or "")
    if not root:
        await post_ephemeral(client, channel, user, "couldn't start a new thread. try again?")
        return None
    db.add_dm_chat(user, root, title=title)
    db.set_active_dm_chat(user, root)
    db.prune_dm_chats(user, int(getattr(runtime.settings, "dm_chat_max", 50)))
    await post_ephemeral(client, channel, user, f"started *{title}*. reply in that thread.")
    return root


async def _use_chat(client, channel: str, user: str, text: str) -> None:
    db = runtime.db
    rows = db.dm_chats(user, 50) if db else []
    match = re.search(r"\d+", text or "")
    if not match or not rows:
        await post_ephemeral(client, channel, user, "usage: `/nugget chat use <number>` (see `/nugget chats`)")
        return
    index = int(match.group(0))
    if not 1 <= index <= len(rows):
        await post_ephemeral(client, channel, user, f"pick 1-{len(rows)}.")
        return
    row = rows[index - 1]
    db.set_active_dm_chat(user, row["thread_ts"])
    db.touch_dm_chat(user, row["thread_ts"])
    await post_ephemeral(
        client,
        channel,
        user,
        f"now using *{row.get('title') or 'that chat'}*. reply in its thread, or just DM me.",
    )


async def _rename_chat(client, channel: str, user: str, title: str) -> None:
    db = runtime.db
    thread_ts = db.active_dm_chat(user) if db else None
    if not thread_ts or not db.dm_chat(user, thread_ts):
        await post_ephemeral(client, channel, user, "no active saved chat. `/nugget chat use <n>` first.")
        return
    if not title:
        await post_ephemeral(client, channel, user, "usage: `/nugget chat rename <title>`")
        return
    db.rename_dm_chat(user, thread_ts, _title(title))
    await post_ephemeral(client, channel, user, f"renamed it to *{_title(title)}*.")


async def _close_chat(client, channel: str, user: str) -> None:
    db = runtime.db
    thread_ts = db.active_dm_chat(user) if db else None
    if not thread_ts:
        await post_ephemeral(client, channel, user, "no active chat to close.")
        return
    if db.is_secret_thread(user, thread_ts):
        await _end_secret(client, channel, user, thread_ts, burn=False)
        return
    db.archive_dm_chat(user, thread_ts)
    db.set_active_dm_chat(user, None)
    await post_ephemeral(client, channel, user, "closed it. the slack thread stays, it just won't be active.")


async def cmd_secret(ack, command, client) -> None:
    await ack()
    if not await _dm_only(client, command, "secret chat"):
        return
    if not getattr(runtime.settings, "secret_chats", True):
        await post_ephemeral(client, command["channel_id"], command["user_id"], "secret chats are turned off.")
        return
    await start_secret_chat(
        client,
        command["channel_id"],
        command["user_id"],
        (command.get("text") or "").strip(),
    )


async def start_secret_chat(client, channel: str, user: str, title: str = "") -> None:
    db = runtime.db
    if db is None:
        return
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": SECRET_ROOT}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "end", "emoji": True},
                    "action_id": "dm_secret_end",
                    "value": "end",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "burn", "emoji": True},
                    "action_id": "dm_secret_burn",
                    "value": "burn",
                    "style": "danger",
                },
            ],
        },
    ]
    response = await post(
        client,
        channel,
        SECRET_PREVIEW,
        blocks=blocks,
        notification_text=SECRET_PREVIEW,
    )
    root = str((response or {}).get("ts") or "")
    if not root:
        await post_ephemeral(client, channel, user, "couldn't start a secret chat. try again?")
        return
    ttl = max(5, int(getattr(runtime.settings, "secret_chat_ttl_minutes", 120)))
    db.mark_secret_thread(
        user, root, expires_at=iso(utcnow() + timedelta(minutes=ttl))
    )
    db.set_active_dm_chat(user, root)
    secret_chat(user, root, create=True)
    label = f" *{_title(title)}*" if title else ""
    await post_ephemeral(
        client,
        channel,
        user,
        f"🔒 secret chat{label} started. nothing is saved, and it self-destructs after "
        f"{ttl} minutes idle or `/nugget end`. reply in the thread so your messages "
        "stay out of the DM list.",
    )


async def cmd_end(ack, command, client) -> None:
    await ack()
    if not await _dm_only(client, command, "secret chat"):
        return
    channel = command["channel_id"]
    user = command["user_id"]
    text = (command.get("text") or "").lower()
    burn = "--burn" in text or "burn" in text.split()
    db = runtime.db
    thread_ts = db.active_dm_chat(user) if db else None
    if not thread_ts or not db.is_secret_thread(user, thread_ts):
        rows = db.secret_threads(user) if db else []
        thread_ts = str(rows[0]["thread_ts"]) if rows else None
    if not thread_ts:
        await post_ephemeral(client, channel, user, "no secret chat is active.")
        return
    await _end_secret(client, channel, user, thread_ts, burn=burn)


async def _end_secret(
    client, channel: str, user: str, thread_ts: str, *, burn: bool
) -> None:
    db = runtime.db
    if db is None or not channel:
        return
    deleted = 0
    if burn:
        deleted = await burn_thread(client, channel, thread_ts)
    cleared = clear_secret(user, thread_ts)
    db.unmark_secret_thread(user, thread_ts)
    if db.active_dm_chat(user) == thread_ts:
        db.set_active_dm_chat(user, None)
    parts = [f"secret chat ended. wiped {cleared} in-memory turns."]
    if burn:
        parts.append(
            f"deleted {deleted} of my messages. slack still keeps your own messages."
            if deleted
            else "nothing of mine was left to delete."
        )
    await post_ephemeral(client, channel, user, " ".join(parts))


async def burn_thread(client, channel: str, thread_ts: str) -> int:
    """Delete the bot's own messages in a thread. Best effort."""
    timestamps: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        try:
            response = await client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200, cursor=cursor
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("could not read thread for burn: %s", error_code(exc))
            break
        data = response.data if hasattr(response, "data") else response
        payload = data if isinstance(data, dict) else {}
        for message in payload.get("messages") or []:
            if message.get("bot_id") == runtime.bot_id or message.get("user") == runtime.bot_user_id:
                ts = message.get("ts")
                if ts:
                    timestamps.append(str(ts))
        cursor = (payload.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    deleted = 0
    for ts in dict.fromkeys(timestamps):
        try:
            await client.chat_delete(channel=channel, ts=ts)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.info("could not delete secret message %s: %s", ts, error_code(exc))
    return deleted


# ------------------------------------------------------------------- buttons
def register(app) -> None:
    app.action("dm_secret_end")(on_secret_end)
    app.action("dm_secret_burn")(on_secret_burn)


def _action_context(body: dict[str, Any]) -> tuple[str, str, str]:
    user = ((body.get("user") or {}).get("id")) or ""
    channel = ((body.get("channel") or {}).get("id")) or ((body.get("container") or {}).get("channel_id")) or ""
    thread_ts = (body.get("container") or {}).get("message_ts") or ""
    return user, channel, str(thread_ts)


async def on_secret_end(ack, body, client) -> None:
    await ack()
    user, channel, thread_ts = _action_context(body)
    if not user or not channel:
        return
    await _end_secret(client, channel, user, thread_ts, burn=False)


async def on_secret_burn(ack, body, client) -> None:
    await ack()
    user, channel, thread_ts = _action_context(body)
    if not user or not channel:
        return
    await _end_secret(client, channel, user, thread_ts, burn=True)


__all__ = [
    "DMRoute",
    "SECRET_PREVIEW",
    "SECRET_REPLY_PREVIEW",
    "SECRET_SYSTEM",
    "SecretChat",
    "burn_thread",
    "chat_for_thread",
    "clear_all_secret",
    "clear_secret",
    "cmd_chat",
    "cmd_chats",
    "cmd_end",
    "cmd_newchat",
    "cmd_secret",
    "prune_secret",
    "register",
    "route_dm",
    "secret_active",
    "secret_chat",
    "should_nudge",
    "split_new_chat",
    "start_new_chat",
    "start_secret_chat",
]
