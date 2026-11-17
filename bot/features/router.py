"""One slash command, many subcommands.

Slack slash commands are workspace-global, so a bot that registers 24 of them
collides with everyone else's. Everything lives under ``/nugget`` instead:
``/nugget ping``, ``/nugget roll 2d6``, ``/nugget poll ...`` and so on.

The feature modules still own the handler functions; this module only decides
which one gets the command payload.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable

from bot.features.access import cmd_access, cmd_accesspanel, cmd_pending
from bot.features.ai_commands import (
    cmd_ask,
    cmd_forget,
    cmd_imagine,
    cmd_speak,
    cmd_summarize,
)
from bot.features.commands import (
    cmd_accounts,
    cmd_budget,
    cmd_cancel,
    cmd_choose,
    cmd_config,
    cmd_digest,
    cmd_help,
    cmd_id,
    cmd_imageaccess,
    cmd_memory,
    cmd_ping,
    cmd_remind,
    cmd_reminders,
    cmd_roll,
    cmd_schedule,
    cmd_stats,
    cmd_storage,
    cmd_sysinfo,
    cmd_todo,
    cmd_whois,
)
from bot.features.dm_chats import (
    cmd_chat,
    cmd_chats,
    cmd_end,
    cmd_newchat,
    cmd_secret,
)
from bot.features.doctor import cmd_doctor
from bot.features.huddles import cmd_huddle
from bot.features.nuggets import cmd_nugget
from bot.features.polls import cmd_poll, cmd_pollclose
from bot.features.recaps import cmd_recap
from bot.features.setup import cmd_setup
from bot.features.welcomer import cmd_pinggroup
from bot.slack_api import post_ephemeral

Handler = Callable[..., Awaitable[None]]

# subcommand -> handler. Keep aliases where a name is easy to mistype.
SUBCOMMANDS: dict[str, Handler] = {
    "help": cmd_help,
    "commands": cmd_help,
    "?": cmd_help,
    "accounts": cmd_accounts,
    "identities": cmd_accounts,
    "whoami": cmd_accounts,
    "ping": cmd_ping,
    "id": cmd_id,
    "whois": cmd_whois,
    "access": cmd_access,
    "accesspanel": cmd_accesspanel,
    "panel": cmd_accesspanel,
    "pending": cmd_pending,
    "reminder": cmd_remind,
    "remind": cmd_remind,
    "timer": cmd_remind,
    "alarm": cmd_remind,
    "schedule": cmd_schedule,
    "reminders": cmd_reminders,
    "cancel": cmd_cancel,
    "roll": cmd_roll,
    "choose": cmd_choose,
    "poll": cmd_poll,
    "pollclose": cmd_pollclose,
    "stats": cmd_stats,
    "storage": cmd_storage,
    "disk": cmd_storage,
    "budget": cmd_budget,
    "digest": cmd_digest,
    "recap": cmd_recap,
    "config": cmd_config,
    "settings": cmd_config,
    "setup": cmd_setup,
    "connect": cmd_setup,
    "integrations": cmd_setup,
    "todo": cmd_todo,
    "todos": cmd_todo,
    "task": cmd_todo,
    "tasks": cmd_todo,
    "sysinfo": cmd_sysinfo,
    "diagnostics": cmd_sysinfo,
    "sys": cmd_sysinfo,
    "doctor": cmd_doctor,
    "health": cmd_doctor,
    "ask": cmd_ask,
    "summarize": cmd_summarize,
    "summary": cmd_summarize,
    "forget": cmd_forget,
    "chats": cmd_chats,
    "chat": cmd_chat,
    "newchat": cmd_newchat,
    "secret": cmd_secret,
    "incognito": cmd_secret,
    "end": cmd_end,
    "memory": cmd_memory,
    "imagine": cmd_imagine,
    "huddle": cmd_huddle,
    "huddles": cmd_huddle,
    "speak": cmd_speak,
    "say": cmd_speak,
    "tts": cmd_speak,
    "imageaccess": cmd_imageaccess,
    "images": cmd_imageaccess,
    "pinggroup": cmd_pinggroup,
    "pings": cmd_pinggroup,
    "fact": cmd_nugget,
    "facts": cmd_nugget,
    "nugget": cmd_nugget,
}


def register(app) -> None:
    app.command("/nugget")(cmd_router)


def resolve(text: str | None) -> tuple[Handler | None, str]:
    """Return (handler, remaining text). Bare /nugget becomes a nugget fact."""
    raw = (text or "").strip()
    if not raw:
        return cmd_nugget, ""
    head, _, rest = raw.partition(" ")
    handler = SUBCOMMANDS.get(head.lower())
    if handler is None:
        return None, raw
    return handler, rest.strip()


# People type commands many ways in chat. These strips turn "nugget, start a
# secret chat about rust" into "secret about rust" without touching ordinary
# sentences that merely mention the bot.
_COMMAND_PREFIX_RE = re.compile(r"^(?:/nugget|nugget)\b\s*[:,]?\s*", re.IGNORECASE)
_FILLER_RE = re.compile(
    r"^(?:(?:please|pls|plz|hey|yo|ok|okay|the|a|an|my|i want to|i wanna|"
    r"i'?d like to|let'?s|let me|go ahead and|just)\s+)+",
    re.IGNORECASE,
)
_VERB_RE = re.compile(
    r"^(?:start|begin|open|create|make|do|run|use|set\s*up|setup)\b[:,]?\s*",
    re.IGNORECASE,
)
_SECRET_CHAT_RE = re.compile(
    r"^(?:a\s+|an\s+|my\s+|the\s+)?secret\s+(?:chat|thread|conversation)\b[:,]?\s*",
    re.IGNORECASE,
)
_NEW_CHAT_TITLE_RE = re.compile(
    r"^(?:a\s+|an\s+|the\s+)?(?:new|another|fresh)\s+(?:chat|thread|conversation)\b[:,]?\s*",
    re.IGNORECASE,
)
_REMINDER_HEADS = frozenset({"remind", "reminder", "timer", "alarm", "schedule"})


def _fuzzy_head(token: str) -> str | None:
    """Correct a mistyped subcommand, but only for an explicit "nugget ..."."""
    token = token.lower()
    if len(token) < 4 or token in SUBCOMMANDS:
        return None
    matches = difflib.get_close_matches(token, SUBCOMMANDS.keys(), n=1, cutoff=0.8)
    return matches[0] if matches else None


def _tidy_reminder_text(rest: str) -> str:
    head, _, tail = rest.partition(" ")
    if head.lower() not in _REMINDER_HEADS or not tail:
        return rest
    tail = re.sub(r"^(?:(?:me|us|to|for)\s+)+", "", tail, flags=re.IGNORECASE)
    return f"{head} {tail}".strip()


def normalize_message_command(text: str | None, *, allow_bare: bool = False) -> str | None:
    """Turn a chat message into slash-command text, or None if it isn't one.

    ``allow_bare`` accepts messages that start straight with a subcommand,
    which is what a mention like "@nugget setup welcome" looks like after the
    mention is stripped.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    prefixed = _COMMAND_PREFIX_RE.match(raw)
    if prefixed:
        rest = raw[prefixed.end() :].strip()
    elif allow_bare:
        rest = raw
    else:
        return None
    if not rest:
        return ""

    setup_verb = False
    for _ in range(4):
        before = rest
        rest = _FILLER_RE.sub("", rest).strip()
        verb = _VERB_RE.match(rest)
        if verb and rest[verb.end() :].strip():
            setup_verb = "setup" in verb.group(0).lower().replace(" ", "")
            rest = rest[verb.end() :].strip()
        if rest == before:
            break

    if prefixed:
        head, _, tail = rest.partition(" ")
        corrected = _fuzzy_head(head)
        if corrected:
            rest = f"{corrected} {tail}".strip()

    secret_match = _SECRET_CHAT_RE.match(rest)
    if secret_match:
        rest = f"secret {rest[secret_match.end() :].strip()}".strip()
    else:
        new_chat = _NEW_CHAT_TITLE_RE.match(rest)
        if new_chat:
            rest = f"newchat {rest[new_chat.end() :].strip()}".strip()

    if setup_verb and not rest.lower().startswith(("setup", "connect", "integrations")):
        rest = f"setup {rest}".strip()

    bare_aliases = {"set up": "setup", "set-up": "setup", "set up welcome": "setup welcome"}
    rest = bare_aliases.get(rest.lower(), rest)

    rest = _tidy_reminder_text(rest)

    handler, remaining = resolve(rest)
    if handler is None:
        return None
    if handler is cmd_help and re.match(
        r"^(?:me|us|with|how|why|can|to|on|for|about)\b", remaining, re.IGNORECASE
    ):
        # "nugget help me debug this" is a request, not the help command.
        return None
    head = rest.partition(" ")[0].lower()
    return f"{head} {remaining}".strip()


class _AutoAck:
    """ack() stand-in for handlers reused outside the slash-command plumbing."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, *args, **kwargs) -> None:
        self.called = True


async def run_command_text(
    text: str | None,
    *,
    client,
    user_id: str,
    channel_id: str,
    thread_ts: str | None = None,
    allow_bare: bool = True,
) -> tuple[bool, str]:
    """Run a nugget command from ordinary chat text.

    Returns ``(ran, canonical_text)``. Handlers post their own messages; this
    helper only builds a payload that looks like a slash command.
    """
    canonical = normalize_message_command(text, allow_bare=allow_bare)
    if not canonical:
        return False, ""
    handler, rest = resolve(canonical)
    if handler is None:
        return False, ""
    ack = _AutoAck()
    command = {
        "text": rest,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts or "",
        "trigger_id": "",
        "response_url": "",
        "channel_name": "directmessage",
        "team_id": "",
    }
    await handler(ack, command, client)
    return True, canonical


async def cmd_router(ack, command, client) -> None:
    handler, rest = resolve(command.get("text"))
    if handler is None:
        await ack()
        await post_ephemeral(
            client,
            command["channel_id"],
            command["user_id"],
            f"unknown subcommand `{rest.split()[0]}`. try `/nugget help`.",
        )
        return

    routed = dict(command)
    routed["text"] = rest
    # the feature handlers each call ack() themselves
    await handler(ack, routed, client)


__all__ = [
    "SUBCOMMANDS",
    "normalize_message_command",
    "register",
    "resolve",
    "run_command_text",
]
