"""Slash commands: help, ping, reminders, dice, choose, stats, digest, whois."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from bot.runtime import runtime
from bot.features.stats import (
    build_digest,
    collect_system_diagnostics,
    format_stats,
    format_system_diagnostics,
    format_uptime,
)
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

*tasks*
• `/nugget todo` — list your pending tasks (`/nugget todo all` for all)
• `/nugget todo add <text>` — add a new task
• `/nugget todo done <id>` — complete a task
• `/nugget todo rm <id>` — remove a task

*developer & admin*
• `/nugget sysinfo` — admin: host diagnostics (RAM, CPU, disk, commit)
• `/nugget config` — view or change runtime settings (recaps, tier2, welcomer, etc.)
• `/nugget stats` — admin workspace stats
• `/nugget digest` — admin: daily digest now
• `/nugget budget` — admin: today's ai spend and limits

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
        f"({elapsed:.0f} ms api round trip, up {format_uptime(runtime.started_at)}). ai: {ai}",
    )


async def cmd_id(ack, command, client) -> None:
    await ack()
    settings = runtime.settings
    channel = command.get("channel_id")
    info = await channel_info(client, channel)
    name = f"#{info.get('name')}" if info else channel
    recap_val = runtime.db.kv_get("config:recaps") if runtime.db else None
    if recap_val is not None:
        recap_status = f"{recap_val} (override)"
    else:
        recap_status = str(settings.recaps_enabled).lower()

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
        f"• recaps: `{recap_status}`",
        f"• timezone: `{settings.timezone}`",
    ]
    from bot.features.access import access_channel_id, access_enabled
    if access_enabled():
        lines.append(f"• tier 2 channel: `{access_channel_id()}`")
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
    user = command.get("user_id")
    if not settings.is_admin(user):
        await _say(client, command, "only admins can see ai usage.", ephemeral=True)
        return
    budget = runtime.budget
    if budget is None:
        await _say(client, command, "ai is off, so there's no usage to show.", ephemeral=True)
        return

    status = budget.status()
    mine = budget.user_status(user)
    my_limit = "unlimited" if mine.limit <= 0 else f"${mine.limit:.2f}"
    workspace_stop = (
        f"${settings.ai_daily_budget_usd:.2f}/day" if settings.ai_daily_budget_usd > 0 else "off"
    )
    lines = [
        "*ai usage — today (UTC)*",
        f"• you: *${mine.spent:.4f}* · *{mine.calls}* calls · limit *{my_limit}*",
        f"• workspace: *${status.spent:.4f}* · *{status.calls}* calls",
        "• resets at 00:00 UTC",
        "",
        "*limits*",
        f"• per user: *${settings.ai_user_daily_budget_usd:.2f}/day* (owner exempt)",
        f"• bot-wide stop: *{workspace_stop}* · hack club ai enforces the account cap",
        f"• requests: *{settings.ai_rate_limit_per_minute or 'off'}* per user/min · "
        f"*{settings.ai_global_rate_limit_per_minute or 'off'}* workspace/min",
        f"• context: *{settings.ai_max_context_chars:,}* chars per request",
        f"• models: `{settings.ai_model}` → `{settings.ai_fallback_model or 'no fallback'}`",
    ]

    rows = runtime.db.ai_usage(status.day, user_id=user)
    if rows:
        lines.append("")
        lines.append("*your breakdown*")
        for row in rows[:8]:
            lines.append(
                f"• `{row['kind']}` · `{row['model']}` — {row['calls']} calls · "
                f"${row['cost_usd']:.4f}"
            )

    top = runtime.db.ai_usage_by_user(status.day, limit=5)
    if top:
        lines.append("")
        lines.append("*top users*")
        for row in top:
            lines.append(f"• <@{row['user_id']}> — *${row['cost_usd']:.4f}* · {row['calls']} calls")
    await _say(client, command, "\n".join(lines), ephemeral=True)


async def cmd_digest(ack, command, client) -> None:
    await ack()
    if not runtime.settings.is_admin(command.get("user_id")):
        await _say(client, command, "only admins can build the digest.", ephemeral=True)
        return
    await _say(client, command, build_digest(runtime.db, runtime.settings, tzinfo()), ephemeral=True)


async def cmd_config(ack, command, client) -> None:
    await ack()
    user = command.get("user_id")
    if not runtime.settings.is_admin(user):
        await _say(client, command, "only admins can view or change bot settings.", ephemeral=True)
        return
    text = (command.get("text") or "").strip()
    parts = text.split()
    action = parts[0].lower() if parts else "list"

    key_aliases = {
        "recap": "recaps",
        "daily_recap": "recaps",
        "daily_recaps": "recaps",
        "digest": "daily_digest",
        "access": "tier2",
        "tier2_channel": "access_channel_id",
        "access_channel": "access_channel_id",
        "ambient": "ambient_ai",
        "weather": "weather_location",
    }
    known_keys = [
        "recaps",
        "daily_digest",
        "welcomer",
        "tier2",
        "access_channel_id",
        "ambient_ai",
        "weather_location",
    ]

    if action in {"list", "show"} or not parts:
        lines = ["*bot runtime settings*"]
        for k in known_keys:
            val = runtime.db.kv_get(f"config:{k}") if runtime.db else None
            status = f"`{val}`" if val is not None else "_(default)_"
            lines.append(f"• *{k}*: {status}")
        lines.append("\n_use `/nugget config <key> <value>` or `/nugget config reset <key>`_")
        await _say(client, command, "\n".join(lines), ephemeral=True)
        return

    if action == "reset":
        if len(parts) < 2:
            await _say(client, command, "usage: `/nugget config reset <key>`", ephemeral=True)
            return
        raw_key = parts[1].lower()
        key = key_aliases.get(raw_key, raw_key)
        if runtime.db:
            runtime.db.kv_del(f"config:{key}")
        if key == "recaps":
            try:
                from bot.features import scheduler
                scheduler._ensure_recap_jobs()
            except Exception:
                pass
        elif key == "daily_digest":
            try:
                from bot.features import scheduler
                scheduler._ensure_digest_job()
            except Exception:
                pass
        await _say(client, command, f":white_check_mark: reset *{key}* to default.", ephemeral=True)
        return

    if action == "get":
        if len(parts) < 2:
            await _say(client, command, "usage: `/nugget config get <key>`", ephemeral=True)
            return
        raw_key = parts[1].lower()
        key = key_aliases.get(raw_key, raw_key)
        val = runtime.db.kv_get(f"config:{key}") if runtime.db else None
        status = f"`{val}`" if val is not None else "_(default)_"
        await _say(client, command, f"• *{key}*: {status}", ephemeral=True)
        return

    if action == "set":
        if len(parts) < 3:
            await _say(client, command, "usage: `/nugget config set <key> <value>`", ephemeral=True)
            return
        raw_key, raw_val = parts[1].lower(), parts[2]
    else:
        if len(parts) < 2:
            await _say(client, command, "usage: `/nugget config <key> <value>`", ephemeral=True)
            return
        raw_key, raw_val = parts[0].lower(), parts[1]

    key = key_aliases.get(raw_key, raw_key)
    val_lower = raw_val.lower()
    if val_lower in {"off", "false", "no", "disable", "disabled", "0"}:
        norm = "false"
    elif val_lower in {"on", "true", "yes", "enable", "enabled", "1"}:
        norm = "true"
    else:
        if key == "access_channel_id":
            m = re.search(r"C[A-Z0-9]{8,}", raw_val)
            norm = m.group(0) if m else raw_val.strip("#")
        else:
            norm = raw_val

    if runtime.db:
        runtime.db.kv_set(f"config:{key}", norm)

    if key == "recaps":
        if norm == "false":
            if runtime.db:
                runtime.db.cancel_reminders_by_kind("recap_public")
                runtime.db.cancel_reminders_by_kind("recap_reminder")
            await _say(client, command, ":white_check_mark: daily recaps disabled and pending reminders cancelled.", ephemeral=True)
            return
        try:
            from bot.features import scheduler
            scheduler._ensure_recap_jobs()
        except Exception:
            pass
        await _say(client, command, ":white_check_mark: daily recaps enabled.", ephemeral=True)
        return

    if key == "daily_digest":
        if norm == "false":
            if runtime.db:
                runtime.db.cancel_reminders_by_kind("digest")
            await _say(client, command, ":white_check_mark: daily digest disabled and pending jobs cancelled.", ephemeral=True)
            return
        try:
            from bot.features import scheduler
            scheduler._ensure_digest_job()
        except Exception:
            pass
        await _say(client, command, ":white_check_mark: daily digest enabled.", ephemeral=True)
        return

    await _say(client, command, f":white_check_mark: updated *{key}* to `{norm}`.", ephemeral=True)


async def cmd_sysinfo(ack, command, client) -> None:
    await ack()
    user = command.get("user_id")
    if not runtime.settings.is_admin(user):
        await _say(client, command, "only admins can inspect system diagnostics.", ephemeral=True)
        return
    info = collect_system_diagnostics(runtime.settings, runtime.db, runtime.started_at)
    await _say(client, command, format_system_diagnostics(info), ephemeral=True)


async def cmd_todo(ack, command, client) -> None:
    await ack()
    user = command.get("user_id")
    text = (command.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in {"list", "show", "all", "ls"}:
        show_all = sub == "all" or arg == "all"
        tasks = runtime.db.list_tasks(user, show_all=show_all) if runtime.db else []
        if not tasks:
            msg = (
                "you don't have any pending tasks! :tada:"
                if not show_all
                else "you don't have any tasks recorded yet."
            )
            await _say(client, command, msg, ephemeral=True)
            return
        lines = [f"*your tasks ({'all' if show_all else 'pending'}):*"]
        for t in tasks:
            box = ":white_check_mark:" if t["done"] else ":white_square:"
            title = f"~{t['title']}~" if t["done"] else t["title"]
            lines.append(f"{box} `#{t['id']}` {title}")
        lines.append("\n_use `/nugget todo add <text>`, `/nugget todo done <id>`, `/nugget todo rm <id>`_")
        await _say(client, command, "\n".join(lines), ephemeral=True)
        return

    if sub in {"add", "new", "+", "create"}:
        if not arg:
            await _say(client, command, "usage: `/nugget todo add <task description>`", ephemeral=True)
            return
        task_id = runtime.db.add_task(user, arg) if runtime.db else 0
        await _say(client, command, f":white_check_mark: added task `#{task_id}`: {arg}", ephemeral=True)
        return

    if sub in {"done", "complete", "check", "finish", "x"}:
        try:
            task_id = int(arg.lstrip("#"))
        except ValueError:
            await _say(client, command, "usage: `/nugget todo done <id>`", ephemeral=True)
            return
        ok = runtime.db.complete_task(task_id, user) if runtime.db else False
        if ok:
            await _say(client, command, f":white_check_mark: completed task `#{task_id}`! :tada:", ephemeral=True)
        else:
            await _say(client, command, f"task `#{task_id}` not found in your list.", ephemeral=True)
        return

    if sub in {"undone", "reopen", "uncheck"}:
        try:
            task_id = int(arg.lstrip("#"))
        except ValueError:
            await _say(client, command, "usage: `/nugget todo undone <id>`", ephemeral=True)
            return
        ok = runtime.db.uncomplete_task(task_id, user) if runtime.db else False
        if ok:
            await _say(client, command, f":white_square: reopened task `#{task_id}`.", ephemeral=True)
        else:
            await _say(client, command, f"task `#{task_id}` not found in your list.", ephemeral=True)
        return

    if sub in {"rm", "delete", "del", "remove"}:
        try:
            task_id = int(arg.lstrip("#"))
        except ValueError:
            await _say(client, command, "usage: `/nugget todo rm <id>`", ephemeral=True)
            return
        ok = runtime.db.delete_task(task_id, user) if runtime.db else False
        if ok:
            await _say(client, command, f":wastebasket: deleted task `#{task_id}`.", ephemeral=True)
        else:
            await _say(client, command, f"task `#{task_id}` not found in your list.", ephemeral=True)
        return

    if sub in {"clear", "cleanup", "prune"}:
        count = runtime.db.clear_completed_tasks(user) if runtime.db else 0
        await _say(client, command, f":wastebasket: cleared {count} completed task(s).", ephemeral=True)
        return

    # Treat bare `/nugget todo buy milk` as adding a task directly
    task_id = runtime.db.add_task(user, text) if runtime.db else 0
    await _say(client, command, f":white_check_mark: added task `#{task_id}`: {text}", ephemeral=True)


# ------------------------------------------------------------------- helpers
async def _say(client, command: dict[str, Any], text: str, *, ephemeral: bool = True) -> None:
    if ephemeral:
        await post_ephemeral(client, command["channel_id"], command["user_id"], text)
    else:
        await post(client, command["channel_id"], text)



