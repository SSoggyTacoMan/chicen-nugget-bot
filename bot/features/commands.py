"""Slash commands: help, ping, reminders, dice, choose, stats, digest, whois."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from bot.runtime import runtime
from bot.features.stats import build_digest, format_stats
from bot.reminders import confirmation_text, create_reminder, dm_target, reminder_line
from bot.slack_api import (
    all_channels,
    channel_info,
    post,
    post_ephemeral,
    user_info,
)
from bot.utils import dice
from bot.utils.text import display_name, extract_id
from bot.utils.timeparse import parse_when
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)


HELP = """*{bot} — everything lives under `/nugget`*

*basics*
• `/nugget` — a random nugget fact
• `/nugget help` — this message
• `/nugget ping` — check I'm alive
• `/nugget id` — show channel/user ids
• `/nugget whois @someone`

*welcoming / access*
• `/nugget access` — request tier 2 access
• `/nugget pending` — admin: list pending requests
• `/nugget accesspanel` — admin: post/update the access panel

*reminders*
• `/nugget reminder 10m stretch` — DM reminder
• `/nugget reminder tomorrow 09:00 standup`
• `/nugget reminder #general daily 09:00 good morning` — admin only
• `/nugget reminder weekly mon 18:00 weekly recap`
• `/nugget reminders` — list yours (`/nugget reminders all` for admins)
• `/nugget cancel <id>`
• `/nugget schedule <#channel> <when> <text>` — admin scheduled message
• `/nugget recap` — admin: post the daily recap prompt now

*ai*
• `/nugget ask <question>` — one-shot ai answer
• `/nugget summarize [n]` — ai catch-up for this channel
• `/nugget imagine <prompt> [--wide|--tall|--ratio 4:3]` — admin image generation
• `/nugget budget` — admin: today's ai spend and limits
• `/nugget forget` — wipe stored ai memory (DM or channel)

*fun / info*
• `/nugget pinggroup [join|leave]` — manage the ping group
• `/nugget poll "question" "option a" "option b"` — 2-5 options
• `/nugget pollclose <id>`
• `/nugget roll 2d6+3`
• `/nugget choose pizza | tacos | sushi`
• `/nugget stats` — admin workspace stats
• `/nugget digest` — admin: daily digest now

or just DM me / mention me and talk to my AI brain."""


# --------------------------------------------------------------------- basic
async def cmd_help(ack, command, client) -> None:
    await ack()
    await _say(
        client,
        command,
        HELP.format(bot=runtime.settings.bot_name),
        ephemeral=True,
    )


async def cmd_ping(ack, command, client) -> None:
    await ack()
    started = time.perf_counter()
    try:
        await client.auth_test()
    except Exception:  # noqa: BLE001 - still report something
        pass
    elapsed = (time.perf_counter() - started) * 1000
    settings = runtime.settings
    ai = f"on · `{settings.ai_model}`" if settings.ai_enabled else "off"
    await post(
        client,
        command["channel_id"],
        f":ping_pong: pong — {settings.bot_name} is awake "
        f"({elapsed:.0f} ms api round trip, up {_uptime()}). ai: {ai}",
    )


async def cmd_id(ack, command, client) -> None:
    await ack()
    settings = runtime.settings
    channel = command.get("channel_id")
    info = await channel_info(client, channel)
    name = f"#{info.get('name')}" if info else channel
    lines = [
        "*ids*",
        f"• channel: `{channel}` ({name})",
        f"• user: `{command.get('user_id')}`",
        f"• team: `{command.get('team_id')}`",
        f"• ai allowed here: `{str(settings.ai_allowed(channel)).lower()}`",
        f"• ai model: `{settings.ai_model}`" if settings.ai_enabled else "• ai: `off`",
        f"• welcomer watches here: `{str(settings.watches(channel)).lower()}`",
        f"• thread helper: `{str(settings.thread_helper).lower()}` · "
        f"ping guard: `{str(settings.usergroup_guard).lower()}`",
        f"• recaps: `{str(settings.recaps_enabled).lower()}`",
        f"• timezone: `{settings.timezone}`",
    ]
    if settings.access_enabled:
        lines.append(f"• tier 2 channel: `{settings.access_channel_id}`")
    await _say(client, command, "\n".join(lines), ephemeral=True)


async def cmd_whois(ack, command, client) -> None:
    await ack()
    target = (command.get("text") or "").strip()
    user_id = extract_id(target, "@")
    if not user_id:
        user_id = command.get("user_id")
    user = await user_info(client, user_id)
    if not user:
        await _say(client, command, "couldn't find that user.", ephemeral=True)
        return
    profile = user.get("profile") or {}
    lines = [
        f"*{display_name(user)}* (<@{user.get('id')}>)",
        f"• id: `{user.get('id')}`",
        f"• username: `{user.get('name')}`",
        f"• title: {profile.get('title') or '—'}",
        f"• timezone: `{user.get('tz') or '—'}` ({user.get('tz_label') or '—'})",
        f"• bot: `{str(user.get('is_bot')).lower()}` · admin: `{str(user.get('is_admin')).lower()}`",
    ]
    await _say(client, command, "\n".join(lines), ephemeral=True)


# ----------------------------------------------------------------- reminders
async def cmd_remind(ack, command, client) -> None:
    await ack()
    await _create_reminder(client, command, require_channel=False)


async def cmd_schedule(ack, command, client) -> None:
    await ack()
    if not runtime.settings.is_admin(command.get("user_id")):
        await _say(client, command, "only admins can schedule channel messages.", ephemeral=True)
        return
    await _create_reminder(client, command, require_channel=True)


async def _create_reminder(client, command: dict[str, Any], *, require_channel: bool) -> None:
    settings = runtime.settings
    raw = (command.get("text") or "").strip()
    user_id = command.get("user_id")
    if not raw:
        await _usage_reminder(client, command)
        return

    target, rest = await _extract_target(client, raw)
    if target is None:
        await _usage_reminder(client, command)
        return
    if require_channel and target == "dm":
        await _say(
            client,
            command,
            "usage: `/schedule <#channel> <when> <text>` — I need a channel to post in.",
            ephemeral=True,
        )
        return
    if target == "dm":
        target_value = dm_target(user_id)
    else:
        if not settings.is_admin(user_id):
            await _say(client, command, "only admins can schedule channel reminders.", ephemeral=True)
            return
        target_value = target

    parsed = parse_when(rest, tz=tzinfo())
    if not parsed:
        await _say(
            client,
            command,
            "couldn't read the time. try `10m`, `tomorrow 09:00`, `daily 08:30`, "
            "`weekly mon 18:00`, or `in 2h`.",
            ephemeral=True,
        )
        return

    reminder = create_reminder(
        runtime.db,
        settings,
        user_id=user_id,
        target=target_value,
        parsed=parsed,
        text=parsed.rest,
        tz=tzinfo(),
    )
    if not reminder:
        await _say(
            client,
            command,
            f"i hit the reminder cap ({settings.max_reminders} active). cancel some first.",
            ephemeral=True,
        )
        return
    await _say(client, command, confirmation_text(reminder, tzinfo(), target_value), ephemeral=True)


async def _extract_target(client, text: str) -> tuple[str | None, str]:
    """Returns (channel_id | "dm" | None, remaining_text)."""
    match = re.match(r"^<#([A-Z0-9]+)(?:\|[^>]+)?>\s*", text)
    if match:
        return match.group(1), text[match.end() :]
    match = re.match(r"^#([a-zA-Z0-9_-]+)\s+", text)
    if match:
        needle = match.group(1).lower()
        for channel in await all_channels(client):
            if str(channel.get("name", "")).lower() == needle:
                return channel.get("id"), text[match.end() :]
        return None, text
    match = re.match(r"^(?:dm|me)\s+", text, re.IGNORECASE)
    if match:
        return "dm", text[match.end() :]
    return "dm", text


async def _usage_reminder(client, command) -> None:
    await _say(
        client,
        command,
        "usage: `/reminder <when> <text>` or `/reminder <#channel> <when> <text>`\n"
        "examples: `/reminder 10m stretch`, `/reminder tomorrow 09:00 standup`, "
        "`/reminder #general daily 09:00 good morning`",
        ephemeral=True,
    )


async def cmd_reminders(ack, command, client) -> None:
    await ack()
    text = (command.get("text") or "").strip().lower()
    user_id = command.get("user_id")
    show_all = text in {"all", "everything"} and runtime.settings.is_admin(user_id)
    rows = runtime.db.list_reminders(None if show_all else user_id)
    if not rows:
        await _say(client, command, "you have no active reminders.", ephemeral=True)
        return
    tz = tzinfo()
    lines = ["*active reminders*"]
    lines.extend(reminder_line(row, tz) for row in rows[:30])
    if len(rows) > 30:
        lines.append(f"...and {len(rows) - 30} more")
    await _say(client, command, "\n".join(lines), ephemeral=True)


async def cmd_cancel(ack, command, client) -> None:
    await ack()
    raw = (command.get("text") or "").strip().lstrip("#")
    if not raw.isdigit():
        await _say(client, command, "usage: `/cancel <reminder-id>`", ephemeral=True)
        return
    reminder_id = int(raw)
    reminder = runtime.db.reminder(reminder_id)
    if not reminder:
        await _say(client, command, f"no reminder `#{reminder_id}`.", ephemeral=True)
        return
    if not (runtime.settings.is_admin(command.get("user_id")) or reminder["user_id"] == command.get("user_id")):
        await _say(client, command, "that reminder belongs to someone else.", ephemeral=True)
        return
    if reminder["done"]:
        await _say(client, command, f"reminder `#{reminder_id}` is already done.", ephemeral=True)
        return
    runtime.db.cancel_reminder(reminder_id)
    await _say(client, command, f"cancelled reminder `#{reminder_id}`.", ephemeral=True)


# ---------------------------------------------------------------------- fun
async def cmd_roll(ack, command, client) -> None:
    await ack()
    notation = (command.get("text") or "d20").strip()
    result = dice.roll(notation)
    if not result:
        await _say(
            client,
            command,
            "invalid dice notation. try `d20`, `2d6+3`, `4d8-1` or `coin`.",
            ephemeral=True,
        )
        return
    await post(
        client,
        command["channel_id"],
        f"<@{command['user_id']}> rolled *{result.notation}*: {result.breakdown} = *{result.total}*",
    )


async def cmd_choose(ack, command, client) -> None:
    await ack()
    raw = command.get("text") or ""
    options = [item.strip() for item in re.split(r"\s*\|\s*|\n+", raw) if item.strip()]
    if len(options) < 2:
        options = [item.strip() for item in re.split(r"\s*,\s*", raw) if item.strip()]
    if len(options) < 2:
        await _say(client, command, "usage: `/choose pizza | tacos | sushi`", ephemeral=True)
        return
    choice = random.choice(options)
    await post(client, command["channel_id"], f"<@{command['user_id']}> I pick: *{choice}*")


# -------------------------------------------------------------------- admin
async def cmd_stats(ack, command, client) -> None:
    await ack()
    if not runtime.settings.is_admin(command.get("user_id")):
        await _say(client, command, "only admins can see workspace stats.", ephemeral=True)
        return
    await _say(client, command, format_stats(runtime.db.stats()), ephemeral=True)


async def cmd_budget(ack, command, client) -> None:
    await ack()
    settings = runtime.settings
    if not settings.is_admin(command.get("user_id")):
        await _say(client, command, "only admins can see the ai budget.", ephemeral=True)
        return
    budget = runtime.budget
    if budget is None:
        await _say(client, command, "ai is off, so there's no budget to show.", ephemeral=True)
        return
    status = budget.status()
    limit = f"${status.limit:.2f}" if status.limit > 0 else "no limit"
    remaining = "unlimited" if status.limit <= 0 else f"${status.remaining:.2f}"
    lines = [
        "*ai budget — today (UTC)*",
        f"• spent: *${status.spent:.4f}* of *{limit}* · *{status.calls}* calls",
        f"• remaining: *{remaining}*",
        "• resets at 00:00 UTC",
        "",
        "*limits*",
        f"• per user: *{settings.ai_rate_limit_per_minute or 'off'}* calls/min",
        f"• workspace: *{settings.ai_global_rate_limit_per_minute or 'off'}* calls/min",
        f"• context: *{settings.ai_max_context_chars:,}* chars per request",
        f"• models: `{settings.ai_model}` → `{settings.ai_fallback_model or 'no fallback'}`",
    ]
    rows = runtime.db.ai_usage(status.day)
    if rows:
        lines.append("")
        lines.append("*breakdown*")
        for row in rows[:8]:
            lines.append(
                f"• `{row['kind']}` · `{row['model']}` — {row['calls']} calls · "
                f"${row['cost_usd']:.4f}"
            )
    await _say(client, command, "\n".join(lines), ephemeral=True)


async def cmd_digest(ack, command, client) -> None:
    await ack()
    if not runtime.settings.is_admin(command.get("user_id")):
        await _say(client, command, "only admins can build the digest.", ephemeral=True)
        return
    await _say(client, command, build_digest(runtime.db, runtime.settings, tzinfo()), ephemeral=True)


# ------------------------------------------------------------------- helpers
def _uptime() -> str:
    if not runtime.started_at:
        return "just now"
    seconds = max(0, int(time.time() - runtime.started_at))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


async def _say(client, command: dict[str, Any], text: str, *, ephemeral: bool = True) -> None:
    if ephemeral:
        await post_ephemeral(client, command["channel_id"], command["user_id"], text)
    else:
        await post(client, command["channel_id"], text)



