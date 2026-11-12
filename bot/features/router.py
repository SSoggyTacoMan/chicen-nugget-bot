"""One slash command, many subcommands.

Slack slash commands are workspace-global, so a bot that registers 24 of them
collides with everyone else's. Everything lives under ``/nugget`` instead:
``/nugget ping``, ``/nugget roll 2d6``, ``/nugget poll ...`` and so on.

The feature modules still own the handler functions; this module only decides
which one gets the command payload.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from bot.features.access import cmd_access, cmd_accesspanel, cmd_pending
from bot.features.ai_commands import cmd_ask, cmd_forget, cmd_imagine, cmd_summarize
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
    cmd_ping,
    cmd_remind,
    cmd_reminders,
    cmd_roll,
    cmd_schedule,
    cmd_stats,
    cmd_sysinfo,
    cmd_todo,
    cmd_whois,
)
from bot.features.nuggets import cmd_nugget
from bot.features.polls import cmd_poll, cmd_pollclose
from bot.features.recaps import cmd_recap
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
    "schedule": cmd_schedule,
    "reminders": cmd_reminders,
    "cancel": cmd_cancel,
    "roll": cmd_roll,
    "choose": cmd_choose,
    "poll": cmd_poll,
    "pollclose": cmd_pollclose,
    "stats": cmd_stats,
    "budget": cmd_budget,
    "digest": cmd_digest,
    "recap": cmd_recap,
    "config": cmd_config,
    "settings": cmd_config,
    "todo": cmd_todo,
    "todos": cmd_todo,
    "task": cmd_todo,
    "tasks": cmd_todo,
    "sysinfo": cmd_sysinfo,
    "diagnostics": cmd_sysinfo,
    "sys": cmd_sysinfo,
    "ask": cmd_ask,
    "summarize": cmd_summarize,
    "summary": cmd_summarize,
    "forget": cmd_forget,
    "imagine": cmd_imagine,
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


__all__ = ["register", "resolve", "SUBCOMMANDS"]
